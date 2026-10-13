"""Этап 2 (спека §8): разделение приёма котировок и расчёта.

Быстрый путь (_ingest_quote): дедуп (source, source_seq | event_at+price),
атомарный persist с перепроверкой свежести (set_quote_if_fresh), формирующийся
бар, WS-рассылка, постановка в market_queue. Очередь: per-instrument FIFO,
durable spool при переполнении (AC11), degraded-флаг, восстановление после
рестарта, изоляция инструментов (AC02), эквивалентность событий
последовательной обработке (AC03), порядок равных по времени сделок (AC04).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from app.adapters.base import QuoteTick
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.scanner import Scanner
from app.models import Direction, Instrument, Zone, ZoneStatus, ZoneType
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.market_queue import MarketQueue
from app.worker import Worker

from .test_quote_loop import FakeAdapter

T0 = 1_790_000_000_000  # фиксированная база времени тиков, мс


def _worker(db, *, broadcast=None, queue_capacity=500) -> Worker:
    settings = Settings()
    settings.detector = DetectorConfig()
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    worker = Worker(db, settings, settings.detector,
                    {"binance": FakeAdapter()}, dispatcher, broadcast=broadcast)
    worker.market_queue = MarketQueue(worker, queue_capacity=queue_capacity)
    return worker


def _instrument(db, symbol="BTCUSDT"):
    iid = db.upsert_instrument(Instrument(
        id=None, asset=symbol.replace("USDT", ""), venue="binance",
        market_type="spot", symbol=symbol, quote_asset="USDT",
    ))
    return db.get_instrument(iid)


def _zone(db, iid, lower=90.0, upper=100.0, tf="D1"):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe=tf, lower=lower, upper=upper,
        formed_at=T0 - 20 * 86_400_000, confirmed_at=T0 - 19 * 86_400_000,
        status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def _spy_on_price(worker, record):
    """Подмена scanner.on_price с записью вызовов; движок настоящий."""
    real = worker.scanner.on_price

    def spy(iid, price, ts, *args, **kwargs):
        record.append((iid, price, ts))
        return real(iid, price, ts, *args, **kwargs)

    worker.scanner.on_price = spy


# --------------------------------------------------------------------- #
# AC04: монотонность источников; равные метки — разные сделки
# --------------------------------------------------------------------- #

async def test_stale_rest_does_not_clobber_fresh_ws_and_equal_ts_trades_live():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        # свежий WS-тик
        ws = QuoteTick(101.0, T0 + 1000, T0 + 1005, "trade", source_seq="100")
        assert await worker._ingest_quote(ins, ws, "ws") is ws
        assert db.get_quote(ins.id) == (101.0, T0 + 1000)
        # запоздавший REST (старше по event_at) не затирает WS-тик
        stale = QuoteTick(99.0, T0, T0 + 1030, "trade")
        assert await worker._ingest_quote(ins, stale, "rest") is None
        assert db.get_quote(ins.id) == (101.0, T0 + 1000)
        detail = db.get_quote_detail(ins.id)
        assert detail["source"] == "ws" and detail["source_seq"] == "100"
        # равные метки — разные сделки: обе приняты, итог — больший source_seq
        t_a = QuoteTick(102.0, T0 + 2000, T0 + 2001, "trade", source_seq="101")
        t_b = QuoteTick(103.0, T0 + 2000, T0 + 2002, "trade", source_seq="102")
        assert await worker._ingest_quote(ins, t_a, "ws") is t_a
        assert await worker._ingest_quote(ins, t_b, "ws") is t_b
        assert db.get_quote(ins.id) == (103.0, T0 + 2000)
        # та же ms, но меньший seq, пришедший позже — отклонён
        t_old = QuoteTick(100.0, T0 + 2000, T0 + 2050, "trade", source_seq="99")
        assert await worker._ingest_quote(ins, t_old, "ws") is None
        assert db.get_quote(ins.id) == (103.0, T0 + 2000)
    finally:
        await worker.market_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# Дедуп: точная копия тика обрабатывается один раз
# --------------------------------------------------------------------- #

async def test_exact_duplicate_is_processed_once():
    db = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        calls = []
        _spy_on_price(worker, calls)

        tick = QuoteTick(100.0, T0, T0 + 5, "trade", source_seq="100")
        assert await worker._ingest_quote(ins, tick, "ws") is tick
        assert await worker._ingest_quote(ins, tick, "ws") is None  # дубль
        # тот же seq под ДРУГИМ источником — не дубль (источник в ключе)
        other = QuoteTick(100.0, T0, T0 + 6, "trade", source_seq="100")
        assert await worker._ingest_quote(ins, other, "rest") is other
        assert await worker.market_queue.wait_idle(ins.id, 5)
        assert len(calls) == 2
    finally:
        await worker.market_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# AC03: события очереди совпадают с последовательным эталоном
# --------------------------------------------------------------------- #

def _events_table(db, zone_id):
    return sorted(
        (e.kind.value, e.occurred_at, e.price)
        for e in db.get_events(zone_id=zone_id)
    )


async def test_tick_sequence_events_match_sequential_reference():
    # заход в зону и выход между «кадрами UI»: последовательность тиков
    ticks = [
        QuoteTick(110.0, T0 + 1_000, T0 + 1_000, "trade", source_seq="1"),
        QuoteTick(95.0, T0 + 2_000, T0 + 2_000, "trade", source_seq="2"),
        QuoteTick(92.0, T0 + 3_000, T0 + 3_000, "trade", source_seq="3"),
        QuoteTick(105.0, T0 + 4_000, T0 + 4_000, "trade", source_seq="4"),
        QuoteTick(96.0, T0 + 5_000, T0 + 5_000, "trade", source_seq="5"),
    ]
    db = Database(":memory:")
    db_ref = Database(":memory:")
    try:
        worker = _worker(db)
        ins = _instrument(db)
        zone = _zone(db, ins.id)
        # эталон: та же последовательность синхронно через scanner.on_price
        iid_ref = _instrument(db_ref).id
        zone_ref = _zone(db_ref, iid_ref)
        scanner_ref = Scanner(db_ref, worker.cfg)
        for t in ticks:
            scanner_ref.on_price(iid_ref, t.price, t.monotonic_at, True,
                                 set(worker.scan_tfs), t.price_kind)
        expected = _events_table(db_ref, zone_ref.id)
        assert expected  # эталон непуст, иначе сравнение ничего не проверяет

        for t in ticks:
            assert await worker._ingest_quote(ins, t, "ws") is t
        assert await worker.market_queue.wait_idle(ins.id, 5)
        assert _events_table(db, zone.id) == expected
    finally:
        await worker.market_queue.stop()
        db.close()
        db_ref.close()


# --------------------------------------------------------------------- #
# AC02: зависший расчёт инструмента A не тормозит ingest инструмента B
# --------------------------------------------------------------------- #

async def test_blocked_calc_does_not_block_other_instrument_ingest():
    db = Database(":memory:")
    gate = threading.Event()
    try:
        broadcasts = []
        worker = _worker(db, broadcast=broadcasts.append)
        ins_a = _instrument(db, "BTCUSDT")
        ins_b = _instrument(db, "ETHUSDT")
        calls = []
        real = worker.scanner.on_price

        def gated(iid, price, ts, *args, **kwargs):
            calls.append((iid, price))
            if iid == ins_a.id:
                gate.wait(10)  # расчёт A «завис» внутри calc-замка
            return real(iid, price, ts, *args, **kwargs)

        worker.scanner.on_price = gated

        tick_a = QuoteTick(100.0, T0 + 1000, T0 + 1000, "trade", source_seq="a1")
        assert await worker._ingest_quote(ins_a, tick_a, "ws") is tick_a
        for _ in range(100):  # ждём, пока потребитель A войдёт в расчёт
            if calls:
                break
            await asyncio.sleep(0.05)
        assert calls and calls[0][0] == ins_a.id

        started = time.monotonic()
        tick_b = QuoteTick(200.0, T0 + 1100, T0 + 1100, "trade", source_seq="b1")
        assert await worker._ingest_quote(ins_b, tick_b, "ws") is tick_b
        elapsed = time.monotonic() - started
        # быстрый путь B (persist + broadcast) не ждёт ни calc-замок,
        # ни замок инструмента A
        assert elapsed < 2
        assert db.get_quote(ins_b.id) == (200.0, T0 + 1100)
        assert any(m["type"] == "price" and m["instrument_id"] == ins_b.id
                   for m in broadcasts)
        # потребитель B ждёт calc-замок: обработка тика B ещё не началась
        assert all(iid != ins_b.id for iid, _ in calls)

        gate.set()
        assert await worker.market_queue.wait_idle(ins_a.id, 5)
        assert await worker.market_queue.wait_idle(ins_b.id, 5)
        assert any(iid == ins_b.id for iid, _ in calls)
    finally:
        gate.set()
        await worker.market_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# AC11: переполнение — durable spool + degraded; дочитка без потерь
# --------------------------------------------------------------------- #

async def test_overflow_spools_and_recovers_in_order():
    """AC11 (durable-first): КАЖДЫЙ принятый тик сразу в durable spool —
    молчаливых потерь нет; переполнение лимита — видимый degraded-флаг
    (meta + diagnostics); дочитка строго в порядке приёма, флаг снимается."""
    db = Database(":memory:")
    gate = threading.Event()
    try:
        worker = _worker(db, queue_capacity=2)
        ins = _instrument(db)
        calls = []
        real = worker.scanner.on_price

        def spy_gated(iid, price, ts, *args, **kwargs):
            calls.append((iid, price, ts))
            gate.wait(5)  # потребитель занят первым тиком — spool копится
            return real(iid, price, ts, *args, **kwargs)

        worker.scanner.on_price = spy_gated

        ticks = [
            QuoteTick(100.0 + i, T0 + i, T0 + i, "trade", source_seq=str(i))
            for i in range(5)
        ]
        for t in ticks:
            assert await worker._ingest_quote(ins, t, "ws") is t
        # durable-first: все 5 в spool (capacity=2 — про память речи нет)
        assert [r["price"] for r in db.load_spool(ins.id)] == [100.0, 101.0, 102.0, 103.0, 104.0]
        raw = db.get_meta(f"quote:degraded:{ins.id}")
        assert raw and json.loads(raw)["reason"] == "overflow"
        diag = worker.market_queue.diagnostics()["instruments"][str(ins.id)]
        assert diag["spooled"] == 5 and diag["degraded"]

        gate.set()
        assert await worker.market_queue.wait_idle(ins.id, 5)
        # все 5 обработаны строго в порядке приёма (spool — по id)
        assert [price for _, price, _ in calls] == [100.0, 101.0, 102.0, 103.0, 104.0]
        assert db.load_spool(ins.id) == []
        assert db.get_meta(f"quote:degraded:{ins.id}") is None
        assert not worker.market_queue.diagnostics()["instruments"][str(ins.id)]["degraded"]
    finally:
        gate.set()
        await worker.market_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# Рестарт: остаток spool'а поднимается и дочитывается; протухший флаг
# degraded без строк снимается на старте
# --------------------------------------------------------------------- #

async def test_spool_survives_restart_and_stale_degraded_flag_is_cleared():
    db = Database(":memory:")
    try:
        ins = _instrument(db)
        for i in range(3):
            db.spool_quote(ins.id, source="ws", event_at=T0 + i,
                           received_at=T0 + i, price=100.0 + i,
                           price_kind="trade", source_seq=str(i))
        db.set_meta(f"quote:degraded:{ins.id}", json.dumps(
            {"since_ms": T0, "reason": "overflow", "oldest_pending_ms": T0}))
        empty = _instrument(db, "ETHUSDT")
        db.set_meta(f"quote:degraded:{empty.id}", json.dumps({"since_ms": T0}))

        worker = _worker(db)  # «новый процесс»: очередь построена заново
        calls = []
        _spy_on_price(worker, calls)
        worker.market_queue.start()
        assert await worker.market_queue.wait_idle(ins.id, 5)
        assert [price for _, price, _ in calls] == [100.0, 101.0, 102.0]
        assert db.load_spool(ins.id) == []
        assert db.get_meta(f"quote:degraded:{ins.id}") is None
        assert db.get_meta(f"quote:degraded:{empty.id}") is None
    finally:
        await worker.market_queue.stop()
        db.close()


# --------------------------------------------------------------------- #
# set_quote_if_fresh: атомарная перепроверка свежести
# --------------------------------------------------------------------- #

def test_set_quote_if_fresh_rules(db):
    iid = 1
    assert db.set_quote_if_fresh(
        iid, 100.0, T0, source="ws", price_kind="trade",
        event_at=T0, received_at=T0 + 5, source_seq="100")
    # старше по event_at — отклонено, запись не изменилась
    assert not db.set_quote_if_fresh(
        iid, 99.0, T0 - 10, source="ws", price_kind="trade",
        event_at=T0 - 10, received_at=T0 + 6, source_seq="101")
    assert db.get_quote(iid) == (100.0, T0)
    # равное event_at, больший seq — принято (другая сделка, AC04)
    assert db.set_quote_if_fresh(
        iid, 101.0, T0, source="ws", price_kind="trade",
        event_at=T0, received_at=T0 + 7, source_seq="102")
    # равное event_at, меньший seq — отклонено (числово: 99 < 102)
    assert not db.set_quote_if_fresh(
        iid, 98.0, T0, source="ws", price_kind="trade",
        event_at=T0, received_at=T0 + 8, source_seq="99")
    assert db.get_quote(iid) == (101.0, T0)
    # равное время, seq только у сохранённой записи — принято
    assert db.set_quote_if_fresh(
        iid, 102.0, T0, source="rest", price_kind="trade",
        event_at=T0, received_at=T0 + 9)
    # более свежая сделка — принята
    assert db.set_quote_if_fresh(
        iid, 103.0, T0 + 100, source="rest", price_kind="trade",
        event_at=T0 + 100, received_at=T0 + 105)
    assert db.get_quote(iid) == (103.0, T0 + 100)
    # event_at=None (mid): база сравнения — received_at
    assert not db.set_quote_if_fresh(
        iid, 90.0, T0 + 50, source="rest", price_kind="mid",
        event_at=None, received_at=T0 + 50)
    assert db.set_quote_if_fresh(
        iid, 95.0, T0 + 150, source="rest", price_kind="mid",
        event_at=None, received_at=T0 + 150)
    detail = db.get_quote_detail(iid)
    assert detail["price_kind"] == "mid" and detail["event_at"] is None
    # сделка с event_at раньше received сохранённой mid — старше базы
    assert not db.set_quote_if_fresh(
        iid, 94.0, T0 + 120, source="ws", price_kind="trade",
        event_at=T0 + 120, received_at=T0 + 160, source_seq="500")
    assert db.get_quote_detail(iid)["price"] == 95.0


# --------------------------------------------------------------------- #
# wait_idle: таймаут, пока расчёт занят
# --------------------------------------------------------------------- #

async def test_wait_idle_timeout_while_calc_busy():
    db = Database(":memory:")
    gate = threading.Event()
    try:
        worker = _worker(db)
        ins = _instrument(db)
        real = worker.scanner.on_price

        def gated(*args, **kwargs):
            gate.wait(5)
            return real(*args, **kwargs)

        worker.scanner.on_price = gated
        tick = QuoteTick(100.0, T0, T0, "trade", source_seq="1")
        assert await worker._ingest_quote(ins, tick, "ws") is tick
        assert await worker.market_queue.wait_idle(ins.id, 0.3) is False
        gate.set()
        assert await worker.market_queue.wait_idle(ins.id, 5)
    finally:
        gate.set()
        await worker.market_queue.stop()
        db.close()
