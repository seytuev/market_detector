"""ТЗ «LTF Current Setup» §15: миграция tools/migrate_ltf_current.py.

Синтетическая БД: дубли версий диапазона (в т.ч. неконтiguous), дубли
anchor-зон BSL по pivot_ref (id и pivot_at одного pivot), зона с той же ценой,
но другим pivot (НЕ дубль, §11), reason-бэкфилл по версии строки, перенос
review/assessment/liquidity-test, идемпотентность повторного --apply.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf.eligibility import ELIGIBILITY_REASONS
from app.models import Instrument

from tools.migrate_ltf_current import (
    BACKUP_TABLES,
    MIGRATION_KEY,
    connect,
    run,
)

BASE = 1_780_000_000_000
H1 = 3_600_000


def _q(con, sql, args=()):
    return con.execute(sql, args)


def _build_db(path: Path) -> dict:
    """Минимальный LTF-датасет с дефектами, которые чинит миграция."""
    db = Database(str(path))
    con = db.conn
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    _q(con, "INSERT INTO zone (instrument_id, type, direction, timeframe,"
            " lower, upper, formed_at, status) VALUES (?,?,?,?,?,?,?,?)",
       (iid, "ob", "bear", "D1", 90.0, 210.0, BASE, "active"))
    zone_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    _q(con, "INSERT INTO ltf_observation (instrument_id, zone_id, cycle_id,"
            " direction, activated_at, state) VALUES (?,?,?,?,?,?)",
       (iid, zone_id, 1, "bear", BASE, "active"))
    obs_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    # сценарий 1: активный; сценарий 2: отменённый (контроль §8 без нарушений)
    _q(con, "INSERT INTO ltf_scenario (observation_id, direction, \"trigger\","
            " stage, state) VALUES (?,?,?,?,?)", (obs_id, "bear", "BOS", "primary",
                                                 "monitoring_entries"))
    sc1 = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    _q(con, "INSERT INTO ltf_scenario (observation_id, direction, \"trigger\","
            " stage, state, cancelled_at, cancellation_reason)"
            " VALUES (?,?,?,?,?,?,?)",
       (obs_id, "bear", "BOS", "primary", "cancelled", BASE + 500 * H1,
        "reverse_bos"))
    sc2 = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    # pivots: p1 — опорный high 200 (якорь BSL)
    _q(con, "INSERT INTO ltf_pivot (instrument_id, price, kind, pivot_at,"
            " candle_open_time, confirmed_at, state) VALUES (?,?,?,?,?,?,?)",
       (iid, 200.0, "high", BASE + 10 * H1, BASE + 10 * H1, BASE + 13 * H1,
        "confirmed"))
    p1 = con.execute("SELECT last_insert_rowid()").fetchone()[0]

    # версии диапазона сценария 1: v2 — подряд-дубль v1, v4 — дубль геометрии
    # v1 через другую геометрию (неконтiguous); available_at инвертирован
    ranges = [
        (sc1, 1, 100.0, 200.0, 150.0, BASE + 100 * H1),   # дубль геометрии G1
        (sc1, 2, 100.0, 200.0, 150.0, BASE + 120 * H1),   # дубль G1
        (sc1, 3, 150.0, 200.0, 175.0, BASE + 110 * H1),   # геометрия G2
        (sc1, 4, 100.0, 200.0, 150.0, BASE + 90 * H1),    # дубль G1 с min
                                                          # available_at → keep;
                                                          # перенумерация по исходным
                                                          # номерам: v3→1 (G2), v4→2 (G1)
        (sc2, 1, 50.0, 60.0, 55.0, BASE + 200 * H1),      # < cancelled_at
    ]
    for sid, v, lo, up, mid, av in ranges:
        _q(con, "INSERT INTO ltf_range (scenario_id, version, lower, upper,"
                " mid, available_at) VALUES (?,?,?,?,?,?)",
           (sid, v, lo, up, mid, av))

    # зоны
    def zone(type_, price_lo, price_up, formed, movement_id=0, validity="fresh",
             evidence=None, first_test_at=None, depth=0.0, extreme=None):
        _q(con, "INSERT INTO ltf_entry_zone (instrument_id, type, direction,"
                " lower, upper, formed_at, movement_id, first_test_at,"
                " validity, max_test_depth, test_extreme, evidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
           (iid, type_, "bear", price_lo, price_up, formed, movement_id,
            first_test_at, validity, depth, extreme,
            json.dumps(evidence or {}, ensure_ascii=False)))
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    # z10/z20 — один и тот же pivot p1: pivot_ref как id и как pivot_at
    z10 = zone("BSL", 200.0, 200.0, BASE + 10 * H1, movement_id=4,
               evidence={"range_anchor": True, "pivot_ref": p1})
    z20 = zone("BSL", 200.0, 200.0, BASE + 10 * H1, movement_id=5,
               validity="tested", first_test_at=BASE + 300 * H1, depth=0.3,
               extreme=210.0,
               evidence={"range_anchor": True, "pivot_ref": BASE + 10 * H1})
    # z50 — ТА ЖЕ ЦЕНА 200, но другой pivot (неизвестный ref) — НЕ дубль (§11)
    z50 = zone("BSL", 200.0, 200.0, BASE + 50 * H1,
               evidence={"range_anchor": True, "pivot_ref": 424242})
    z30 = zone("BSL", 120.0, 120.0, BASE + 60 * H1,
               evidence={"range_anchor": True, "pivot_ref": 313131})
    z40 = zone("OB", 190.0, 195.0, BASE + 70 * H1)

    def entry(sid, zid, ver, state="fresh", eligible=1, overlap="none"):
        _q(con, "INSERT INTO ltf_scenario_entry (scenario_id, entry_zone_id,"
                " range_version, eligible, overlap, state, added_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?)",
           (sid, zid, ver, eligible, overlap, state, BASE + 400 * H1,
            BASE + 400 * H1))

    # сценарий 1: 9 привязок → 5 после нормализации
    entry(sc1, z10, 1)   # survive (z10,1)
    entry(sc1, z10, 2)   # коллизия → delete
    entry(sc1, z20, 2)   # z20→z10, коллизия → delete
    entry(sc1, z20, 3)   # survive (z10,2)
    entry(sc1, z10, 3)   # коллизия → delete
    entry(sc1, z30, 4)   # survive (z30,1): версия-дубль → геометрия v1
    entry(sc1, z40, 3)   # survive (z40,2)
    entry(sc1, z40, 0)   # survive (z40,0) — range_pending кандидат
    entry(sc1, z50, 1)   # survive (z50,1)
    # сценарий 2 (отменённый): привязка ДО cancelled_at — нарушения нет
    entry(sc2, z30, 1)

    # связи z20, которые миграция обязана перенести на z10
    _q(con, "INSERT INTO ltf_liquidity_test (entry_zone_id, scenario_id, level,"
            " touch_at, candle_open_time, state, close_price, sweep_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
       (z20, sc1, 200.0, BASE + 310 * H1, BASE + 310 * H1, "confirmed", 199.0,
        BASE + 311 * H1))
    _q(con, "INSERT INTO ltf_review (entry_zone_id, scenario_id, decision,"
            " text) VALUES (?,?,?,?)", (z20, sc1, "correct", "ok"))
    rev_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    _q(con, "INSERT INTO ltf_review_assessment (entry_zone_id, review_id,"
            " review_decision, geometry_verdict) VALUES (?,?,?,?)",
       (z20, rev_id, "correct", "valid"))
    con.commit()
    db.close()
    return {"sc1": sc1, "sc2": sc2, "p1": p1, "z10": z10, "z20": z20,
            "z30": z30, "z40": z40, "z50": z50}


@pytest.fixture
def mig(tmp_path):
    db_path = tmp_path / "t.db"
    ids = _build_db(db_path)
    return {
        "db": db_path, "ids": ids, "cfg": DetectorConfig(),
        "report": tmp_path / "report.json",
        "log": tmp_path / "log.jsonl",
        "backups": tmp_path / "backups",
    }


def _run(mig, apply):
    return run(mig["db"], apply=apply, report_path=mig["report"],
               log_path=mig["log"], backup_dir=mig["backups"], cfg=mig["cfg"])


def test_dry_run_no_writes(mig):
    rep = _run(mig, apply=False)
    t = rep["totals"]
    # v4 (available_at раньше v1) — keep геометрии [100,200]; версий 5 → 3
    assert t["range_versions_before"] == 5
    assert t["range_versions_after"] == 3
    assert t["range_versions_deleted"] == 2
    assert t["entries_before"] == 10
    assert t["entries_after"] == 7
    assert t["entries_deleted"] == 3
    assert t["zones_merged"] == 1 and t["zone_dup_groups"] == 1
    assert rep["mode"] == "dry-run"
    con = connect(mig["db"], readonly=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM ltf_range").fetchone()[0] == 5
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%bak_ltfmig'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM meta WHERE key=?", (MIGRATION_KEY,)
        ).fetchone()[0] == 0
    finally:
        con.close()
    assert mig["report"].exists()


def test_apply_normalizes(mig):
    ids = mig["ids"]
    rep = _run(mig, apply=True)
    assert rep["mode"] == "apply"
    assert Path(rep["backup"]).exists()
    con = connect(mig["db"], readonly=True)
    try:
        sc1 = ids["sc1"]
        # (c) версии: остались 2 уникальные геометрии, перенумерованы 1..2 в
        # порядке исходных номеров (keep G2 — v3, keep G1 — v4), цепочка
        # prev_version_id восстановлена
        rows = con.execute(
            "SELECT id, version, lower, upper, prev_version_id FROM ltf_range"
            " WHERE scenario_id=? ORDER BY version", (sc1,),
        ).fetchall()
        assert [(r["version"], r["lower"], r["upper"]) for r in rows] == [
            (1, 150.0, 200.0), (2, 100.0, 200.0)]
        assert rows[0]["prev_version_id"] is None
        assert rows[1]["prev_version_id"] == rows[0]["id"]
        # keep геометрии [100,200] — версия с min available_at (исходная v4)
        av = con.execute(
            "SELECT available_at FROM ltf_range WHERE scenario_id=? AND version=2",
            (sc1,)).fetchone()[0]
        assert av == BASE + 90 * H1

        # привязки: нет дублей (scenario, zone, version); переносы на месте
        dups = con.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM ltf_scenario_entry"
            " GROUP BY scenario_id, entry_zone_id, range_version"
            " HAVING COUNT(*) > 1)").fetchone()[0]
        assert dups == 0
        got = con.execute(
            "SELECT entry_zone_id, range_version, reason FROM ltf_scenario_entry"
            " WHERE scenario_id=? ORDER BY entry_zone_id, range_version",
            (sc1,)).fetchall()
        expected = sorted([
            (ids["z10"], 1), (ids["z10"], 2),
            (ids["z30"], 2),
            (ids["z40"], 0), (ids["z40"], 1),
            (ids["z50"], 2),
        ])
        assert [(r["entry_zone_id"], r["range_version"]) for r in got] == expected
        # (e) reason-бэкфилл по версии строки: sweep после переноса теста →
        # swept_level у z10; z30 вне Premium [150,200] на v2 → outside_pd;
        # OB z40 в Premium [175,200] на v1 → ok; версия 0 → range_pending
        reasons = {(r["entry_zone_id"], r["range_version"]): r["reason"]
                   for r in got}
        assert reasons[(ids["z10"], 1)] == "swept_level"
        assert reasons[(ids["z10"], 2)] == "swept_level"
        assert reasons[(ids["z30"], 2)] == "outside_pd"
        assert reasons[(ids["z40"], 1)] == "ok"
        assert reasons[(ids["z40"], 0)] == "range_pending"
        assert reasons[(ids["z50"], 2)] == "ok"
        allr = con.execute("SELECT DISTINCT reason FROM ltf_scenario_entry"
                           ).fetchall()
        assert all(r["reason"] in ELIGIBILITY_REASONS for r in allr)

        # (d) дедуп: z20 удалена, z10 и z50 (та же цена, другой pivot) живы;
        # статистика тестов слита в z10; pivot_ref нормализован к id
        assert con.execute("SELECT COUNT(*) FROM ltf_entry_zone WHERE id=?",
                           (ids["z20"],)).fetchone()[0] == 0
        keep = con.execute("SELECT * FROM ltf_entry_zone WHERE id=?",
                           (ids["z10"],)).fetchone()
        assert keep["validity"] == "tested"
        assert keep["first_test_at"] == BASE + 300 * H1
        assert keep["max_test_depth"] == 0.3
        assert keep["test_extreme"] == 210.0
        assert json.loads(keep["evidence"])["pivot_ref"] == ids["p1"]
        assert con.execute("SELECT COUNT(*) FROM ltf_entry_zone WHERE id=?",
                           (ids["z50"],)).fetchone()[0] == 1

        # переносы связей на keep-id
        assert con.execute(
            "SELECT entry_zone_id FROM ltf_liquidity_test").fetchone()[0] == ids["z10"]
        assert con.execute(
            "SELECT entry_zone_id FROM ltf_review").fetchone()[0] == ids["z10"]
        assert con.execute(
            "SELECT entry_zone_id FROM ltf_review_assessment").fetchone()[0] == ids["z10"]

        # (b) бэкапы таблиц с исходными данными
        for t in BACKUP_TABLES:
            assert con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (t + "_bak_ltfmig",)).fetchone()
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_range_bak_ltfmig").fetchone()[0] == 5
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_entry_zone_bak_ltfmig").fetchone()[0] == 5
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_scenario_entry_bak_ltfmig").fetchone()[0] == 10

        # (f)/(g) журнал и meta-ключ
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_migration_log").fetchone()[0] > 0
        assert con.execute("SELECT value FROM meta WHERE key=?",
                           (MIGRATION_KEY,)).fetchone()
        # §8: нарушений отменённых сценариев не зафиксировано
        assert rep["totals"]["post_cancel_late_versions"] == 0
        assert rep["totals"]["post_cancel_late_entries"] == 0
    finally:
        con.close()
    # JSONL-журнал записан
    lines = mig["log"].read_text(encoding="utf-8").strip().splitlines()
    actions = {json.loads(x)["action"] for x in lines}
    assert {"file_backup", "zone_merge", "reason_backfill", "done"} <= actions
    # отчёт «после»: повторный план пуст
    assert rep["post_apply_totals"] == {
        "range_versions_deleted": 0, "entries_deleted": 0,
        "zones_merged": 0, "reason_updates": 0,
    }


def test_apply_idempotent(mig):
    _run(mig, apply=True)
    rep2 = _run(mig, apply=True)
    t = rep2["totals"]
    assert t["range_versions_deleted"] == 0
    assert t["entries_deleted"] == 0
    assert t["zones_merged"] == 0
    assert t["reason_updates"] == 0
    assert rep2["migration_key_present"] is True
    assert "0 изменений" in rep2["note"]
    con = connect(mig["db"], readonly=True)
    try:
        # данные не изменились от повторного прогона
        assert con.execute("SELECT COUNT(*) FROM ltf_range").fetchone()[0] == 3
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_scenario_entry").fetchone()[0] == 7
        assert con.execute("SELECT COUNT(*) FROM ltf_entry_zone").fetchone()[0] == 4
        assert con.execute(
            "SELECT COUNT(*) FROM ltf_review").fetchone()[0] == 1
    finally:
        con.close()


def test_reason_by_row_version_not_head(mig):
    """Без future leakage: строка классифицируется по СВОЕЙ версии диапазона,
    а не по голове. z30 (уровень 120): на v1 [100,200] (Premium [150,200]) —
    outside_pd и на своей версии, и на голове; z40 OB [190,195]: на v2
    [150,200] Premium [175,200] — ok; если бы классифицировали по голове
    отменённого сценария 2 [50,60], результат отличался бы."""
    rep = _run(mig, apply=True)
    sc = next(s for s in rep["scenarios"] if s["scenario_id"] == mig["ids"]["sc1"])
    assert sc["reasons_after"].get("ok") == 2       # z40@v2, z50@v1
    assert sc["reasons_after"].get("swept_level") == 2
    assert sc["reasons_after"].get("outside_pd") == 1
    assert sc["reasons_after"].get("range_pending") == 1
