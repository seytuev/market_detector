"""ТЗ 07.10.2026 §13: одноразовая идемпотентная миграция БД
(сервисный модуль — вызывается из воркера при старте и из CLI-обёртки
tools/migrate_taken_levels.py).

Исходная формулировка — снятая
ликвидность SSL/BSL не должна предлагаться как зона входа.

Действия (каждое — со счётчиком в отчёте):
- §13.1: taken/invalid уровни ssl/bsl (и taken-зоны прочих типов) теряют
  entry_eligible.
- §13.2: «зеркальные» кандидаты ssl/bsl (evidence с признаками создания из
  пересечения другого уровня: ключи mirror/mirrored_from, origin=crossing)
  исключаются: market_validity='invalid', entry_eligible=0, причина — в
  evidence (JSON merge, журнал не удаляется). В текущем коде движка таких
  ключей нет (app/engine/scanner.py, app/engine/liquidity.py) — ожидаемый
  счётчик 0.
- LTF: привязки ltf_scenario_entry к уровням с терминальным тестом
  (confirmed/failed) -> invalid/eligible=0/reason='swept_level_migration';
  ошибочно fresh ltf_entry_zone с завершённым тестом -> validity='tested'
  (правило движка: касание/level_broken — факт истории «tested»).
- §13.3: pending/failed-доставки входовых событий (approach, touch,
  depth_50, depth_90, fvg_weakened) по снятым/невалидным ssl/bsl -> stale;
  недоставленные ltf_event kind='touch' отменённых/закрытых сценариев ->
  delivered=1. Только закрытие очереди, массовых отправок нет.
- §13.4: активные ssl/bsl без свечей инструмента за период жизни зоны ->
  evidence needs_recheck=1 (не инвалидируем без данных).

Идемпотентность (§13.7): флаг meta 'migration:taken_levels_2026_10_07';
все UPDATE дополнительно самоидемпотентны (условия исключают уже
мигрированные строки). Повторный запуск ничего не меняет.

CLI: python tools/migrate_taken_levels.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

MIGRATION_KEY = "migration:taken_levels_2026_10_07"
DEFAULT_DB = "data/htf_zones.db"
DEFAULT_BACKUP_DIR = Path("data/backups")

ENTRY_EVENT_KINDS = ("approach", "touch", "depth_50", "depth_90", "fvg_weakened")
MIRROR_EVIDENCE_KEYS = ("mirror", "mirrored_from")
MIRROR_ORIGIN = "crossing"

COUNTER_KEYS = (
    "zones_liquidity_blocked",       # §13.1: ssl/bsl taken/invalid -> entry_eligible=0
    "zones_taken_other_blocked",     # §13.1: taken прочих типов -> entry_eligible=0
    "mirror_candidates_excluded",    # §13.2
    "ltf_entries_invalidated",       # ltf_scenario_entry -> invalid
    "ltf_zones_marked_tested",       # ltf_entry_zone fresh -> tested
    "deliveries_staled",             # §13.3 delivery -> stale
    "ltf_events_closed",             # §13.3 ltf_event -> delivered=1
    "zones_flagged_needs_recheck",   # §13.4
)

_W_LIQ = ("type IN ('ssl','bsl') AND (status='taken' OR market_validity='invalid')"
          " AND entry_eligible=1")
_W_TAKEN_OTHER = ("status='taken' AND entry_eligible=1"
                  " AND type NOT IN ('ssl','bsl')")
_W_LTF_ENTRIES = ("state != 'invalid' AND entry_zone_id IN"
                  " (SELECT entry_zone_id FROM ltf_liquidity_test"
                  "  WHERE state IN ('confirmed','failed'))"
                  # §13.1: допуск снимается во всех АКТИВНЫХ сценариях;
                  # привязки отменённых/закрытых — история, не трогаем
                  " AND scenario_id IN (SELECT id FROM ltf_scenario"
                  "  WHERE state NOT IN ('cancelled','closed'))")
_W_LTF_ZONES = ("validity='fresh' AND id IN"
                " (SELECT entry_zone_id FROM ltf_liquidity_test"
                "  WHERE state IN ('confirmed','failed'))")
_W_DELIVERY = (
    "status IN ('pending','failed') AND EXISTS ("
    " SELECT 1 FROM json_each(delivery.event_ids) je"
    " JOIN event e ON e.id = je.value"
    " JOIN zone z ON z.id = e.zone_id"
    f" WHERE e.kind IN ({','.join(repr(k) for k in ENTRY_EVENT_KINDS)})"
    "   AND z.type IN ('ssl','bsl')"
    "   AND (z.status='taken' OR z.market_validity='invalid'))"
)
_W_LTF_EVENT = ("delivered=0 AND kind='touch' AND scenario_id IN"
                " (SELECT id FROM ltf_scenario WHERE state IN ('cancelled','closed'))")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _backup(db_path: Path, backup_dir: Path) -> Path:
    """Файловый бэкап через SQLite backup API (copy2 при WAL небезопасен,
    как в tools/restore_unified_zones.py)."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{db_path.name}.bak-taken-levels-{int(time.time())}"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)
    out.close()
    src.close()
    return dst


