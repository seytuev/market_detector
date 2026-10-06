"""ТЗ переработки L02: причинность и неопределённость контекста §18.

Приёмка:
- факт учитывается только если был доступен на момент решения (as_of):
  зоны/события «из будущего» не меняют результат исторического запроса;
- зона, сформированная позже проверяемого события, не является его
  контекстом (историческому движению не приписывается поздний контекст);
- неподтверждённый геометрический fallback не включает контекстное
  исключение §18 (движок не эмитит факт htf_fvg50).
"""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.context import find_htf_fvg50_test
from app.models import (
    Direction,
    Event,
    EventKind,
    Zone,
    ZoneStatus,
    ZoneType,
)
from tests.conftest import H1_MS
from tests.test_ltf_breaks import _series
from tests.test_ltf_context_short import (
    SERIES_CTX_CLOSES,
    SERIES_CTX_HL,
    _feed,
)

T0 = 1_780_000_000_000


def _bull_d1_fvg(db: Database, instrument_id: int, formed_at: int,
                 lower: float = 6.5, upper: float = 8.0) -> int:
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=lower, upper=upper,
        formed_at=formed_at, confirmed_at=formed_at + 1000,
        status=ZoneStatus.ACTIVE,
    ))


# ------------------------- find_htf_fvg50_test -------------------------

def test_as_of_excludes_future_zones(db: Database, instrument_id: int):
    zid = _bull_d1_fvg(db, instrument_id, formed_at=T0 + 10 * H1_MS)
    # на момент до формирования зоны факта нет
    assert find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR, as_of=T0
    ) is None
    # после формирования — есть
    hit = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR, as_of=T0 + 11 * H1_MS
    )
    assert hit is not None and hit["zone_id"] == zid


def test_historical_query_stable_against_future_facts(
    db: Database, instrument_id: int
):
    early = _bull_d1_fvg(db, instrument_id, formed_at=T0 - 8_000_000)
    before = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR, as_of=T0, event_time=T0
    )
    assert before is not None and before["zone_id"] == early
    # зона и подтверждающее событие «из будущего» не меняют запрос на T0
    late = _bull_d1_fvg(db, instrument_id, formed_at=T0 + 5 * H1_MS,
                        lower=6.8, upper=8.2)
    db.insert_event(Event(
        id=None, zone_id=early, cycle_id=1, kind=EventKind.DEPTH_50,
        occurred_at=T0 + 6 * H1_MS, detected_at=T0 + 6 * H1_MS,
        price=7.0, depth=0.5,
    ))
    after = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR, as_of=T0, event_time=T0
    )
    assert after == before
    # а на момент после их появления видна и поздняя зона, и событие
    later = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR,
        as_of=T0 + 7 * H1_MS, event_time=T0,
    )
    assert later is not None and later["zone_id"] in (early, late)


def test_event_after_as_of_does_not_confirm(db: Database,
                                            instrument_id: int):
    zid = _bull_d1_fvg(db, instrument_id, formed_at=T0 - 8_000_000)
    db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.FVG_WEAKENED,
        occurred_at=T0 + 1000, detected_at=T0 + 1000, price=7.0, depth=0.5,
    ))
    # событие позже момента решения недоступно: только геометрия
    hit = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR, as_of=T0, event_time=T0
    )
    assert hit["via"] == "geometry"
    # на момент после события — подтверждение событием
    hit2 = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR,
        as_of=T0 + 2000, event_time=T0,
    )
    assert hit2["via"] == "event" and hit2["confirmed"] is True


def test_geometry_fallback_confirmation_status(db: Database,
                                               instrument_id: int):
    # зона существовала к моменту теста — fallback подтверждён
    _bull_d1_fvg(db, instrument_id, formed_at=T0 - 8_000_000)
    hit = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR,
        as_of=T0, event_time=T0 - 1000,
    )
    assert hit["via"] == "geometry" and hit["confirmed"] is True
    # без момента теста причинность не доказана — неподтверждённый факт
    hit2 = find_htf_fvg50_test(db, instrument_id, 7.0, Direction.BEAR)
    assert hit2["via"] == "geometry" and hit2["confirmed"] is False


def test_zone_formed_after_test_is_not_its_context(db: Database,
                                                   instrument_id: int):
    # зона сформирована позже проверяемого движения: достижение её 50%
    # этим движением невозможно — fallback неподтверждён
    _bull_d1_fvg(db, instrument_id, formed_at=T0 + 3 * H1_MS)
    hit = find_htf_fvg50_test(
        db, instrument_id, 7.0, Direction.BEAR,
        as_of=T0 + 10 * H1_MS, event_time=T0,
    )
    assert hit is not None
    assert hit["via"] == "geometry" and hit["confirmed"] is False


# ------------------------- движок: fallback не включает §18 -------------------------

def test_engine_does_not_emit_unconfirmed_fvg50(db: Database,
                                                instrument_id: int):
    """D1 FVG сформирована ПОСЛЕ экстремума движения: факт htf_fvg50 не
    эмитится, контекст не полон, FVG вне Premium не допускаются."""
    cfg = DetectorConfig()
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_CTX_HL, SERIES_CTX_CLOSES, instrument_id)
    db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=16.0, upper=18.0,
        formed_at=T0 - 10_000_000, confirmed_at=T0 - 9_000_000,
        status=ZoneStatus.ACTIVE,
    ))
    # экстремум 7.2 — idx22; зона формируется позже него (idx24)
    _bull_d1_fvg(db, instrument_id,
                 formed_at=candles[24].open_time + 1)
    parent = db.get_zones(instrument_id=instrument_id,
                          types=[ZoneType.OB])[0]
    obs = engine.on_htf_zone_touched(instrument_id, parent, occurred_at=T0)

    _feed(db, engine, instrument_id, candles, 25)
    evs = sorted(db.list_ltf_events(observation_id=obs.id, limit=1000),
                 key=lambda e: (e.occurred_at, e.id))
    facts = [e.payload.get("fact") for e in evs
             if e.kind == "context_update"]
    # снятие SSL зафиксировано, а тест 50% D1 FVG — нет: зоны к моменту
    # теста не существовало (L02)
    assert "counter_sweep" in facts
    assert "htf_fvg50" not in facts
    # entries_ready без контекстного допуска: только зона в Premium
    ready = [e for e in evs if e.kind == "entries_ready"]
    assert ready, "ожидался entries_ready без контекстных допусков"
    entries = ready[-1].payload["entries"]
    assert all("outside_premium" not in e for e in entries)
    assert {e["lower"] for e in entries} == {9.6}
