"""Read-only incident snapshot: SOL HTF waiting and H1 structure.

Reads SQLite through mode=ro and optionally queries the local HTTP API.
Never writes application settings, candles, observations or reviews.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    con = sqlite3.connect(f"file:{(ROOT / 'data/htf_zones.db').as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")

    def rows(sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in con.execute(sql, args)]

    ins = rows("SELECT * FROM instrument WHERE symbol='SOLUSDT' AND venue='binance' AND market_type='spot'")[0]
    iid = ins["id"]
    args = (iid,)
    report = {
        "captured_at_msk": dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(),
        "instrument": ins,
        "detector_settings": json.loads((ROOT / "data/settings.json").read_text(encoding="utf-8"))["detector"],
        "candles": rows("SELECT timeframe, closed, count(*) AS count, min(open_time) AS first_open, max(close_time) AS last_close FROM candle WHERE instrument_id=? GROUP BY timeframe,closed", args),
        "last_h1": rows("SELECT * FROM candle WHERE instrument_id=? AND timeframe='H1' ORDER BY open_time DESC LIMIT 3", args),
        "last_d1": rows("SELECT * FROM candle WHERE instrument_id=? AND timeframe='D1' ORDER BY open_time DESC LIMIT 2", args),
        "zone_counts": rows("SELECT type,timeframe,status,market_validity,count(*) AS count FROM zone WHERE instrument_id=? GROUP BY type,timeframe,status,market_validity", args),
        "unreviewed_candidates": rows("SELECT z.id,z.type,z.timeframe,z.lower,z.upper,z.formed_at,z.confirmed_at,z.status FROM zone z WHERE z.instrument_id=? AND z.status='candidate' AND z.market_validity='active' AND z.display_until IS NULL AND NOT EXISTS(SELECT 1 FROM review r WHERE r.zone_id=z.id)", args),
        "active_parents": rows("SELECT id,type,timeframe,direction,lower,upper,formed_at,confirmed_at,status,market_validity,display_until,entry_eligible,has_tests,max_test_depth FROM zone WHERE instrument_id=? AND type IN ('ob','fvg') AND status IN ('active','weakened')", args),
        "observations": rows("SELECT * FROM ltf_observation WHERE instrument_id=?", args),
        "pivot_counts": rows("SELECT state,kind,role,count(*) AS count,max(pivot_at) AS last_pivot,max(confirmed_at) AS last_confirmed FROM ltf_pivot WHERE instrument_id=? GROUP BY state,kind,role", args),
        "scenarios": rows("SELECT s.* FROM ltf_scenario s JOIN ltf_observation o ON o.id=s.observation_id WHERE o.instrument_id=?", args),
        "recent_liquidity_events": rows("SELECT e.*,z.type,z.timeframe,z.lower,z.upper FROM event e JOIN zone z ON z.id=e.zone_id WHERE z.instrument_id=? AND z.type IN ('ssl','bsl') ORDER BY e.occurred_at DESC LIMIT 12", args),
        "near_ssl": rows("SELECT id,timeframe,lower,upper,status,market_validity,formed_at,confirmed_at,display_until,end_reason,evidence FROM zone WHERE instrument_id=? AND type='ssl' AND lower BETWEEN 113 AND 119 ORDER BY formed_at DESC", args),
        "meta": rows("SELECT key,value FROM meta WHERE key IN (?,?,?)", (f"ltf:h1:last_close:{iid}",f"replaying:{iid}",f"stale:{iid}")),
    }
    con.rollback()
    con.close()
    token = "dev-token"
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("HTF_AUTH_TOKEN="):
                token = line.partition("=")[2].strip().strip("\"'")
    report["runtime"] = {}
    with httpx.Client(timeout=20, headers={"Authorization": f"Bearer {token}"}) as client:
        for name, endpoint in {
            "current": f"/api/ltf/instruments/{iid}/current",
            "overview": "/api/ltf/instruments",
            "h1_candles": f"/api/candles?instrument_id={iid}&timeframe=H1&limit=2500",
        }.items():
            for port in (8000, 8877):
                try:
                    response = client.get(f"http://127.0.0.1:{port}" + endpoint,
                                          headers={"Authorization": f"Bearer {token if port == 8000 else 'dev-token'}"})
                    result = {"http_status": response.status_code}
                    if response.status_code == 200:
                        body = response.json()
                        if name == "h1_candles":
                            result["count"] = len(body)
                            result["last"] = body[-3:]
                        else:
                            result["body"] = body
                    report["runtime"][f"{port}:{name}"] = result
                except httpx.HTTPError as exc:
                    report["runtime"][f"{port}:{name}"] = {"error_type": type(exc).__name__}
    out = ROOT / "data/diag/sol_wait_htf_2026_10_07.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved read-only snapshot: {out}")
    print(f"Unreviewed SOL candidates: {len(report['unreviewed_candidates'])}")
    for name, result in report["runtime"].items():
        body = result.get("body", {})
        if name.endswith(":current"):
            print(name, json.dumps({k: body.get(k) for k in ("price", "stage", "context_id", "scenario_id", "selected_context_basis", "counts")}, ensure_ascii=False))
        elif name.endswith(":h1_candles"):
            print(name, "HTTP", result.get("http_status"), "count", result.get("count"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
