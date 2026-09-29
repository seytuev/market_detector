"""Бенчмарк startup-restore LTF: replay открытых наблюдений на консистентной
копии живой БД (SQLite backup API), замер времени и числа SQL-запросов.

Диагностируемая проблема (data/diag/ltf_current_setup_final_report.md §6):
restore после рестарта прогоняет всю H1-историю по каждому наблюдению, и на
каждой свече выполняются одни и те же SELECT (per-candle N+1) — restore идёт
часами.

Режимы:
  --mode off      кэш LTF-чтений выключен — поведение кода до оптимизации
  --mode on       кэш включён (прод-режим)
  --mode compare  оба прогона на двух идентичных копиях + построчное сравнение
                  итогового состояния ltf_* таблиц (доказательство идентичности
                  replay: те же события, версии, привязки, касания)

Ограничители для быстрой оценки (экстраполяция линейна по свечам×replay):
  --obs N           replay только N первых открытых наблюдений (по id)
  --max-candles N   оставить в копии только последние N закрытых H1-свечей
                    на инструмент
  --once-per-instrument   replay один раз на инструмент (как оптимизированный
                    worker._ltf_restore_all: replay_observation зависит только
                    от instrument_id, повторы идемпотентны)

Прочее:
  --source PATH     живая БД (по умолчанию data/htf_zones.db), открывается
                    read-only; живая БД не меняется
  --keep            не удалять рабочие копии после прогона
  --profile         cProfile первого replay (top-30 по cumtime)

Отчёт печатается в stdout и пишется в data/diag/ltf_restore_bench_<ts>.json.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_detector_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.engine.ltf import LtfEngine  # noqa: E402

LTF_OPEN_STATES = ("waiting_structure", "active", "paused_data")

LTF_TABLES = [
    "ltf_observation", "ltf_scenario", "ltf_pivot", "ltf_pivot_role_log",
    "ltf_structure_event", "ltf_movement", "ltf_range", "ltf_entry_zone",
    "ltf_scenario_entry", "ltf_liquidity_test", "ltf_event",
]

_RE_SQL = re.compile(r"^\s*(\w+)\s+(?:OR\s+\w+\s+)?(?:INTO\s+|UPDATE\s+)?"
                     r"(?:FROM\s+)?\"?(\w+)\"?")


class SqlCounter:
    """Счётчик SQL-запросов через set_trace_callback (по операциям/таблицам)."""

    def __init__(self, conn: sqlite3.Connection):
        self.total = 0
        self.by_table: dict[str, int] = {}
        self._conn = conn
        conn.set_trace_callback(self._on_statement)

    def _on_statement(self, sql: str) -> None:
        self.total += 1
        m = _RE_SQL.match(sql)
        key = f"{m.group(1).upper()} {m.group(2)}" if m else sql[:40]
        self.by_table[key] = self.by_table.get(key, 0) + 1

    def close(self) -> None:
        self._conn.set_trace_callback(None)


def copy_live_db(source: str, dst_path: str) -> None:
    """Консистентная копия живой БД через SQLite backup API (source — read-only)."""
    src = sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def truncate_h1(db_path: str, max_candles: int) -> None:
    """Оставить только последние max_candles закрытых H1-свечей на инструмент."""
    conn = sqlite3.connect(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT instrument_id FROM candle WHERE timeframe='H1'"
        )]
        for iid in ids:
            cutoff = conn.execute(
                "SELECT open_time FROM candle WHERE instrument_id=? "
                "AND timeframe='H1' AND closed=1 "
                "ORDER BY open_time DESC LIMIT 1 OFFSET ?",
                (iid, max_candles - 1),
            ).fetchone()
            if cutoff:
                conn.execute(
                    "DELETE FROM candle WHERE instrument_id=? AND timeframe='H1' "
                    "AND open_time<?", (iid, cutoff[0]),
                )
        conn.commit()
    finally:
        conn.close()


def load_live_cfg(source_db: str):
    """DetectorConfig как у живого приложения: ENV-дефолты + data/settings.json."""
    cfg = load_detector_config()
    settings_json = Path(source_db).resolve().parent / "settings.json"
    if settings_json.exists():
        from app.web.api import _apply_detector_payload

        payload = json.loads(settings_json.read_text(encoding="utf-8"))
        _apply_detector_payload(cfg, payload.get("detector", payload))
    return cfg


def open_observations(db: Database) -> list:
    obs = [
        o for o in db.list_ltf_observations()
        if o.state in LTF_OPEN_STATES
    ]
    return sorted(obs, key=lambda o: o.id)


def run_restore(db_path: str, cfg, ltf_cache: bool, obs_limit: int | None,
                once_per_instrument: bool, profile: bool) -> dict:
    """Эмуляция worker._ltf_restore_all (только DB-часть, без догрузки H1)."""
    db = Database(db_path, ltf_cache=ltf_cache)
    engine = LtfEngine(db, cfg, scan_cursors=ltf_cache)
    counter = SqlCounter(db.conn._conn)  # raw sqlite3.Connection внутри обёртки
    try:
        obs_list = open_observations(db)
        if obs_limit is not None:
            obs_list = obs_list[:obs_limit]
        if once_per_instrument:
            seen: set[int] = set()
            obs_list = [o for o in obs_list
                        if not (o.instrument_id in seen or seen.add(o.instrument_id))]
        per_obs: list[dict] = []
        t_all = time.perf_counter()
        for i, obs in enumerate(obs_list):
            t0 = time.perf_counter()
            q0 = counter.total
            if profile and i == 0:
                pr = cProfile.Profile()
                pr.enable()
                res = engine.replay_observation(obs.id)
                pr.disable()
                buf = io.StringIO()
                pstats.Stats(pr, stream=buf).sort_stats("cumulative").print_stats(30)
                print(buf.getvalue())
            else:
                res = engine.replay_observation(obs.id)
            per_obs.append({
                "observation_id": obs.id,
                "instrument_id": obs.instrument_id,
                "candles": res.processed,
                "seconds": round(time.perf_counter() - t0, 3),
                "sql_queries": counter.total - q0,
                "events_new": len(res.events),
                "touches": len(res.touches),
                "ranges_created": len(res.ranges_created),
            })
            print(f"  obs {obs.id}: {res.processed} свечей, "
                  f"{per_obs[-1]['seconds']} c, "
                  f"{per_obs[-1]['sql_queries']} запросов", flush=True)
        total_s = time.perf_counter() - t_all
        return {
            "ltf_cache": ltf_cache,
            "replays": len(per_obs),
            "candles_total": sum(p["candles"] for p in per_obs),
            "seconds_total": round(total_s, 3),
            "sql_total": counter.total,
            "sql_by_table": dict(sorted(counter.by_table.items(),
                                        key=lambda kv: -kv[1])),
            "per_observation": per_obs,
        }
    finally:
        counter.close()
        db.close()


def dump_state(db_path: str) -> dict[str, list[tuple]]:
    """Построчный снимок ltf_* таблиц и ltf-курсоров meta (для diff)."""
    conn = sqlite3.connect(db_path)
    try:
        out: dict[str, list[tuple]] = {}
        for table in LTF_TABLES:
            out[table] = conn.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall()
        out["meta:ltf"] = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'ltf:%' ORDER BY key"
        ).fetchall()
        return out
    finally:
        conn.close()


def diff_states(a: dict, b: dict) -> list[str]:
    diffs: list[str] = []
    for key in a:
        if a[key] == b[key]:
            continue
        only_a = [r for r in a[key] if r not in b[key]]
        only_b = [r for r in b[key] if r not in a[key]]
        diffs.append(f"{key}: {len(a[key])} vs {len(b[key])} строк; "
                     f"только в off: {len(only_a)}, только в on: {len(only_b)}; "
                     f"пример off: {only_a[:1]}; пример on: {only_b[:1]}")
    return diffs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["off", "on", "compare"], default="compare")
    ap.add_argument("--source", default="data/htf_zones.db")
    ap.add_argument("--obs", type=int, default=None)
    ap.add_argument("--max-candles", type=int, default=None)
    ap.add_argument("--once-per-instrument", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    cfg = load_live_cfg(args.source)
    workdir = Path(tempfile.mkdtemp(prefix="ltf_bench_",
                                    dir=Path(args.source).resolve().parent))
    print(f"рабочие копии: {workdir}")
    report: dict = {"mode": args.mode, "source": args.source,
                    "obs_limit": args.obs, "max_candles": args.max_candles,
                    "once_per_instrument": args.once_per_instrument}

    modes = ["off", "on"] if args.mode == "compare" else [args.mode]
    paths: dict[str, str] = {}
    try:
        for mode in modes:
            dst = str(workdir / f"copy_{mode}.db")
            t0 = time.perf_counter()
            copy_live_db(args.source, dst)
            print(f"копия {mode}: backup API за {time.perf_counter() - t0:.1f} c")
            if args.max_candles:
                truncate_h1(dst, args.max_candles)
            paths[mode] = dst
            print(f"--- прогон replay (ltf_cache={'on' if mode == 'on' else 'off'})",
                  flush=True)
            report[f"run_{mode}"] = run_restore(
                dst, cfg, ltf_cache=(mode == "on"),
                obs_limit=args.obs,
                once_per_instrument=args.once_per_instrument,
                profile=args.profile,
            )
        if args.mode == "compare":
            print("--- сравнение итогового состояния ltf_* таблиц")
            diffs = diff_states(dump_state(paths["off"]), dump_state(paths["on"]))
            report["state_diff"] = diffs
            report["state_identical"] = not diffs
            print("идентично" if not diffs else "РАСХОЖДЕНИЯ:\n" + "\n".join(diffs))
        out = Path("data/diag") / f"ltf_restore_bench_{int(time.time())}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"отчёт: {out}")
    finally:
        if not args.keep:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
            print("рабочие копии удалены")


if __name__ == "__main__":
    main()
