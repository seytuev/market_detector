"""Слой данных LTF Confirmations (Этап A): модели, схема, репозитории.

Проверяются создание всех сущностей §12, идемпотентность вставок по
уникальным ключам, миграция существующей файловой БД и to_dict.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import Database
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
    LtfMovement,
    LtfObservation,
    LtfPivot,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
    LtfStructureEvent,
)

T0 = 1_780_000_000_000  # произвольная опора времени, ms UTC


def _make_zone(db: Database, instrument_id: int) -> int:
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    assert zid is not None
    return zid


@pytest.fixture
def observation(db: Database, instrument_id: int) -> LtfObservation:
    zone_id = _make_zone(db, instrument_id)
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, activated_at=T0,
        evidence={"htf_event_id": 42},
    ))


@pytest.fixture
def scenario(db: Database, observation: LtfObservation) -> LtfScenario:
    return db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=observation.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", created_at=T0 + 10, updated_at=T0 + 10,
    ))


# ---------- observations ----------

def test_observation_insert_idempotent(db: Database, instrument_id: int):
    zone_id = _make_zone(db, instrument_id)
    o1 = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, activated_at=T0,
    ))
    assert o1.id is not None
    # повторная вставка (повторное HTF-событие) возвращает существующую строку
    o2 = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, activated_at=T0 + 999,
    ))
    assert o2.id == o1.id
    assert o2.activated_at == T0  # исходные данные не переписаны
    assert len(db.list_ltf_observations()) == 1


def test_observation_update_and_filters(db: Database, observation: LtfObservation):
    db.update_ltf_observation(
        observation.id, state="active", data_quality="stale", updated_at=T0 + 5,
        evidence={"note": "тест"},
    )
    got = db.get_ltf_observation(observation.id)
    assert got is not None
    assert got.state == "active"
    assert got.data_quality == "stale"
    assert got.evidence == {"note": "тест"}
    assert db.get_ltf_observation_by_zone(observation.zone_id, 1).id == observation.id
    assert db.list_ltf_observations(state="active")[0].id == observation.id
    assert db.list_ltf_observations(state="waiting_structure") == []
    assert db.list_ltf_observations(instrument_id=observation.instrument_id)
    assert db.list_ltf_observations(instrument_id=observation.instrument_id + 99) == []


# ---------- scenarios ----------

def test_scenario_lifecycle(db: Database, observation: LtfObservation):
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=observation.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", created_at=T0, updated_at=T0,
    ))
    assert sc.id is not None
    assert db.get_active_ltf_scenario(observation.id).id == sc.id
    db.update_ltf_scenario(sc.id, state="monitoring_entries", updated_at=T0 + 1)
    assert db.get_ltf_scenario(sc.id).state == "monitoring_entries"
    # отмена по обратному слому (§6.5) — сценарий больше не активен
    db.update_ltf_scenario(
        sc.id, state="cancelled", cancellation_reason="reverse_bos",
        cancelled_at=T0 + 2, updated_at=T0 + 2,
    )
    assert db.get_active_ltf_scenario(observation.id) is None
    cancelled = db.list_ltf_scenarios(observation_id=observation.id, state="cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].cancellation_reason == "reverse_bos"
    assert db.list_ltf_scenarios(observation_id=observation.id + 99) == []


# ---------- pivots ----------

def test_pivot_insert_list_and_role_log(db: Database, instrument_id: int):
    pid = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=105.5, kind="high",
        pivot_at=T0, candle_open_time=T0, confirmed_at=T0 + 3 * 3_600_000,
        state="confirmed",
    ))
    pid2 = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=99.0, kind="low",
        pivot_at=T0 + 1000, candle_open_time=T0 + 1000, left=3, right=3,
    ))
    pivots = db.list_ltf_pivots(instrument_id)
    assert [p.id for p in pivots] == [pid, pid2]
    assert db.list_ltf_pivots(instrument_id, since_ms=T0 + 1000)[0].id == pid2
    # §5.2: пересмотр роли с сохранением истории
    db.update_ltf_pivot_role(pid, "HH", changed_at=T0 + 10)
    db.update_ltf_pivot_role(pid, "LH", changed_at=T0 + 20)
    p = db.get_ltf_pivot(pid)
    assert p.role == "LH" and p.role_assigned_at == T0 + 20
    log = db.list_ltf_pivot_role_log(pid)
    assert [(e["old_role"], e["new_role"]) for e in log] == [("none", "HH"), ("HH", "LH")]
    # вызов с той же ролью — no-op: ни UPDATE, ни новой строки лога
    db.update_ltf_pivot_role(pid, "LH", changed_at=T0 + 30)
    assert db.get_ltf_pivot(pid).role_assigned_at == T0 + 20
    assert len(db.list_ltf_pivot_role_log(pid)) == 2
    assert db.list_ltf_pivots(instrument_id + 99) == []


# ---------- structure events ----------

def test_structure_event_idempotent(db: Database, scenario: LtfScenario):
    ev = LtfStructureEvent(
        id=None, scenario_id=scenario.id, kind="BOS", stage="primary",
        direction=Direction.BULL, break_level=110.0,
        break_candle_open_time=T0 + 100, occurred_at=T0 + 100, detected_at=T0 + 200,
        level_key=f"lvl:{110.0}", ref_pivot_ids=[1, 2], evidence={"close": 111.0},
    )
    e1 = db.insert_ltf_structure_event(ev)
    assert e1.id is not None
    e2 = db.insert_ltf_structure_event(LtfStructureEvent(**{**ev.__dict__, "id": None}))
    assert e2.id == e1.id  # §6: один слом на уровень/этап
    assert db.has_ltf_structure_event(scenario.id, ev.level_key, "primary")
    assert not db.has_ltf_structure_event(scenario.id, ev.level_key, "secondary")
    # другой этап того же уровня — отдельное событие (вторичный BOS)
    e3 = db.insert_ltf_structure_event(LtfStructureEvent(
        **{**ev.__dict__, "id": None, "stage": "secondary"}
    ))
    assert e3.id != e1.id
    events = db.list_ltf_structure_events(scenario.id)
    assert len(events) == 2
    assert events[0].ref_pivot_ids == [1, 2]
    assert events[0].accompanying is False


# ---------- movements / ranges ----------

def test_movement_and_range_versions(db: Database, scenario: LtfScenario):
    mid_ = db.insert_ltf_movement(LtfMovement(
        id=None, scenario_id=scenario.id, start_pivot_id=1, end_pivot_id=2,
        start_at=T0, end_at=T0 + 100, break_event_id=7, confirmed_at=T0 + 200,
        source_candle_ids=[T0, T0 + 50, T0 + 100],
    ))
    m = db.get_ltf_movement(mid_)
    assert m is not None
    assert m.source_candle_ids == [T0, T0 + 50, T0 + 100]
    assert m.provenance_status == "ok"
    assert db.list_ltf_movements(scenario.id)[0].id == mid_

    r1 = db.insert_ltf_range(LtfRange(
        id=None, scenario_id=scenario.id, version=1, lower=100.0, upper=110.0,
        mid=105.0, anchor_low_pivot_id=1, anchor_high_pivot_id=2,
        available_at=T0 + 300,
    ))
    r2 = db.insert_ltf_range(LtfRange(
        id=None, scenario_id=scenario.id, version=2, lower=98.0, upper=112.0,
        mid=105.0, available_at=T0 + 400, prev_version_id=r1,
    ))
    cur = db.get_current_ltf_range(scenario.id)
    assert cur is not None and cur.id == r2 and cur.version == 2
    assert [r.version for r in db.list_ltf_ranges(scenario.id)] == [1, 2]
    # §7: версия не переписывается задним числом
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_ltf_range(LtfRange(
            id=None, scenario_id=scenario.id, version=2, lower=1.0, upper=2.0,
            mid=1.5, available_at=T0 + 500,
        ))


# ---------- entry zones ----------

def test_entry_zone_idempotent_and_update(db: Database, instrument_id: int):
    z = LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=98.0, upper=102.0, formed_at=T0, confirmed_at=T0 + 100,
        evidence={"candles": [1, 2, 3]},
    )
    z1 = db.insert_ltf_entry_zone(z)
    assert z1.id is not None
    z2 = db.insert_ltf_entry_zone(LtfEntryZone(**{**z.__dict__, "id": None}))
    assert z2.id == z1.id
    assert len(db.list_ltf_entry_zones(instrument_id=instrument_id)) == 1
    # другой movement_id — другая зона по дедуп-ключу
    z3 = db.insert_ltf_entry_zone(LtfEntryZone(
        **{**z.__dict__, "id": None, "movement_id": 5}
    ))
    assert z3.id != z1.id
    assert [x.id for x in db.list_ltf_entry_zones(movement_id=5)] == [z3.id]
    # первое касание потребляет зону (§9)
    db.update_ltf_entry_zone(z1.id, first_test_at=T0 + 500, validity="tested")
    got = db.get_ltf_entry_zone(z1.id)
    assert got.first_test_at == T0 + 500 and got.validity == "tested"
    assert got.evidence == {"candles": [1, 2, 3]}
    assert got.mid == 100.0 and not got.is_level
    lvl = LtfEntryZone(
        id=None, instrument_id=instrument_id, type="BSL", direction=Direction.BEAR,
        lower=115.0, upper=115.0, formed_at=T0,
    )
    assert lvl.is_level and lvl.mid == 115.0


# ---------- scenario entries ----------

def test_scenario_entry_upsert(db: Database, scenario: LtfScenario, instrument_id: int):
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB", direction=Direction.BULL,
        lower=98.0, upper=102.0, formed_at=T0,
    ))
    se = LtfScenarioEntry(
        id=None, scenario_id=scenario.id, entry_zone_id=ez.id, range_version=1,
        eligible=True, overlap="partial", added_at=T0, updated_at=T0,
    )
    se1 = db.upsert_ltf_scenario_entry(se)
    assert se1.id is not None
    # та же тройка (scenario, zone, range_version) — обновление, не дубликат
    se2 = db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=scenario.id, entry_zone_id=ez.id, range_version=1,
        eligible=False, overlap="none", state="out_of_range",
        added_at=T0 + 1, updated_at=T0 + 1,
    ))
    assert se2.id == se1.id
    assert se2.state == "out_of_range" and se2.eligible is False
    assert len(db.list_ltf_scenario_entries(scenario.id)) == 1
    # новая версия диапазона — новая строка (§7: старая геометрия сохраняется)
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=scenario.id, entry_zone_id=ez.id, range_version=2,
        overlap="full", added_at=T0 + 2, updated_at=T0 + 2,
    ))
    assert len(db.list_ltf_scenario_entries(scenario.id)) == 2
    assert len(db.list_ltf_scenario_entries(scenario.id, state="out_of_range")) == 1
    assert db.get_ltf_scenario_entry(se1.id).overlap == "none"


# ---------- liquidity tests ----------

def test_liquidity_test_lifecycle(db: Database, scenario: LtfScenario, instrument_id: int):
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="SSL", direction=Direction.BULL,
        lower=95.0, upper=95.0, formed_at=T0,
    ))
    tid = db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=ez.id, scenario_id=scenario.id, level=95.0,
        touch_at=T0 + 10, candle_open_time=T0,
    ))
    awaiting = db.list_ltf_liquidity_tests(state="awaiting_close")
    assert [t.id for t in awaiting] == [tid]
    # §10: снятие и возврат закрытия той же свечи
    db.update_ltf_liquidity_test(
        tid, state="confirmed", close_price=96.0, sweep_at=T0 + 50, resolved_at=T0 + 60,
    )
    t = db.list_ltf_liquidity_tests(scenario_id=scenario.id)[0]
    assert t.state == "confirmed" and t.close_price == 96.0 and t.sweep_at == T0 + 50
    assert db.list_ltf_liquidity_tests(state="awaiting_close") == []
    assert db.list_ltf_liquidity_tests(scenario_id=scenario.id + 99) == []


# ---------- events (§11.5) ----------

def test_ltf_event_dedupe_and_delivery(db: Database, observation: LtfObservation,
                                       scenario: LtfScenario):
    ev = LtfEvent(
        id=None, observation_id=observation.id, scenario_id=scenario.id,
        kind="touch", payload={"entry_zone_id": 1, "price": 98.5},
        occurred_at=T0, detected_at=T0 + 1,
        dedupe_key=f"touch:{scenario.id}:1",  # §11.5: без range_version
    )
    e1, created1 = db.insert_ltf_event(ev)
    assert created1 and e1.id is not None
    e2, created2 = db.insert_ltf_event(LtfEvent(**{**ev.__dict__, "id": None}))
    assert not created2 and e2.id == e1.id
    # другой ключ — другое событие
    e3, created3 = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=observation.id, scenario_id=None, kind="note",
        occurred_at=T0 + 1, detected_at=T0 + 1, dedupe_key="note:1", delayed=True,
    ))
    assert created3
    by_obs = db.list_ltf_events(observation_id=observation.id)
    assert len(by_obs) == 2
    by_sc = db.list_ltf_events(scenario_id=scenario.id)
    assert [e.id for e in by_sc] == [e1.id]
    assert by_sc[0].payload == {"entry_zone_id": 1, "price": 98.5}
    db.mark_ltf_event_delivered(e1.id)
    assert db.list_ltf_events(observation_id=observation.id)[-1].delivered is True
    assert e3.delayed is True


# ---------- to_dict ----------

def test_to_dict_json_serializable(db: Database, observation: LtfObservation,
                                   scenario: LtfScenario, instrument_id: int):
    pid = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=105.0, kind="high",
        pivot_at=T0, candle_open_time=T0,
    ))
    pivot = db.get_ltf_pivot(pid)
    se = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=scenario.id, kind="SMS", stage="primary",
        direction=Direction.BULL, break_level=101.0,
        break_candle_open_time=T0, occurred_at=T0, detected_at=T0,
        level_key="lvl:101.0", accompanying=True,
    ))
    mid_ = db.insert_ltf_movement(LtfMovement(
        id=None, scenario_id=scenario.id, start_pivot_id=pid, end_pivot_id=pid,
        start_at=T0, end_at=T0 + 1,
    ))
    rid = db.insert_ltf_range(LtfRange(
        id=None, scenario_id=scenario.id, version=1, lower=100.0, upper=110.0,
        mid=105.0, available_at=T0,
    ))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=98.0, upper=102.0, formed_at=T0, movement_id=mid_,
    ))
    sentry = db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=scenario.id, entry_zone_id=ez.id, range_version=1,
    ))
    tid = db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=ez.id, scenario_id=scenario.id, level=95.0,
        touch_at=T0, candle_open_time=T0,
    ))
    evt, _ = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=observation.id, kind="bos", occurred_at=T0,
        detected_at=T0, dedupe_key="bos:1",
    ))
    objects = [
        observation,
        db.get_ltf_scenario(scenario.id),
        pivot,
        db.list_ltf_structure_events(scenario.id)[0],
        db.get_ltf_movement(mid_),
        db.get_current_ltf_range(scenario.id),
        ez,
        sentry,
        db.list_ltf_liquidity_tests(scenario_id=scenario.id)[0],
        db.list_ltf_events(observation_id=observation.id)[0],
    ]
    for obj in objects:
        d = obj.to_dict()
        assert d["id"] is not None
        json.dumps(d)  # сериализуемо для API
    assert se.accompanying is True
    assert ez.to_dict()["direction"] == "bull"
    assert ez.to_dict()["mid"] == 100.0
    assert rid is not None


# ---------- миграция существующей БД ----------

def test_migrate_existing_db_without_ltf(tmp_path):
    """Старая файловая БД (без ltf_*): Database(path) добавляет LTF-таблицы,
    повторное открытие не падает."""
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """CREATE TABLE schema_version (version INTEGER NOT NULL);
           INSERT INTO schema_version VALUES (1);
           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
           CREATE TABLE zone (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               instrument_id INTEGER NOT NULL, type TEXT NOT NULL,
               direction TEXT NOT NULL, timeframe TEXT NOT NULL,
               lower REAL NOT NULL, upper REAL NOT NULL,
               formed_at INTEGER NOT NULL, confirmed_at INTEGER,
               status TEXT NOT NULL, cycle_id INTEGER NOT NULL DEFAULT 1,
               source TEXT NOT NULL DEFAULT 'auto',
               rule_version TEXT NOT NULL DEFAULT '0.1',
               evidence TEXT NOT NULL DEFAULT '{}',
               created_at INTEGER NOT NULL DEFAULT 0);
           CREATE TABLE visit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               zone_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
               entered_at INTEGER NOT NULL, exited_at INTEGER,
               max_depth REAL NOT NULL DEFAULT 0,
               observed INTEGER NOT NULL DEFAULT 1);"""
    )
    raw.commit()
    raw.close()

    db = Database(path)
    tables = {
        r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for name in (
        "ltf_observation", "ltf_scenario", "ltf_pivot", "ltf_pivot_role_log",
        "ltf_structure_event", "ltf_movement", "ltf_range", "ltf_entry_zone",
        "ltf_scenario_entry", "ltf_liquidity_test", "ltf_event",
    ):
        assert name in tables, name
    db.close()
    # повторное открытие — миграция идемпотентна
    db2 = Database(path)
    db2.close()
