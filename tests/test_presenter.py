"""Presenter: нормализованный тип события и единый снимок (ТЗ 07.10.2026 §4, §11)."""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.presenter import (
    NormalizedKind,
    htf_snapshot,
    ltf_snapshot,
)
from app.notify.queue import EventView


def _mk(db: Database, ztype: ZoneType, lower: float, upper: float):
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zid = db.insert_zone(
        Zone(None, iid, ztype, Direction.BULL, "D1", lower=lower, upper=upper,
             formed_at=now_ms() - 10_000, confirmed_at=now_ms() - 9_000,
             status=ZoneStatus.ACTIVE, created_at=now_ms())
    )
    return iid, zid


def _view(db: Database, zid: int, kind: EventKind, ts: int,
          evidence: dict | None = None) -> EventView:
    eid = db.insert_event(
        Event(None, zid, 1, kind, occurred_at=ts, detected_at=ts,
              price=2600.15, evidence=evidence or {})
    )
    event = next(e for e in db.get_events(zone_id=zid) if e.id == eid)
    zone = db.get_zone(zid)
    ins = db.get_instrument(zone.instrument_id)
    return EventView(event=event, zone=zone, instrument=ins)


def test_htf_snapshot_approach_carries_distance():
    db = Database(":memory:")
    _, zid = _mk(db, ZoneType.FVG, 81951.0, 82563.0)
    snap = htf_snapshot(_view(db, zid, EventKind.APPROACH, now_ms(),
                              evidence={"distance_pct": 0.0151}))
    assert snap.kind == NormalizedKind.APPROACH
    assert snap.distance_pct == 0.0151
    assert snap.symbol == "ETHUSDT"          # точный символ, не «ETH» (§5.1)
    assert snap.venue == "binance"           # источник — внутри снимка
    assert not snap.is_level
    assert snap.mid == (81951.0 + 82563.0) / 2


def test_htf_snapshot_level_has_no_mid():
    """Для SSL/BSL — «Уровень», середина не выдумывается (T11)."""
    db = Database(":memory:")
    _, zid = _mk(db, ZoneType.SSL, 2600.15, 2600.15)
    snap = htf_snapshot(_view(db, zid, EventKind.LEVEL_TAKEN, now_ms()))
    assert snap.kind == NormalizedKind.LIQUIDITY_TAKEN
    assert snap.is_level
    assert snap.mid is None
    assert snap.lower == snap.upper == 2600.15


def test_htf_kind_mapping_mutex():
    """Противоречивые утверждения взаимоисключены: FVG_WEAKENED — это
    достижение 50%, а не приближение; LEVEL_TAKEN — не касание зоны."""
    db = Database(":memory:")
    _, zid = _mk(db, ZoneType.FVG, 100.0, 110.0)
    assert htf_snapshot(_view(db, zid, EventKind.FVG_WEAKENED, now_ms())).kind \
        == NormalizedKind.DEPTH_50
    assert htf_snapshot(_view(db, zid, EventKind.FVG_FILLED, now_ms())).kind \
        == NormalizedKind.ZONE_INVALIDATED
    assert htf_snapshot(_view(db, zid, EventKind.TOUCH, now_ms())).kind \
        == NormalizedKind.TOUCH


def test_htf_status_from_engine_not_invented():
    """Статус — реальный из движка: снятая зона не «актуальна» (§5.2)."""
    db = Database(":memory:")
    _, zid = _mk(db, ZoneType.SSL, 2600.15, 2600.15)
    db.update_zone(zid, status=ZoneStatus.TAKEN)
    snap = htf_snapshot(_view(db, zid, EventKind.LEVEL_TAKEN, now_ms()))
    assert snap.status_ru == "снята"


def test_ltf_snapshot_break_and_touch():
    from types import SimpleNamespace as NS

    ctx = NS(
        instrument=NS(id=7, symbol="BTCUSDT", venue="binance",
                      market_type="spot"),
        zone=NS(id=42, type=NS(value="fvg"), timeframe="D1",
                direction=NS(value="bear")),
        observation=None,
        scenario=NS(direction=NS(value="bear")),
    )
    ev = NS(
        id=5, kind="bos", occurred_at=1780000000000,
        detected_at=1780000001000, scenario_id=3,
        payload={"level": 2690.40, "close": 2688.10},
    )

    snap = ltf_snapshot(ev, ctx)
    assert snap.kind == NormalizedKind.BOS_CONFIRMED
    assert snap.symbol == "BTCUSDT"
    assert snap.parent_zone_id == 42
    assert snap.parent_type == "fvg"
    assert snap.parent_timeframe == "D1"
    assert snap.direction == "bear"
    assert snap.as_of == 1780000001000

    touch = NS(
        id=6, kind="touch", occurred_at=1780000000000,
        detected_at=1780000001000, scenario_id=3,
        payload={"entry_zone_id": 9, "type": "SSL", "lower": 2600.15,
                 "upper": 2600.15, "candle_open_time": 1779996400000},
    )

    snap2 = ltf_snapshot(touch, ctx)
    assert snap2.kind == NormalizedKind.ENTRY_TOUCH
    assert snap2.is_level and snap2.mid is None
    assert snap2.candle_close_at == snap2.event_at
    assert snap2.scenario_id == 3
