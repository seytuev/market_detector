"""ТЗ 07.10.2026 §13: миграция tools/migrate_taken_levels.py — снятая
ликвидность SSL/BSL не предлагается как зона входа.

Проверки: (а) taken ssl с entry_eligible=1 → 0; (б) scenario_entry к
swept-уровню → invalid/eligible=0, сама зона fresh → tested; (в) pending
delivery на touch по taken-зоне → stale (факт level_taken не трогаем);
(г) повторный запуск ничего не меняет (флаг meta + счётчики нулевые);
(д) активная зона без свечей → evidence needs_recheck; (е) валидные
активные зоны не затронуты. Плюс dry-run без изменений и §13.3 ltf_event.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.models import (
    Candle,
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
    LtfObservation,
    LtfScenario,
    LtfScenarioEntry,
)

from app.services.taken_levels_migration import MIGRATION_KEY, run


def _seed(path: Path) -> dict:
    """Датасет со всеми дефектами §13 + контрольные валидные строки."""
    db = Database(str(path))
    now = now_ms()
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    iid2 = db.upsert_instrument(
        Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT")
    )

    taken_ssl = db.insert_zone(Zone(
        None, iid, ZoneType.SSL, Direction.BULL, "D1",
        lower=2600.0, upper=2600.0, formed_at=now - 10_000,
        confirmed_at=now - 9_000, status=ZoneStatus.TAKEN, created_at=now))
    active_ssl = db.insert_zone(Zone(
        None, iid, ZoneType.BSL, Direction.BEAR, "D1",
        lower=2700.0, upper=2700.0, formed_at=now - 10_000,
        confirmed_at=now - 9_000, status=ZoneStatus.ACTIVE, created_at=now))
    active_ob = db.insert_zone(Zone(
        None, iid, ZoneType.OB, Direction.BULL, "D1",
        lower=2500.0, upper=2550.0, formed_at=now - 10_000,
        confirmed_at=now - 9_000, status=ZoneStatus.ACTIVE, created_at=now))
    # §13.4: активный уровень инструмента без единой свечи
    no_candle_bsl = db.insert_zone(Zone(
        None, iid2, ZoneType.BSL, Direction.BEAR, "D1",
        lower=70000.0, upper=70000.0, formed_at=now - 10_000,
        confirmed_at=now - 9_000, status=ZoneStatus.ACTIVE, created_at=now))
    # свеча покрывает период жизни зон iid (formed_at = now-10_000)
    db.insert_candles([Candle(
        iid, "D1", now - 8_500, now - 8_000,
        2550.0, 2650.0, 2540.0, 2600.0, True, "test")])

    # (в) входовое событие по taken-зоне в очереди + контрольный факт
    touch_eid = db.insert_event(Event(
        None, taken_ssl, 1, EventKind.TOUCH,
        occurred_at=now - 500, detected_at=now - 500, price=2600.0))
    fact_eid = db.insert_event(Event(
        None, taken_ssl, 1, EventKind.LEVEL_TAKEN,
        occurred_at=now - 400, detected_at=now - 400, price=2600.0))
    db.conn.execute(
        "INSERT INTO delivery (event_ids, destination, status, idempotency_key)"
        " VALUES (?, 'telegram', 'pending', 'mig-test-touch')",
        (json.dumps([touch_eid]),))
    db.conn.execute(
        "INSERT INTO delivery (event_ids, destination, status, idempotency_key)"
        " VALUES (?, 'telegram', 'pending', 'mig-test-fact')",
        (json.dumps([fact_eid]),))
    db.conn.commit()

    # (б) активная привязка к уровню с подтверждённым снятием
    obs = db.insert_ltf_observation(LtfObservation(
        None, iid, active_ob, 1, 1, Direction.BULL,
        activated_at=now - 8_000, state="active"))
    sc = db.insert_ltf_scenario(LtfScenario(
        None, obs.id, Direction.BULL, "BOS", "primary",
        state="monitoring_entries", created_at=now, updated_at=now))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        None, iid, "SSL", Direction.BULL, 2400.0, 2400.0, formed_at=now - 5_000))
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        None, ez.id, sc.id, 2400.0, touch_at=now - 1_000,
        candle_open_time=now - 1_000, state="confirmed"))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        None, sc.id, ez.id, 1, eligible=True, overlap="full", state="fresh",
        added_at=now, updated_at=now))

    # §13.3: touch отменённого сценария висит недоставленным
    sc2 = db.insert_ltf_scenario(LtfScenario(
        None, obs.id, Direction.BULL, "BOS", "secondary",
        state="cancelled", created_at=now, updated_at=now))
    db.insert_ltf_event(LtfEvent(
        None, obs.id, "touch", now - 300, now - 300, "mig-test:touch:1",
        scenario_id=sc2.id, payload={"entry_zone_id": ez.id}))

    db.close()
    return {
        "taken_ssl": taken_ssl, "active_ssl": active_ssl, "active_ob": active_ob,
        "no_candle_bsl": no_candle_bsl, "entry_zone": ez.id,
        "scenario_entry_zone": ez.id, "scenario": sc.id,
    }


def _conn(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def test_taken_ssl_loses_entry_eligible(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    rep = run(db_path, backup_dir=tmp_path / "backups")
    assert rep["counters"]["zones_liquidity_blocked"] == 1
    con = _conn(db_path)
    row = con.execute(
        "SELECT entry_eligible, status FROM zone WHERE id=?",
        (ids["taken_ssl"],)).fetchone()
    con.close()
    assert row["entry_eligible"] == 0
    assert row["status"] == "taken"  # статус-история не переписывается


def test_scenario_entry_to_swept_level_invalidated(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    rep = run(db_path, backup_dir=tmp_path / "backups")
    assert rep["counters"]["ltf_entries_invalidated"] == 1
    assert rep["counters"]["ltf_zones_marked_tested"] == 1
    con = _conn(db_path)
    entry = con.execute(
        "SELECT eligible, state, reason FROM ltf_scenario_entry"
        " WHERE scenario_id=? AND entry_zone_id=?",
        (ids["scenario"], ids["entry_zone"])).fetchone()
    zone = con.execute(
        "SELECT validity FROM ltf_entry_zone WHERE id=?",
        (ids["entry_zone"],)).fetchone()
    con.close()
    assert entry["eligible"] == 0
    assert entry["state"] == "invalid"
    assert entry["reason"] == "swept_level_migration"
    # tested — факт истории теста (правило движка), не invalid
    assert zone["validity"] == "tested"


def test_pending_delivery_on_taken_zone_staled(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path)
    rep = run(db_path, backup_dir=tmp_path / "backups")
    assert rep["counters"]["deliveries_staled"] == 1
    assert rep["counters"]["ltf_events_closed"] == 1
    con = _conn(db_path)
    rows = {r["idempotency_key"]: r for r in con.execute(
        "SELECT idempotency_key, status, delivered_at FROM delivery").fetchall()}
    ltf_ev = con.execute(
        "SELECT delivered FROM ltf_event WHERE dedupe_key='mig-test:touch:1'"
    ).fetchone()
    con.close()
    assert rows["mig-test-touch"]["status"] == "stale"
    assert rows["mig-test-touch"]["delivered_at"] is not None
    # факт инвалидации (level_taken) — не входовое событие, очередь не трогаем
    assert rows["mig-test-fact"]["status"] == "pending"
    assert ltf_ev["delivered"] == 1


def test_second_run_is_noop(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    first = run(db_path, backup_dir=tmp_path / "backups")
    assert any(v > 0 for v in first["counters"].values())
    second = run(db_path, backup_dir=tmp_path / "backups")
    assert second["skipped"] is True
    assert all(v == 0 for v in second["counters"].values())
    con = _conn(db_path)
    flag = con.execute(
        "SELECT value FROM meta WHERE key=?", (MIGRATION_KEY,)).fetchone()
    zone = con.execute(
        "SELECT entry_eligible FROM zone WHERE id=?", (ids["taken_ssl"],)).fetchone()
    deliveries = con.execute(
        "SELECT COUNT(*) c FROM delivery WHERE status='stale'").fetchone()
    con.close()
    assert flag is not None
    assert json.loads(flag["value"])["done"] is True
    assert zone["entry_eligible"] == 0
    assert deliveries["c"] == 1  # ни копий, ни повторных изменений


def test_active_zone_without_candles_flagged_needs_recheck(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    rep = run(db_path, backup_dir=tmp_path / "backups")
    assert rep["counters"]["zones_flagged_needs_recheck"] == 1
    con = _conn(db_path)
    row = con.execute(
        "SELECT status, market_validity, entry_eligible, evidence FROM zone"
        " WHERE id=?", (ids["no_candle_bsl"],)).fetchone()
    con.close()
    ev = json.loads(row["evidence"])
    assert ev["needs_recheck"] == 1
    # без данных не инвалидируем и допуск не снимаем
    assert row["status"] == "active"
    assert row["market_validity"] == "active"
    assert row["entry_eligible"] == 1


def test_valid_active_zones_untouched(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    rep = run(db_path, backup_dir=tmp_path / "backups")
    assert rep["counters"]["zones_taken_other_blocked"] == 0
    assert rep["counters"]["mirror_candidates_excluded"] == 0
    con = _conn(db_path)
    rows = {r["id"]: r for r in con.execute(
        "SELECT id, entry_eligible, market_validity, evidence FROM zone"
        " WHERE id IN (?, ?)",
        (ids["active_ob"], ids["active_ssl"])).fetchall()}
    con.close()
    ob, ssl = rows[ids["active_ob"]], rows[ids["active_ssl"]]
    assert ob["entry_eligible"] == 1 and ob["market_validity"] == "active"
    ob_ev = json.loads(ob["evidence"])
    assert "needs_recheck" not in ob_ev and "migration_taken_levels" not in ob_ev
    assert ssl["entry_eligible"] == 1 and ssl["market_validity"] == "active"
    assert "needs_recheck" not in json.loads(ssl["evidence"])


def test_dry_run_counts_without_changes(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _seed(db_path)
    rep = run(db_path, dry_run=True, backup_dir=tmp_path / "backups")
    assert rep["backup"] is None
    assert rep["counters"]["zones_liquidity_blocked"] == 1
    assert rep["counters"]["ltf_entries_invalidated"] == 1
    assert rep["counters"]["deliveries_staled"] == 1
    assert rep["counters"]["zones_flagged_needs_recheck"] == 1
    con = _conn(db_path)
    zone = con.execute(
        "SELECT entry_eligible FROM zone WHERE id=?", (ids["taken_ssl"],)).fetchone()
    flag = con.execute(
        "SELECT 1 FROM meta WHERE key=?", (MIGRATION_KEY,)).fetchone()
    delivery = con.execute(
        "SELECT status FROM delivery WHERE idempotency_key='mig-test-touch'"
    ).fetchone()
    con.close()
    assert zone["entry_eligible"] == 1  # ничего не изменилось
    assert flag is None
    assert delivery["status"] == "pending"
