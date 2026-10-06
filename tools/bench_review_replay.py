"""Замер review-replay (POST /api/zones/{id}/review → replay_instrument) на копии
боевой БД: время прогона + дамп предметного состояния для сравнения до/после.

Примеры:
  .venv/Scripts/python.exe tools/bench_review_replay.py data/diag/bench_before_d1.db \
      --iid 1 --tf D1 --dump data/diag/state_before_d1.json
  .venv/Scripts/python.exe tools/bench_review_replay.py data/diag/bench_before_h1.db \
      --iid 1 --tf H1 --start-ms 1774044000000 --dump data/diag/state_before_h1.json

Сравнение дампов:  python tools/bench_review_replay.py --compare A.json B.json
"""
from __future__ import annotations

import argparse
import json
import time

from app.config import load_detector_config
from app.db import Database
from app.engine.scanner import Scanner

STATE_TABLES = ("zone", "zone_relation", "visit", "event", "inner_level")
# колонки, зависящие от wall-clock прогона (не от рыночных данных)
VOLATILE_COLS = {
    "event": ("detected_at",),
    "zone": ("created_at",),
    "inner_level": ("created_at",),
}


def dump_state(db: Database) -> dict:
    out = {}
    for table in STATE_TABLES:
        cols = [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")]
        drop = set(VOLATILE_COLS.get(table, ()))
        keep = [i for i, c in enumerate(cols) if c not in drop]
        rows = [
            [r[i] for i in keep]
            for r in db.conn.execute(f"SELECT * FROM {table} ORDER BY 1")
        ]
        out[table] = {"cols": [c for c in cols if c not in drop], "rows": rows}
    return out


def compare(path_a: str, path_b: str) -> int:
    a = json.load(open(path_a, encoding="utf-8"))
    b = json.load(open(path_b, encoding="utf-8"))
    rc = 0
    for table in STATE_TABLES:
        ra, rb = a[table]["rows"], b[table]["rows"]
        if ra == rb:
            print(f"{table}: OK ({len(ra)} строк)")
            continue
        rc = 1
        sa = {tuple(r) for r in ra}
        sb = {tuple(r) for r in rb}
        only_a = [r for r in ra if tuple(r) not in sb][:3]
        only_b = [r for r in rb if tuple(r) not in sa][:3]
        print(f"{table}: РАСХОЖДЕНИЕ ({len(ra)} vs {len(rb)} строк)")
        for r in only_a:
            print("  только в A:", r[:6], "...")
        for r in only_b:
            print("  только в B:", r[:6], "...")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?")
    ap.add_argument("--iid", type=int)
    ap.add_argument("--tf")
    ap.add_argument("--start-ms", type=int, default=None)
    ap.add_argument("--dump", default=None)
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = ap.parse_args()

    if args.compare:
        raise SystemExit(compare(*args.compare))

    db = Database(args.db)
    cfg = load_detector_config()
    sc = Scanner(db, cfg)
    n_candles = db.conn.execute(
        "SELECT COUNT(*) FROM candle WHERE instrument_id=? AND timeframe=? AND closed=1",
        (args.iid, args.tf),
    ).fetchone()[0]
    n_before = {
        t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in STATE_TABLES
    }
    t0 = time.time()
    events = sc.replay_instrument(
        args.iid, start_ms=args.start_ms, timeframes={args.tf}
    )
    dt = time.time() - t0
    n_after = {
        t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in STATE_TABLES
    }
    print(json.dumps({
        "db": args.db, "iid": args.iid, "tf": args.tf,
        "start_ms": args.start_ms, "candles_total": n_candles,
        "seconds": round(dt, 2), "events_created": len(events),
        "rows_delta": {t: n_after[t] - n_before[t] for t in STATE_TABLES},
    }, ensure_ascii=False, indent=2))
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(dump_state(db), f)
        print("dump:", args.dump)
    db.close()


if __name__ == "__main__":
    main()
