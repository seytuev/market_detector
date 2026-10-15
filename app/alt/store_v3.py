"""Stable structure IDs, immutable causal snapshots, and a separate W1 journal."""
from __future__ import annotations

import json
from typing import Any

from .engine_v3 import RULES_VERSION, analyze, _id


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def persist_result(db, asset_id: int, source_id: int, result: dict, *, backfill: bool = False) -> int:
    if result["status"] in ("data_pending", "insufficient_history"):
        db.set_meta(f"alt:v3:coverage:{source_id}", _json({"status": result["status"], "computed_at_ms": result["computed_at_ms"], "last_week_close_ms": result.get("last_week_close_ms")}))
        return 0
    with db.batch_writes():
        existing_revisions = {r[0] for r in db.conn.execute(
            "SELECT r.revision_id FROM alt_structure_revision r JOIN alt_structure s ON s.id=r.structure_id WHERE s.source_id=? AND s.rules_version=?",
            (source_id, RULES_VERSION)).fetchall()}
        existing_events = {r[0] for r in db.conn.execute("SELECT event_key FROM alt_v3_event WHERE source_id=?", (source_id,)).fetchall()}
        for structure in result["structures"]:
            g = structure["geometry"]
            db.conn.execute(
                """INSERT INTO alt_structure(asset_id,source_id,structure_key,rules_version,state,eligible,
                   lower,upper,base_start_open_time,base_end_open_time,available_at_ms,computed_at_ms,payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id,structure_key,rules_version) DO UPDATE SET
                   state=excluded.state,eligible=excluded.eligible,lower=excluded.lower,upper=excluded.upper,
                   base_end_open_time=excluded.base_end_open_time,available_at_ms=excluded.available_at_ms,
                   computed_at_ms=excluded.computed_at_ms,payload_json=excluded.payload_json""",
                (asset_id, source_id, structure["structure_key"], RULES_VERSION, structure["state"], 1,
                 g["lower"], g["upper"], g["base_start_open_time"], g["base_end_open_time"], g["available_at_ms"],
                 result["computed_at_ms"], "{}"))
            sid = db.conn.execute("SELECT id FROM alt_structure WHERE source_id=? AND structure_key=? AND rules_version=?",
                                  (source_id, structure["structure_key"], RULES_VERSION)).fetchone()["id"]
            previous = None
            for r in structure["revisions"]:
                if r["revision_id"] in existing_revisions:
                    previous = r["revision_id"]
                    continue
                quality = {**r["quality_metrics"], "scenario_state": r["scenario_state"]}
                db.conn.execute(
                    """INSERT OR IGNORE INTO alt_structure_revision(structure_id,revision_id,rules_version,geometry_kind,
                       lower,upper,mid,width,base_start_open_time,base_end_open_time,base_end_confirmed_at_ms,
                       available_at_ms,computed_at_ms,supersedes_revision_id,revision_reason,state,
                       boundary_evidence_json,quality_json,selection_reason,data_coverage_json,params_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, r["revision_id"], RULES_VERSION, r["geometry_kind"], r["lower"], r["upper"], r["mid"], r["width"],
                     r["base_start_open_time"], r["base_end_open_time"], r["base_end_confirmed_at_ms"], r["available_at_ms"],
                     result["computed_at_ms"], previous, r["revision_reason"], r["state"], _json(r["boundary_evidence"]),
                     _json(quality), r["selection_reason"], _json(r["data_coverage"]), _json(r["params"])))
                previous = r["revision_id"]
            for e in structure["scenario_state"]["events"]:
                if _id(source_id, e["id"]) not in existing_events:
                    insert_event(db, asset_id, source_id, structure["structure_key"], e, delivered=backfill)
        if backfill:
            db.conn.execute("UPDATE alt_v3_event SET delivered=1 WHERE source_id=? AND delivered=0", (source_id,))
        db.set_meta(f"alt:v3:coverage:{source_id}", _json({"status": result["status"], "computed_at_ms": result["computed_at_ms"], "last_week_close_ms": result.get("last_week_close_ms")}))
    return len(result["structures"])


def insert_event(db, asset_id, source_id, structure_key, event, *, delivered=False):
    if event["event_type"] == "retest" and event.get("payload", {}).get("precision") == "D1":
        # A live trade observation is more precise than its later D1 replay.
        for row in db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE source_id=? AND structure_key=? AND event_type='retest' AND event_time_ms>? AND event_time_ms<=?",
                                   (source_id, structure_key, event["event_time_ms"]-86_400_000, event["event_time_ms"])).fetchall():
            existing = json.loads(row["payload_json"])
            if existing["payload"].get("zone_id") == event["payload"].get("zone_id") and existing["payload"].get("precision") != "D1":
                return
    key = _id(source_id, event["id"])
    db.conn.execute("""INSERT OR IGNORE INTO alt_v3_event(event_key,asset_id,source_id,structure_key,event_type,
                    event_time_ms,payload_json,delivered) VALUES(?,?,?,?,?,?,?,?)""",
                    (key, asset_id, source_id, structure_key, event["event_type"], event["event_time_ms"],
                     _json(event), int(delivered)))


def persist_candles(db, asset_id, source_id, candles, computed_at_ms, *, backfill=False):
    result = analyze(candles, as_of_ms=computed_at_ms, computed_at_ms=computed_at_ms)
    persist_result(db, asset_id, source_id, result, backfill=backfill)
    return result


def list_structures(db, asset_id, source_id, as_of_ms=None):
    rows = db.conn.execute("SELECT * FROM alt_structure WHERE asset_id=? AND source_id=? AND rules_version=? ORDER BY base_start_open_time",
                           (asset_id, source_id, RULES_VERSION)).fetchall()
    return [f for r in rows if (f := frame(db, r["id"], as_of_ms)) is not None]


def frame(db, structure_id, as_of_ms=None):
    row = db.conn.execute("SELECT * FROM alt_structure WHERE id=? AND rules_version=?", (structure_id, RULES_VERSION)).fetchone()
    if row is None:
        return None
    sql = "SELECT * FROM alt_structure_revision WHERE structure_id=?"
    args: list[Any] = [structure_id]
    if as_of_ms is not None:
        sql += " AND available_at_ms<=?"
        args.append(as_of_ms)
    r = db.conn.execute(sql + " ORDER BY available_at_ms DESC,id DESC LIMIT 1", args).fetchone()
    if r is None:
        return None
    quality = json.loads(r["quality_json"])
    return {**{k: r[k] for k in ("lower", "upper", "mid", "width", "state", "revision_id", "rules_version", "geometry_kind",
            "base_start_open_time", "base_end_open_time", "base_end_confirmed_at_ms", "available_at_ms")},
            "range_id": structure_id, "structure_id": structure_id, "asset_id": row["asset_id"], "source_id": row["source_id"],
            "structure_key": row["structure_key"], "geometry_source": "structural", "eligible": True,
            "boundary_evidence": json.loads(r["boundary_evidence_json"]), "quality_metrics": quality,
            "scenario_state": quality.get("scenario_state", {}), "computed_at_ms": row["computed_at_ms"], "children": []}


def select_frame(db, asset_id, source_id, as_of_ms=None):
    frames = list_structures(db, asset_id, source_id, as_of_ms)
    # A completed recent cycle must never be displaced by an older open one.
    return max(frames, key=lambda f: (f["base_start_open_time"], f["available_at_ms"])) if frames else None
