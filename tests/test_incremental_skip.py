"""Этап 4 (спека §9): AC05 — повторная доставка тех же закрытых свечей не
запускает движок, уведомления и bump версий; AC06 — дыра/ревизия за курсором
вызывает replay из безопасной точки, равный полному эталону; AC07 — смена
конфигурации/решений review при тех же OHLC принуждает к пересчёту."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.adapters.base import QuoteTick, TIMEFRAME_MS
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine import Scanner
from app.engine.ltf import LtfEngine
from app.models import Direction, Instrument, Review, Zone, ZoneStatus, ZoneType
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.data_sync import period_open
from app.services.metrics import get_registry
from app.worker import Worker
from .conftest import make_candle

AT = 1_791_806_407_000  # фиксированные часы; сетка ТФ — от period_open
D1_MS = TIMEFRAME_MS["D1"]
H1_MS = TIMEFRAME_MS["H1"]


class Exchange:
    """Детерминированный источник: свечи из rows[(symbol, tf)] по окну."""

    venue = "binance"

    def __init__(self, clock):
        self.clock = clock
        self.rows: dict = {}

    async def klines(self, symbol, tf, start, end, include_forming=False):
        rows = self.rows.get((symbol, tf), [])
        return [
            replace(c) for c in rows
            if start <= c.open_time <= end and (include_forming or c.closed)
        ]

    async def last_price(self, symbol):
        return QuoteTick(100.0, self.clock[0], self.clock[0], "trade")


def _flat_rows(tf, iid, count=8):
    """Ровная серия закрытых свечей + формирующая на текущем интервале."""
    opening = period_open(tf, AT)
    step = TIMEFRAME_MS[tf]
    rows = [
        make_candle(opening - i * step, 100, 101, 99, 100, tf, iid,
                    source="binance")
        for i in range(count, 0, -1)
    ]
    rows.append(make_candle(opening, 100, 101, 99, 100, tf, iid,
                            closed=False, source="binance"))
    return rows


@pytest.fixture
def market(monkeypatch):
    clock = [AT]
    monkeypatch.setattr("app.services.data_sync.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.worker.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.engine.scanner.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.engine.ltf.engine._real_now_ms", lambda: clock[0])
    db = Database(":memory:")
    cfg = DetectorConfig()
    cfg.ltf_enabled = True
    settings = Settings()
    settings.detector = cfg
    exchange = Exchange(clock)
    sender = LogSender()
    engine = LtfEngine(db, cfg)
    worker = Worker(db, settings, cfg, {"binance": exchange},
                    EventDispatcher(db, cfg, sender), ltf_engine=engine)
    worker.data_sync.ready = True
    iid = db.upsert_instrument(Instrument(
        None, "BTC", "binance", "spot", "BTCUSDT", "USDT"))
    ins = db.get_instrument(iid)
    for tf in ("D1", "H1"):
        exchange.rows["BTCUSDT", tf] = _flat_rows(tf, iid)
    yield db, worker, exchange, clock, ins, sender


async def _stop(worker, db):
    await worker.calc_queue.stop()
    await worker.market_queue.stop()
    db.close()


def _event_count(db):
    return db.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]


def _skip_counter(tf):
    series = get_registry().snapshot()["series"]
    return series.get(f"calc_skipped_unchanged{{timeframe={tf}}}", {}) \
        .get("value", 0)


def _spy(fn, calls):
    def wrapper(*args, **kwargs):
        calls.append(args)
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------- #
# AC05: пропуск неизменившихся данных
# --------------------------------------------------------------------- #

async def test_second_identical_sync_skips_engine_htf(market):
    db, worker, exchange, clock, ins, sender = market
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        first = await worker.data_sync.sync_candles(ins, "D1")
        assert first["status"] == "ok" and first["new_bars"] == 8
        assert calls  # первый прогон обработал хвост
        n_calls = len(calls)
        events_before = _event_count(db)
        cards_before = len(sender.cards)
        skip_before = _skip_counter("D1")

        second = await worker.data_sync.sync_candles(ins, "D1")
        assert second["status"] == "ok"
        assert second["new_bars"] == 0 and second["revised_bars"] == 0
        assert len(calls) == n_calls  # движок не вызывался повторно
        assert _event_count(db) == events_before
        assert len(sender.cards) == cards_before
        assert _skip_counter("D1") > skip_before
        # задание выполнено и удалено; курсор на последнем закрытии
        assert db.conn.execute("SELECT COUNT(*) FROM calc_task").fetchone()[0] == 0
        last = db.last_candle(ins.id, "D1")
        assert db.get_meta(f"data_sync:processed:{ins.id}:D1") == str(last.close_time)
    finally:
        await _stop(worker, db)


async def test_skip_is_zero_writes_at_calc_level(market):
    """Пропуск — ноль записей: ни событий, ни bump state_seq, ни доставки."""
    db, worker, exchange, clock, ins, sender = market
    try:
        await worker.data_sync.sync_candles(ins, "D1")
        seq0 = db.get_state_seq()
        events0 = _event_count(db)
        # повторная обработка того же канала напрямую (очередная семантика)
        result = await worker._process_synced_channel(
            ins, "D1", None, None, recovery_earliest=None)
        assert result is None
        await worker._deliver_synced_channel(ins, "D1", result)
        assert db.get_state_seq() == seq0
        assert _event_count(db) == events0
        assert not sender.cards
    finally:
        await _stop(worker, db)


async def test_second_identical_sync_skips_engine_h1(market):
    db, worker, exchange, clock, ins, sender = market
    engine_calls = []
    parent_calls = []
    engine = worker.ltf_engine
    engine.process_h1_close = _spy(engine.process_h1_close, engine_calls)
    worker._ltf_open_reached_parents = _spy(
        worker._ltf_open_reached_parents, parent_calls)
    try:
        first = await worker.data_sync.sync_candles(ins, "H1")
        assert first["status"] == "ok"
        assert engine_calls and parent_calls
        n_engine, n_parents = len(engine_calls), len(parent_calls)
        ltf_events0 = db.conn.execute("SELECT COUNT(*) FROM ltf_event").fetchone()[0]
        skip_before = _skip_counter("H1")

        second = await worker.data_sync.sync_candles(ins, "H1")
        assert second["status"] == "ok"
        assert len(engine_calls) == n_engine
        assert len(parent_calls) == n_parents
        assert db.conn.execute("SELECT COUNT(*) FROM ltf_event").fetchone()[0] == ltf_events0
        assert _skip_counter("H1") > skip_before
    finally:
        await _stop(worker, db)


async def test_forming_candle_never_triggers_calculation(market):
    db, worker, exchange, clock, ins, sender = market
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        await worker.data_sync.sync_candles(ins, "D1")
        n_calls = len(calls)
        opening = period_open("D1", clock[0])
        # формирующий бар изменился (high вырос) — закрытые те же
        exchange.rows["BTCUSDT", "D1"][-1] = make_candle(
            opening, 100, 105, 99, 103, "D1", ins.id, closed=False,
            source="binance")
        status = await worker.data_sync.sync_candles(ins, "D1")
        assert status["status"] == "ok" and status["new_bars"] == 0
        assert len(calls) == n_calls  # формирующая свеча расчёт не запускает
        # WS-путь: формирующая kline вообще не ставит задание в очередь
        await worker._ws_on_kline(ins, "D1", make_candle(
            opening, 100, 106, 99, 104, "D1", ins.id, closed=False,
            source="binance_ws"))
        await asyncio.sleep(0)
        assert db.conn.execute("SELECT COUNT(*) FROM calc_task").fetchone()[0] == 0
        assert len(calls) == n_calls
    finally:
        await _stop(worker, db)


async def test_legacy_path_when_queue_disabled(market):
    """Откат этапа: HTF_CALC_QUEUE_ENABLED=0 — прежний синхронный вызов,
    durable-строк не создаётся."""
    db, worker, exchange, clock, ins, sender = market
    worker.settings.calc_queue_enabled = False
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        status = await worker.data_sync.sync_candles(ins, "D1")
        assert status["status"] == "ok" and status["new_bars"] == 8
        assert calls
        assert db.conn.execute("SELECT COUNT(*) FROM calc_task").fetchone()[0] == 0
    finally:
        await _stop(worker, db)


# --------------------------------------------------------------------- #
# AC07: инвалидация по зависимостям
# --------------------------------------------------------------------- #

async def test_config_change_forces_recalc_with_same_ohlc(market):
    db, worker, exchange, clock, ins, sender = market
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        await worker.data_sync.sync_candles(ins, "D1")
        n_calls = len(calls)
        events0 = _event_count(db)
        # прод-разводка: POST /api/settings мутирует тот же DetectorConfig
        worker.cfg.depth_worked = 0.8
        status = await worker.data_sync.sync_candles(ins, "D1")
        assert status["status"] == "ok" and status["new_bars"] == 0
        # движок пересчитал свежий хвост несмотря на те же OHLC (идемпотентно)
        assert len(calls) > n_calls
        assert _event_count(db) == events0  # дублей нет
        # зависимости зафиксированы — следующая синхронизация снова пропускается
        skip_before = _skip_counter("D1")
        await worker.data_sync.sync_candles(ins, "D1")
        assert _skip_counter("D1") > skip_before
    finally:
        await _stop(worker, db)


async def test_review_decision_invalidates_deps(market):
    db, worker, exchange, clock, ins, sender = market
    zid = db.insert_zone(Zone(
        id=None, instrument_id=ins.id, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=90.0, upper=95.0, formed_at=AT - 10 * D1_MS,
        confirmed_at=AT - 9 * D1_MS, status=ZoneStatus.ACTIVE))
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        await worker.data_sync.sync_candles(ins, "D1")
        n_calls = len(calls)
        skip_before = _skip_counter("D1")
        await worker.data_sync.sync_candles(ins, "D1")
        assert _skip_counter("D1") > skip_before  # контроль: сейчас пропускает
        # решение review при тех же OHLC — инвалидация и пересчёт
        db.add_review(Review(id=None, zone_id=zid, decision="confirm",
                             author="owner", text="", boundary_version=1,
                             created_at=AT))
        await worker.data_sync.sync_candles(ins, "D1")
        assert len(calls) > n_calls
    finally:
        await _stop(worker, db)


async def test_zone_status_change_invalidates_deps(market):
    db, worker, exchange, clock, ins, sender = market
    zid = db.insert_zone(Zone(
        id=None, instrument_id=ins.id, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=90.0, upper=95.0, formed_at=AT - 10 * D1_MS,
        confirmed_at=AT - 9 * D1_MS, status=ZoneStatus.ACTIVE))
    calls = []
    worker.scanner.on_closed_candle = _spy(worker.scanner.on_closed_candle, calls)
    try:
        await worker.data_sync.sync_candles(ins, "D1")
        n_calls = len(calls)
        db.update_zone(zid, status=ZoneStatus.REJECTED)  # как при отклонении в UI
        await worker.data_sync.sync_candles(ins, "D1")
        assert len(calls) > n_calls
    finally:
        await _stop(worker, db)


# --------------------------------------------------------------------- #
# AC06: replay из безопасной точки = полный эталонный replay
# --------------------------------------------------------------------- #

def _structured_series(iid, count=30):
    """Серия D1: бычий FVG на тройке 17/18/19 (low[19] > high[17]), на 26 —
    заход в зону (TOUCH/DEPTH события), дальше восстановление."""
    opening = period_open("D1", AT)
    start = opening - count * D1_MS
    rows = []
    price = 100.0
    for i in range(count):
        ot = start + i * D1_MS
        o, c = price, price + 0.5
        h, l = c + 0.3, o - 0.3
        if i == 18:  # импульс
            o, h, l, c = price, price + 3.5, price - 0.3, price + 3.0
        elif i == 19:  # low выше high свечи 17 → FVG
            o, h, l, c = price + 2.7, price + 3.4, price + 1.2, price + 3.1
        elif i == 26:  # глубокий заход в зону FVG с возвратом
            o, h, l, c = price, price + 0.5, 110.0, price + 0.3
        rows.append(make_candle(ot, o, h, l, c, "D1", iid, source="binance"))
        price = c
    return rows


def _zones_snapshot(db):
    return sorted(
        (z.type.value, z.direction.value, z.timeframe, z.lower, z.upper,
         z.formed_at, z.confirmed_at, z.status.value, z.cycle_id,
         z.display_from, z.display_until, z.market_validity)
        for z in db.get_zones()
    )


def _events_snapshot(db):
    zones = {z.id: z for z in db.get_zones()}
    out = []
    for e in db.get_events(limit=100000):
        z = zones.get(e.zone_id)
        zkey = None if z is None else (
            z.type.value, z.direction.value, z.timeframe, z.lower, z.upper,
            z.formed_at, z.cycle_id)
        # detected_at — служебное время, не рыночный факт: не сравниваем
        out.append((zkey, e.kind.value, e.occurred_at, e.price, e.depth, e.delayed))
    return sorted(out)


def _reference_replay(rows):
    """Полный эталонный replay по полной серии на свежей БД."""
    ref = Database(":memory:")
    ref_iid = ref.upsert_instrument(Instrument(
        None, "BTC", "binance", "spot", "BTCUSDT", "USDT"))
    ref.insert_candles([replace(c, instrument_id=ref_iid) for c in rows])
    Scanner(ref, DetectorConfig()).replay_instrument(ref_iid, None, {"D1"})
    return ref


async def test_hole_behind_cursor_replays_to_reference(market):
    db, worker, exchange, clock, ins, sender = market
    # Дыра в инертном дрейфе (любая тройка дрейфовых свечей одинакова —
    # первый прогон с дырой не публикует иных зон/событий, а replay историю
    # не переписывает задним числом). Формация FVG (17-20) и заход в зону
    # (26) — ПОСЛЕ дыры, так что replay из безопасной точки пересчитывает их.
    hole_idx = 5
    full = _structured_series(ins.id)
    try:
        # первая синхронизация: серия с дырой (свеча hole_idx отсутствует)
        with_hole = [c for i, c in enumerate(full) if i != hole_idx]
        with_hole.append(make_candle(period_open("D1", AT), full[-1].close,
                                     full[-1].close + 1, full[-1].close - 1,
                                     full[-1].close, "D1", ins.id,
                                     closed=False, source="binance"))
        exchange.rows["BTCUSDT", "D1"] = with_hole
        first = await worker.data_sync.sync_candles(ins, "D1")
        assert first["gaps"] and first["reason"] == "history_gap"  # дыра видна
        # источник доставил пропущенную свечу — синхронизация расширяет окно
        exchange.rows["BTCUSDT", "D1"] = full + [with_hole[-1]]
        second = await worker.data_sync.sync_candles(ins, "D1")
        assert second["status"] == "ok" and not second["gaps"]
        ref = _reference_replay(full)
        assert _zones_snapshot(db) == _zones_snapshot(ref)
        assert _events_snapshot(db) == _events_snapshot(ref)
        assert _events_snapshot(db)  # в серии есть реальные события
        last = db.last_candle(ins.id, "D1")
        assert db.get_meta(f"data_sync:processed:{ins.id}:D1") == str(last.close_time)
        # после replay хвост стабилен: следующая синхронизация пропускается
        skip_before = _skip_counter("D1")
        await worker.data_sync.sync_candles(ins, "D1")
        assert _skip_counter("D1") > skip_before
    finally:
        await _stop(worker, db)


async def test_revision_behind_cursor_replays_to_reference(market):
    db, worker, exchange, clock, ins, sender = market
    full = _structured_series(ins.id)
    forming = make_candle(period_open("D1", AT), full[-1].close,
                          full[-1].close + 1, full[-1].close - 1,
                          full[-1].close, "D1", ins.id, closed=False,
                          source="binance")
    try:
        exchange.rows["BTCUSDT", "D1"] = [*full, forming]
        first = await worker.data_sync.sync_candles(ins, "D1")
        assert first["status"] == "ok"
        # коррекция источника: ПОСЛЕДНЯЯ закрытая свеча (уже обработана —
        # close_time на курсоре) изменила OHLC; окно синхронизации её видит
        revised_full = list(full)
        revised_full[-1] = replace(
            full[-1], high=full[-1].high + 2.0, close=full[-1].close + 1.5)
        exchange.rows["BTCUSDT", "D1"] = [*revised_full, forming]
        second = await worker.data_sync.sync_candles(ins, "D1")
        assert second["status"] == "ok" and second["revised_bars"] == 1
        ref = _reference_replay(revised_full)
        assert _zones_snapshot(db) == _zones_snapshot(ref)
        assert _events_snapshot(db) == _events_snapshot(ref)
    finally:
        await _stop(worker, db)


# --------------------------------------------------------------------- #
# AC18: сбой расчёта не сдвигает курсор/отпечаток; повторный прогон честный
# --------------------------------------------------------------------- #

async def test_failed_calc_keeps_cursor_and_fingerprint(market):
    db, worker, exchange, clock, ins, sender = market
    try:
        first = await worker.data_sync.sync_candles(ins, "D1")
        assert first["status"] == "ok"
        cursor0 = db.get_meta(f"data_sync:processed:{ins.id}:D1")
        fp0 = db.get_meta(f"htf:ohlc:{ins.id}:D1")
        assert cursor0 and fp0
        # часы на интервал вперёд: бывший формирующий бар закрылся,
        # открылся новый — и расчёт падает на нём
        clock[0] += D1_MS
        rows = exchange.rows["BTCUSDT", "D1"]
        new_closed = make_candle(rows[-1].open_time, 100, 101, 99, 100.5,
                                 "D1", ins.id, source="binance")
        exchange.rows["BTCUSDT", "D1"] = [*rows[:-1], new_closed, make_candle(
            rows[-1].open_time + D1_MS, 100.5, 101, 100, 100.7, "D1", ins.id,
            closed=False, source="binance")]
        real = worker.scanner.on_closed_candle
        worker.scanner.on_closed_candle = _failing_once(real)
        with pytest.raises(RuntimeError, match="boom"):
            await worker.data_sync.sync_candles(ins, "D1")
        # курсор и отпечаток НЕ сдвинулись; задание видно как failed (AC18)
        assert db.get_meta(f"data_sync:processed:{ins.id}:D1") == cursor0
        assert db.get_meta(f"htf:ohlc:{ins.id}:D1") == fp0
        row = db.conn.execute(
            "SELECT status, error FROM calc_task").fetchone()
        assert row["status"] == "failed" and "boom" in row["error"]
        # следующий цикл синхронизации перезапускает задание — успешно
        status = await worker.data_sync.sync_candles(ins, "D1")
        assert status["status"] == "ok"
        assert db.get_meta(f"data_sync:processed:{ins.id}:D1") != cursor0
        assert db.conn.execute("SELECT COUNT(*) FROM calc_task").fetchone()[0] == 0
    finally:
        await _stop(worker, db)


def _failing_once(real):
    state = {"failed": False}

    def wrapper(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    return wrapper
