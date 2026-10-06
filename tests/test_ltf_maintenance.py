"""Автоархивация неактивных наблюдений (closed_stale) и resync pivots
при смене ltf_structure_left/right."""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.structure import sync_structure
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfObservation, LtfScenario, LtfStructureEvent
from tests.test_ltf_engine import SERIES_H_CLOSES, SERIES_H_HL
from tests.test_ltf_breaks import _series

T0 = 1_780_000_000_000
DAY_MS = 86_400_000
NOW = T0 + 200 * DAY_MS


def _zone(db: Database, instrument_id: int) -> int:
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=9.0, upper=10.0,
        formed_at=T0 - 10_000_000, confirmed_at=T0 - 9_000_000,
        status=ZoneStatus.ACTIVE,
    ))


def _obs(db, instrument_id, state, updated_at, activated_at=T0):
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=_zone(db, instrument_id),
        zone_version=1, cycle_id=1, direction=Direction.BEAR, state=state,
        activated_at=activated_at, created_at=activated_at,
        updated_at=updated_at,
    ))


def test_archive_stale_observation(db: Database, cfg, instrument_id: int):
    engine = LtfEngine(db, cfg)  # ltf_observation_stale_days = 14 по умолчанию
    obs = _obs(db, instrument_id, "active", NOW - 20 * DAY_MS)
    assert engine.archive_stale_observations(now_ms=NOW) == [obs.id]
    assert db.get_ltf_observation(obs.id).state == "closed_stale"
    # идемпотентно: повторный проход ничего не трогает
    assert engine.archive_stale_observations(now_ms=NOW) == []


def test_archive_keeps_fresh_observation(db: Database, cfg, instrument_id: int):
    engine = LtfEngine(db, cfg)
    obs = _obs(db, instrument_id, "waiting_structure", NOW - 1 * DAY_MS)
    assert engine.archive_stale_observations(now_ms=NOW) == []
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"


def test_archive_threshold_configurable(db: Database, cfg, instrument_id: int):
    obs = _obs(db, instrument_id, "active", NOW - 20 * DAY_MS)
    cfg.ltf_observation_stale_days = 30
    engine = LtfEngine(db, cfg)
    assert engine.archive_stale_observations(now_ms=NOW) == []
    assert db.get_ltf_observation(obs.id).state == "active"
    # 0 — автоархивация выключена совсем
    cfg.ltf_observation_stale_days = 0
    assert engine.archive_stale_observations(now_ms=NOW) == []
    assert db.get_ltf_observation(obs.id).state == "active"


def test_archive_cancels_active_scenario(db: Database, cfg, instrument_id: int):
    engine = LtfEngine(db, cfg)
    obs = _obs(db, instrument_id, "active", NOW - 20 * DAY_MS)
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=NOW - 30 * DAY_MS, updated_at=NOW - 20 * DAY_MS,
    ))
    assert engine.archive_stale_observations(now_ms=NOW) == [obs.id]
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "cancelled" and sc.cancellation_reason == "stale"
    assert db.get_ltf_observation(obs.id).state == "closed_stale"
    events = db.list_ltf_events(observation_id=obs.id, limit=100)
    assert [(e.kind, e.payload["reason"]) for e in events] == [
        ("cancellation", "stale"),
    ]


def test_archive_respects_recent_structure_events(db: Database, cfg,
                                                  instrument_id: int):
    """updated_at наблюдения старый, но сценарий ловил сломы недавно —
    наблюдение живое, архивировать нельзя."""
    engine = LtfEngine(db, cfg)
    obs = _obs(db, instrument_id, "active", NOW - 20 * DAY_MS)
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=NOW - 30 * DAY_MS, updated_at=NOW - 20 * DAY_MS,
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="secondary",
        direction=Direction.BEAR, break_level=9.0,
        break_candle_open_time=NOW - DAY_MS, occurred_at=NOW - DAY_MS,
        detected_at=NOW - DAY_MS, level_key="bos:secondary:ll:1:9.0",
    ))
    assert engine.archive_stale_observations(now_ms=NOW) == []
    assert db.get_ltf_observation(obs.id).state == "active"


