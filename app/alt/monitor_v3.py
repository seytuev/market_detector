"""Touch observations between W1 closes. Scenario invalidation stays in engine_v3."""
from __future__ import annotations

import json
import math

from .engine_v3 import RULES_VERSION, _id, week_start
from .store_v3 import frame, insert_event
from .universe import is_excluded_asset


def observe_quote(db, source_id, price, event_at, received_at, *, price_kind="trade", source_seq=None):
    if price_kind != "trade" or event_at is None or not math.isfinite(price) or price <= 0:
        return 0
    if event_at > received_at + 5000 or received_at - event_at > 60_000:
        return 0
    coverage = json.loads(db.get_meta(f"alt:v3:coverage:{source_id}") or "{}")
    if coverage.get("rules_version") != RULES_VERSION or coverage.get("status") != "ok" or (coverage.get("last_week_close_ms") or 0) < week_start(event_at):
        return 0  # resolve the latest W1 invalidations before accepting touches
    cursor_key = f"alt:v3:quote:{source_id}"
    count = 0
    with db.batch_writes():
        previous = json.loads(db.get_meta(cursor_key) or "{}")
        if event_at < previous.get("at", -1) or event_at == previous.get("at", -1) and source_seq == previous.get("seq"):
            return 0
        rows = db.conn.execute("SELECT id FROM alt_structure WHERE source_id=? AND rules_version=?", (source_id, RULES_VERSION)).fetchall()
        for row in rows:
            f = frame(db, row["id"], event_at)
            if f is None:
                continue
            source = db.get_alt_instrument_source(f["asset_id"])
            asset = db.get_alt_asset(f["asset_id"])
            if source is None or source.id != source_id or asset is None or not asset.enabled or is_excluded_asset(asset):
                continue
            state = f["scenario_state"]
            scenarios = [s for s in state.get("scenarios", []) if s["bos_at_ms"] <= event_at and s["cancelled_at_ms"] is None]
            active = {s["id"] for s in scenarios}
            zones = [z for z in state.get("pd_zones", []) if z["available_at_ms"] <= event_at
                     and z["invalidated_at_ms"] is None and z["scenario_id"] in active]
            if scenarios or f["base_end_confirmed_at_ms"] is not None and f["base_end_confirmed_at_ms"] <= event_at:
                zones += [{"id": f["structure_key"]+":"+side, "kind": "boundary", "lower": value, "upper": value}
                          for side, value in (("L", f["lower"]), ("U", f["upper"]))]
            for z in zones:
                key = f"alt:v3:contact:{source_id}:{z['id']}"
                prior = json.loads(db.get_meta(key) or "{}")
                inside = z["lower"] <= price <= z["upper"]
                prev_price = previous.get("price")
                crossed = (prev_price is not None and min(prev_price, price) <= z["upper"] and max(prev_price, price) >= z["lower"])
                if (inside or crossed) and not prior.get("inside", False):
                    event = {"id": _id(f["structure_key"], z["id"], "trade", event_at, source_seq),
                        "event_type": "retest", "event_time_ms": event_at, "available_at_ms": event_at, "price": price,
                        "payload": {"candle_open_time": (event_at//86_400_000)*86_400_000,
                            "rules_version": RULES_VERSION, "zone_id": z["id"], "kind": z["kind"],
                            "zone": {"lower": z["lower"], "upper": z["upper"]}, "precision": "trade" if inside else "quote_crossing", "timeframe": "W1"}}
                    insert_event(db, f["asset_id"], source_id, f["structure_key"], event)
                    count += 1
                db.set_meta(key, json.dumps({"inside": inside, "at": event_at}))
        db.set_meta(cursor_key, json.dumps({"at": event_at, "price": price, "seq": source_seq}))
    return count
