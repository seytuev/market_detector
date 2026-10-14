"""Causal daily structure, independent of H1 scenarios and their storage."""
from __future__ import annotations

from ..engine.ltf.breaks import _SideScan, _bos_condition, _sms_condition
from ..engine.ltf.pivots import find_h1_pivots
from ..engine.ltf.roles import assign_roles
from ..models import Direction
from ..notify.formatting import fmt_price_ru

DAY_MS = 86_400_000
RULE_VERSION = "d1-structure-2"


def project_daily(db, instrument_id: int, as_of: int) -> dict:
    candles = db.get_candles(instrument_id, "D1", end_ms=as_of)
    instrument = db.get_instrument(instrument_id)
    tick = 10 ** -instrument.precision if instrument else 0
    return daily_structure(candles, as_of, tick=tick)


def daily_structure(candles, as_of: int, *, tick: float = 0) -> dict:
    closed = sorted((c for c in candles if c.timeframe == "D1"
                     and c.closed and c.close_time <= as_of), key=lambda c: c.open_time)
    # Split at missing days: a structural pattern cannot span a data gap.
    gap = any(b.open_time - a.open_time != DAY_MS for a, b in zip(closed, closed[1:]))
    start = 0
    for i in range(1, len(closed)):
        if closed[i].open_time - closed[i - 1].open_time != DAY_MS:
            start = i
    sample = closed[start:]
    pivots = [p for p in find_h1_pivots(sample, 2, 2, tick=tick) if p.state == "confirmed"]
    pending = sorted(pivots, key=lambda p: (p.confirmed_at, p.pivot_at))
    known, events = [], []
    initial_direction = None
    sides = [_SideScan(Direction.BULL, as_of), _SideScan(Direction.BEAR, as_of)]
    pi = 0
    for candle in sample:
        newly = []
        while pi < len(pending) and pending[pi].confirmed_at <= candle.close_time:
            newly.append(pending[pi])
            pi += 1
        if newly:
            known.extend(newly)
            known.sort(key=lambda p: (p.pivot_at, p.kind))
            roles = assign_roles(known).roles
            revised = [p for p, role in zip(known, roles)
                       if p not in newly and role.startswith("internal_") and p.role != role]
            for pivot, role in zip(known, roles):
                pivot.role = role
            # An earlier HL/LH becomes internal only when the new pullback
            # confirms. Arm SMS now; never rewrite an earlier candle's event.
            for pivot in revised + newly:
                for side in sides:
                    side.absorb(pivot)
            if initial_direction is None:
                high = next((p for p in reversed(known) if p.kind == "high"), None)
                low = next((p for p in reversed(known) if p.kind == "low"), None)
                if high and low:
                    if high.role == "HH" and low.role == "HL":
                        initial_direction = "bull"
                    elif high.role == "LH" and low.role == "LL":
                        initial_direction = "bear"
        for side in sides:
            events.extend(side.on_candle(candle))
    events.sort(key=lambda e: (e.occurred_at, e.kind != "BOS", e.level_key))
    last_time = events[-1].occurred_at if events else None
    bos_events = [e for e in events if e.kind == "BOS"]
    bos_time = bos_events[-1].occurred_at if bos_events else None
    latest = [e for e in bos_events if e.occurred_at == bos_time]
    directions = {e.direction.value for e in latest}
    direction = (next(iter(directions)) if len(directions) == 1 else
                 initial_direction if not bos_events else None)
    basis = "close_break" if bos_events and direction else "pivot_pair" if direction else None
    warning_event = next((e for e in reversed(events) if e.kind == "SMS" and not e.accompanying
                          and (bos_time is None or e.occurred_at > bos_time)
                          and e.direction.value != direction), None)
    quality = ("no_data" if not sample else "stale" if as_of - sample[-1].close_time > 2 * DAY_MS
               else "insufficient" if len(sample) < 5 else "gap" if gap else "ok")
    if quality != "ok":
        direction = None
    label = {"bull": "Восходящая структура D1", "bear": "Нисходящая структура D1"}.get(
        direction, "Направление D1 не подтверждено")
    reason = {"no_data": "нет закрытых свечей D1", "stale": "свечи D1 устарели",
              "gap": "в истории D1 есть разрыв", "insufficient": "недостаточно свечей D1"}.get(quality)
    event_rows = [{"kind": e.kind, "stage": e.stage, "direction": e.direction.value,
                   "level": e.break_level, "occurred_at": e.occurred_at,
                   "open_time": e.break_candle_open_time,
                   "anchor_at": e.evidence.get("broken_pivot_id", e.break_candle_open_time)}
                  for e in events]
    last = next((e for e in event_rows if e["occurred_at"] == last_time and e["kind"] == "BOS"),
                event_rows[-1] if event_rows else None)
    text = label + (f" · {reason}" if reason else "")
    if last:
        text += f" · {last['kind']} {'вверх' if last['direction'] == 'bull' else 'вниз'} {fmt_price_ru(last['level'])}"
    if warning_event:
        text += " · предупреждение о возможной смене; ждём BOS D1"
    expected = {}
    for side in sides:
        expected[side.direction.value] = {"bos": _bos_condition(side), "sms": _sms_condition(side)}
        for condition in expected[side.direction.value].values():
            condition["timeframe"] = "D1"
    return {"timeframe": "D1", "rule_version": RULE_VERSION, "as_of": as_of,
            "direction": direction, "basis": basis, "data_quality": quality, "text": text,
            "last_close_at": sample[-1].close_time if sample else None,
            "last_event": last, "events": event_rows,
            "warning": ({"kind": "SMS", "direction": warning_event.direction.value,
                         "occurred_at": warning_event.occurred_at,
                         "level": warning_event.break_level} if warning_event else None),
            "pivots": [{"pivot_at": p.pivot_at, "confirmed_at": p.confirmed_at,
                        "kind": p.kind, "role": p.role, "price": p.price} for p in known],
            "expected": expected}


def daily_alignment(daily: dict, direction) -> dict:
    direction = getattr(direction, "value", direction)
    direction = {"long": "bull", "short": "bear"}.get(direction, direction)
    trend = daily.get("direction")
    relation = ("unknown" if not trend or direction not in ("bull", "bear") else
                "aligned" if direction == trend else "countertrend")
    text = {"unknown": "Согласование с D1 пока не подтверждено",
            "aligned": "Сценарий по направлению структуры D1",
            "countertrend": "Контртрендовый сценарий против структуры D1"}[relation]
    return {"relation": relation, "direction": trend, "text": text}
