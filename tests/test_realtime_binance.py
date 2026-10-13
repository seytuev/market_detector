"""Этап 3 (спека §7): realtime-поток Binance Spot по WebSocket.

Фейковый сервер — websockets.serve на 127.0.0.1:0; клиент — настоящий
BinanceRealtime с короткими таймаутами. Проверяется: нормализация trade
(T — время сделки, не E) и kline (x → closed), формирующий бар без расчёта,
закрытый бар через боевой путь обработки (идемпотентно), отбраковка мусора
без разрыва соединения, дедуп при пересечении эпох после reconnect,
idle-таймаут, backoff с джиттером, пропуск REST-опроса при здоровой
подписке, recovery-догрузка (AC10), флаг realtime_enabled (AC21).
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from websockets.asyncio.server import serve

from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import Instrument, now_ms
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.realtime.binance_ws import BinanceRealtime, backoff_delays
from app.services.data_sync import period_open
from app.services.metrics import MetricsRegistry
from app.worker import Worker

from .conftest import make_candle
from .test_quote_loop import FakeAdapter

D1_MS = 86_400_000
T0 = 1_790_000_000_000  # фиксированная база времени тиков, мс


class WsFakeAdapter(FakeAdapter):
    """REST-заглушка с заявленной WS-возможностью (как BinanceSpotAdapter)."""

    def realtime_supported(self):
        return True


def _worker(db, *, realtime_enabled=False, adapter=None) -> Worker:
    settings = Settings()
    settings.detector = DetectorConfig()
    settings.realtime_enabled = realtime_enabled
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    return Worker(db, settings, settings.detector,
                  {"binance": adapter or WsFakeAdapter()}, dispatcher)


def _instrument(db, symbol="BTCUSDT"):
    iid = db.upsert_instrument(Instrument(
        id=None, asset=symbol.replace("USDT", ""), venue="binance",
        market_type="spot", symbol=symbol, quote_asset="USDT",
    ))
    return db.get_instrument(iid)


def _realtime(worker, *, metrics=None, **overrides) -> BinanceRealtime:
    params = dict(
        base_url="",
        on_trade=worker._ws_on_trade,
        on_kline=worker._ws_on_kline,
        on_reconnect=worker._ws_on_reconnect,
        idle_timeout=2.0,
        lifetime=3600.0,
        reconnect_base=0.05,
        reconnect_cap=0.2,
        metrics=metrics or MetricsRegistry(),
    )
    params.update(overrides)
    return BinanceRealtime(**params)


def _stub_recovery(worker) -> list:
    """sync_candles — записывающий no-op: recovery-догрузки не ходят в REST
    и не вызывают _process_synced_channel (иначе шпионы тестов грязнятся)."""
    calls = []

    async def fake_sync(ins, tf, reason="periodic", job_id=None):
        calls.append((ins.id, tf, reason))
        return {"status": "ok"}

    worker.data_sync.sync_candles = fake_sync
    return calls


# --------------------------------------------------------------------- #
# Фейковый сервер Binance (combined stream, события {"stream","data"})
# --------------------------------------------------------------------- #

def trade_msg(symbol, tid, price, t, e=None):
    return {"stream": f"{symbol.lower()}@trade", "data": {
        "e": "trade", "E": t + 5 if e is None else e, "s": symbol,
        "t": tid, "p": str(price), "q": "0.01", "T": t, "m": True, "M": True}}


def kline_msg(symbol, interval, open_time, close_time, o, h, l, c, closed):
    return {"stream": f"{symbol.lower()}@kline_{interval}", "data": {
        "e": "kline", "E": close_time, "s": symbol, "k": {
            "t": open_time, "T": close_time, "s": symbol, "i": interval,
            "f": 1, "L": 2, "o": str(o), "c": str(c), "h": str(h),
            "l": str(l), "v": "1", "n": 2, "x": closed,
            "q": "1", "V": "1", "Q": "1", "B": "0"}}}


class FakeBinanceServer:
    """websockets.serve на 127.0.0.1:0. handler(conn, n) — сценарий n-го
    подключения; возврат из handler закрывает соединение."""

    def __init__(self, handler):
        self._handler = handler
        self.connections = 0
        self.server = None

    async def __aenter__(self) -> "FakeBinanceServer":
        async def _wrap(conn):
            self.connections += 1
            try:
                await self._handler(conn, self.connections)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # сценарий завершился/соединение оборвалось
        self.server = await serve(_wrap, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"


async def wait_for(cond, timeout=5.0, interval=0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


def _metric(registry, name):
    return registry.snapshot()["series"].get(name, {}).get("value")


# --------------------------------------------------------------------- #
# 1. trade → _ingest_quote: event_at = T (не E), source_seq = trade id
# --------------------------------------------------------------------- #

async def test_trade_message_ingested_as_normalized_quote():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        t_trade = T0 + 500
        price = 101.25

        async def handler(conn, n):
            await conn.send(json.dumps(
                trade_msg("BTCUSDT", 777, price, t_trade, e=t_trade + 900)))
            await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(lambda: db.get_quote(ins.id) is not None)
            # котировка записана по времени СДЕЛКИ (T), не по времени события (E)
            assert db.get_quote(ins.id) == (price, t_trade)
            detail = db.get_quote_detail(ins.id)
            assert detail["source"] == "ws"
            assert detail["price_kind"] == "trade"
            assert detail["source_seq"] == "777"
            assert detail["event_at"] == t_trade
            # здоровье подписки и status()
            assert await wait_for(lambda: rt.is_healthy(ins.id))
            st = rt.status()["instruments"]["BTCUSDT"]
            assert st["connected"] and st["last_valid_message_at"] is not None
            assert st["last_error"] is None
            assert _metric(rt._metrics, 'ws_messages{venue=binance}') == 1
            await rt.stop()
            assert rt.status()["instruments"]["BTCUSDT"]["connected"] is False
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 2. формирующая kline (x=false): upsert бара, расчёт НЕ запускается
# --------------------------------------------------------------------- #

async def test_forming_kline_upserts_bar_without_calculation():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        process_calls = []
        real_process = worker._process_synced_channel

        async def spy_process(*args, **kwargs):
            process_calls.append(args)
            return await real_process(*args, **kwargs)

        worker._process_synced_channel = spy_process
        open_time = period_open("D1", now_ms())

        async def handler(conn, n):
            await conn.send(json.dumps(kline_msg(
                "BTCUSDT", "1d", open_time, open_time + D1_MS - 1,
                100.0, 105.0, 99.0, 103.5, False)))
            await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(
                lambda: db.last_candle(ins.id, "D1", closed_only=False) is not None)
            bar = db.last_candle(ins.id, "D1", closed_only=False)
            assert bar.closed is False
            assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 99.0, 103.5)
            assert bar.source == "binance_ws"
            await asyncio.sleep(0.2)  # дать шанс ошибочной обработке
            assert process_calls == []  # расчёт не запускался
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 3. закрытая kline (x=true): вставка + боевой путь обработки; повтор —
#    идемпотентно (без дублей событий и повторной обработки)
# --------------------------------------------------------------------- #

async def test_closed_kline_processed_once_and_duplicate_is_noop():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        open_now = period_open("D1", now_ms())
        old = make_candle(open_now - 2 * D1_MS, 100.0, 106.0, 96.0, 102.0,
                          "D1", instrument_id=ins.id)
        db.insert_candles([old])
        kline_open = open_now - D1_MS  # только что закрывшийся бар
        kline_close = kline_open + D1_MS - 1
        processed_key = f"data_sync:processed:{ins.id}:D1"
        closed_calls = []
        real_closed = worker.scanner.on_closed_candle

        def spy_closed(candle, *args, **kwargs):
            closed_calls.append(candle.open_time)
            return real_closed(candle, *args, **kwargs)

        worker.scanner.on_closed_candle = spy_closed
        msg = json.dumps(kline_msg(
            "BTCUSDT", "1d", kline_open, kline_close,
            102.0, 108.0, 101.0, 107.0, True))

        async def handler(conn, n):
            await conn.send(msg)
            await conn.send(msg)  # дубликат закрытия (повторная доставка)
            await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(
                lambda: db.get_meta(processed_key) == str(kline_close))
            assert await wait_for(lambda: closed_calls == [kline_open])
            candle = db.last_candle(ins.id, "D1")
            assert candle.open_time == kline_open and candle.closed is True
            assert candle.close == 107.0
            events_before = [
                (e.zone_id, e.kind, e.occurred_at) for e in db.get_events(limit=10000)]
            await asyncio.sleep(0.3)  # дубликат дообработался бы за это время
            # повторный закрытый бар: ни повторной обработки, ни новых событий
            assert closed_calls == [kline_open]
            assert [
                (e.zone_id, e.kind, e.occurred_at) for e in db.get_events(limit=10000)
            ] == events_before
            assert db.get_candles(ins.id, "D1")[-1].open_time == kline_open
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 4. мусорные сообщения: drop + ws_malformed, соединение живёт
# --------------------------------------------------------------------- #

async def test_malformed_messages_dropped_and_connection_survives():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        open_time = period_open("D1", now_ms())
        bad_payloads = [
            "не json",
            json.dumps([1, 2, 3]),
            json.dumps({"stream": "btcusdt@trade", "data": {
                "e": "trade", "E": 1, "s": "BTCUSDT", "t": 1,
                "p": "-5", "q": "1", "T": T0, "m": True, "M": True}}),
            json.dumps({"stream": "btcusdt@trade", "data": {
                "e": "trade", "E": 1, "s": "BTCUSDT", "t": 2,
                "p": "NaN", "q": "1", "T": T0, "m": True, "M": True}}),
            json.dumps(trade_msg("ETHUSDT", 3, 50.0, T0)),  # чужой символ
            json.dumps(kline_msg(  # low > high — битый OHLC
                "BTCUSDT", "1d", open_time, open_time + D1_MS - 1,
                100.0, 99.0, 101.0, 100.5, False)),
        ]
        good = json.dumps(trade_msg("BTCUSDT", 9, 100.0, T0 + 1000))

        async def handler(conn, n):
            for raw in bad_payloads:
                await conn.send(raw)
            await conn.send(good)
            await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(lambda: db.get_quote(ins.id) is not None)
            assert db.get_quote(ins.id) == (100.0, T0 + 1000)
            assert _metric(rt._metrics, 'ws_malformed{venue=binance}') == 6
            assert _metric(rt._metrics, 'ws_messages{venue=binance}') == 7
            assert srv.connections == 1  # соединение не рвалось
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 5. reconnect с пересечением: сделки 3..5 при повторной доставке —
#    дедуп, on_price применён ровно один раз на сделку
# --------------------------------------------------------------------- #

async def test_reconnect_overlap_is_deduplicated():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        applied = []
        real_on_price = worker.scanner.on_price

        def spy_on_price(iid, price, ts, *args, **kwargs):
            applied.append(price)
            return real_on_price(iid, price, ts, *args, **kwargs)

        worker.scanner.on_price = spy_on_price

        async def handler(conn, n):
            ids = (1, 2, 3, 4, 5) if n == 1 else (3, 4, 5, 6)
            for i in ids:
                await conn.send(json.dumps(
                    trade_msg("BTCUSDT", i, 100.0 + i, T0 + i * 1000)))
            if n == 1:
                await conn.close()  # обрыв после 5 сделок
            else:
                await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(lambda: srv.connections >= 2)
            assert await wait_for(lambda: db.get_quote(ins.id) == (106.0, T0 + 6000))
            assert await worker.market_queue.wait_idle(ins.id, 5)
            # 6 уникальных сделок, каждая применена один раз
            assert sorted(applied) == [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
            assert db.get_quote_detail(ins.id)["source_seq"] == "6"
            assert rt.status()["instruments"]["BTCUSDT"]["reconnects"] >= 1
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 6. idle-таймаут: молчащее соединение переподключается
# --------------------------------------------------------------------- #

async def test_idle_timeout_triggers_reconnect():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)

        async def handler(conn, n):
            await conn.wait_closed()  # молчим

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url, idle_timeout=0.3,
                           reconnect_base=0.02, reconnect_cap=0.05)
            rt.start([ins], ("D1",))
            assert await wait_for(lambda: srv.connections >= 2, timeout=5)
            assert _metric(rt._metrics, 'ws_reconnects{venue=binance}') >= 1
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 7. backoff: экспонента до потолка, джиттер в границах ±20%
# --------------------------------------------------------------------- #

def test_backoff_jitter_bounds():
    exact = backoff_delays(base=1.0, cap=60.0, rand=lambda: 0.5)
    assert [next(exact) for _ in range(8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    randomized = backoff_delays(base=1.0, cap=60.0)
    expected = 1.0
    for _ in range(10):
        delay = next(randomized)
        base = min(60.0, expected)
        assert 0.8 * base <= delay <= 1.2 * base
        expected *= 2


# --------------------------------------------------------------------- #
# 8. quote_loop: здоровая подписка — REST пропускается; нездоровая —
#    фолбэк продолжается; без realtime — прежнее поведение
# --------------------------------------------------------------------- #

async def test_quote_loop_skips_rest_only_when_realtime_healthy():
    db = Database(":memory:")
    try:
        adapter = WsFakeAdapter()
        worker = _worker(db, adapter=adapter)
        ins = _instrument(db)
        assert worker.realtime is None  # флаг выключен — REST как прежде
        await worker._quote_poll_once()
        assert adapter.price_calls.get("BTCUSDT") == 1

        healthy = SimpleNamespace(flag=True)
        worker.realtime = SimpleNamespace(
            is_healthy=lambda iid: healthy.flag)
        await worker._quote_poll_once()
        assert adapter.price_calls.get("BTCUSDT") == 1  # пропущен REST
        healthy.flag = False
        await worker._quote_poll_once()
        assert adapter.price_calls.get("BTCUSDT") == 2  # фолбэк работает
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 9. AC10: на (пере)подключении планируется ws_recovery по каждому ТФ
# --------------------------------------------------------------------- #

async def test_recovery_sync_scheduled_on_connect_and_reconnect():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        calls = _stub_recovery(worker)

        async def handler(conn, n):
            if n == 1:
                await asyncio.sleep(0.1)
                await conn.close()
            else:
                await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url,
                           reconnect_base=0.02, reconnect_cap=0.05)
            rt.start([ins], ("D1",))
            # H1 подписывается всегда + scan D1: по 2 догрузки на подключение
            assert await wait_for(lambda: len(calls) >= 2)
            assert {(iid, tf, r) for iid, tf, r in calls[:2]} == {
                (ins.id, "H1", "ws_recovery"), (ins.id, "D1", "ws_recovery")}
            assert await wait_for(lambda: srv.connections >= 2)
            assert await wait_for(lambda: len(calls) >= 4)  # второй заход
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# 10. AC21: realtime_enabled=false — компонент не создаётся, REST прежний;
#     возможность источника (realtime_supported) отделена от флага
# --------------------------------------------------------------------- #

def test_realtime_disabled_or_unsupported_creates_nothing():
    db = Database(":memory:")
    try:
        w_off = _worker(db)
        assert w_off.realtime is None
        w_on = _worker(db, realtime_enabled=True)
        assert w_on.realtime is not None
        # адаптер без realtime-возможности — обычный REST-режим, не ошибка
        w_plain = _worker(db, realtime_enabled=True, adapter=FakeAdapter())
        assert w_plain.realtime is None
    finally:
        db.close()


# --------------------------------------------------------------------- #
# 11. update_subscriptions: живое добавление/снятие/смена ТФ без рестарта
# --------------------------------------------------------------------- #

def test_update_subscriptions_reconciles_desired_set():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        rt = _realtime(worker, base_url="ws://127.0.0.1:1")  # не стартуем
        rt.update_subscriptions([ins], ("D1",))
        assert set(rt._links) == {"BTCUSDT"}
        assert rt._links["BTCUSDT"].streams == (
            "btcusdt@trade", "btcusdt@kline_1h", "btcusdt@kline_1d")
        eth = _instrument(db, "ETHUSDT")
        rt.update_subscriptions([ins, eth], ("D1", "W1"))
        assert set(rt._links) == {"BTCUSDT", "ETHUSDT"}
        assert rt._links["BTCUSDT"].streams == (
            "btcusdt@trade", "btcusdt@kline_1h",
            "btcusdt@kline_1d", "btcusdt@kline_1w")
        # выключенный инструмент снимается с подписки
        db.set_instrument_enabled(ins.id, False)
        rt.update_subscriptions(
            [i for i in db.get_instruments(enabled_only=True)], ("D1", "W1"))
        assert set(rt._links) == {"ETHUSDT"}
        assert rt.status()["instruments"]["ETHUSDT"]["connected"] is False
    finally:
        db.close()


# --------------------------------------------------------------------- #
# §4: часы источника впереди — clock_skew считается, тик принимается
# --------------------------------------------------------------------- #

async def test_clock_skew_counted_but_tick_accepted():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        _stub_recovery(worker)
        future_t = now_ms() + 120_000

        async def handler(conn, n):
            await conn.send(json.dumps(
                trade_msg("BTCUSDT", 5, 100.0, future_t, e=future_t)))
            await conn.wait_closed()

        async with FakeBinanceServer(handler) as srv:
            rt = _realtime(worker, base_url=srv.url)
            rt.start([ins], ("D1",))
            assert await wait_for(lambda: db.get_quote(ins.id) is not None)
            assert db.get_quote(ins.id) == (100.0, future_t)
            assert _metric(rt._metrics, 'clock_skew{venue=binance}') == 1
            await rt.stop()
    finally:
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        db.close()
