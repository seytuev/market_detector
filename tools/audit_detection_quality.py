"""Read-only audit of local data and isolated reproductions of detector defects.

Run: .venv/Scripts/python.exe tools/audit_detection_quality.py
Does not initialize or migrate the production Database, fetch data or send alerts.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.alt.engine_v2 import cluster_pivots
from app.config import DetectorConfig
from app.db import Database
from app.engine.fvg import scan_fvgs
from app.engine.liquidity import PivotRecord
from app.engine.ltf.context import find_htf_fvg50_test
from app.models import Candle, Direction, Instrument, Zone, ZoneStatus, ZoneType
from app.models_alt import AltRangeEpisode
from app.services.alt_overview import select_current_episode
from app.services.htf_parent import parent_decision

DAY = 86_400_000
T0 = 1_767_225_600_000  # 2026-01-01 UTC


def snapshot() -> dict:
    path = ROOT / "data" / "htf_zones.db"
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    queries = {
        "instruments": "SELECT id,asset,venue,enabled,ltf_analyze FROM instrument",
        "candles": """SELECT instrument_id,timeframe,count(*) n,
            min(open_time) first_open,max(open_time) last_open
            FROM candle WHERE closed=1 GROUP BY 1,2""",
        "gaps": """WITH x AS (SELECT instrument_id,timeframe,open_time,
            lag(open_time) OVER (PARTITION BY instrument_id,timeframe ORDER BY open_time) prev
            FROM candle WHERE closed=1)
            SELECT instrument_id,timeframe,count(*) n,max(open_time-prev) max_gap_ms
            FROM x WHERE open_time-prev > CASE timeframe WHEN 'H1' THEN 3600000
            WHEN 'D1' THEN 86400000 ELSE 604800000 END GROUP BY 1,2""",
        "zone_versions": "SELECT rule_version,count(*) n FROM zone GROUP BY 1",
        "open_observations": """SELECT state,count(*) n FROM ltf_observation
            WHERE state IN ('active','waiting_structure') GROUP BY 1""",
        "open_observations_with_finished_parent": """SELECT o.id,o.zone_id,o.state,
            z.type,z.status,z.display_until,z.market_validity
            FROM ltf_observation o JOIN zone z ON z.id=o.zone_id
            WHERE o.state IN ('active','waiting_structure') AND
            (z.display_until IS NOT NULL OR z.market_validity!='active'
            OR z.status NOT IN ('active','weakened','candidate'))""",
        "latest_reviews": """SELECT r.decision,count(*) n FROM review r JOIN
            (SELECT zone_id,max(id) id FROM review GROUP BY zone_id) l ON r.id=l.id
            GROUP BY 1""",
        "latest_assessments": """SELECT r.geometry_verdict,count(*) n
            FROM review_assessment r JOIN
            (SELECT zone_id,max(id) id FROM review_assessment GROUP BY zone_id) l ON r.id=l.id
            GROUP BY 1""",
        "alt_assets": "SELECT count(*) n FROM alt_asset",
        "alt_candles": "SELECT count(*) n FROM alt_candle",
        "alt_episode_tables": """SELECT name FROM sqlite_master
            WHERE type='table' AND name IN ('alt_range_episode','alt_sweep_episode')""",
        "alt_runs": """SELECT id,status,processed,errors,started_ms,summary_json
            FROM alt_run ORDER BY id DESC LIMIT 3""",
    }
    try:
        return {name: [dict(row) for row in conn.execute(query)]
                for name, query in queries.items()}
    finally:
        conn.rollback()
        conn.close()


def probes() -> dict:
    out = {}
    candles = [
        Candle(1, "D1", T0 + d * DAY, T0 + (d + 1) * DAY - 1,
               o, h, l, c)
        for d, o, h, l, c in [(0, 10, 11, 9, 10), (1, 11, 13, 10, 12),
                              (4, 14, 16, 13, 15)]
    ]
    out["fvg_across_missing_days"] = {
        "input_day_offsets": [0, 1, 4],
        "fvg_count": len(scan_fvgs(candles, "D1")),
        "expected": "Reject a three-candle pattern across missing D1 intervals.",
    }
    ps = [PivotRecord(ZoneType.SSL, price, T0 + i * DAY, T0 + (i + 3) * DAY,
                      "D1", ()) for i, price in enumerate([100, 100.9, 101.8, 102.7])]
    clusters = cluster_pivots(ps, 1.0)
    out["alt_chained_cluster"] = {
        "tolerance": 1.0,
        "clusters": [[p.price for p in cl.pivots] for cl in clusters],
        "largest_span": max(max(p.price for p in cl.pivots)
                            - min(p.price for p in cl.pivots) for cl in clusters),
    }
    # Only an in-memory Database is initialized here.
    db = Database(":memory:")
    iid = db.upsert_instrument(Instrument(None, "TEST", "binance", "spot", "TESTUSDT", "USDT"))
    zid = db.insert_zone(Zone(None, iid, ZoneType.FVG, Direction.BULL, "D1",
                              10, 12, T0, T0 + DAY, ZoneStatus.ACTIVE))
    before_confirmation = find_htf_fvg50_test(
        db, iid, 10.5, Direction.BEAR, as_of=T0 + DAY // 2,
        event_time=T0 + DAY // 2,
    )
    out["h1_context_before_d1_confirmation"] = {
        "formed_at": T0, "confirmed_at": T0 + DAY, "as_of": T0 + DAY // 2,
        "result": before_confirmation,
        "expected": "No confirmed D1 FVG context before confirmed_at.",
    }
    historical_before = find_htf_fvg50_test(
        db, iid, 10.5, Direction.BEAR, as_of=T0 + 2 * DAY,
        event_time=T0 + 2 * DAY,
    )
    db.update_zone(zid, status=ZoneStatus.ARCHIVED)
    historical_after = find_htf_fvg50_test(
        db, iid, 10.5, Direction.BEAR, as_of=T0 + 2 * DAY,
        event_time=T0 + 2 * DAY,
    )
    out["historical_context_depends_on_current_status"] = {
        "before_later_archive": historical_before,
        "after_later_archive_same_as_of": historical_after,
        "expected": "The historical query must preserve the same answer.",
    }
    db.close()
    parent = Zone(None, 1, ZoneType.OB, Direction.BULL, "D1", 10, 12,
                  T0, T0 + DAY, ZoneStatus.ACTIVE, needs_replay=True)
    out["parent_needs_replay_admitted"] = vars(parent_decision(
        parent, DetectorConfig(), T0 + 2 * DAY))
    manual = Zone(None, 1, ZoneType.OB, Direction.BULL, "D1", 10, 12,
                  T0, T0 + 10 * DAY, ZoneStatus.ACTIVE, source="manual")
    out["manual_parent_future_confirmation"] = {
        "confirmed_at": manual.confirmed_at, "as_of": T0 + DAY,
        "decision": vars(parent_decision(manual, DetectorConfig(), T0 + DAY)),
    }
    old = AltRangeEpisode(
        id=1, asset_id=1, source_id=1, origin_key="old",
        anchor_start_open_time=T0, base_start_open_time=T0,
        lower=10, upper=20, width=10, mid=15, state="accompaniment",
        base_end_open_time=T0 + 10 * DAY,
        base_end_reason="breakout_confirmed",
        base_end_confirmed_at_ms=T0 + 11 * DAY,
    )
    new = AltRangeEpisode(
        id=2, asset_id=1, source_id=1, origin_key="new",
        anchor_start_open_time=T0 + 350 * DAY, base_start_open_time=T0 + 350 * DAY,
        lower=11, upper=14, width=3, mid=12.5, state="accompaniment",
        base_end_open_time=T0 + 390 * DAY,
        base_end_reason="breakout_confirmed",
        base_end_confirmed_at_ms=T0 + 391 * DAY,
    )
    selected, reason, alternatives = select_current_episode(
        [old, new], 15, T0 + 400 * DAY)
    out["old_base_overrides_recent_breakout"] = {
        "selected_id": selected.id if selected else None, "reason": reason,
        "alternatives": [x.id for x in alternatives],
        "old_breakout_age_days": 389, "new_breakout_age_days": 9,
        "expected": "Old containment must not bypass expiry or hide the recent alternative.",
    }
    return out


if __name__ == "__main__":
    result = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Local checkout; local SQLite read transaction; synthetic probes in memory.",
        "snapshot": snapshot(), "probes": probes(),
    }
    out = ROOT / "docs" / "audit" / "detection_quality_2026_10_08.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["probes"], ensure_ascii=False, indent=2))
    print(f"Saved: {out}")
