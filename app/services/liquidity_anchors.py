"""H1 display anchors for D1/W1 liquidity; detection timestamps stay intact."""
from math import isclose

from ..models import TIMEFRAME_MINUTES, ZoneType

HOUR = 3_600_000


def refresh_liquidity_anchors(db, instrument_id):
    levels = [z for z in db.get_zones(instrument_id, types=[ZoneType.BSL, ZoneType.SSL])
              if z.timeframe in ("D1", "W1") and z.source != "manual"]
    anchors = {}
    for z in levels:
        end = z.formed_at + TIMEFRAME_MINUTES[z.timeframe] * 60_000
        bars = db.get_candles(instrument_id, "H1", start_ms=z.formed_at, end_ms=end - 1)
        complete = (len(bars) == (end - z.formed_at) // HOUR
                    and all(c.open_time == z.formed_at + n * HOUR for n, c in enumerate(bars)))
        matches = [c.open_time for c in bars
                   if isclose(c.high if z.type == ZoneType.BSL else c.low,
                              z.lower, rel_tol=1e-12, abs_tol=0)]
        exact = complete and bool(matches)
        anchors[z.id] = min(matches) if exact else None
    for z in levels:
        at = anchors[z.id]
        ev = dict(z.evidence)
        ev.update(extreme_at=at, extreme_timeframe="H1",
                  extreme_anchor_quality="exact" if at is not None else "incomplete")
        members = [m for m in levels if m.id in ev.get("group_members", [z.id])
                   and m.lower == ev.get("group_level", z.lower)]
        group_exact = bool(members) and all(anchors[m.id] is not None for m in members)
        ev["group_extreme_at"] = min(anchors[m.id] for m in members) if group_exact else None
        start = at if at is not None else z.formed_at
        if ev != z.evidence or z.display_from != start:
            db.update_zone(z.id, evidence=ev, display_from=start)
