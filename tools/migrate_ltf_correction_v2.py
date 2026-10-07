#!/usr/bin/env python3
"""§15 (Этап 8): коррекционный пересчёт LTF v2 — причинная цепочка после
Этапов 2–7 (эпохи, origin_reversal-диапазоны, level_broken, fvg_filled,
reverse-break provenance, guard replay по эпохам).

Стратегия (per активное наблюдение):
  1. L03-resync pivots по новой версии правил (RULE_VERSION_LTF, ltf-0.3):
     старые опоры superseded (ссылки истории разрешимы), канон — заново.
  2. Снимок ltf_event наблюдения (для восстановления delivered-меток).
  3. Удаление производного состояния наблюдения (сценарии + дети + события;
     паттерн cleanup_ltf_duplicates --rebuild) — закрытые наблюдения и их
     история НЕ трогаются, pivots не удаляются.
  4. replay_observation — канонический пересчёт по сохранённым свечам тем же
     кодом, что в проде; события восстанавливаются delayed=True (аудит,
     §15.3: ничего не рассылается; pending_ltf_events после миграции = 0).
  5. Восстановление delivered-меток журнала: dedupe_key содержит
     AUTOINCREMENT-id сценариев/зон и при перестройке меняется, поэтому
     строки журнала не сохраняются, а маркеры доставки переносятся на
     пересчитанные события по семантическому ключу (kind, occurred_at,
     стабильные поля payload) — «уже отправленное не удаляется из журнала».
  6. Проверка read-model: per инструмент eligible_count (SQL list_ltf_
     eligible_zones) == числу admitted_scenario_entries активного сценария
     (инвариант Этапа 1), pending_ltf_events == 0.
  7. HTF-родитель: недействительность только ЛОГИРУЕТСЯ — закрытие
     наблюдений по HTF_INVALIDATED выполняет worker.check_parent_validity
     на следующем цикле (app/worker.py), миграция журналирует found-invalid.

Безопасность: --dry-run по умолчанию (ничего не пишет); --apply требует
--live для живой data/htf_zones.db; файловый бэкап в <db_dir>/backups/ и
табличные *_bak_ltfv2 до изменений; журнал ltf_migration_log + JSONL в
data/diag; meta-ключ ltf:migration:v2:done — повторный --apply = «уже
выполнено» (идемпотентность; к тому же весь пересчёт детерминирован и
сходится сам).

Запуск:
  .venv/Scripts/python.exe tools/migrate_ltf_correction_v2.py [--db PATH]
  .venv/Scripts/python.exe tools/migrate_ltf_correction_v2.py --db copy.db --apply
  .venv/Scripts/python.exe tools/migrate_ltf_correction_v2.py --apply --live
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.engine.ltf import LtfEngine  # noqa: E402
from app.engine.ltf.eligibility import admitted_scenario_entries  # noqa: E402
from tools.migrate_ltf_current import (  # noqa: E402
    LOG_DDL,
    _Logger,
    connect,
    file_backup,
    load_effective_config,
)
from tools.cleanup_ltf_duplicates import (  # noqa: E402
    delete_scenarios,
    recompute_observation_state,
)

MIGRATION_KEY = "ltf:migration:v2:done"
BAK_SUFFIX = "_bak_ltfv2"
OPEN_STATES = ("waiting_structure", "active", "paused_data")
BACKUP_TABLES = (
    "ltf_observation", "ltf_scenario", "ltf_structure_event", "ltf_movement",
    "ltf_range", "ltf_entry_zone", "ltf_scenario_entry", "ltf_liquidity_test",
    "ltf_event", "ltf_pivot",
)
LIVE_DB = ROOT / "data" / "htf_zones.db"

# поля payload, стабильные при пересчёте (id-ключи нестабильны: AUTOINCREMENT)
_SEMKEY_FIELDS = (
    "break_level", "level", "type", "lower", "upper", "candle_open_time",
    "reason", "outcome",
)


def _ts(ms) -> str:
    import datetime

    if ms is None:
        return "—"
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime(
        "%Y-%m-%d %H:%M:%S")


def _event_semkey(kind: str, occurred_at: int, payload: dict) -> tuple:
    """Семантический ключ события для переноса delivered-меток: вид + рыночное
    время + стабильные при пересчёте поля (без id сценариев/зон/тестов)."""
    return (kind, occurred_at) + tuple(
        f"{f}={payload[f]}" for f in _SEMKEY_FIELDS if payload.get(f) is not None
    )


# --------------------------------------------------------------------------- #
# план (dry-run и apply): активные наблюдения + дефекты старого состояния
# --------------------------------------------------------------------------- #

def build_plan(con: sqlite3.Connection, observation_ids: list[int] | None) -> dict:
    if observation_ids:
        marks = ",".join("?" * len(observation_ids))
        obs = con.execute(
            f"SELECT * FROM ltf_observation WHERE id IN ({marks}) ORDER BY id",
            observation_ids,
        ).fetchall()
    else:
        obs = con.execute(
            "SELECT * FROM ltf_observation WHERE state IN "
            f"({','.join('?' * len(OPEN_STATES))}) ORDER BY id",
            list(OPEN_STATES),
        ).fetchall()
    instruments = sorted({o["instrument_id"] for o in obs})
    sc_cols = {r["name"] for r in con.execute("PRAGMA table_info(ltf_scenario)")}
    se_cols = {r["name"] for r in con.execute("PRAGMA table_info(ltf_scenario_entry)")}
    has_v2_cols = {"reverse_break_level_price", "structural_epoch_id"} <= sc_cols
    defects: dict[str, int] = {
        "cancelled_without_reverse_provenance": 0,
        "bsl_ssl_failed_but_not_level_broken": 0,
        "fvg_filled_but_not_marked": 0,
        "scenarios_without_epoch": 0,
    }
    if has_v2_cols:
        defects["cancelled_without_reverse_provenance"] = con.execute(
            "SELECT COUNT(*) FROM ltf_scenario WHERE cancellation_reason IN "
            "('reverse_bos','reverse_sms') AND reverse_break_level_price IS NULL"
        ).fetchone()[0]
        defects["scenarios_without_epoch"] = con.execute(
            "SELECT COUNT(*) FROM ltf_scenario WHERE structural_epoch_id IS NULL"
        ).fetchone()[0]
    else:
        # БД до миграции схемы Этапа 2: provenance/эпохи отсутствуют у всех
        n = con.execute(
            "SELECT COUNT(*) FROM ltf_scenario WHERE cancellation_reason IN "
            "('reverse_bos','reverse_sms')"
        ).fetchone()[0]
        defects["cancelled_without_reverse_provenance"] = n
        defects["scenarios_without_epoch"] = con.execute(
            "SELECT COUNT(*) FROM ltf_scenario"
        ).fetchone()[0]
    reason_filter = (
        "se.reason NOT IN ('level_broken','swept_level')"
        if "reason" in se_cols else "1=1"
    )
    defects["bsl_ssl_failed_but_not_level_broken"] = con.execute(
        f"""SELECT COUNT(DISTINCT se.id) FROM ltf_scenario_entry se
           JOIN ltf_entry_zone z ON z.id = se.entry_zone_id
           JOIN ltf_liquidity_test t ON t.entry_zone_id = z.id
           WHERE z.type IN ('BSL','SSL') AND t.state = 'failed'
             AND {reason_filter}"""
    ).fetchone()[0]
    fvg_filter = "se.reason != 'fvg_filled'" if "reason" in se_cols else "1=1"
    defects["fvg_filled_but_not_marked"] = con.execute(
        f"""SELECT COUNT(*) FROM ltf_scenario_entry se
           JOIN ltf_entry_zone z ON z.id = se.entry_zone_id
           WHERE z.type = 'FVG' AND z.max_test_depth >= 1.0
             AND {fvg_filter}"""
    ).fetchone()[0]
    observations = []
    for o in obs:
        sc = con.execute(
            "SELECT * FROM ltf_scenario WHERE observation_id=? ORDER BY id",
            (o["id"],),
        ).fetchall()
        n_events = con.execute(
            "SELECT COUNT(*), SUM(delivered) FROM ltf_event WHERE observation_id=?",
            (o["id"],),
        ).fetchone()
        observations.append({
            "id": o["id"], "instrument_id": o["instrument_id"],
            "zone_id": o["zone_id"], "state": o["state"],
            "direction": o["direction"], "scenarios": len(sc),
            "events": n_events[0], "delivered": n_events[1] or 0,
        })
    parents = []
    # семантика engine.check_parent_validity: converted / archived с
    # инвалидирующим end_reason / market_validity='invalid'
    invalidating = ("breaker_broken", "prb_broken", "jumped_through", "swept")
    for o in obs:
        z = con.execute(
            "SELECT id, status, market_validity, end_reason FROM zone WHERE id=?",
            (o["zone_id"],),
        ).fetchone()
        if z is None:
            continue
        end = z["end_reason"] or ""
        if (z["market_validity"] == "invalid" or z["status"] == "converted"
                or (z["status"] == "archived"
                    and end.startswith(invalidating))):
            parents.append({
                "observation_id": o["id"], "zone_id": z["id"],
                "status": z["status"], "market_validity": z["market_validity"],
                "end_reason": z["end_reason"],
            })
    return {"observations": observations, "instruments": instruments,
            "defects": defects, "invalid_parents": parents}


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def snapshot_events(con: sqlite3.Connection, obs_ids: list[int]) -> list[dict]:
    if not obs_ids:
        return []
    marks = ",".join("?" * len(obs_ids))
    return [
        dict(r) for r in con.execute(
            f"SELECT * FROM ltf_event WHERE observation_id IN ({marks}) "
            "ORDER BY id", obs_ids,
        )
    ]


def restore_delivered(db: Database, old_events: list[dict],
                      obs_ids: list[int]) -> dict:
    """Перенести delivered=True старого журнала на пересчитанные события по
    семантическому ключу (first-wins, детерминировано по id)."""
    old_delivered = {}
    for e in old_events:
        if not e["delivered"]:
            continue
        key = _event_semkey(e["kind"], e["occurred_at"],
                            json.loads(e["payload"] or "{}"))
        old_delivered.setdefault(key, []).append(e["id"])
    matched, used = 0, set()
    marks = ",".join("?" * len(obs_ids)) or "NULL"
    new_events = db.conn.execute(
        f"SELECT * FROM ltf_event WHERE observation_id IN ({marks}) ORDER BY id",
        obs_ids,
    ).fetchall()
    for e in new_events:
        key = _event_semkey(e["kind"], e["occurred_at"],
                            json.loads(e["payload"] or "{}"))
        ids = old_delivered.get(key)
        if not ids:
            continue
        old_id = ids[0]
        if old_id in used:
            continue
        used.add(old_id)
        db.mark_ltf_event_delivered(e["id"])
        matched += 1
    return {"delivered_old": sum(len(v) for v in old_delivered.values()),
            "delivered_restored": matched}


def apply_migration(db_path: Path, plan: dict, logger: _Logger,
                    backup_dir: Path) -> dict:
    backup = file_backup(db_path, backup_dir)
    logger.log("file_backup", {"path": str(backup)})

    db = Database(str(db_path), ltf_cache=True)
    cfg = load_effective_config(db_path)
    engine = LtfEngine(db, cfg, scan_cursors=True)
    con = db.conn
    con.execute(LOG_DDL)
    for t in BACKUP_TABLES:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (t + BAK_SUFFIX,),
        ).fetchone()
        if not exists:
            con.execute(f"CREATE TABLE {t}{BAK_SUFFIX} AS SELECT * FROM {t}")
            logger.log("table_backup", {"table": t + BAK_SUFFIX})

    # (1) L03-resync pivots по новой версии правил (идемпотентно)
    resynced = engine.resync_structure_params()
    logger.log("structure_resync", resynced)

    obs_ids = [o["id"] for o in plan["observations"]]
    by_instrument: dict[int, list[int]] = {}
    for o in plan["observations"]:
        by_instrument.setdefault(o["instrument_id"], []).append(o["id"])

    # (2) снимок журнала → (3) удаление производного состояния → (4) replay
    report_obs: list[dict] = []
    now = int(time.time() * 1000)
    for iid in sorted(by_instrument):
        ids = by_instrument[iid]
        old_events = snapshot_events(con, ids)
        scenario_ids = [
            r[0] for r in con.execute(
                "SELECT id FROM ltf_scenario WHERE observation_id IN "
                f"({','.join('?' * len(ids))})", ids,
            )
        ]
        counts = delete_scenarios(con, scenario_ids, iid)
        counts["ltf_event(obs)"] = con.execute(
            f"DELETE FROM ltf_event WHERE observation_id IN "
            f"({','.join('?' * len(ids))})", ids,
        ).rowcount
        states = [recompute_observation_state(con, oid, now) for oid in ids]
        logger.log("rebuild_deleted", {"instrument_id": iid,
                                       "observations": ids, **counts})
        seen: set[int] = set()
        for oid in ids:
            if oid in seen:
                continue
            seen.add(oid)
            engine.replay_observation(oid)
        delivery = restore_delivered(db, old_events, ids)
        for oid, st in zip(ids, states):
            scs = db.list_ltf_scenarios(observation_id=oid)
            cancelled = [s for s in scs if s.cancelled_at is not None]
            ranges = [r for s in scs for r in db.list_ltf_ranges(s.id)]
            tests = [t for s in scs
                     for t in db.list_ltf_liquidity_tests(scenario_id=s.id)]
            zones = {
                e.entry_zone_id
                for s in scs for e in db.list_ltf_scenario_entries(s.id)
            }
            filled = 0
            for zid in zones:
                z = db.get_ltf_entry_zone(zid)
                if z is not None and z.type == "FVG" and z.max_test_depth >= 1.0:
                    filled += 1
            info = {
                "observation_id": oid, "state_before": st,
                "scenarios": len(scs), "cancelled": len(cancelled),
                "ranges": len(ranges),
                "origin_reversal_ranges": sum(
                    1 for r in ranges if r.kind == "origin_reversal"),
                "levels_terminal": sum(
                    1 for t in tests if t.state in ("confirmed", "failed")),
                "fvg_filled": filled, **delivery,
            }
            report_obs.append(info)
            logger.log("rebuild_observation", info)

    # (6) проверка read-model: SQL-счётчик == admitted (инвариант Этапа 1)
    verification = []
    eligible_sql: dict[int, int] = {}
    for row in db.list_ltf_eligible_zones():
        eligible_sql[row["instrument_id"]] = (
            eligible_sql.get(row["instrument_id"], 0) + 1)
    for iid in sorted(by_instrument):
        active = [
            s for oid in by_instrument[iid]
            for s in db.list_ltf_scenarios(observation_id=oid)
            if s.state in ("range_pending", "monitoring_entries")
        ]
        admitted = sum(len(admitted_scenario_entries(db, s.id))
                       for s in active)
        sql_count = eligible_sql.get(iid, 0)
        verification.append({
            "instrument_id": iid, "eligible_sql": sql_count,
            "eligible_admitted": admitted,
            "consistent": sql_count == admitted,
        })
        logger.log("verify_snapshot", verification[-1])
    pending = len(db.pending_ltf_events())
    logger.log("verify_pending", {"pending_ltf_events": pending})

    db.set_meta(MIGRATION_KEY, str(now))
    logger.log("done", {"key": MIGRATION_KEY, "ts": now})
    db.close()
    return {"observations": report_obs, "verification": verification,
            "pending_ltf_events": pending, "backup": str(backup)}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(LIVE_DB))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="обязателен для --apply по живой БД")
    ap.add_argument("--observation-id", type=int, action="append", default=None)
    ap.add_argument("--backup-dir", default=None)
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        sys.exit(f"БД не найдена: {db_path}")
    if args.apply and db_path == LIVE_DB.resolve() and not args.live:
        sys.exit("--apply по живой БД требует --live (или укажите копию --db)")

    run_id = time.strftime("ltfv2_%Y%m%d_%H%M%S")
    log_jsonl = ROOT / "data" / "diag" / f"migration_ltf_v2_{run_id}.jsonl"
    logger = _Logger(run_id, log_jsonl)

    con = connect(db_path, readonly=not args.apply)
    con.execute(LOG_DDL) if args.apply else None
    done = con.execute(
        "SELECT value FROM meta WHERE key=?", (MIGRATION_KEY,)
    ).fetchone()
    if done and args.apply:
        print(f"миграция уже выполнена (meta {MIGRATION_KEY}={done[0]}) — "
              "0 изменений")
        con.close()
        return
    plan = build_plan(con, args.observation_id)
    if not args.apply:
        con.close()

    print(f"Режим: {'APPLY' if args.apply else 'DRY-RUN'} | БД: {db_path}")
    print(f"Активных наблюдений: {len(plan['observations'])} "
          f"(инструменты: {plan['instruments']})")
    print(f"Дефекты старого состояния (исправит пересчёт): "
          f"{json.dumps(plan['defects'], ensure_ascii=False)}")
    if plan["invalid_parents"]:
        print(f"HTF-родители недействительны (закроет worker, миграция только "
              f"логирует): {json.dumps(plan['invalid_parents'], ensure_ascii=False)}")
    for o in plan["observations"]:
        print(f"  obs #{o['id']} ({o['direction']}, {o['state']}): "
              f"сценариев {o['scenarios']}, событий {o['events']} "
              f"(delivered {o['delivered']})")
    if not plan["observations"]:
        print("Активных наблюдений нет — пересчитывать нечего.")
        return
    if not args.apply:
        print("\nDRY-RUN: изменений нет. Для пересчёта: --apply "
              "(по копии) или --apply --live (по живой).")
        return

    backup_dir = Path(args.backup_dir) if args.backup_dir \
        else db_path.parent / "backups"
    result = apply_migration(db_path, plan, logger, backup_dir)
    # журнал — в ту же БД после миграции (лог не затрагивает рыночные данные)
    con2 = connect(db_path)
    con2.execute(LOG_DDL)
    logger.flush(con2)
    con2.close()
    print("\n=== Итог пересчёта ===")
    for o in result["observations"]:
        print(json.dumps(o, ensure_ascii=False))
    print("Проверка read-model:", json.dumps(result["verification"],
                                               ensure_ascii=False))
    print(f"pending_ltf_events после миграции: {result['pending_ltf_events']}")
    print(f"Бэкап: {result['backup']}; журнал: {log_jsonl}")


if __name__ == "__main__":
    main()
