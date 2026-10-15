"""Offline current W1 rules replay; writes a report without live feeds."""
from pathlib import Path
import argparse
import json
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alt.engine_v3 import DAY, analyze
from app.alt.store_v3 import persist_result
from app.db import Database


def replay():
    fixtures = Path(__file__).resolve().parents[1]/"tests/fixtures/alt_v2/ohlc"
    report = []
    for ticker in ("NEAR", "LINK", "HBAR", "AAVE", "SUI", "ENA", "PUMP", "TAO", "QNT", "ONDO"):
        rows = json.loads((fixtures/f"{ticker}.json").read_text())
        at = max(r["open_time"] for r in rows)+DAY
        db = Database(":memory:")
        try:
            start = perf_counter()
            result = analyze(rows, as_of_ms=at, computed_at_ms=at)
            persist_result(db, 1, 1, result, backfill=True)
            elapsed = perf_counter()-start
            start = perf_counter()
            persist_result(db, 1, 1, result)
            repeated = perf_counter()-start
            report.append({"asset": ticker, "rules_version": result["rules_version"],
                "as_of_ms": at, "status": result["status"], "bases": len(result["structures"]),
                "replay_store_s": round(elapsed, 3), "repeat_store_s": round(repeated, 3),
                "pending_events": db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0],
                "cycles": [{"lower": b["geometry"]["lower"], "upper": b["geometry"]["upper"],
                            "start": b["geometry"]["base_start_open_time"], "end": b["geometry"]["base_end_open_time"]}
                           for b in result["structures"]]})
        finally:
            db.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/alt_v3_replay_report.json"))
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = replay()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    for r in result:
        print(f"{r['asset']}: {r['status']}, {r['bases']} bases, {r['replay_store_s']:.3f}s, pending={r['pending_events']}")
