"""§5/§12 (этап 2): причинная цепочка сценария — origin break/movement,
структурная эпоха, уровень отмены (reverse break) как хранимый факт с
происхождением, курсор last_processed_close и выбор текущего сценария."""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H2_CLOSES,
    SERIES_H2_HL,
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _events,
    _feed,
    _setup,
)


def _full_candles(instrument_id: int):
    return _series(SERIES_H_HL + SERIES_H2_HL, SERIES_H2_CLOSES, instrument_id)


def test_causal_chain_stored_on_open(db: Database, cfg, instrument_id: int):
    """Триггерное событие и исходное движение хранятся на сценарии явно."""
    engine = LtfEngine(db, cfg)
    candles = _full_candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 14)

    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.trigger_event_id is not None
    assert sc.origin_break_event_id == sc.trigger_event_id
    trig = db.list_ltf_structure_events(sc.id)
    assert [e.id for e in trig] == [sc.origin_break_event_id]
    # исходное движение — движение триггерного слома (§8.1)
    assert sc.origin_movement_id is not None
    mv = db.get_ltf_movement(sc.origin_movement_id)
    assert mv is not None and mv.break_event_id == sc.origin_break_event_id
    # первая эпоха, курсор обработки — закрытие свечи слома
    assert sc.structural_epoch_id == 1
    assert sc.last_processed_close == candles[14].close_time


def test_structure_event_evidence_provenance(db: Database, cfg, instrument_id: int):
    """evidence слома несёт пробитый pivot, его роль и подтверждение (§5/§12)."""
    engine = LtfEngine(db, cfg)
    candles = _full_candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 14)

    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    ev = db.list_ltf_structure_events(sc.id)[0]
    e = ev.evidence
    assert e["broken_pivot_id"] in ev.ref_pivot_ids
    assert e["role_at_event"] == "HL"          # опорный HL медвежьей пары
    assert e["level_price"] == ev.break_level == 9.0
    assert e["close_price"] == 7.9
    assert e["candle_close_time"] == candles[14].close_time
    pivot = db.get_ltf_pivot(e["broken_pivot_id"])
    assert pivot is not None and pivot.confirmed_at == e["pivot_confirmed_at"]


def test_reverse_break_level_stored_on_cancellation(
    db: Database, cfg, instrument_id: int
):
    """§5/§12: уровень отмены — из ref-pivot обратной машины, хранится на
    сценарии (цена/pivot/confirmed_at) и в payload события cancellation."""
    engine = LtfEngine(db, cfg)
    candles = _full_candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 24)

    sc = db.get_ltf_scenario(
        db.list_ltf_scenarios(observation_id=obs.id)[0].id
    )
    assert sc.state == "cancelled" and sc.cancellation_reason == "reverse_bos"
    bull = [
        e for e in db.list_ltf_structure_events(sc.id)
        if e.direction == Direction.BULL
    ]
    assert len(bull) == 1
    rev = bull[0]
    assert sc.reverse_break_level_price == rev.break_level
    assert sc.reverse_break_pivot_id == rev.evidence["broken_pivot_id"]
    assert sc.reverse_break_pivot_id in rev.ref_pivot_ids
    pivot = db.get_ltf_pivot(sc.reverse_break_pivot_id)
    assert pivot is not None
    assert pivot.price == sc.reverse_break_level_price
    assert sc.reverse_break_confirmed_at == pivot.confirmed_at
    # курсор обработки дошёл до свечи отмены
    assert sc.last_processed_close == candles[24].close_time
    # та же тройка — в payload события отмены (журнал «почему отменён»)
    cancel = [e for e in _events(db, obs.id) if e.kind == "cancellation"][-1]
    assert cancel.payload["break_level"] == sc.reverse_break_level_price
    assert cancel.payload["reverse_break_pivot_id"] == sc.reverse_break_pivot_id
    assert (
        cancel.payload["reverse_break_confirmed_at"]
        == sc.reverse_break_confirmed_at
    )


def test_epoch_increments_and_active_selection_after_cancellation(
    db: Database, cfg, instrument_id: int
):
    """Эпоха +1 после отмены обратным сломом; текущий сценарий — только
    живой: отменённый не возвращается, переоткрытый — возвращается."""
    engine = LtfEngine(db, cfg)
    candles = _full_candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 24)

    sc1 = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc1.structural_epoch_id == 1
    # отменённый сценарий текущим не является
    assert db.get_active_ltf_scenario(obs.id) is None
    ranges1 = db.list_ltf_ranges(sc1.id)
    # §7 Этап 4: сначала origin_reversal от якоря движения (idx17),
    # затем continuation при подтверждении пары LH→LL (idx23)
    assert [r.kind for r in ranges1] == ["origin_reversal", "continuation"]
    assert all(r.structural_epoch_id == 1 for r in ranges1)

    _feed(db, engine, instrument_id, candles, 34)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 2
    sc2 = scenarios[1]
    assert sc2.structural_epoch_id == 2
    assert db.get_ltf_scenario(sc1.id).structural_epoch_id == 1
    # активен ровно новый сценарий новой эпохи
    active = db.get_active_ltf_scenario(obs.id)
    assert active is not None and active.id == sc2.id
    ranges2 = db.list_ltf_ranges(sc2.id)
    assert ranges2 and all(
        r.structural_epoch_id == 2 and r.kind in ("origin_reversal", "continuation")
        for r in ranges2
    )
