"""Causal daily structure, independent of H1 scenarios and their storage."""
from __future__ import annotations

from ..engine.ltf.market_structure import replay_structure, RULE_VERSION as STRUCTURE_RULE_VERSION
from ..engine.ltf.pivots import find_h1_pivots
from ..models import open_times_follow
from ..models import Direction
from ..notify.formatting import fmt_price_ru

DAY_MS = 86_400_000
RULE_VERSION = STRUCTURE_RULE_VERSION


def project_daily(db, instrument_id: int, as_of: int) -> dict:
    candles = db.get_candles(instrument_id, "D1", end_ms=as_of)
    instrument = db.get_instrument(instrument_id)
    tick = 10 ** -instrument.precision if instrument else 0
    return daily_structure(candles, as_of, tick=tick)


def daily_structure(candles, as_of: int, *, tick: float = 0) -> dict:
    return timeframe_structure(candles, as_of, timeframe="D1", tick=tick)


def project_weekly(db, instrument_id: int, as_of: int) -> dict:
    instrument = db.get_instrument(instrument_id)
    tick = 10 ** -instrument.precision if instrument else 0
    return timeframe_structure(db.get_candles(instrument_id, "W1", end_ms=as_of), as_of, timeframe="W1", tick=tick)


def timeframe_structure(candles, as_of: int, *, timeframe: str, tick: float = 0) -> dict:
    closed = sorted((c for c in candles if c.timeframe == timeframe
                     and c.closed and c.close_time <= as_of), key=lambda c: c.open_time)
    # Split at missing days: a structural pattern cannot span a data gap.
    gap = any(not open_times_follow(a.open_time, b.open_time, timeframe) for a, b in zip(closed, closed[1:]))
    start = 0
    for i in range(1, len(closed)):
        if not open_times_follow(closed[i - 1].open_time, closed[i].open_time, timeframe):
            start = i
    sample = closed[start:]
    pivots = [p for p in find_h1_pivots(sample, 2, 2, tick=tick) if p.state == "confirmed"]
    machine, events = replay_structure(pivots, sample, as_of)
    known = sorted(pivots, key=lambda p: (p.pivot_at, p.kind))
    for pivot in known:
        pivot.role = machine.roles.get((pivot.kind, pivot.pivot_at), "none")
    last_time = events[-1].occurred_at if events else None
    direction = machine.direction.value if machine.direction else None
    basis = "close_break" if events else "pivot_pair" if direction else None
    warning_event = None
    interval = DAY_MS if timeframe == "D1" else 7 * DAY_MS
    quality = ("no_data" if not sample else "stale" if as_of - sample[-1].close_time > 2 * interval
               else "insufficient" if len(sample) < 5 else "gap" if gap else "ok")
    if quality != "ok":
        direction = None
    label = {"bull": f"Восходящая структура {timeframe}", "bear": f"Нисходящая структура {timeframe}"}.get(
        direction, f"Направление {timeframe} не подтверждено")
    reason = {"no_data": f"нет закрытых свечей {timeframe}", "stale": f"свечи {timeframe} устарели",
              "gap": f"в истории {timeframe} есть разрыв", "insufficient": f"недостаточно свечей {timeframe}"}.get(quality)
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
    expected = {}
    for side in (Direction.BULL, Direction.BEAR):
        expected[side.value] = {"bos": machine.condition(side, "BOS"), "sms": machine.condition(side, "SMS")}
        for condition in expected[side.value].values():
            condition["timeframe"] = timeframe
    parent_frames = [{"direction": frame.direction.value, "origin": frame.origin.price,
                      "extreme": frame.extreme.price if frame.extreme else None}
                     for frame in machine.frames[:-1]]
    return {"timeframe": timeframe, "parent_structures": parent_frames, "rule_version": RULE_VERSION, "as_of": as_of,
            "main_structure": machine.main_structure(),
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
