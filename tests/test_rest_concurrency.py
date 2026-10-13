"""Этап 5 perf-спеки (§10; AC08/AC09): ограниченная параллельность REST
(rest_concurrency на venue), single-flight одного канала, venue-wide
Retry-After без retry-шторма, изоляция зависшего инструмента, invalid
symbol без бесконечных повторов, учёт веса Binance."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
import respx

from app.adapters.base import AdapterError, QuoteTick, TIMEFRAME_MS
from app.adapters.binance import BinanceSpotAdapter
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Instrument, now_ms
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.data_sync import JOB_KEY, period_open, read_json
from app.services.metrics import get_registry
from app.worker import Worker
from .conftest import make_candle


class CountingExchange:
    """In-memory источник: счётчик и задержки klines по символу."""

    venue = "binance"

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.delays = {}          # symbol -> seconds sleep в klines
        self.invalid = set()      # (symbol, tf) -> AdapterError invalid_symbol
        self.quote_calls = 0

    async def catalog(self):
        return [
            Instrument(None, s.replace("USDT", ""), "binance", "spot", s, "USDT")
            for s in ("BTCUSDT", "ETHUSDT")
        ]

    async def klines(self, symbol, tf, start, end, include_forming=False):
        self.calls.append((symbol, tf, start, end))
        if (symbol, tf) in self.invalid:
            raise AdapterError(
                f"binance: спотовый инструмент не существует: {symbol} (код -1121)",
                invalid_symbol=True)
        delay = self.delays.get(symbol)
        if delay:
            await asyncio.sleep(delay)
        return [
            replace(c) for c in self.rows.get((symbol, tf), [])
            if start <= c.open_time <= end and (include_forming or c.closed)
        ]

    async def last_price(self, symbol):
        self.quote_calls += 1
        return QuoteTick(100.0, now_ms(), now_ms(), "trade")

    async def status(self):
        return {"ok": True}


def _rows(tf, iid, *, count=6):
    opening = period_open(tf, now_ms())
    step = TIMEFRAME_MS[tf]
    rows = [
        make_candle(opening - i * step, 100, 101, 99, 100, tf, iid, source="binance")
        for i in range(count - 1, 0, -1)
    ]
    rows.append(make_candle(opening, 100, 101, 99, 100, tf, iid,
                            closed=False, source="binance"))
    return rows


def _make_worker(db, exchange, *, rest_concurrency=None):
    settings = Settings()
    settings.detector = DetectorConfig()
    settings.events_enabled = False
    if rest_concurrency is not None:
        settings.rest_concurrency = rest_concurrency
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    return Worker(
        db, settings, settings.detector, {"binance": exchange}, dispatcher,
        ltf_engine=LtfEngine(db, settings.detector),
    )


async def _two_instruments(db, exchange):
    instruments = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        iid = db.upsert_instrument(
            Instrument(None, symbol[:-4], "binance", "spot", symbol, "USDT"))
        ins = db.get_instrument(iid)
        instruments.append(ins)
        for tf in ("D1", "W1", "H1"):
            exchange.rows[symbol, tf] = _rows(tf, iid)
            db.insert_candles(exchange.rows[symbol, tf][:3])
    return instruments


async def test_rest_concurrency_setting_bounds_per_venue():
    """§10: начальная конкурентность REST — настройка rest_concurrency;
    per-venue семафоры подчиняются ей (1 — строго последовательно)."""
    db = Database(":memory:")
    exchange = CountingExchange()
    worker = _make_worker(db, exchange, rest_concurrency=1)
    active = 0
    peak = 0

    async def slow():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    await asyncio.gather(*(worker.data_sync.request("binance", slow) for _ in range(3)))
    assert peak == 1
    await worker.calc_queue.stop()
    db.close()

    db = Database(":memory:")
    worker = _make_worker(db, CountingExchange(), rest_concurrency=2)
    active = peak = 0
    await asyncio.gather(*(worker.data_sync.request("binance", slow) for _ in range(4)))
    assert peak == 2
    await worker.calc_queue.stop()
    db.close()


async def test_sync_candles_single_flight(tmp_path):
    """§10: два конкурентных sync_candles одного канала — один запрос klines
    к источнику; ожидающий получает копию того же результата."""
    db = Database(":memory:")
    exchange = CountingExchange()
    worker = _make_worker(db, exchange)
    ins = (await _two_instruments(db, exchange))[0]
    exchange.delays["BTCUSDT"] = 0.05

    before = len(exchange.calls)
    first, second = await asyncio.gather(
        worker.data_sync.sync_candles(ins, "D1"),
        worker.data_sync.sync_candles(ins, "D1"),
    )
    d1_calls = [c for c in exchange.calls[before:] if c[1] == "D1"]
    assert len(d1_calls) == 1, d1_calls
    assert first["status"] == second["status"] == "ok"
    assert first is not second and first == second
    await worker.calc_queue.stop()
    db.close()


async def test_retry_after_venue_wide_and_no_storm():
    """AC09/§10: Retry-After — общий для venue: конкурентный запрос того же
    источника ждёт cooldown, чужой venue не затронут; повторов — минимум."""
    db = Database(":memory:")
    worker = _make_worker(db, CountingExchange())
    sync = worker.data_sync
    calls = {"binance": [], "bybit": []}

    async def binance_fn():
        calls["binance"].append(time.monotonic())
        if len(calls["binance"]) == 1:
            raise AdapterError("throttled", retry_after=0.25)
        return "ok"

    async def bybit_fn():
        calls["bybit"].append(time.monotonic())
        return "ok"

    t0 = time.monotonic()
    first = asyncio.create_task(sync.request("binance", binance_fn))
    await asyncio.sleep(0.05)  # первый получил 429 и поставил cooldown venue
    second = asyncio.create_task(sync.request("binance", binance_fn))
    third = asyncio.create_task(sync.request("bybit", bybit_fn))
    assert await third == "ok"
    assert calls["bybit"][0] - t0 < 0.2  # чужой venue прошёл сразу
    assert await first == "ok" and await second == "ok"
    # нет retry-шторма: 1 неудача + 1 повтор первого + 1 дождавшийся второй
    assert len(calls["binance"]) == 3
    assert calls["binance"][1] - calls["binance"][0] >= 0.24
    assert calls["binance"][2] - calls["binance"][0] >= 0.24
    await worker.calc_queue.stop()
    db.close()


async def test_boundary_tick_isolates_hanging_instrument(tmp_path):
    """AC08/§10: зависший источник одного инструмента (до request timeout)
    не останавливает boundary-синхронизацию остальных."""
    db = Database(":memory:")
    exchange = CountingExchange()
    worker = _make_worker(db, exchange)
    sync = worker.data_sync
    sync.request_timeout_seconds = 0.3
    instruments = await _two_instruments(db, exchange)
    slow, fast = instruments
    exchange.delays[slow.symbol] = 60.0  # завис навсегда, снимет wait_for

    events = []

    async def klines(symbol, tf, start, end, include_forming=False):
        events.append((time.monotonic(), "enter", symbol))
        try:
            return await CountingExchange.klines(
                exchange, symbol, tf, start, end, include_forming=include_forming)
        finally:
            events.append((time.monotonic(), "exit", symbol))

    exchange.klines = klines

    at = now_ms()
    for ins in instruments:
        for tf in ("D1", "W1", "H1"):
            head = db.last_candle(ins.id, tf, closed_only=False)
            opening = period_open(tf, at, head.open_time if head else None)
            if tf == "D1":
                sync._boundary_due[ins.id, tf] = {"opening": opening, "attempt": 0, "due": at}
            else:
                sync._boundary_due[ins.id, tf] = {
                    "opening": opening, "attempt": 4, "due": at + 3_600_000}

    started = time.monotonic()
    await sync.boundary_tick(at)
    elapsed = time.monotonic() - started
    # быстрый инструмент закрыл бар, пока медленный ещё висел в таймауте
    exits = {s: t for t, kind, s in events if kind == "exit"}
    assert exits[fast.symbol] < exits[slow.symbol]
    assert elapsed < 5, "boundary_tick не должен ждать зависший инструмент последовательно"
    assert sync._boundary_due[fast.id, "D1"]["attempt"] == 4   # получил закрытие
    assert sync._boundary_due[slow.id, "D1"]["attempt"] == 1   # отложен на retry
    await worker.calc_queue.stop()
    db.close()


async def test_invalid_symbol_marked_and_skipped_until_next_job(tmp_path):
    """§10: invalid symbol (Binance -1121) не повторяется в цикле — канал
    помечается instrument:invalid и пропускается до следующего job/рестарта."""
    db = Database(":memory:")
    exchange = CountingExchange()
    worker = _make_worker(db, exchange)
    ins = (await _two_instruments(db, exchange))[0]
    exchange.invalid.add((ins.symbol, "D1"))

    with pytest.raises(AdapterError):
        await worker.data_sync.sync_candles(ins, "D1")
    assert db.get_meta(f"instrument:invalid:{ins.id}") is not None
    calls = len(exchange.calls)
    with pytest.raises(AdapterError):
        await worker.data_sync.sync_candles(ins, "D1")
    assert len(exchange.calls) == calls  # без нового запроса к источнику
    series = get_registry().snapshot()["series"]
    assert series["rest_invalid_symbol{venue=binance}"]["value"] >= 1

    # следующий ручной job снимает блокировку — источник получает новый шанс
    worker.data_sync.ready = True
    worker.data_sync.start()
    await worker.data_sync._task
    assert any(c[0] == ins.symbol and c[1] == "D1" for c in exchange.calls[calls:])
    job = read_json(db, JOB_KEY)
    assert job["status"] == "partial"  # канал снова упал, но job дошёл до конца
    await worker.calc_queue.stop()
    await worker.market_queue.stop()
    db.close()


async def test_rest_weight_gauge_recorded():
    """§10: после запроса фиксируется gauge rest_weight_used{venue}, если
    адаптер захватил заголовок веса (наблюдение без смены троттлинга)."""
    db = Database(":memory:")
    worker = _make_worker(db, CountingExchange())

    class Source:
        last_weight = 4321

        async def call(self):
            return "ok"

    assert await worker.data_sync.request("binance", Source().call) == "ok"
    series = get_registry().snapshot()["series"]
    assert series["rest_weight_used{venue=binance}"]["value"] == 4321
    await worker.calc_queue.stop()
    db.close()


async def test_binance_adapter_captures_weight_and_marks_invalid_symbol():
    """BinanceSpotAdapter: X-MBX-USED-WEIGHT-1M сохраняется (регистр заголовка
    не важен), -1121 помечается invalid_symbol (для пропуска без повторов)."""
    adapter = BinanceSpotAdapter("https://test.binance")
    with respx.mock:
        respx.get("https://test.binance/api/v3/trades").respond(
            200, json=[{"price": "1.0", "time": 1, "id": 1}],
            headers={"X-MBX-USED-WEIGHT-1M": "123"})
        tick = await adapter.last_price("BTCUSDT")
        assert tick.price == 1.0
        assert adapter.last_weight == 123

        respx.get("https://test.binance/api/v3/klines").respond(
            429, json={"code": -1003, "msg": "Too much request weight used."},
            headers={"x-mbx-used-weight-1m": "6100", "Retry-After": "2"})
        with pytest.raises(AdapterError) as throttled:
            await adapter.klines("BTCUSDT", "D1", 0, 1)
        assert throttled.value.retry_after == 2
        assert adapter.last_weight == 6100

        respx.get("https://test.binance/api/v3/klines").respond(
            400, json={"code": -1121, "msg": "Invalid symbol."})
        with pytest.raises(AdapterError) as invalid:
            await adapter.klines("NOPE", "D1", 0, 1)
        assert invalid.value.invalid_symbol is True
        assert invalid.value.retry_after is None
    await adapter.aclose()
