#!/usr/bin/env python3
"""ТЗ «LTF Current Setup» (2026-09-23) §15: миграция и нормализация данных LTF.

Что делает (Этап 3 ТЗ):
1) --dry-run (по умолчанию): отчёт JSON + stdout — по каждому сценарию версий
   vs уникальных геометрий, зоны по reason до/после, дубли зон, дубли привязок.
2) --apply:
   a. файловый бэкап БД (+ -wal/-shm) в <db_dir>/backups/;
   b. табличные бэкапы внутри БД (<table>_bak_ltfmig), если ещё не созданы;
   c. нормализация версий диапазона: одна версия на уникальную геометрию
      (lower, upper, anchor ids) — первая по времени (min available_at);
      версии перенумеровываются 1..N в исходном порядке, привязки
      ltf_scenario_entry с удалённых версий переносятся на сохранённую версию
      ТОЙ ЖЕ геометрии (классификация привязки остаётся при своей геометрии —
      никакого future leakage; запасной вариант «макс. номер <= исходной» не
      нужен: одна версия каждой геометрии сохраняется всегда);
   d. дедуп зон BSL/SSL по canonical-происхождению — evidence.pivot_ref
      (ТЗ §11: цена — не идентичность; pivot_ref в виде pivot_at нормализуется
      к id pivot). Зоны с одинаковой ценой, но разными pivot НЕ объединяются;
      movement-зоны (OB/FVG) с разными movement_id НЕ объединяются (§11).
      Остаётся min(id); на него переносятся ltf_scenario_entry, ltf_review,
      ltf_review_assessment, ltf_liquidity_test и entry_zone_id в payload
      ltf_event; статистика тестов сливается (max depth, min first_test_at);
   e. бэкфилл reason/state/eligible/overlap всех ltf_scenario_entry через
      evaluate_entry по версии диапазона, на которой стоит строка (не по
      последней — без future leakage);
   f. журнал: таблица ltf_migration_log + JSONL в data/diag;
   g. идемпотентность: meta-ключ ltf:migration:v1:done; все шаги сходятся и
      без него (повторный --apply → «0 изменений»);
   h. только прямые SQL: движок событий/Telegram не вызывается (§15).

Нарушения §8 (версии/привязки отменённых сценариев позже cancelled_at) только
фиксируются в отчёте: миграция не удаляет рыночную историю молча — такие строки
— дефект движка (исправлен в коде), их пересчёт — задача replay с run_id.

Откат: файловый бэкап + таблицы *_bak_ltfmig (не удалять). Пользовательские
отметки (ltf_review/ltf_review_assessment) бэкапятся до изменений и при дедупе
переносятся на сохранённый id зоны — откат из bak не теряет их.

Запуск:
    .venv/Scripts/python.exe tools/migrate_ltf_current.py [--db data/htf_zones.db]
    .venv/Scripts/python.exe tools/migrate_ltf_current.py --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DetectorConfig, load_detector_config, parse_bool
from app.engine.ltf.eligibility import entry_reason, evaluate_entry
from app.engine.ltf.ranges import RangeDraft
from app.models import Direction
from app.models_ltf import LtfEntryZone, LtfLiquidityTest, LtfMovement

MIGRATION_KEY = "ltf:migration:v1:done"
BAK_SUFFIX = "_bak_ltfmig"
# 4 таблицы по ТЗ §15 + таблицы, которые миграция тоже изменяет (откат без
# потери пользовательских отметок и истории тестов)
BACKUP_TABLES = (
    "ltf_entry_zone",
    "ltf_scenario_entry",
    "ltf_range",
    "ltf_pivot",
    "ltf_liquidity_test",
    "ltf_review",
    "ltf_review_assessment",
)

LOG_DDL = """CREATE TABLE IF NOT EXISTS ltf_migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
)"""


# --------------------------------------------------------------------------- #
# подключение и конфигурация
# --------------------------------------------------------------------------- #

def connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
        con.execute("PRAGMA busy_timeout = 30000")
        con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    return con


def load_effective_config(db_path: Path) -> DetectorConfig:
    """Итоговый DetectorConfig как у приложения: ENV, затем data/settings.json
    (app/main.py + app/web/api.py._load_detector_from_file)."""
    cfg = load_detector_config()
    settings_file = Path(db_path).parent / "settings.json"
    if settings_file.exists():
        try:
            payload = json.loads(settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        det = payload.get("detector", payload)
        known = {f.name: type(f.default) for f in fields(cfg)}
        for key, value in det.items():
            if key not in known:
                continue
            try:
                if known[key] is bool:
                    setattr(cfg, key, parse_bool(value))
                else:
                    setattr(cfg, key, known[key](value))
            except (ValueError, TypeError):
                continue
    return cfg


def has_reason_column(con: sqlite3.Connection) -> bool:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(ltf_scenario_entry)")}
    return "reason" in cols


def ensure_schema(con: sqlite3.Connection) -> None:
    """Идемпотентные дополнения схемы (как Database.migrate): колонка reason
    (в живой БД может отсутствовать — процесс не перезапускался) и журнал."""
    if not has_reason_column(con):
        con.execute(
            "ALTER TABLE ltf_scenario_entry ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
        )
    con.execute(LOG_DDL)


# --------------------------------------------------------------------------- #
# план миграции (общий для dry-run и apply; dry-run его не исполняет)
# --------------------------------------------------------------------------- #

def _zone_origin_key(z: sqlite3.Row, evidence: dict, pivot_index: dict) -> tuple:
    """Canonical-происхождение уровня (ТЗ §11): опорный pivot, а не цена.
    pivot_ref до материализации — pivot_at: нормализуем к id pivot, чтобы
    две записи одного экстремума (светка-время и id) слились в одну группу."""
    ref = evidence.get("pivot_ref")
    if ref is not None:
        kind = "high" if z["type"] == "BSL" else "low"
        norm = pivot_index.get((z["instrument_id"], kind, ref), ref)
        return ("pivot", z["instrument_id"], z["type"], z["direction"], norm)
    # fallback: та же свеча-основание и та же цена — один экстремум
    return ("geom", z["instrument_id"], z["type"], z["direction"],
            z["lower"], z["formed_at"])


def _merged_zone_attrs(members: list[sqlite3.Row]) -> dict:
    """Слитая статистика тестов группы дублей (ТЗ §3: максимум за всю историю,
    мелкий поздний тест не стирает более глубокий прежний)."""
    first_tests = [m["first_test_at"] for m in members if m["first_test_at"] is not None]
    if any(m["validity"] == "invalid" for m in members):
        validity = "invalid"
    elif any(m["validity"] == "tested" for m in members):
        validity = "tested"
    else:
        validity = "fresh"
    depth = max((m["max_test_depth"] or 0.0) for m in members)
    extremes = [m["test_extreme"] for m in members if m["test_extreme"] is not None]
    extreme = None
    if extremes:
        bear = members[0]["direction"] == "bear"
        extreme = max(extremes) if bear else min(extremes)
    return {
        "first_test_at": min(first_tests) if first_tests else None,
        "validity": validity,
        "max_test_depth": depth,
        "test_extreme": extreme,
    }


def build_plan(con: sqlite3.Connection, cfg: DetectorConfig) -> dict:
    """Полный план: нормализация версий, дедуп зон, переносы привязок,
    бэкфилл reason. Чистое чтение — dry-run и apply строят один и тот же план."""
    with_reason = has_reason_column(con)
    reason_sel = "reason" if with_reason else "'' AS reason"

    scenarios = {
        r["id"]: r for r in con.execute("SELECT * FROM ltf_scenario ORDER BY id")
    }
    ranges_by_sc: dict[int, list[sqlite3.Row]] = {}
    for r in con.execute("SELECT * FROM ltf_range ORDER BY scenario_id, version"):
        ranges_by_sc.setdefault(r["scenario_id"], []).append(r)
    zones = {
        r["id"]: r for r in con.execute("SELECT * FROM ltf_entry_zone ORDER BY id")
    }
    zone_evidence = {zid: json.loads(z["evidence"] or "{}") for zid, z in zones.items()}
    entries = con.execute(
        f"SELECT id, scenario_id, entry_zone_id, range_version, eligible, overlap,"
        f" state, {reason_sel}, added_at, updated_at FROM ltf_scenario_entry"
        " ORDER BY added_at, id"
    ).fetchall()
    movements: dict[int, list[LtfMovement]] = {}
    for r in con.execute("SELECT * FROM ltf_movement ORDER BY id"):
        movements.setdefault(r["scenario_id"], []).append(LtfMovement(
            id=r["id"], scenario_id=r["scenario_id"],
            start_pivot_id=r["start_pivot_id"], end_pivot_id=r["end_pivot_id"],
            start_at=r["start_at"], end_at=r["end_at"],
            break_event_id=r["break_event_id"], confirmed_at=r["confirmed_at"],
            source_candle_ids=json.loads(r["source_candle_ids"] or "[]"),
            provenance_status=r["provenance_status"],
        ))
    liq_tests: dict[int, list[LtfLiquidityTest]] = {}
    for r in con.execute("SELECT * FROM ltf_liquidity_test ORDER BY id"):
        liq_tests.setdefault(r["scenario_id"], []).append(LtfLiquidityTest(
            id=r["id"], entry_zone_id=r["entry_zone_id"],
            scenario_id=r["scenario_id"], level=r["level"],
            touch_at=r["touch_at"], candle_open_time=r["candle_open_time"],
            state=r["state"], close_price=r["close_price"],
            sweep_at=r["sweep_at"], resolved_at=r["resolved_at"],
        ))
    pivot_index = {
        (r["instrument_id"], r["kind"], r["pivot_at"]): r["id"]
        for r in con.execute("SELECT id, instrument_id, kind, pivot_at FROM ltf_pivot")
    }

    # ---------- (c) нормализация версий диапазона ----------
    version_map: dict[tuple[int, int], int] = {}   # (sid, old_ver) -> new_ver
    range_delete_ids: list[int] = []
    range_updates: list[tuple[int, int, int | None]] = []  # (id, new_ver, new_prev_id)
    range_by_new: dict[tuple[int, int], sqlite3.Row] = {}
    per_scenario: dict[int, dict] = {}

    for sid, rows in ranges_by_sc.items():
        groups: dict[tuple, list[sqlite3.Row]] = {}
        for r in rows:
            g = (r["lower"], r["upper"], r["anchor_low_pivot_id"],
                 r["anchor_high_pivot_id"])
            groups.setdefault(g, []).append(r)
        # одна версия на геометрию — первая по времени подтверждения опор
        kept = [
            min(gr, key=lambda r: (r["available_at"], r["version"]))
            for gr in groups.values()
        ]
        # перенумерация 1..N в исходном порядке версий: движок считает
        # «текущей» max(version) — голову сохраняем той же строкой
        kept.sort(key=lambda r: r["version"])
        new_prev: int | None = None
        for i, k in enumerate(kept, 1):
            range_updates.append((k["id"], i, new_prev))
            range_by_new[(sid, i)] = k
            new_prev = k["id"]
        new_ver_of_id = {k["id"]: i for i, k in enumerate(kept, 1)}
        kept_ids = {k["id"] for k in kept}
        for g, gr in groups.items():
            keep_row = min(gr, key=lambda r: (r["available_at"], r["version"]))
            target = new_ver_of_id[keep_row["id"]]
            for r in gr:
                version_map[(sid, r["version"])] = target
                if r["id"] not in kept_ids:
                    range_delete_ids.append(r["id"])
        per_scenario[sid] = {
            "versions_before": len(rows), "versions_after": len(kept),
        }

    # ---------- (d) дедуп зон по canonical-происхождению ----------
    origin_groups: dict[tuple, list[sqlite3.Row]] = {}
    for z in zones.values():
        if z["type"] not in ("BSL", "SSL"):
            continue  # OB/FVG: разное movement_id — разное происхождение (§11)
        origin_groups.setdefault(
            _zone_origin_key(z, zone_evidence[z["id"]], pivot_index), []
        ).append(z)
    zone_map: dict[int, int] = {}                  # dup_id -> keep_id
    zone_merge_groups: list[dict] = []
    for key, members in origin_groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda m: m["id"])
        keep = members[0]
        attrs = _merged_zone_attrs(members)
        norm_ref = key[4] if key[0] == "pivot" else None
        zone_merge_groups.append({
            "key": list(key), "keep": keep["id"],
            "dropped": [m["id"] for m in members[1:]], "attrs": attrs,
            "norm_pivot_ref": norm_ref,
        })
        for m in members[1:]:
            zone_map[m["id"]] = keep["id"]

    # контроль: точные дубли OB/FVG (UNIQUE-ключ их не допускает — отчёт)
    ob_fvg_exact_dups = con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM ltf_entry_zone"
        " WHERE type IN ('OB','FVG')"
        " GROUP BY instrument_id, type, direction, lower, upper, formed_at,"
        " movement_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]

    # liquidity-тесты дублей после слияния принадлежат keep-зоне: swept_level
    # keep-зоны обязан видеть подтверждённое снятие (п.17 — не воскресает)
    if zone_map:
        for tests in liq_tests.values():
            for t in tests:
                if t.entry_zone_id in zone_map:
                    t.entry_zone_id = zone_map[t.entry_zone_id]

    # ---------- перенос привязок: версия + зона, коллизии ----------
    # выживает самая ранняя строка (min added_at, id) — исходный факт
    # привязки; поздние строки — след баг-ребайндинга уровней на каждой версии
    survive: dict[tuple[int, int, int], dict] = {}
    entry_delete_ids: list[int] = []
    for e in entries:
        zid = zone_map.get(e["entry_zone_id"], e["entry_zone_id"])
        nv = version_map.get((e["scenario_id"], e["range_version"]),
                             e["range_version"])
        key = (e["scenario_id"], zid, nv)
        if key in survive:
            entry_delete_ids.append(e["id"])
            continue
        survive[key] = {
            "id": e["id"], "scenario_id": e["scenario_id"],
            "entry_zone_id": zid, "range_version": nv,
            "cur_zone_id": e["entry_zone_id"], "cur_version": e["range_version"],
            "eligible": e["eligible"], "overlap": e["overlap"],
            "state": e["state"], "reason": e["reason"],
        }

    # эффективные объекты зон после слияния (статистика тестов — слитая)
    zone_eff: dict[int, LtfEntryZone] = {}
    merged_attrs = {g["keep"]: g["attrs"] for g in zone_merge_groups}
    for zid, z in zones.items():
        if zid in zone_map:
            continue
        attrs = merged_attrs.get(zid, {})
        zone_eff[zid] = LtfEntryZone(
            id=zid, instrument_id=z["instrument_id"], type=z["type"],
            direction=Direction(z["direction"]), lower=z["lower"],
            upper=z["upper"], formed_at=z["formed_at"],
            confirmed_at=z["confirmed_at"], movement_id=z["movement_id"],
            first_test_at=attrs.get("first_test_at", z["first_test_at"]),
            validity=attrs.get("validity", z["validity"]),
            max_test_depth=attrs.get("max_test_depth", z["max_test_depth"]),
            test_extreme=attrs.get("test_extreme", z["test_extreme"]),
            source=z["source"], rule_version=z["rule_version"],
            evidence=zone_evidence[zid],
        )

    # ---------- (e) бэкфилл reason по версии строки (без future leakage) ----------
    reason_updates: list[dict] = []
    for key, e in survive.items():
        sc = scenarios.get(e["scenario_id"])
        if sc is None:
            continue
        direction = Direction(sc["direction"])
        rrow = range_by_new.get((e["scenario_id"], e["range_version"]))
        rng = None
        if rrow is not None:
            rng = RangeDraft(
                direction=direction, lower=rrow["lower"], upper=rrow["upper"],
                mid=rrow["mid"], anchor_low_ref=rrow["anchor_low_pivot_id"],
                anchor_high_ref=rrow["anchor_high_pivot_id"],
                available_at=rrow["available_at"],
            )
        zone = zone_eff.get(e["entry_zone_id"])
        if zone is None:
            continue
        ev = evaluate_entry(
            zone, direction, cfg, rng,
            movements=movements.get(e["scenario_id"], []),
            liquidity_tests=liq_tests.get(e["scenario_id"], []),
        )
        new_vals = (ev.reason, ev.state, int(ev.eligible), ev.overlap)
        cur_vals = (e["reason"], e["state"], int(e["eligible"]), e["overlap"])
        e["new"] = new_vals
        if new_vals != cur_vals:
            reason_updates.append({"id": e["id"], "vals": new_vals})

    # ---------- контроль §8: отменённые сценарии (только отчёт) ----------
    late_versions = con.execute(
        "SELECT COUNT(*) FROM ltf_range r JOIN ltf_scenario s ON s.id=r.scenario_id"
        " WHERE s.state='cancelled' AND r.available_at > s.cancelled_at"
    ).fetchone()[0]
    late_entries = con.execute(
        "SELECT COUNT(*) FROM ltf_scenario_entry e JOIN ltf_scenario s"
        " ON s.id=e.scenario_id WHERE s.state='cancelled'"
        " AND (e.added_at > s.cancelled_at OR e.updated_at > s.cancelled_at)"
    ).fetchone()[0]

    # ---------- сводки для отчёта ----------
    entries_before_by_sc: dict[int, int] = {}
    reasons_before_by_sc: dict[int, dict[str, int]] = {}
    anchor_entries_before = 0
    for e in entries:
        sid = e["scenario_id"]
        entries_before_by_sc[sid] = entries_before_by_sc.get(sid, 0) + 1
        rb = entry_reason(SimpleNamespace(reason=e["reason"], state=e["state"]))
        reasons_before_by_sc.setdefault(sid, {})[rb] = (
            reasons_before_by_sc.setdefault(sid, {}).get(rb, 0) + 1
        )
        if zone_evidence.get(e["entry_zone_id"], {}).get("range_anchor"):
            anchor_entries_before += 1
    entries_after_by_sc: dict[int, int] = {}
    reasons_after_by_sc: dict[int, dict[str, int]] = {}
    anchor_entries_after = 0
    for key, e in survive.items():
        sid = e["scenario_id"]
        entries_after_by_sc[sid] = entries_after_by_sc.get(sid, 0) + 1
        ra = e.get("new", (e["reason"],))[0]
        reasons_after_by_sc.setdefault(sid, {})[ra] = (
            reasons_after_by_sc.setdefault(sid, {}).get(ra, 0) + 1
        )
        if zone_evidence.get(e["entry_zone_id"], {}).get("range_anchor"):
            anchor_entries_after += 1

    scenario_ids = sorted(set(per_scenario) | set(entries_before_by_sc))
    for sid in scenario_ids:
        sc = scenarios.get(sid)
        per_scenario.setdefault(sid, {"versions_before": 0, "versions_after": 0})
        per_scenario[sid].update({
            "state": sc["state"] if sc else "?",
            "observation_id": sc["observation_id"] if sc else None,
            "entries_before": entries_before_by_sc.get(sid, 0),
            "entries_after": entries_after_by_sc.get(sid, 0),
            "reasons_before": reasons_before_by_sc.get(sid, {}),
            "reasons_after": reasons_after_by_sc.get(sid, {}),
        })

    return {
        "version_map": version_map,
        "range_updates": range_updates,
        "range_delete_ids": range_delete_ids,
        "zone_map": zone_map,
        "zone_merge_groups": zone_merge_groups,
        "survive": survive,
        "entry_delete_ids": entry_delete_ids,
        "reason_updates": reason_updates,
        "ob_fvg_exact_dups": ob_fvg_exact_dups,
        "post_cancel_late_versions": late_versions,
        "post_cancel_late_entries": late_entries,
        "per_scenario": per_scenario,
        "totals": {
            "range_versions_before": sum(len(v) for v in ranges_by_sc.values()),
            "range_versions_after": len(range_by_new),
            "range_versions_deleted": len(range_delete_ids),
            "entries_before": len(entries),
            "entries_after": len(survive),
            "entries_deleted": len(entry_delete_ids),
            "zones_before": len(zones),
            "zones_after": len(zones) - len(zone_map),
            "zones_merged": len(zone_map),
            "zone_dup_groups": len(zone_merge_groups),
            "reason_updates": len(reason_updates),
            "anchor_entries_before": anchor_entries_before,
            "anchor_entries_after": anchor_entries_after,
            "ob_fvg_exact_dups": ob_fvg_exact_dups,
            "post_cancel_late_versions": late_versions,
            "post_cancel_late_entries": late_entries,
        },
    }


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def file_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"{db_path.stem}_pre_ltf_migration_{ts}.db"
    shutil.copy2(db_path, dst)
    for suf in ("-wal", "-shm"):
        src = Path(str(db_path) + suf)
        if src.exists():
            shutil.copy2(src, Path(str(dst) + suf))
    return dst


class _Logger:
    """Журнал нормализации: таблица ltf_migration_log + JSONL."""

    def __init__(self, run_id: str, log_path: Path | None):
        self.run_id = run_id
        self.log_path = log_path
        self.rows: list[tuple[str, int, str, str]] = []

    def log(self, action: str, detail: dict) -> None:
        ts = int(time.time() * 1000)
        self.rows.append((self.run_id, ts, action,
                          json.dumps(detail, ensure_ascii=False)))

    def flush(self, con: sqlite3.Connection) -> None:
        con.executemany(
            "INSERT INTO ltf_migration_log (run_id, ts, action, detail)"
            " VALUES (?,?,?,?)", self.rows,
        )
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                for run_id, ts, action, detail in self.rows:
                    f.write(json.dumps({
                        "run_id": run_id, "ts": ts, "action": action,
                        "detail": json.loads(detail),
                    }, ensure_ascii=False) + "\n")


def apply_plan(con: sqlite3.Connection, plan: dict, logger: _Logger) -> dict:
    """Исполнение плана в одной транзакции. Только прямые SQL — движок
    событий/Telegram не вызывается (§15: исторические события не рассылаем)."""
    counts = {k: 0 for k in (
        "range_versions_deleted", "entries_remapped", "entries_deleted",
        "zones_merged", "liquidity_tests_repointed", "reviews_repointed",
        "assessments_repointed", "events_repointed", "reason_updates",
    )}

    con.execute("BEGIN IMMEDIATE")
    try:
        ensure_schema(con)
        # (b) табличные бэкапы — идемпотентно
        for t in BACKUP_TABLES:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (t + BAK_SUFFIX,),
            ).fetchone()
            if not exists:
                con.execute(f"CREATE TABLE {t}{BAK_SUFFIX} AS SELECT * FROM {t}")
                n = con.execute(f"SELECT COUNT(*) FROM {t}{BAK_SUFFIX}").fetchone()[0]
                logger.log("table_backup", {"table": t + BAK_SUFFIX, "rows": n})

        # (c) версии: временная отрицательная нумерация → новая 1..N → удалить
        con.execute("UPDATE ltf_range SET version = -version")
        con.executemany(
            "UPDATE ltf_range SET version=?, prev_version_id=? WHERE id=?",
            [(nv, prev, rid) for rid, nv, prev in plan["range_updates"]],
        )
        if plan["range_delete_ids"]:
            con.executemany(
                "DELETE FROM ltf_range WHERE id=?",
                [(i,) for i in plan["range_delete_ids"]],
            )
        counts["range_versions_deleted"] = len(plan["range_delete_ids"])
        for sid, info in sorted(plan["per_scenario"].items()):
            if info["versions_before"] != info["versions_after"]:
                logger.log("range_dedup", {"scenario_id": sid, **{
                    k: info[k] for k in ("versions_before", "versions_after")}})

        # привязки: сначала удалить проигравшие коллизии, затем перенести
        if plan["entry_delete_ids"]:
            con.executemany(
                "DELETE FROM ltf_scenario_entry WHERE id=?",
                [(i,) for i in plan["entry_delete_ids"]],
            )
        counts["entries_deleted"] = len(plan["entry_delete_ids"])
        moved = [
            (e["entry_zone_id"], e["range_version"], e["id"])
            for e in plan["survive"].values()
            if e["entry_zone_id"] != e["cur_zone_id"]
            or e["range_version"] != e["cur_version"]
        ]
        con.executemany(
            "UPDATE ltf_scenario_entry SET entry_zone_id=?, range_version=?"
            " WHERE id=?", moved,
        )
        counts["entries_remapped"] = len(moved)

        # (d) дедуп зон: слить статистику, перенести связи, удалить дубли
        for g in plan["zone_merge_groups"]:
            keep, attrs = g["keep"], g["attrs"]
            evidence = dict(
                json.loads(
                    con.execute("SELECT evidence FROM ltf_entry_zone WHERE id=?",
                                (keep,)).fetchone()[0] or "{}"
                )
            )
            if g["norm_pivot_ref"] is not None:
                evidence["pivot_ref"] = g["norm_pivot_ref"]
            con.execute(
                "UPDATE ltf_entry_zone SET first_test_at=?, validity=?,"
                " max_test_depth=?, test_extreme=?, evidence=? WHERE id=?",
                (attrs["first_test_at"], attrs["validity"],
                 attrs["max_test_depth"], attrs["test_extreme"],
                 json.dumps(evidence, ensure_ascii=False), keep),
            )
            logger.log("zone_merge", {"keep": keep, "dropped": g["dropped"],
                                      "key": g["key"]})
        for dup, keep in plan["zone_map"].items():
            cur = con.execute(
                "UPDATE ltf_liquidity_test SET entry_zone_id=? WHERE entry_zone_id=?",
                (keep, dup),
            )
            counts["liquidity_tests_repointed"] += cur.rowcount
            cur = con.execute(
                "UPDATE ltf_review SET entry_zone_id=? WHERE entry_zone_id=?",
                (keep, dup),
            )
            counts["reviews_repointed"] += cur.rowcount
            cur = con.execute(
                "UPDATE ltf_review_assessment SET entry_zone_id=?"
                " WHERE entry_zone_id=?", (keep, dup),
            )
            counts["assessments_repointed"] += cur.rowcount
            # payload доставленных событий: верхнеуровневый entry_zone_id;
            # вложенные массивы entries[] — история доставки, не трогаем
            cur = con.execute(
                "UPDATE ltf_event SET payload = json_set(payload,"
                " '$.entry_zone_id', ?)"
                " WHERE json_extract(payload, '$.entry_zone_id') = ?",
                (keep, dup),
            )
            counts["events_repointed"] += cur.rowcount
        if plan["zone_map"]:
            con.executemany(
                "DELETE FROM ltf_entry_zone WHERE id=?",
                [(i,) for i in plan["zone_map"]],
            )
        counts["zones_merged"] = len(plan["zone_map"])

        # (e) бэкфилл reason по версии строки
        con.executemany(
            "UPDATE ltf_scenario_entry SET reason=?, state=?, eligible=?,"
            " overlap=? WHERE id=?",
            [(*u["vals"], u["id"]) for u in plan["reason_updates"]],
        )
        counts["reason_updates"] = len(plan["reason_updates"])
        logger.log("reason_backfill", {"rows": counts["reason_updates"]})
        logger.log("post_cancel_check", {
            "late_versions": plan["post_cancel_late_versions"],
            "late_entries": plan["post_cancel_late_entries"],
        })

        # (g) meta-ключ идемпотентности
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (MIGRATION_KEY, json.dumps({
                "run_id": logger.run_id,
                "done_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
                "counts": counts,
            }, ensure_ascii=False)),
        )
        logger.log("done", counts)
        logger.flush(con)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


# --------------------------------------------------------------------------- #
# отчёт и точка входа
# --------------------------------------------------------------------------- #

def compose_report(plan: dict, mode: str, db_path: Path,
                   migration_done: bool, extra: dict | None = None) -> dict:
    scenarios = [
        {"scenario_id": sid, **info}
        for sid, info in sorted(
            plan["per_scenario"].items(),
            key=lambda kv: (-kv[1].get("versions_before", 0), kv[0]),
        )
        if info.get("versions_before") or info.get("entries_before")
    ]
    return {
        "generated_at_utc": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
        "db": str(db_path),
        "mode": mode,
        "migration_key_present": migration_done,
        "totals": plan["totals"],
        "scenarios": scenarios,
        "zone_merge_groups": [
            {"key": g["key"], "keep": g["keep"], "dropped": g["dropped"]}
            for g in plan["zone_merge_groups"]
        ],
        **(extra or {}),
    }


def print_summary(report: dict) -> None:
    t = report["totals"]
    mode = "ПРИМЕНЕНО" if report["mode"] == "apply" else "DRY-RUN (план)"
    print(f"[{mode}] БД: {report['db']}")
    print(f"  версий диапазона: {t['range_versions_before']} ->"
          f" {t['range_versions_after']} (удалено дублей:"
          f" {t['range_versions_deleted']})")
    print(f"  привязок ltf_scenario_entry: {t['entries_before']} ->"
          f" {t['entries_after']} (удалено коллизий: {t['entries_deleted']})")
    print(f"  привязок range_anchor-уровней: {t['anchor_entries_before']} ->"
          f" {t['anchor_entries_after']}")
    print(f"  зон: {t['zones_before']} -> {t['zones_after']} (слито дублей"
          f" BSL/SSL по pivot_ref: {t['zones_merged']} в"
          f" {t['zone_dup_groups']} группах)")
    print(f"  reason бэкфилл/пересчёт: {t['reason_updates']} строк")
    print(f"  §8 (отменённые сценарии, только контроль): поздних версий"
          f" {t['post_cancel_late_versions']}, поздних привязок"
          f" {t['post_cancel_late_entries']}")
    print(f"  точных дублей OB/FVG: {t['ob_fvg_exact_dups']}")
    worst = [s for s in report["scenarios"] if s["versions_before"]
             != s["versions_after"]][:5]
    for s in worst:
        print(f"    сценарий {s['scenario_id']} ({s['state']}): версий"
              f" {s['versions_before']} -> {s['versions_after']}, привязок"
              f" {s['entries_before']} -> {s['entries_after']}")


def run(db_path: Path, apply: bool, report_path: Path | None,
        log_path: Path | None, backup_dir: Path | None,
        cfg: DetectorConfig | None = None) -> dict:
    db_path = Path(db_path)
    cfg = cfg or load_effective_config(db_path)
    con = connect(db_path, readonly=not apply)
    try:
        meta = con.execute(
            "SELECT value FROM meta WHERE key=?", (MIGRATION_KEY,)
        ).fetchone() if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone() else None
        already_done = meta is not None
        plan = build_plan(con, cfg)
        changed = (
            plan["range_delete_ids"] or plan["entry_delete_ids"]
            or plan["zone_map"] or plan["reason_updates"]
            or any(
                e["entry_zone_id"] != e["cur_zone_id"]
                or e["range_version"] != e["cur_version"]
                for e in plan["survive"].values()
            )
        )
        extra: dict = {}
        if not apply:
            report = compose_report(plan, "dry-run", db_path, already_done)
        elif already_done and not changed:
            extra["note"] = ("миграция уже применена (meta"
                             f" {MIGRATION_KEY}); план пуст — 0 изменений")
            report = compose_report(plan, "apply", db_path, already_done, extra)
        else:
            backup = file_backup(db_path, backup_dir
                                 or (db_path.parent / "backups"))
            run_id = f"ltfmig-{time.strftime('%Y%m%d_%H%M%S')}"
            logger = _Logger(run_id, log_path)
            logger.log("file_backup", {"path": str(backup)})
            counts = apply_plan(con, plan, logger)
            extra.update({"backup": str(backup), "run_id": run_id,
                          "applied": counts})
            # отчёт «после»: план по нормализованной БД должен быть пустым
            plan_after = build_plan(con, cfg)
            extra["post_apply_totals"] = {
                k: plan_after["totals"][k] for k in (
                    "range_versions_deleted", "entries_deleted",
                    "zones_merged", "reason_updates")
            }
            report = compose_report(plan, "apply", db_path, already_done, extra)
    finally:
        con.close()
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default="data/htf_zones.db", help="путь к БД")
    p.add_argument("--apply", action="store_true",
                   help="применить миграцию (по умолчанию — dry-run)")
    p.add_argument("--report", default=None,
                   help="путь JSON-отчёта (по умолчанию <db_dir>/diag/"
                        "ltf_migration_report.json)")
    p.add_argument("--log", default=None,
                   help="путь JSONL-журнала (по умолчанию <db_dir>/diag/"
                        "ltf_migration_log.jsonl)")
    p.add_argument("--backup-dir", default=None,
                   help="каталог файловых бэкапов (по умолчанию <db_dir>/backups)")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    diag = db_path.parent / "diag"
    report_path = Path(args.report) if args.report else (
        diag / "ltf_migration_report.json")
    log_path = Path(args.log) if args.log else (diag / "ltf_migration_log.jsonl")
    backup_dir = Path(args.backup_dir) if args.backup_dir else None

    report = run(db_path, apply=args.apply, report_path=report_path,
                 log_path=log_path, backup_dir=backup_dir)
    print_summary(report)
    if report.get("note"):
        print(f"  {report['note']}")
    if report.get("backup"):
        print(f"  файловый бэкап: {report['backup']}")
    print(f"  отчёт: {report_path}")
    return 0


if __name__ == "__main__":
    # Windows-консоль (cp1251): вывод всегда в UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
