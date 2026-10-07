"""§6 (Этап 3): аудит BOS/SMS — регрессионные тесты.

- п.4: полнота evidence каждого вида события (primary/secondary BOS, SMS,
  accompanying SMS, обратный слом): broken_pivot_id, role_at_event,
  level_price, pivot_confirmed_at, свеча слома/закрытия, close_price,
  тип/этап, направление, структура-происхождение.
- п.5: повторные закрытия за уже пробитым уровнем копий не создают.
- п.6: свеча BOS+SMS — оба факта в журнале структуры, одно уведомление,
  один набор зон (без дублей).
- п.7: новые наблюдения порождаются только касанием HTF-зоны, не событиями
  структуры H1.

Детекция (строгое закрытие, SMS, вторичный с откатом, непрерывное падение —
не два этапа) уже покрыта tests/test_ltf_breaks.py.
"""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.breaks import detect_breaks
from app.models import Direction
from tests.conftest import H1_MS
from tests.test_ltf_breaks import (
    SERIES_A_HL,
    SERIES_A_CLOSES,
    SERIES_B_HL,
    _pivots_with_roles,
    _series,
    NOW,
)
from tests.test_ltf_engine import (
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _events,
    _feed,
    _setup,
)

REQUIRED_EVIDENCE = (
    "broken_pivot_id", "role_at_event", "level_price", "pivot_confirmed_at",
    "candle_close_time", "close_price",
)


def _assert_full_event(ev) -> None:
    """§6 п.4: обязательные поля события слома (draft или строка БД)."""
    e = ev.evidence
    for key in REQUIRED_EVIDENCE:
        assert e.get(key) is not None, (ev.kind, ev.stage, key)
    assert e["broken_pivot_id"] in ev.ref_pivot_ids
    assert e["level_price"] == ev.break_level
    assert e["role_at_event"] in (
        "HH", "HL", "LH", "LL", "internal_low", "internal_high",
    )
    assert e["close_price"] is not None
    assert ev.break_candle_open_time is not None       # break_candle_id
    assert ev.kind in ("BOS", "SMS")                   # тип
    assert ev.stage in ("primary", "secondary")        # фаза
    assert ev.direction in (Direction.BEAR, Direction.BULL)
    # структура-происхождение: опорная пара/тройка pivots уровня
    assert ev.ref_pivot_ids
    assert e.get("anchor_price") is not None or e.get("ref_price") is not None


def test_evidence_completeness_detect_level():
    """primary/secondary BOS, самостоятельный SMS, accompanying SMS."""
    # SERIES_A: медвежий primary + secondary (откат есть)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW)
    assert [(e.kind, e.stage) for e in res.events] == [
        ("BOS", "primary"), ("BOS", "secondary"),
    ]
    for ev in res.events:
        _assert_full_event(ev)
    # SERIES_B: самостоятельный SMS
    candles = _series(SERIES_B_HL, {18: 10.9})
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW)
    assert [e.kind for e in res.events] == ["SMS"]
    _assert_full_event(res.events[0])
    # §6.5: accompanying — обе записи с полным evidence
    candles = _series(SERIES_B_HL[:-1] + [(12.5, 8.8)], {18: 8.9})
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW)
    assert [(e.kind, e.accompanying) for e in res.events] == [
        ("BOS", False), ("SMS", True),
    ]
    for ev in res.events:
        _assert_full_event(ev)


def test_evidence_completeness_engine_reverse_break(db: Database, cfg,
                                                    instrument_id: int):
    """Обратный слом (отмена) хранит ту же полноту evidence + ref-pivot
    обратной машины (Этап 2 использует его для уровня отмены)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 24)

    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    events = db.list_ltf_structure_events(sc.id)
    assert {(e.kind, e.direction) for e in events} == {
        ("BOS", Direction.BEAR), ("BOS", Direction.BULL),
    }
    for ev in events:
        _assert_full_event(ev)
    rev = [e for e in events if e.direction == Direction.BULL][0]
    assert rev.evidence["broken_pivot_id"] == sc.reverse_break_pivot_id
    assert rev.evidence["pivot_confirmed_at"] == sc.reverse_break_confirmed_at


def test_repeated_closes_beyond_level_single_event(db: Database, cfg,
                                                   instrument_id: int):
    """п.5: закрытия за пробитым уровнем на следующих свечах копий события
    не создают — ни в детекторе, ни в БД, ни после replay."""
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    # детектор: idx14 (7.9) и idx15-17 (середины < 9.0) — один primary BOS 9.0
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW)
    primaries = [e for e in res.events
                 if e.kind == "BOS" and e.stage == "primary"
                 and e.break_level == 9.0]
    assert len(primaries) == 1

    engine = LtfEngine(db, cfg)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 17)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    keys = [e.level_key for e in db.list_ltf_structure_events(sc.id)]
    assert len(keys) == len(set(keys)) == 1
    engine.replay_observation(obs.id)
    assert len(db.list_ltf_structure_events(sc.id)) == 1
    assert [e.kind for e in _events(db, obs.id)].count("bos") == 1


def test_accompanying_candle_single_zone_and_notification_set(
    db: Database, cfg, instrument_id: int
):
    """п.6: свеча BOS+SMS — оба факта в ltf_structure_event (SMS с
    accompanying=True), одно уведомление bos, одно движение, один набор
    зон; дедуп-ключи событий уникальны, replay ничего не дублирует."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_B_HL[:-1] + [(12.5, 8.8)], {18: 8.9},
                      instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 18)

    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    struct = db.list_ltf_structure_events(sc.id)
    assert [(e.kind, e.accompanying) for e in struct] == [
        ("BOS", False), ("SMS", True),
    ]
    for ev in struct:
        _assert_full_event(ev)
    # уведомление одно (bos несёт объединённый payload), sms-события нет
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["bos"]
    keys = [e.dedupe_key for e in evs]
    assert len(keys) == len(set(keys))
    # одно движение; зоны одного поиска (по первичному слому, §8.1)
    assert len(db.list_ltf_movements(sc.id)) == 1
    n_zones = len(db.list_ltf_entry_zones(instrument_id=instrument_id))
    assert n_zones > 0
    n_entries = len(db.list_ltf_scenario_entries(sc.id))
    # replay: ни событий, ни зон, ни привязок не прибавляется
    engine.replay_observation(obs.id)
    assert [e.kind for e in _events(db, obs.id)] == ["bos"]
    assert len(db.list_ltf_entry_zones(instrument_id=instrument_id)) == n_zones
    assert len(db.list_ltf_scenario_entries(sc.id)) == n_entries
    assert len(db.list_ltf_structure_events(sc.id)) == 2


def test_observations_created_only_by_htf_touch(db: Database, cfg,
                                                instrument_id: int):
    """п.7: события структуры H1 (pivots/BOS/SMS/вторичные) новых наблюдений
    не порождают — только касание HTF-зоны (идемпотентное, §4)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    zone = db.get_zone(zid)
    obs = engine.on_htf_zone_touched(instrument_id, zone, T0)
    assert len(db.list_ltf_observations()) == 1
    # повторное HTF-событие по той же зоне/циклу — то же наблюдение
    assert engine.on_htf_zone_touched(instrument_id, zone, T0 + 1).id == obs.id
    assert len(db.list_ltf_observations()) == 1
    # полный прогон со сломами/диапазонами/отменой — наблюдений не прибавилось
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    assert len(db.list_ltf_observations()) == 1
