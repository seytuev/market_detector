"""Presentation of an H1 event against its local movement, without journal writes.

Scenario ranges are historical context. The range on the event chart belongs to
the instrument's local H1 leg. Never mix the two or substitute a live quote.
"""
from dataclasses import replace

from ..services.h1_setup import _lookback_ms, local_leg_at, revision_at


def direction_of(event, ctx):
    return (event.payload or {}).get("direction") or (
        ctx.scenario.direction.value if ctx.scenario else
        ctx.observation.direction.value if ctx.observation else ""
    )


def present_event(db, cfg, event, ctx, *, cache=None, current=False):
    """Keep the raw event in Details; return a copy suitable for the summary.

    Provenance and current validity are filtered here. Distance and position
    relative to PD do not introduce new trading rules. Legacy scenario-only
    data stays available when a local movement has not been reconstructed.
    A cache is scoped to one delivery/render pass, never to the live database.
    """
    if ctx.instrument is None:
        return event
    cache = {} if cache is None else cache
    iid, moment = ctx.instrument.id, int(event.occurred_at)
    key = (iid, moment)
    if key not in cache:
        leg = local_leg_at(db, iid, moment)
        rev = revision_at(db, leg["id"], moment) if leg else None
        bars = db.get_candles(iid, "H1", start_ms=moment - 3_600_000,
                              end_ms=moment, closed_only=True)
        close = next((c.close for c in reversed(bars) if 0 <= moment - c.close_time <= 1), None)
        cache[key] = leg, rev, close
    leg, rev, close = cache[key]
    p = dict(event.payload or {})
    direction = direction_of(event, ctx)
    p["direction"] = direction
    if close is not None and p.get("close") is None and p.get("close_price") is None:
        p["close"] = close
    structural = event.kind in ("bos", "sms", "entries_ready", "range_ready")
    if structural and leg and leg["direction"] == direction:
        p["range_source"] = "local_h1"
        p["range"] = ({"lower": rev["lower"], "upper": rev["upper"], "mid": rev["eq"]}
                      if rev and rev.get("lower") is not None else None)
        p["range_status"] = rev.get("range_status") if rev else "range_pending"
        p["range_pending"] = p["range"] is None
    elif structural and p.get("range"):
        p["range_source"] = "scenario"

    entries, excluded = [], []
    window = (int(leg["origin_at"]) - _lookback_ms(cfg)
              if leg and leg.get("origin_at") is not None else None)
    for entry in p.get("entries") or []:
        item = dict(entry)
        zid = item.get("entry_zone_id")
        zone_key = ("zone", zid)
        if zid is not None and zone_key not in cache:
            cache[zone_key] = db.get_ltf_entry_zone(zid)
        zone = cache.get(zone_key) if zid is not None else None
        formed = item.get("formed_at")
        confirmed = item.get("confirmed_at")
        if zone is not None:
            formed = zone.formed_at if formed is None else formed
            confirmed = zone.confirmed_at if confirmed is None else confirmed
        reason = None
        if zone is not None and (zone.instrument_id != iid or zone.direction.value != direction):
            reason = "other_instrument_or_direction"
        elif leg and leg["direction"] != direction:
            reason = "other_movement"
        elif window is not None and formed is not None and int(formed) < window:
            reason = "other_movement"
        elif leg and (formed is None or confirmed is None):
            reason = "provenance_unavailable"
        elif confirmed is not None and int(confirmed) > moment + 1:
            reason = "not_confirmed_at_event"
        elif current and zone is not None and zone.validity == "invalid":
            reason = "invalid"
        if reason:
            excluded.append({**item, "presentation_exclusion": reason})
        else:
            if formed is not None:
                item["formed_at"] = formed
            entries.append(item)
    p["entries"] = entries
    if excluded:
        p["excluded_entries"] = excluded
    return replace(event, payload=p)