def _load_evidence(raw: str) -> dict:
    try:
        ev = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return ev if isinstance(ev, dict) else {}


def _is_mirror(ev: dict) -> bool:
    """§13.2: признаки создания уровня из пересечения другого уровня."""
    return any(k in ev for k in MIRROR_EVIDENCE_KEYS) or ev.get("origin") == MIRROR_ORIGIN


def run(db_path: str | Path, dry_run: bool = False,
        backup_dir: str | Path | None = None) -> dict:
    """Выполняет миграцию (или подсчёт при dry_run). Возвращает отчёт:
    {"db", "dry_run", "skipped", "backup", "counters": {...}}."""
    db_path = Path(db_path)
    counters = {k: 0 for k in COUNTER_KEYS}
    report = {"db": str(db_path), "dry_run": dry_run, "skipped": False,
              "backup": None, "counters": counters}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        flag = conn.execute(
            "SELECT value FROM meta WHERE key=?", (MIGRATION_KEY,)
        ).fetchone()
        if flag is not None and not dry_run:
            report["skipped"] = True
            print(f"флаг {MIGRATION_KEY} уже установлен — "
                  f"миграция выполнена ранее, изменений нет")
            return report

        if not dry_run:
            # бэкап — рядом с БД (на Railway БД в /data, а CWD readonly)
            bdir = Path(backup_dir) if backup_dir else db_path.parent / "backups"
            backup = _backup(db_path, bdir)
            report["backup"] = str(backup)
            print(f"бэкап: {backup}")

        now = _now_ms()

        def apply(key: str, count_sql: str, update_sql: str,
                  params: tuple = ()) -> None:
            counters[key] = conn.execute(count_sql).fetchone()[0]
            if not dry_run and counters[key]:
                conn.execute(update_sql, params)

        # §13.1: снятая/невалидная ликвидность не предлагается как вход
        apply("zones_liquidity_blocked",
              f"SELECT COUNT(*) FROM zone WHERE {_W_LIQ}",
              f"UPDATE zone SET entry_eligible=0 WHERE {_W_LIQ}")
        apply("zones_taken_other_blocked",
              f"SELECT COUNT(*) FROM zone WHERE {_W_TAKEN_OTHER}",
              f"UPDATE zone SET entry_eligible=0 WHERE {_W_TAKEN_OTHER}")

        # §13.2: аномальные «зеркальные» кандидаты ssl/bsl
        mirror_rows = []
        for r in conn.execute(
                "SELECT id, market_validity, entry_eligible, evidence FROM zone"
                " WHERE type IN ('ssl','bsl')").fetchall():
            ev = _load_evidence(r["evidence"])
            if not _is_mirror(ev):
                continue
            mark = ev.get("migration_taken_levels") or {}
            already = (r["market_validity"] == "invalid"
                       and r["entry_eligible"] == 0
                       and mark.get("mirror_excluded"))
            if not already:
                mirror_rows.append((r["id"], ev))
        counters["mirror_candidates_excluded"] = len(mirror_rows)
        if not dry_run:
            for zid, ev in mirror_rows:
                ev["migration_taken_levels"] = {
                    "mirror_excluded": True,
                    "reason": "зеркальный кандидат: создан из пересечения "
                              "другого уровня (§13.2)",
                    "at": now,
                }
                conn.execute(
                    "UPDATE zone SET market_validity='invalid', entry_eligible=0,"
                    " evidence=? WHERE id=?",
                    (json.dumps(ev, ensure_ascii=False), zid))

        # LTF: привязки к уровням с терминальным тестом снятия
        apply("ltf_entries_invalidated",
              f"SELECT COUNT(*) FROM ltf_scenario_entry WHERE {_W_LTF_ENTRIES}",
              "UPDATE ltf_scenario_entry SET eligible=0, state='invalid',"
              f" reason='swept_level_migration', updated_at=? WHERE {_W_LTF_ENTRIES}",
              (now,))
        # validity зоны — факт истории: tested (правило движка §9/§10),
        # invalid не выставляем
        apply("ltf_zones_marked_tested",
              f"SELECT COUNT(*) FROM ltf_entry_zone WHERE {_W_LTF_ZONES}",
              f"UPDATE ltf_entry_zone SET validity='tested' WHERE {_W_LTF_ZONES}")

        # §13.3: закрытие очереди доставки по снятым зонам (без отправок)
        apply("deliveries_staled",
              f"SELECT COUNT(*) FROM delivery WHERE {_W_DELIVERY}",
              "UPDATE delivery SET status='stale', delivered_at=?"
              f" WHERE {_W_DELIVERY}",
              (now,))
        apply("ltf_events_closed",
              f"SELECT COUNT(*) FROM ltf_event WHERE {_W_LTF_EVENT}",
              f"UPDATE ltf_event SET delivered=1 WHERE {_W_LTF_EVENT}")

        # §13.4: активные ssl/bsl без свечей за период жизни — needs_recheck
        recheck_rows = []
        for r in conn.execute(
                "SELECT id, instrument_id, formed_at, evidence FROM zone"
                " WHERE type IN ('ssl','bsl') AND status='active'").fetchall():
            has_candles = conn.execute(
                "SELECT 1 FROM candle WHERE instrument_id=? AND open_time>=?"
                " LIMIT 1", (r["instrument_id"], r["formed_at"])).fetchone()
            if has_candles:
                continue
            ev = _load_evidence(r["evidence"])
            if ev.get("needs_recheck"):
                continue
            recheck_rows.append((r["id"], ev))
        counters["zones_flagged_needs_recheck"] = len(recheck_rows)
        if not dry_run:
            for zid, ev in recheck_rows:
                ev["needs_recheck"] = 1
                conn.execute("UPDATE zone SET evidence=? WHERE id=?",
                             (json.dumps(ev, ensure_ascii=False), zid))

        if not dry_run:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (MIGRATION_KEY, json.dumps(
                    {"done": True, "at": now, "counters": counters},
                    ensure_ascii=False)))
            conn.commit()
    finally:
        conn.close()

    _print_report(report)
    return report


def _print_report(report: dict) -> None:
    c = report["counters"]
    mode = ("DRY-RUN (только подсчёты, изменений нет)"
            if report["dry_run"] else "ВЫПОЛНЕНО")
    print(f"миграция taken_levels: {mode}")
    print(f"  §13.1 ssl/bsl taken/invalid -> entry_eligible=0: {c['zones_liquidity_blocked']}")
    print(f"  §13.1 taken прочих типов -> entry_eligible=0:    {c['zones_taken_other_blocked']}")
    if c["mirror_candidates_excluded"]:
        print(f"  §13.2 зеркальные кандидаты исключены:           {c['mirror_candidates_excluded']}")
    else:
        print("  §13.2 зеркальных кандидатов не найдено")
    print(f"  LTF привязки swept-уровней -> invalid:           {c['ltf_entries_invalidated']}")
    print(f"  LTF зоны fresh -> tested (факт теста):           {c['ltf_zones_marked_tested']}")
    print(f"  §13.3 доставки pending/failed -> stale:          {c['deliveries_staled']}")
    print(f"  §13.3 ltf_event touch -> delivered:              {c['ltf_events_closed']}")
    print(f"  §13.4 активные без свечей -> needs_recheck:      {c['zones_flagged_needs_recheck']}")
