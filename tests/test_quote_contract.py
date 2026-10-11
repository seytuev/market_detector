"""Этап 7: контракт котировок и адаптеры (правила v1 §9, §10.1).

R45: mid/ticker не выдаётся за сделку (event_at=None). Монотонность по
event_at/received_at (F8/R47): старый тик не откатывает котировку,
формирующийся бар и состояние зон. R22/R23: quote-касание фиксируется без
H1-close, повторная обработка OHLC той же H1 не создаёт второе touch, факты
связаны общей активацией. Возраст данных и задержка обнаружения — раздельно.
"""
from __future__ import annotations

from app.adapters.base import QuoteTick
from app.db import Database
from app.engine.ltf.engine import LtfEngine
from app.engine.scanner import Scanner
from app.models import Direction, EventKind, Zone, ZoneStatus, ZoneType, now_ms
from tests.conftest import H1_MS, make_candle, make_h1_candles
from tests.test_quote_loop import FakeAdapter, _make_worker, _seed, _zone

T = 1_780_012_800_000
H = 3_600_000


def _htf_zone(db, iid, lower=90.0, upper=110.0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=lower, upper=upper, formed_at=T - 2 * H,
        confirmed_at=T - H, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


# --------------------------------------------------------------------- #
# Монотонность: event_at, когда известен; иначе received_at
# --------------------------------------------------------------------- #

async def test_monotonic_trade_then_info_price():
    db = Database(":memory:")
    try:
        adapter = FakeAdapter()
        worker = _make_worker(db, adapter)
        ins = await _seed(db, worker)
        now = now_ms()
        # доказанная сделка на T+1ч
        adapter.price = QuoteTick(100.0, now + H, now, "trade")
        await worker._poll_quote(ins)
        assert db.get_quote(ins.id) == (100.0, now + H)
        # информационная mid, полученная РАНЬШЕ event_at сделки — игнорируется
        adapter.price = QuoteTick(90.0, None, now, "mid")
        await worker._poll_quote(ins)
        assert db.get_quote(ins.id) == (100.0, now + H)
        # mid, полученная позже — принимается (монотонность по received_at)
        adapter.price = QuoteTick(95.0, None, now + 2 * H, "mid")
        await worker._poll_quote(ins)
        assert db.get_quote(ins.id) == (95.0, now + 2 * H)
        detail = db.get_quote_detail(ins.id)
        assert detail["price_kind"] == "mid"
        assert detail["event_at"] is None
        assert detail["received_at"] == now + 2 * H
    finally:
        db.close()


async def test_r47_stale_tick_keeps_forming_bar_and_zones():
    db = Database(":memory:")
    try:
        adapter = FakeAdapter()
        worker = _make_worker(db, adapter)
        ins = await _seed(db, worker)
        now = now_ms()
        # формирующийся H1-бар в БД
        bar_open = now - (now % H1_MS)
        db.insert_candles([make_candle(bar_open, 100.0, 101.0, 99.0, 100.5,
                                       timeframe="H1", instrument_id=ins.id,
                                       closed=False)])
        adapter.price = QuoteTick(102.0, now, now, "trade")
        await worker._poll_quote(ins)
        forming = db.last_candle(ins.id, "H1", closed_only=False)
        assert forming.high == 102.0
        # старый тик — бар и котировка не откатываются
        adapter.price = QuoteTick(98.0, now - H, now - H, "trade")
        await worker._poll_quote(ins)
        forming = db.last_candle(ins.id, "H1", closed_only=False)
        assert forming.high == 102.0 and forming.close == 102.0
        assert db.get_quote(ins.id) == (102.0, now)
    finally:
        db.close()


# --------------------------------------------------------------------- #
# R45/§10.1: вид цены в evidence контакта; возраст и задержка раздельно
# --------------------------------------------------------------------- #

def test_contact_evidence_marks_price_kind(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    ev = scanner.on_price(instrument_id, 91.0, T, True, price_kind="mid")
    assert [e.kind for e in ev] == [EventKind.TOUCH]
    assert ev[0].evidence["price_kind"] == "mid"


async def test_age_and_detection_lag_stored():
    db = Database(":memory:")
    try:
        adapter = FakeAdapter()
        worker = _make_worker(db, adapter)
        ins = await _seed(db, worker)
        now = now_ms()
        adapter.price = QuoteTick(100.0, now - 5_000, now - 1_000, "trade")
        await worker._poll_quote(ins)
        detail = db.get_quote_detail(ins.id)
        assert detail["age_ms"] >= 5_000
        assert detail["detection_lag_ms"] >= 1_000
    finally:
        db.close()


# --------------------------------------------------------------------- #
# R22/R23: quote-касание без H1-close; общая активация; без второго touch
# --------------------------------------------------------------------- #

def test_r22_r23_quote_touch_shared_activation(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id)
    engine = LtfEngine(db, cfg)
    engine.on_htf_zone_touched(instrument_id, zone, T)
    act = db.get_open_zone_activation(zone.id, zone.cycle_id)
    assert act is not None and act["observation_id"] is not None
    scanner = Scanner(db, cfg)
    # касание по тику фиксируется сразу, без закрытия H1 (R22)
    ev = scanner.on_price(instrument_id, 91.0, T + H, True, price_kind="trade")
    assert [e.kind for e in ev] == [EventKind.TOUCH]
    # углубление на следующем тике — событие ссылается на ту же активацию
    ev2 = scanner.on_price(instrument_id, 100.5, T + 2 * H, True,
                           price_kind="trade")
    assert ev2 and ev2[0].activation_id == act["id"]
    assert ev2[0].visit_id is not None
    # R23: та же H1 обработана как закрытая — второго touch-сообщения нет
    db.insert_candles(make_h1_candles(
        [(100.0, 100.6, 99.8, 100.2)], T, instrument_id=instrument_id))
    engine.process_h1_close(instrument_id, now_ms=T + 2 * H)
    touches = [e for e in db.get_events(zone.id)
               if e.kind == EventKind.TOUCH]
    assert len(touches) == 1