def test_resync_supersedes_pivots_keeping_history(db: Database, cfg,
                                                  instrument_id: int):
    """L03: смена l/r создаёт новую версию расчёта; старые pivots помечаются
    superseded, а не удаляются — исторические ссылки и журнал ролей
    сохраняются."""
    engine = LtfEngine(db, cfg)
    engine.on_htf_zone_touched(instrument_id, db.get_zone(_zone(db, instrument_id)), T0)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    db.insert_candles(candles)
    now = candles[-1].close_time
    sync_structure(db, cfg, instrument_id, candles, now)
    old = db.list_ltf_pivots(instrument_id)
    assert old and all((p.left, p.right) == (3, 3) for p in old)
    old_ids = {p.id for p in old}
    old_keys = {(p.kind, p.pivot_at) for p in old}
    logs_before = {pid: db.list_ltf_pivot_role_log(pid) for pid in old_ids}

    cfg.ltf_structure_left = 5
    cfg.ltf_structure_right = 5
    res = engine.resync_structure_params(now_ms=now)
    new = db.list_ltf_pivots(instrument_id)
    assert res[instrument_id] == len(new)
    assert not old_ids & {p.id for p in new}      # новая версия — новые строки
    assert all((p.left, p.right) == (5, 5) for p in new)
    assert {(p.kind, p.pivot_at) for p in new} != old_keys
    # старые опоры сохранены и разрешимы: читаются по id с пометкой superseded
    kept = db.list_ltf_pivots(instrument_id, include_superseded=True)
    assert {p.id for p in kept} >= old_ids
    for pid in old_ids:
        p = db.get_ltf_pivot(pid)
        assert p is not None and p.superseded_by is not None
    # журнал ролей старых pivots не тронут
    assert all(
        db.list_ltf_pivot_role_log(pid) == logs_before[pid] for pid in old_ids
    )
    # версии расчёта: старая и новая существуют, новые pivots — новой версии
    new_cv = {p.calc_version_id for p in new}
    assert len(new_cv) == 1
    cv = db.get_calc_version(new_cv.pop())
    assert cv["params"] == {"left": 5, "right": 5}
    # повторный resync без смены параметров — no-op
    assert engine.resync_structure_params(now_ms=now) == {}


def test_resync_keeps_old_structure_events_resolvable(db: Database, cfg,
                                                      instrument_id: int):
    """Приёмка L03: смена left/right не ломает просмотр старого BOS/SMS —
    событие и его опоры читаются после перестройки."""
    engine = LtfEngine(db, cfg)
    obs = engine.on_htf_zone_touched(
        instrument_id, db.get_zone(_zone(db, instrument_id)), T0
    )
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    db.insert_candles(candles)
    now = candles[-1].close_time
    sync_structure(db, cfg, instrument_id, candles, now)
    old_pivot = db.list_ltf_pivots(instrument_id)[0]
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=now, updated_at=now,
    ))
    ev = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=old_pivot.price,
        break_candle_open_time=candles[-2].open_time,
        occurred_at=candles[-2].close_time, detected_at=now,
        level_key="bos:primary:test:1", ref_pivot_ids=[old_pivot.id],
    ))

    cfg.ltf_structure_left = 5
    cfg.ltf_structure_right = 5
    engine.resync_structure_params(now_ms=now)

    got = db.list_ltf_structure_events(sc.id)
    assert [e.id for e in got] == [ev.id]
    ref = db.get_ltf_pivot(got[0].ref_pivot_ids[0])
    assert ref is not None and ref.price == old_pivot.price
    assert ref.superseded_by is not None


def test_resync_without_observations_is_noop(db: Database, cfg,
                                             instrument_id: int):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    db.insert_candles(candles)
    sync_structure(db, cfg, instrument_id, candles, candles[-1].close_time)
    before = {(p.kind, p.pivot_at) for p in db.list_ltf_pivots(instrument_id)}
    cfg.ltf_structure_left = 5
    assert engine.resync_structure_params() == {}  # нет наблюдений — не трогаем
    assert {(p.kind, p.pivot_at)
            for p in db.list_ltf_pivots(instrument_id)} == before
