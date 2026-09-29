"""Чистка дублей LTF-сценариев после инцидента с параллельными replay.

Два режима (по умолчанию — dry-run, ничего не пишет; --apply применяет):

1. Дедуп (по умолчанию): для каждого наблюдения строятся окна сценариев в
   РЫНОЧНОМ времени [occurred триггера; occurred отмены). Сценарий, чей
   триггер попал внутрь окна более раннего сценария того же направления, —
   дубль (механика инцидента: повторный replay с иными ролями давал другой
   level_key, и дедуп §11.5 его не ловил; в движке это теперь закрыто
   _scenario_windows в engine.py). Каноничным считается самый РАННИЙ сценарий
   окна (по occurred триггера, затем по id) — то же правило, что в коде.

2. --rebuild: полная перестройка истории наблюдений — удаляются ВСЕ сценарии
   и события выбранных наблюдений (+ их зоны/диапазоны/тесты/движения),
   наблюдение возвращается в waiting_structure. Следующий запуск сервера
   (_ltf_restore_all) или engine.replay_observation восстанавливает каноничную
   историю по сохранённым свечам. Рекомендуется, когда пересчёты ролей
   расходились (дедуп оставляет артефакты неканоничного варианта).

Примеры:
    python tools/cleanup_ltf_duplicates.py --db data/htf_zones.db
    python tools/cleanup_ltf_duplicates.py --db copy.db --apply
    python tools/cleanup_ltf_duplicates.py --db copy.db --rebuild --observation-id 1 --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# дети → родители (FK), ltf_event.scenario_id — без FK, тоже чистим
CHILD_TABLES = (
    "ltf_liquidity_test",
    "ltf_scenario_entry",
    "ltf_range",
    "ltf_movement",
    "ltf_structure_event",
    "ltf_event",
)
OPEN_STATES = ("range_pending", "monitoring_entries")


def _ts(ms) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _connect(path: str, write: bool) -> sqlite3.Connection:
    if write:
        con = sqlite3.connect(path)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _scenario_window(con: sqlite3.Connection, sc: sqlite3.Row) -> tuple[int, float]:
    """[occurred триггера; occurred отмены) — как engine._scenario_windows."""
    trig = None
    if sc["trigger_event_id"] is not None:
        trig = con.execute(
            "SELECT occurred_at FROM ltf_structure_event WHERE id=?",
            (sc["trigger_event_id"],),
        ).fetchone()
    start = trig[0] if trig is not None else sc["created_at"]
    end = float("inf")
    if sc["state"] in ("cancelled", "closed"):
        rev = con.execute(
            """SELECT MIN(occurred_at) FROM ltf_structure_event
               WHERE scenario_id=? AND direction != ?""",
            (sc["id"], sc["direction"]),
        ).fetchone()[0]
        end = rev if rev is not None else (sc["cancelled_at"] or float("inf"))
    return start, end


def _trigger_label(con: sqlite3.Connection, sc: sqlite3.Row) -> str:
    if sc["trigger_event_id"] is None:
        return "?"
    r = con.execute(
        "SELECT break_level, break_candle_open_time FROM ltf_structure_event WHERE id=?",
        (sc["trigger_event_id"],),
    ).fetchone()
    if r is None:
        return "?"
    return f"{r[0]} @ {_ts(r[1])}"


def find_duplicates(con: sqlite3.Connection, obs: sqlite3.Row):
    """Каноничные и дубли окнами по рыночному времени."""
    rows = con.execute(
        "SELECT * FROM ltf_scenario WHERE observation_id=? AND direction=? "
        "ORDER BY id", (obs["id"], obs["direction"]),
    ).fetchall()
    kept, dups = [], []
    for sc in rows:
        start, end = _scenario_window(con, sc)
        cover = next((k for k in kept if k[1] <= start < k[2]), None)
        if cover is not None:
            dups.append((sc, start, end, cover))
        else:
            kept.append((sc, start, end))
    return kept, dups


def delete_scenarios(con: sqlite3.Connection, scenario_ids: list[int],
                     instrument_id: int) -> dict[str, int]:
    """FK-безопасное удаление сценариев и осиротевших entry-зон."""
    counts: dict[str, int] = {}
    if not scenario_ids:
        return counts
    q = ",".join("?" * len(scenario_ids))
    zone_candidates = [
        r[0] for r in con.execute(
            f"SELECT DISTINCT entry_zone_id FROM ltf_scenario_entry "
            f"WHERE scenario_id IN ({q}) "
            f"UNION SELECT entry_zone_id FROM ltf_liquidity_test "
            f"WHERE scenario_id IN ({q})", scenario_ids + scenario_ids,
        )
    ]
    for t in CHILD_TABLES:
        n = con.execute(
            f"DELETE FROM {t} WHERE scenario_id IN ({q})", scenario_ids
        ).rowcount
        counts[t] = n
    n = con.execute(
        f"DELETE FROM ltf_scenario WHERE id IN ({q})", scenario_ids
    ).rowcount
    counts["ltf_scenario"] = n
    orphans = [
        z for z in zone_candidates
        if con.execute("SELECT 1 FROM ltf_scenario_entry WHERE entry_zone_id=? "
                       "LIMIT 1", (z,)).fetchone() is None
        and con.execute("SELECT 1 FROM ltf_liquidity_test WHERE entry_zone_id=? "
                        "LIMIT 1", (z,)).fetchone() is None
    ]
    if orphans:
        qz = ",".join("?" * len(orphans))
        counts["ltf_entry_zone"] = con.execute(
            f"DELETE FROM ltf_entry_zone WHERE id IN ({qz})", orphans
        ).rowcount
    return counts


def recompute_observation_state(con: sqlite3.Connection, obs_id: int,
                                now_ms: int) -> str:
    """Состояние наблюдения от оставшихся сценариев (closed_* не трогаем)."""
    obs = con.execute("SELECT * FROM ltf_observation WHERE id=?",
                      (obs_id,)).fetchone()
    if obs["state"] in ("closed_by_parent", "closed_by_user", "closed_stale"):
        return obs["state"]
    active = con.execute(
        "SELECT 1 FROM ltf_scenario WHERE observation_id=? AND state IN "
        "('range_pending','monitoring_entries') LIMIT 1", (obs_id,),
    ).fetchone()
    state = "active" if active else "waiting_structure"
    if state != obs["state"]:
        con.execute("UPDATE ltf_observation SET state=?, updated_at=? WHERE id=?",
                    (state, now_ms, obs_id))
    return state


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--observation-id", type=int, action="append", default=None,
                    help="повторяемый; по умолчанию — все наблюдения")
    ap.add_argument("--rebuild", action="store_true",
                    help="полная перестройка: удалить ВСЕ сценарии/события "
                         "наблюдений (канон восстановит replay)")
    ap.add_argument("--wipe-pivots", action="store_true",
                    help="вместе с --rebuild: удалить ltf_pivot + role_log "
                         "инструмента (только если перестраиваются ВСЕ его "
                         "открытые наблюдения)")
    ap.add_argument("--apply", action="store_true",
                    help="применить (без флага — только отчёт)")
    args = ap.parse_args()

    con = _connect(args.db, write=args.apply)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if args.observation_id:
        observations = [
            con.execute("SELECT * FROM ltf_observation WHERE id=?", (i,)).fetchone()
            for i in args.observation_id
        ]
        observations = [o for o in observations if o is not None]
    else:
        observations = con.execute(
            "SELECT * FROM ltf_observation ORDER BY id").fetchall()
    if not observations:
        sys.exit("наблюдения не найдены")

    mode = "REBUILD" if args.rebuild else "DEDUP"
    print(f"Режим: {mode} | {'APPLY' if args.apply else 'DRY-RUN'} | БД: {args.db}")
    total: dict[str, int] = {}
    for obs in observations:
        print(f"\n=== наблюдение #{obs['id']} ({obs['direction']}, "
              f"зона {obs['zone_id']}, состояние {obs['state']}) ===")
        scenarios = con.execute(
            "SELECT * FROM ltf_scenario WHERE observation_id=? ORDER BY id",
            (obs["id"],)).fetchall()
        if args.rebuild:
            for sc in scenarios:
                print(f"  удалить sc{sc['id']}: {sc['trigger']}/{sc['stage']} "
                      f"{sc['state']} триггер {_trigger_label(con, sc)}")
            if args.apply:
                counts = delete_scenarios(con, [s["id"] for s in scenarios],
                                          obs["instrument_id"])
                # журнал наблюдения в т.ч. безscenario-события (note и т.п.)
                counts["ltf_event(obs)"] = con.execute(
                    "DELETE FROM ltf_event WHERE observation_id=?",
                    (obs["id"],)).rowcount
                state = recompute_observation_state(con, obs["id"], now_ms)
                print(f"  удалено: {counts}; состояние → {state}")
                for t, n in counts.items():
                    total[t] = total.get(t, 0) + n
            else:
                for t in CHILD_TABLES:
                    q = ",".join("?" * len(scenarios)) or "NULL"
                    n = con.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE scenario_id IN ({q})",
                        [s["id"] for s in scenarios]).fetchone()[0] if scenarios else 0
                    total[t] = total.get(t, 0) + n
                    print(f"  {t}: {n}")
                total["ltf_scenario"] = total.get("ltf_scenario", 0) + len(scenarios)
                print(f"  ltf_scenario: {len(scenarios)}")
                n_ev = con.execute(
                    "SELECT COUNT(*) FROM ltf_event WHERE observation_id=?",
                    (obs["id"],)).fetchone()[0]
                total["ltf_event(obs)"] = total.get("ltf_event(obs)", 0) + n_ev
                print(f"  ltf_event (весь журнал наблюдения): {n_ev}")
            continue

        kept, dups = find_duplicates(con, obs)
        for sc, start, end in kept:
            print(f"  keep  sc{sc['id']}: {sc['trigger']} {_trigger_label(con, sc)} "
                  f"окно [{_ts(start)}; {_ts(end) if end != float('inf') else '…'}) "
                  f"({sc['state']})")
        for sc, start, end, cover in dups:
            print(f"  ДУБЛЬ sc{sc['id']}: {sc['trigger']} {_trigger_label(con, sc)} "
                  f"— триггер {_ts(start)} внутри окна sc{cover[0]['id']} "
                  f"[{_ts(cover[1])}; "
                  f"{_ts(cover[2]) if cover[2] != float('inf') else '…'})")
        if dups and args.apply:
            counts = delete_scenarios(con, [s["id"] for s, *_ in dups],
                                      obs["instrument_id"])
            state = recompute_observation_state(con, obs["id"], now_ms)
            print(f"  удалено: {counts}; состояние → {state}")
            for t, n in counts.items():
                total[t] = total.get(t, 0) + n
        elif dups:
            ids = [s["id"] for s, *_ in dups]
            q = ",".join("?" * len(ids))
            for t in CHILD_TABLES:
                n = con.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE scenario_id IN ({q})",
                    ids).fetchone()[0]
                total[t] = total.get(t, 0) + n
                print(f"    удалил бы из {t}: {n}")
            total["ltf_scenario"] = total.get("ltf_scenario", 0) + len(ids)
            print(f"    удалил бы из ltf_scenario: {len(ids)}")

    if args.rebuild and args.wipe_pivots:
        instruments = {o["instrument_id"] for o in observations}
        for iid in instruments:
            open_obs = con.execute(
                "SELECT id FROM ltf_observation WHERE instrument_id=? "
                "AND state IN ('waiting_structure','active','paused_data')",
                (iid,)).fetchall()
            covered = {o["id"] for o in observations}
            if any(o[0] not in covered for o in open_obs):
                print(f"\n!! --wipe-pivots пропущен для инструмента {iid}: "
                      f"не все его открытые наблюдения перестраиваются")
                continue
            n_p = con.execute("SELECT COUNT(*) FROM ltf_pivot WHERE instrument_id=?",
                              (iid,)).fetchone()[0]
            n_l = con.execute(
                "SELECT COUNT(*) FROM ltf_pivot_role_log WHERE pivot_id IN "
                "(SELECT id FROM ltf_pivot WHERE instrument_id=?)", (iid,)).fetchone()[0]
            print(f"\nинструмент {iid}: wipe pivots — ltf_pivot: {n_p}, "
                  f"role_log: {n_l}")
            if args.apply:
                con.execute(
                    "DELETE FROM ltf_pivot_role_log WHERE pivot_id IN "
                    "(SELECT id FROM ltf_pivot WHERE instrument_id=?)", (iid,))
                con.execute("DELETE FROM ltf_pivot WHERE instrument_id=?", (iid,))
                total["ltf_pivot"] = total.get("ltf_pivot", 0) + n_p
                total["ltf_pivot_role_log"] = total.get("ltf_pivot_role_log", 0) + n_l

    print(f"\nИТОГО {'удалено' if args.apply else 'было бы удалено'}: {total or 'ничего'}")
    if not args.apply:
        print("Dry-run: БД не изменена. Повторите с --apply для применения.")
    con.commit() if args.apply else None
    con.close()


if __name__ == "__main__":
    main()
