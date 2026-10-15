"""H1 OB/FVG после BOS/SMS: время третьей свечи отказом не является.

Приёмка ТЗ 11.10.2026. Геометрия базы не меняется. Пример BTCUSDT
проверяется отдельно на копии рабочей БД, не этой синтетикой.
"""
from __future__ import annotations

import json

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine, detect_entry_zones
from app.engine.ltf.entries import MovementDraft
from app.models import Direction
from tests.conftest import H1_MS, make_h1_candles
from tests.test_ltf_engine import (
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _events,
    _feed,
    _series,
    _setup,
)
from tests.test_ltf_entries import _by_bounds, _detection


def test_fvg_on_and_after_break_kept_with_separate_times():
    candles, avail, mv, cfg = _detection(DetectorConfig())
    det = detect_entry_zones(candles, mv, avail, Direction.BEAR, cfg)
    d = _by_bounds(det)
    # Тройка, чья третья свеча позже слома, остаётся в контексте.
    late = d[("FVG", 11.8, 12.4)]
    assert late.evidence["relation"] == "after_break"
    assert late.evidence["break_at"] == mv.end_at
    assert late.formed_at > mv.end_at
    assert late.confirmed_at > late.formed_at
    # Зона до слома по-прежнему находится и не помечается как поздняя.
    early = d[("FVG", 13.8, 14.9)]
    assert early.evidence["relation"] == "in_break_movement"
    assert early.formed_at <= mv.end_at
    # Отказ «после слома» не используется. Раньше опоры и чужое направление — да.
    assert not any("после" in r["reason"] for r in det.rejected)
    wrong = detect_entry_zones(candles, mv, avail, Direction.BULL, cfg)
    assert any("направление" in r["reason"] for r in wrong.rejected)
    assert [z for z in wrong.zones if z.direction != Direction.BULL] == []


def test_context_finished_bounds_discovery_without_clock_limit():
    candles, avail, mv, cfg = _detection(DetectorConfig())
    # Дедлайн — закрытие слома. Поздняя тройка к завершённому контексту не относится.
    det = detect_entry_zones(
        candles, mv, avail, Direction.BEAR, cfg,
        context_until=mv.end_at + H1_MS,
    )
    assert ("FVG", 11.8, 12.4) not in _by_bounds(det)
    # A finished context bounds discovery itself. Later candidates are not
    # scanned just to create rejection messages for years of future candles.
    assert all(z.confirmed_at <= mv.end_at + H1_MS for z in det.zones)
    assert not any(r.get('formed_at') == T0 + 13 * H1_MS for r in det.rejected)
    # Тот же ряд без дедлайна позднюю зону принимает: числа свечей после слома нет.
    open_det = detect_entry_zones(candles, mv, avail, Direction.BEAR, cfg)
    assert ("FVG", 11.8, 12.4) in _by_bounds(open_det)


def test_two_candles_are_not_a_confirmed_zone():
    candles, avail, mv, cfg = _detection(DetectorConfig())
    cut = [c for c in candles if c.open_time <= T0 + 12 * H1_MS]
    det = detect_entry_zones(cut, mv, avail, Direction.BEAR, cfg)
    assert ("FVG", 11.8, 12.4) not in _by_bounds(det)


def test_origin_before_leg_still_rejected():
    candles = make_h1_candles([
        (12.0, 12.4, 10.0, 10.2),
        (10.2, 10.4, 8.0, 8.2),
        (8.2, 8.4, 6.0, 6.2),
        (9.0, 11.0, 8.8, 10.8),
        (10.5, 10.6, 9.4, 9.6),
        (9.2, 9.3, 8.0, 8.2),
    ], T0)
    # Первая тройка начинается на idx0, опора ноги — idx3.
    movement = MovementDraft(
        scenario_id=1, direction=Direction.BEAR, start_pivot_ref=1, end_pivot_ref=2,
        start_at=T0 + 3 * H1_MS, end_at=T0 + 5 * H1_MS,
        window_start=T0, break_event_key="t",
    )
    det = detect_entry_zones(candles, movement, [], Direction.BEAR, DetectorConfig())
    assert any("раньше опоры" in r["reason"] for r in det.rejected)
    assert not any(z.evidence.get("fvg_candles", [None])[0] < movement.start_at
                   for z in det.zones if z.type == "FVG")


def test_engine_finds_zone_on_later_close_without_new_break(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zone = db.get_zone(_setup(db, instrument_id))
    obs = engine.on_htf_zone_touched(instrument_id, zone, occurred_at=T0)

    _feed(db, engine, instrument_id, candles, 14)
    assert ("FVG", 8.2, 9.2) not in {
        (z.type, z.lower, z.upper)
        for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
    }
    bos = _events(db, obs.id)[0]
    assert bos.kind == "bos"
    assert not any(e["lower"] == 8.2 for e in bos.payload.get("entries") or [])

    _feed(db, engine, instrument_id, candles, 15)
    found = [
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 8.2, 9.2)
    ]
    assert len(found) == 1
    late = found[0]
    assert late.evidence["relation"] == "after_break"
    assert late.evidence["break_at"] == candles[14].open_time
    assert late.confirmed_at == candles[15].close_time + 1
    assert [e.kind for e in _events(db, obs.id)] == ["bos"]
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert any(
        e.entry_zone_id == late.id
        for e in db.list_ltf_scenario_entries(sc.id)
    )

    confirmed = late.confirmed_at
    n_zones = len(db.list_ltf_entry_zones(instrument_id=instrument_id))
    n_entries = len(db.list_ltf_scenario_entries(sc.id))
    _feed(db, engine, instrument_id, candles, 15)
    engine.replay_observation(obs.id)
    again = [
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 8.2, 9.2)
    ]
    assert len(again) == 1 and again[0].id == late.id
    assert again[0].confirmed_at == confirmed
    assert len(db.list_ltf_entry_zones(instrument_id=instrument_id)) == n_zones
    assert len(db.list_ltf_scenario_entries(sc.id)) == n_entries


