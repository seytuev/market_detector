"""D1 priority and the nearest opposite daily zone for countertrend targets.

This projection never cancels an H1 scenario or changes its entry zones.
"""
from .d1_structure import daily_alignment
from ..models import ZoneStatus
from ..notify.formatting import fmt_price_ru

TARGET_TYPES = {"ob", "fvg", "breaker", "prb"}


def nearest_daily_obstacle(db, instrument_id, direction, reference, as_of):
    direction = getattr(direction, "value", direction)
    direction = {"long": "bull", "short": "bear"}.get(direction, direction)
    if reference is None or direction not in ("bull", "bear"):
        return None
    candidates = []
    for z in db.get_zones(instrument_id=instrument_id, timeframes={"D1"}):
        typ = z.zone_type if z.type.value == "manual" else z.type.value
        if (typ not in TARGET_TYPES or z.direction.value == direction or z.needs_replay
                or z.evidence.get("direction_unset")):
            continue
        if z.formed_at > as_of or (z.source == "manual" and z.created_at > as_of):
            continue
        confirmed = z.confirmed_at
        if confirmed is None:
            if not (z.manual_confirmation_only or z.source == "manual"):
                continue
            confirmed = z.created_at
        if confirmed > as_of:
            continue
        ends = [int(v) for v in (z.display_until, z.evidence.get("invalidated_at")) if v is not None]
        if ends and min(ends) <= as_of:
            continue
        if not ends and (z.market_validity != "active" or z.status not in
                         (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED, ZoneStatus.CANDIDATE)):
            continue
        if direction == "bull":
            if z.upper < reference:
                continue
            boundary = z.lower
            distance = max(0, boundary - reference)
        else:
            if z.lower > reference:
                continue
            boundary = z.upper
            distance = max(0, reference - boundary)
        candidates.append((distance, z.id or 0, z, typ, boundary))
    if not candidates:
        return None
    _, _, zone, typ, boundary = min(candidates, key=lambda c: c[:2])
    return {"zone_id": zone.id, "type": typ.upper(), "timeframe": "D1",
            "lower": zone.lower, "upper": zone.upper, "price": boundary,
            "status": "inside" if zone.lower <= reference <= zone.upper else "ahead",
            "text": f"До {typ.upper()} D1 {fmt_price_ru(boundary)} · границы {fmt_price_ru(zone.lower)}–{fmt_price_ru(zone.upper)}"}


def synchronize_scenario(db, instrument_id, daily, direction, as_of, *, reference=None,
                         h1_confirmed=False, has_htf_source=False):
    alignment = daily_alignment(daily, direction)
    relation = alignment["relation"]
    confirmed = h1_confirmed and has_htf_source
    priority = ("main" if relation == "aligned" else
                "countertrend" if relation == "countertrend" else "unresolved")
    status = ("Основной сценарий" if priority == "main" else "Контртрендовый сценарий"
              if priority == "countertrend" else "Приоритет D1 не определён")
    if not confirmed:
        status += " · ждём H1 из HTF-зоны"
    target = (nearest_daily_obstacle(db, instrument_id, direction, reference, as_of)
              if priority == "countertrend" else None)
    target_text = (target["text"] if target else "Актуальная противоположная зона D1 впереди не найдена"
                   if priority == "countertrend" and reference is not None else
                   "Для цели нужна цена отсчёта" if priority == "countertrend" else None)
    return {**alignment, "priority": priority, "status": status,
            "h1_confirmed": bool(h1_confirmed), "has_htf_source": bool(has_htf_source),
            "confirmation_complete": bool(confirmed), "target": target, "target_text": target_text,
            "daily_warning": daily.get("warning"), "preserve_entry_zones": True}
