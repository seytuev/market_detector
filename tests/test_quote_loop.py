"""F02/A02: быстрый цикл котировок — независимое от HTF-цикла обновление
котировки, касание по тику без дублей, изоляция ошибок и backoff,
legacy-режим (quote_poll_seconds = 0), авто-порог свежести котировки
в единой оценке качества (F03)."""
from __future__ import annotations

from dataclasses import replace

from app.adapters.base import AdapterError
from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import (
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.quality import data_quality
from app.worker import QUOTE_FAIL_K, SEED, Worker

from .conftest import make_candle

D1_MS = 1440 * 60_000
W1_MS = 10080 * 60_000
H1_MS = 3_600_000


class FakeAdapter:
    """In-memory источник (как в test_worker): каталог SEED, цена, счётчики
    обращений и набор символов с ошибкой котировки."""

    venue = "binance"

    def __init__(self):
        self.candles = []
        self.price = (110.0, now_ms())
        self.klines_calls = 0
        self.price_calls: dict[str, int] = {}
        self.price_fail: set[str] = set()

    async def catalog(self):
        return [
            Instrument(None, s.replace("USDT", ""), "binance", "spot", s, "USDT")
            for _, s in SEED
        ]

    async def klines(self, symbol, timeframe, start_ms, end_ms, include_forming=False):
        self.klines_calls += 1
        return [
            replace(c) for c in self.candles
            if c.timeframe == timeframe and start_ms <= c.open_time <= end_ms
        ]

    async def last_price(self, symbol):
        self.price_calls[symbol] = self.price_calls.get(symbol, 0) + 1
        if symbol in self.price_fail:
            raise AdapterError("котировка недоступна (тест)")
        return self.price


def _make_worker(db, adapter, quote_poll_seconds=10):
    settings = Settings()
    settings.detector = DetectorConfig()
    settings.quote_poll_seconds = quote_poll_seconds
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    return Worker(db, settings, settings.detector, {"binance": adapter}, dispatcher)


async def _seed(db, worker, symbol="BTCUSDT") -> Instrument:
    await worker.seed_instruments()
    return next(i for i in db.get_instruments() if i.symbol == symbol)


def _fresh_candles(close: float = 110.0):
    now = now_ms()
    return [
        make_candle(now - D1_MS, close - 1, close + 1, close - 2, close, "D1"),
        make_candle(now - W1_MS, close - 1, close + 1, close - 2, close, "W1"),
    ]


def _zone(db, instrument_id, lower=90.0, upper=100.0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=lower, upper=upper,
        formed_at=now_ms() - 10 * D1_MS, confirmed_at=now_ms() - 9 * D1_MS,
        status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


async def test_quote_loop_updates_quote_independent_of_htf_cycle():
    """Котировка обновляется быстрым циклом без HTF-опроса: после
    _quote_poll_once get_quote свежий, свечи не запрашиваются."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    worker = _make_worker(db, adapter)
    ins = await _seed(db, worker)
    assert db.get_quote(ins.id) is None

    adapter.price = (111.5, now_ms())
    await worker._quote_poll_once()

    assert db.get_quote(ins.id) == adapter.price
    assert adapter.klines_calls == 0  # HTF-цикл не затрагивался


async def test_quote_updates_open_h1_and_skips_expired_bar():
    """Котировка двигает close текущего часа. Бар прошлого часа, ещё
    помеченный незакрытым, ценой нового часа не переписывается.
    События детектора от этого не появляются: H1 не входит в scan_tfs."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    worker = _make_worker(db, adapter)
    ins = await _seed(db, worker)
    now = now_ms()
    hour_open = now - (now % H1_MS)
    db.insert_candles([
        make_candle(
            hour_open, 100, 101, 99, 100,
            timeframe="H1", instrument_id=ins.id, closed=False,
        ),
    ])
    adapter.price = (107.5, now)
    await worker._quote_poll_once()
    bar = db.last_candle(ins.id, "H1", closed_only=False)
    assert bar is not None and bar.closed is False
    assert bar.close == 107.5
    assert bar.high == 107.5
    assert bar.low == 99
    assert db.get_events() == []

    db2 = Database(":memory:")
    adapter2 = FakeAdapter()
    worker2 = _make_worker(db2, adapter2)
    ins2 = await _seed(db2, worker2)
    prev = hour_open - H1_MS
    db2.insert_candles([
        make_candle(
            prev, 100, 101, 99, 100,
            timeframe="H1", instrument_id=ins2.id, closed=False,
        ),
    ])
    adapter2.price = (150.0, now)
    await worker2._quote_poll_once()
    stale = db2.last_candle(ins2.id, "H1", closed_only=False)
    assert stale is not None
    assert stale.close == 100
    assert stale.high == 101


async def test_poll_once_skips_quote_when_quote_loop_enabled():
    """При включённом цикле котировок HTF-опрос цену не запрашивает —
    владелец котировки один (иначе дубли и гонки)."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _fresh_candles()
    worker = _make_worker(db, adapter)  # quote_poll_seconds = 10
    ins = await _seed(db, worker)

    await worker.poll_once()

    assert adapter.price_calls == {}
    assert db.get_quote(ins.id) is None


async def test_poll_instrument_fetches_quote_when_quote_loop_disabled():
    """quote_poll_seconds = 0 — прежнее поведение: котировку забирает
    HTF-цикл вместе со свечами."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _fresh_candles()
    worker = _make_worker(db, adapter, quote_poll_seconds=0)
    ins = await _seed(db, worker)

    await worker.poll_once()

    assert db.get_quote(ins.id) == adapter.price


async def test_quote_loop_touch_event_not_duplicated():
    """Тик внутрь зоны регистрирует TOUCH один раз; повторные опросы той же
    цены события не дублируют (дедупликация движка)."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    worker = _make_worker(db, adapter)
    ins = await _seed(db, worker)
    zone = _zone(db, ins.id)
    adapter.price = (95.0, now_ms())  # цена внутри зоны [90;100] → TOUCH

    await worker._quote_poll_once()
    events = db.get_events(zone_id=zone.id)
    assert len(events) == 1

    await worker._quote_poll_once()  # тот же тик повторно
    await worker._quote_poll_once()
    assert len(db.get_events(zone_id=zone.id)) == 1


async def test_quote_loop_error_isolation_and_backoff():
    """Ошибка котировки одного инструмента не мешает остальным; после
    QUOTE_FAIL_K подряд ошибок инструмент пропускается (backoff), затем
    опрашивается снова, успех сбрасывает счётчики."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.price_fail.add("BTCUSDT")
    worker = _make_worker(db, adapter)
    btc = await _seed(db, worker)
    eth = next(i for i in db.get_instruments() if i.symbol == "ETHUSDT")

    await worker._quote_poll_once()
    assert db.get_quote(btc.id) is None
    assert db.get_quote(eth.id) == adapter.price  # сосед не пострадал

    for _ in range(QUOTE_FAIL_K - 1):
        await worker._quote_poll_once()
    calls_btc = adapter.price_calls["BTCUSDT"]
    assert calls_btc == QUOTE_FAIL_K

    # пауза после K-й ошибки: инструмент не опрашивается, остальные — да
    await worker._quote_poll_once()
    assert adapter.price_calls["BTCUSDT"] == calls_btc
    calls_eth = adapter.price_calls["ETHUSDT"]
    await worker._quote_poll_once()
    assert adapter.price_calls["ETHUSDT"] > calls_eth

    # пауза истекла — инструмент снова в опросе; источник восстановился
    adapter.price_fail.discard("BTCUSDT")
    for _ in range(40):  # перекрывает максимальную паузу (2**5 циклов)
        await worker._quote_poll_once()
        if db.get_quote(btc.id) is not None:
            break
    assert db.get_quote(btc.id) == adapter.price


def test_quality_quote_threshold_follows_quote_loop():
    """F03+F02: авто-порог свежести котировки — от quote_poll_seconds, когда
    цикл включён; явный stale_quote_seconds важнее; выключенный цикл —
    прежний порог 2*poll_seconds."""
    db = Database(":memory:")
    now = now_ms()

    settings = Settings()
    settings.detector = DetectorConfig()
    settings.poll_seconds = 1800
    settings.quote_poll_seconds = 10  # авто-порог max(2*10, 30) = 30 с

    db.set_quote(1, 100.0, now - 60_000)
    q = data_quality(db, settings, 1, now)
    assert q["channels"]["quote"]["status"] == "stale"
    assert q["channels"]["quote"]["threshold_s"] == 30

    db.set_quote(1, 100.0, now - 5_000)
    q = data_quality(db, settings, 1, now)
    assert q["channels"]["quote"]["status"] == "ok"

    # явная настройка важнее авто-порога
    settings.detector.stale_quote_seconds = 120
    db.set_quote(1, 100.0, now - 60_000)
    q = data_quality(db, settings, 1, now)
    assert q["channels"]["quote"]["status"] == "ok"
    assert q["channels"]["quote"]["threshold_s"] == 120

    # цикл выключен — прежний порог 2*poll_seconds (60 с — свежо)
    settings.detector.stale_quote_seconds = 0
    settings.quote_poll_seconds = 0
    q = data_quality(db, settings, 1, now)
    assert q["channels"]["quote"]["status"] == "ok"
    assert q["channels"]["quote"]["threshold_s"] == 3600