def test_detection_continues_after_range_is_fixed(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    obs = engine.on_htf_zone_touched(
        instrument_id, db.get_zone(_setup(db, instrument_id)), occurred_at=T0,
    )
    _feed(db, engine, instrument_id, candles, 23)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "monitoring_entries"
    assert db.get_current_ltf_range(sc.id) is not None
    before = {
        (z.type, z.lower, z.upper)
        for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
    }
    # Свеча 24 исходного ряда отменяет сценарий. Вместо неё — три нижних бара.
    # Low₁ 7.2 > High₃ 5.4: медвежий FVG [5.4; 7.2] после уже фиксированного PD.
    base_hl = SERIES_H_HL[:24]
    closes = {k: v for k, v in SERIES_H_CLOSES.items() if k < 24}
    closes.update({24: 7.3, 25: 6.3, 26: 5.3})
    tail = _series(base_hl + [(7.4, 7.2), (6.4, 6.2), (5.4, 5.2)], closes, instrument_id)
    _feed(db, engine, instrument_id, tail, 26)
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "monitoring_entries"
    now = {
        (z.type, z.lower, z.upper)
        for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
    }
    assert ("FVG", 5.4, 7.2) in now - before
    fresh = next(
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 5.4, 7.2)
    )
    assert fresh.evidence["relation"] == "after_break"
    assert any(
        e.entry_zone_id == fresh.id
        for e in db.list_ltf_scenario_entries(sc.id)
    )


def test_cancelled_scenario_does_not_receive_a_later_zone(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    obs = engine.on_htf_zone_touched(
        instrument_id, db.get_zone(_setup(db, instrument_id)), occurred_at=T0,
    )
    _feed(db, engine, instrument_id, candles, 24)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "cancelled"
    linked = len(db.list_ltf_scenario_entries(sc.id))
    start = len(candles)
    tail_hl = SERIES_H_HL + [(7.4, 7.2), (6.4, 6.2), (5.4, 5.2)]
    tail = _series(
        tail_hl,
        {**SERIES_H_CLOSES, start: 7.3, start + 1: 6.3, start + 2: 5.3},
        instrument_id,
    )
    _feed(db, engine, instrument_id, tail, start + 2)
    assert db.get_ltf_scenario(sc.id).state == "cancelled"
    assert len(db.list_ltf_scenario_entries(sc.id)) == linked


def test_rescan_restores_missing_zone_without_a_live_alert(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    obs = engine.on_htf_zone_touched(
        instrument_id, db.get_zone(_setup(db, instrument_id)), occurred_at=T0,
    )
    _feed(db, engine, instrument_id, candles, 15)
    late = next(
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 8.2, 9.2)
    )
    confirmed = late.confirmed_at
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    db.conn.execute("DELETE FROM ltf_scenario_entry WHERE entry_zone_id=?", (late.id,))
    db.conn.execute("DELETE FROM ltf_entry_zone WHERE id=?", (late.id,))
    db._commit()
    db._bump_ltf_cache()
    db.set_meta(f"ltf:context_zones_rescan:v1:{instrument_id}", "")
    before = [e.id for e in _events(db, obs.id)]
    stats = engine.rescan_active_context_zones(instrument_id)
    assert stats["zones_added"] == 1
    restored = next(
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 8.2, 9.2)
    )
    assert restored.confirmed_at == confirmed
    assert any(
        e.entry_zone_id == restored.id
        for e in db.list_ltf_scenario_entries(sc.id)
    )
    again = engine.rescan_active_context_zones(instrument_id)
    assert again["zones_added"] == 0
    assert len([
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if (z.type, z.lower, z.upper) == ("FVG", 8.2, 9.2)
    ]) == 1
    new_events = [e for e in _events(db, obs.id) if e.id not in before]
    assert not any(e.processing_mode == "live" and not e.delayed for e in new_events)
    raw = json.loads(db.get_meta(f"ltf:announced_entries:{sc.id}") or "[]")
    assert restored.id in raw


def test_bull_after_break_mirrors_bear():
    # Зеркало: бычий разрыв после end_at. L = High первой, U = Low третьей.
    bars = [
        (10, 11, 9, 10),
        (12, 14, 11, 13),
        (15, 18, 14, 17),
        (19, 20, 16, 17),
    ]
    candles = make_h1_candles(bars, T0)
    movement = MovementDraft(
        scenario_id=1, direction=Direction.BULL, start_pivot_ref=1, end_pivot_ref=2,
        start_at=candles[0].open_time, end_at=candles[0].open_time,
        window_start=candles[0].open_time - 5 * H1_MS, break_event_key="bull",
    )
    det = detect_entry_zones(candles, movement, [], Direction.BULL, DetectorConfig())
    late = [
        z for z in det.zones
        if z.type == "FVG" and z.evidence.get("relation") == "after_break"
    ]
    assert late
    assert all(z.direction == Direction.BULL and z.lower < z.upper for z in late)
    bear = detect_entry_zones(candles, movement, [], Direction.BEAR, DetectorConfig())
    assert [z for z in bear.zones if z.type == "FVG"] == []
