"""Causal W1 bases and depression/BOS scenarios (alt-0.5).

All decisions consume closed UTC weeks. D1 is the source of OHLC and
historical touch observations, never a substitute for a W1 close. Live
touches are handled by monitor_v3 without waiting for either candle close.
No asset names, target prices, or calendar windows enter the rules.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import math
from statistics import median
from typing import Any, Sequence

DAY = 86_400_000
WEEK = 7 * DAY
MONDAY = 4 * DAY  # 1970-01-05 UTC
RULES_VERSION = "alt-0.5"


@dataclass(frozen=True)
class RulesV3:
    min_base_days: int = 101
    min_rebound_fraction: float = 0.15
    min_containment: float = 0.80
    max_directional_efficiency: float = 0.35
    max_center_shift_atr: float = 1.0
    min_lower_reactions: int = 2
    reaction_tolerance_fraction: float = 0.10
    midpoint_tolerance_fraction: float = 0.06
    max_candidates: int = 64
    # A closed week below L is already a depression candidate. There is
    # deliberately no minimum duration, drawdown gate, or pre-BOS stop.


@dataclass(frozen=True)
class Week:
    t: int
    open: float
    high: float
    low: float
    close: float
    low_day: int
    high_day: int

    @property
    def available_at(self) -> int:
        return self.t + WEEK


def week_start(t: int) -> int:
    return ((t - MONDAY) // WEEK) * WEEK + MONDAY


def _id(*parts: Any) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:24]


def _value(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, dict) else getattr(row, key)


def normalize(rows: Sequence[Any], as_of_ms: int | None = None) -> list[dict]:
    out = {}
    sources = set()
    for row in rows:
        t = int(_value(row, "open_time"))
        if as_of_ms is not None and t + DAY > as_of_ms:
            continue
        values = {key: float(_value(row, key)) for key in ("open", "high", "low", "close")}
        if t % DAY or not all(math.isfinite(v) and v > 0 for v in values.values()):
            raise ValueError("Invalid D1 timestamp or price")
        if not (values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]):
            raise ValueError("Invalid D1 OHLC")
        item = {"open_time": t, **values}
        if t in out and out[t] != item:
            raise ValueError("Conflicting duplicate D1 candle")
        out[t] = item
        source = row.get("source_id") if isinstance(row, dict) else getattr(row, "source_id", None)
        if source is not None:
            sources.add(source)
    if len(sources) > 1:
        raise ValueError("Mixed candle sources")
    return [out[t] for t in sorted(out)]


def aggregate_weeks(rows: Sequence[Any], as_of_ms: int | None = None) -> list[Week]:
    groups: dict[int, list[dict]] = {}
    for row in normalize(rows, as_of_ms):
        groups.setdefault(week_start(row["open_time"]), []).append(row)
    result = []
    for t, group in groups.items():
        if len(group) != 7 or [r["open_time"] for r in group] != [t + n * DAY for n in range(7)]:
            continue  # partial/gapped weeks are never W1 decisions
        low = min(group, key=lambda r: (r["low"], r["open_time"]))
        high = max(group, key=lambda r: (r["high"], -r["open_time"]))
        result.append(Week(t, group[0]["open"], high["high"], low["low"], group[-1]["close"], low["open_time"], high["open_time"]))
    return result


@dataclass
class Candidate:
    start: int
    start_index: int
    lower: float
    upper: float
    lower_day: int
    upper_day: int


@dataclass
class Base:
    key: str
    start: int
    lower: float
    upper: float
    lower_day: int
    upper_day: int
    mature_at: int
    state: str = "mature"
    end: int | None = None
    end_confirmed: int | None = None
    depression: dict | None = None
    scenarios: list[dict] = field(default_factory=list)
    zones: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)
    latest_high: tuple[float, int] | None = None
    high_since_low: tuple[float, int] | None = None
    pd_from_index: int | None = None
    midpoint_wicks: int = 0
    contact: dict = field(default_factory=dict)


def _event(base: Base, event_type: str, at: int, price: float, *, candle: int, **payload: Any) -> dict:
    event = {"id": _id(base.key, event_type, at, payload.get("zone_id", "")), "event_type": event_type,
             "event_time_ms": at, "available_at_ms": at, "price": price,
             "payload": {"candle_open_time": candle, "rules_version": RULES_VERSION, **payload}}
    if not any(e["id"] == event["id"] for e in base.events):
        base.events.append(event)
    return event


def _snapshot(base: Base, at: int, reason: str, cfg: RulesV3) -> None:
    width = base.upper - base.lower
    snapshot = {
        "revision_id": _id(base.key, at, reason), "rules_version": RULES_VERSION,
        "geometry_kind": "completed_base" if base.end is not None else "structural_base",
        "lower": base.lower, "upper": base.upper, "mid": (base.lower + base.upper) / 2, "width": width,
        "base_start_open_time": base.start, "base_end_open_time": base.end,
        "base_end_confirmed_at_ms": base.end_confirmed, "available_at_ms": at,
        "revision_reason": reason, "state": base.state,
        "boundary_evidence": {
            "lower": {"price": base.lower, "supports": [{"price": base.lower, "formed_at": base.lower_day}], "method": "wick"},
            "upper": {"price": base.upper, "supports": [{"price": base.upper, "formed_at": base.upper_day}], "method": "wick"},
        },
        "quality_metrics": {"midpoint_wick_reactions": base.midpoint_wicks, "midpoint_is_optional": True},
        "selection_reason": "Closed W1 balance, independent cycle, age >= 101 calendar days",
        "data_coverage": {"timeframe": "W1", "closed_only": True}, "params": asdict(cfg),
        # Per-revision state prevents future scenarios/zones leaking into as_of.
        "scenario_state": {"depression": deepcopy(base.depression), "scenarios": deepcopy(base.scenarios),
                           "pd_zones": deepcopy(base.zones), "events": deepcopy(base.events)},
    }
    base.revisions.append(snapshot)


def _qualifies(c: Candidate, weeks: list[Week], i: int, cfg: RulesV3) -> bool:
    window = weeks[c.start_index:i + 1]
    width = c.upper - c.lower
    if weeks[i].available_at - c.start < cfg.min_base_days * DAY or width <= 0:
        return False
    if width / c.lower < cfg.min_rebound_fraction:
        return False
    closes = [w.close for w in window]
    path = sum(abs(a - b) for a, b in zip(closes, closes[1:]))
    efficiency = abs(closes[-1] - closes[0]) / path if path else 0
    containment = sum(c.lower <= v <= c.upper for v in closes) / len(closes)
    tolerance = cfg.reaction_tolerance_fraction * width
    lows = [j for j in range(c.start_index, i) if
            weeks[j].low <= c.lower + tolerance and
            (j == 0 or weeks[j].low < weeks[j - 1].low) and weeks[j].low <= weeks[j + 1].low]
    third = max(2, len(window) // 3)
    atr = median(p.high - p.low for p in window)
    center_shift = abs(median(closes[:third]) - median(closes[-third:]))
    recent_support = bool(lows) and lows[-1] >= c.start_index + len(window) // 2
    upper_confirmed = c.upper_day < weeks[i].t
    return (efficiency <= cfg.max_directional_efficiency and containment >= cfg.min_containment
            and len(lows) >= cfg.min_lower_reactions and recent_support and upper_confirmed
            and center_shift <= cfg.max_center_shift_atr * atr)


def _zones(base: Base, weeks: list[Week], i: int, scenario: dict) -> None:
    """Only the causal W1 leg that broke the protected maximum supplies PD."""
    begin = scenario["leg_start_index"]
    bos_i = scenario["bos_index"]
    w = weeks[i]
    # OB is the last opposite candle before the BOS displacement, full wick.
    if i == bos_i:
        opposite = next((j for j in range(i - 1, begin - 1, -1) if weeks[j].close < weeks[j].open), None)
        if opposite is not None:
            b = weeks[opposite]
            base.zones.append({"id": _id(base.key, scenario["id"], "OB", b.t), "kind": "OB", "lower": b.low, "upper": b.high,
                               "formed_at": b.t, "available_at_ms": w.available_at, "invalidated_at_ms": None,
                               "scenario_id": scenario["id"], "source_candles": [b.t], "timeframe": "W1"})
    # Include the FVG whose displacement candle is BOS, once candle three closes.
    # Earlier gaps within the same leg are available at BOS, never backdated.
    indices = range(max(begin + 2, 2), i + 1) if i == bos_i else [i]
    for j in indices:
        if j > bos_i + 1 or j - 2 < begin:
            continue
        a, b, c = weeks[j - 2:j + 1]
        if a.t + WEEK != b.t or b.t + WEEK != c.t or a.high >= c.low:
            continue
        zone_id = _id(base.key, scenario["id"], "FVG", a.t, c.t)
        if any(z["id"] == zone_id for z in base.zones):
            continue
        if any(p.close < a.high for p in weeks[j + 1:i + 1]):
            continue
        base.zones.append({"id": zone_id, "kind": "FVG", "lower": a.high, "upper": c.low,
                           "formed_at": c.t, "available_at_ms": max(scenario["bos_at_ms"], c.available_at),
                           "invalidated_at_ms": None, "scenario_id": scenario["id"],
                           "source_candles": [a.t, b.t, c.t], "timeframe": "W1"})


def _week_step(base: Base, weeks: list[Week], i: int, cfg: RulesV3) -> None:
    w = weeks[i]
    at = w.available_at
    changed = False
    # Local maxima need a right-hand closed W1; outside bars cannot both
    # introduce a new maximum and use it to explain an earlier low.
    if i >= 2 and weeks[i - 2].t + WEEK == weeks[i - 1].t and weeks[i - 1].t + WEEK == w.t:
        prev = weeks[i - 1]
        if prev.high > weeks[i - 2].high and prev.high >= w.high:
            base.latest_high = (prev.high, prev.t)
    mid = (base.lower + base.upper) / 2
    tol = (base.upper - base.lower) * cfg.midpoint_tolerance_fraction
    if min(abs(w.low - mid), abs(w.high - mid)) <= tol:
        base.midpoint_wicks += 1
        changed = True

    for scenario in base.scenarios:
        if scenario["cancelled_at_ms"] is None:
            _zones(base, weeks, i, scenario)
            if w.t >= scenario["bos_at_ms"] and w.close < scenario["minimum"]:
                scenario["cancelled_at_ms"] = at
                _event(base, "cancelled", at, w.close, candle=w.t, scenario_id=scenario["id"], level=scenario["minimum"], timeframe="W1")
                changed = True
        for zone in base.zones:
            if zone["scenario_id"] == scenario["id"] and zone["invalidated_at_ms"] is None and w.t >= zone["available_at_ms"] and w.close < zone["lower"]:
                zone["invalidated_at_ms"] = at
                _event(base, "zone_invalidated", at, w.close, candle=w.t, zone_id=zone["id"], kind=zone["kind"], timeframe="W1")
                changed = True

    if base.end is None:
        if base.depression is None and w.close <= base.upper and not any(s["cancelled_at_ms"] is None for s in base.scenarios):
            if w.close < base.lower:
                base.depression = {"id": _id(base.key, "depression", w.t), "started_at": w.t,
                                   "minimum": w.low, "minimum_at": w.low_day,
                                   "bos_level": base.latest_high[0] if base.latest_high else None,
                                   "bos_anchor": base.latest_high[1] if base.latest_high else None,
                                   "leg_start_index": i, "state": "pending_bos"}
                _event(base, "manipulation_started", at, w.low, candle=w.t, timeframe="W1")
                changed = True
            elif not base.scenarios and (w.low < base.lower or w.high > base.upper):
                # A wick followed by a W1 close inside the existing balance
                # is a boundary refinement, not a depression.
                if w.low < base.lower:
                    base.lower, base.lower_day = w.low, w.low_day
                if w.high > base.upper:
                    base.upper, base.upper_day = w.high, w.high_day
                _event(base, "boundary_refined", at, w.low, candle=w.t, timeframe="W1")
                changed = True
        elif base.depression is not None:
            dep = base.depression
            if w.low < dep["minimum"]:
                dep["minimum"], dep["minimum_at"] = w.low, w.low_day
                dep["leg_start_index"] = i
                if base.latest_high and base.latest_high[1] < w.t:
                    dep["bos_level"], dep["bos_anchor"] = base.latest_high
                changed = True
            if dep["bos_level"] is not None and w.close > dep["bos_level"] and w.t > dep["started_at"]:
                scenario = {**deepcopy(dep), "state": "confirmed", "bos_at_ms": at, "bos_index": i,
                            "cancelled_at_ms": None, "base_lower": base.lower, "base_upper": base.upper}
                base.scenarios.append(scenario)
                _event(base, "bos_confirmed", at, dep["bos_level"], candle=w.t, scenario_id=dep["id"], timeframe="W1", minimum=dep["minimum"])
                _zones(base, weeks, i, scenario)
                base.depression = None
                base.state = "active"
                changed = True
        if w.close > base.upper:
            base.end = w.t + WEEK - DAY
            base.end_confirmed = at
            base.state = "completed"
            _event(base, "breakout", at, w.close, candle=w.t, level=base.upper, timeframe="W1")
            changed = True
    if changed or (base.revisions and base.zones != base.revisions[-1]["scenario_state"]["pd_zones"]):
        _snapshot(base, at, "weekly_update", cfg)


def _daily_touches(base: Base, row: dict) -> None:
    t = row["open_time"]
    scenarios = [s for s in base.scenarios if s["bos_at_ms"] <= t and s["cancelled_at_ms"] is None]
    zones = [z for z in base.zones if z["available_at_ms"] <= t and z["invalidated_at_ms"] is None
             and any(s["id"] == z["scenario_id"] for s in scenarios)]
    if scenarios or base.end_confirmed is not None and base.end_confirmed <= t:
        zones += [{"id": base.key + ":" + side, "kind": "boundary", "lower": price, "upper": price} for side, price in (("L", base.lower), ("U", base.upper))]
    for z in zones:
        hit = row["low"] <= z["upper"] and row["high"] >= z["lower"]
        if hit and not base.contact.get(z["id"], False):
            _event(base, "retest", t + DAY, min(z["upper"], max(z["lower"], row["open"])), candle=t,
                   zone_id=z["id"], kind=z["kind"], zone={"lower": z["lower"], "upper": z["upper"]},
                   precision="D1", timeframe="W1")
        base.contact[z["id"]] = z["lower"] <= row["close"] <= z["upper"]


def analyze(rows: Sequence[Any], *, as_of_ms: int | None = None, computed_at_ms: int = 0,
            params: RulesV3 | None = None) -> dict:
    cfg = params or RulesV3()
    candles = normalize(rows, as_of_ms)
    weeks = aggregate_weeks(candles, as_of_ms)
    result = {"rules_version": RULES_VERSION, "computed_at_ms": computed_at_ms,
              "params": asdict(cfg), "structures": [], "status": "no_structure",
              "last_week_close_ms": weeks[-1].available_at if weeks else None}
    if len(weeks) < 3:
        result["status"] = "insufficient_history"
        return result
    gaps = [a["open_time"] + DAY for a, b in zip(candles, candles[1:]) if b["open_time"] != a["open_time"] + DAY]
    if gaps:
        result.update(status="data_pending", missing_after=gaps)
        return result
    bases: list[Base] = []
    candidates: list[Candidate] = []
    cycle_floor = -1
    by_close = {w.available_at: i for i, w in enumerate(weeks)}
    for row in candles:
        for base in bases:
            old_events = len(base.events)
            _daily_touches(base, row)
            if len(base.events) != old_events:
                _snapshot(base, row["open_time"] + DAY, "touch_observed", cfg)
        i = by_close.get(row["open_time"] + DAY)
        if i is None:
            continue
        w = weeks[i]
        for base in bases:
            _week_step(base, weeks, i, cfg)
            if base.end_confirmed == w.available_at:
                cycle_floor = max(cycle_floor, w.t)
        candidates = [c for c in candidates if c.start > cycle_floor]
        alive = []
        for c in candidates:
            if w.close < c.lower:
                continue  # a new lower stabilization must establish its own start
            if w.low < c.lower:
                c.lower, c.lower_day = w.low, w.low_day
            if w.high > c.upper:
                c.upper, c.upper_day = w.high, w.high_day
            alive.append(c)
        candidates = alive
        if i >= 2 and weeks[i - 1].t > cycle_floor and weeks[i - 1].low < weeks[i - 2].low and weeks[i - 1].low <= w.low:
            p = weeks[i - 1]
            candidates.append(Candidate(p.t, i - 1, p.low, max(p.high, w.high), p.low_day,
                                        p.high_day if p.high >= w.high else w.high_day))
        for c in list(candidates):
            if not _qualifies(c, weeks, i, cfg):
                continue
            # Internal consolidations do not replace a current outer base.
            overlap = [b for b in bases if b.end is None and c.lower < b.upper and c.upper > b.lower]
            if overlap:
                continue
            base = Base(_id(RULES_VERSION, c.start), c.start, c.lower, c.upper, c.lower_day, c.upper_day, w.available_at)
            for older in bases:
                if older.end is None and c.upper <= older.lower:
                    older.end = c.start - DAY
                    older.end_confirmed = w.available_at
                    older.state = "superseded"
                    _event(older, "cycle_superseded", w.available_at, c.lower, candle=w.t,
                           next_structure_key=base.key, timeframe="W1")
                    _snapshot(older, w.available_at, "independent_cycle", cfg)
            if i >= 2:
                highs = [p for j, p in enumerate(weeks[:i]) if j > 0 and p.high > weeks[j - 1].high and p.high >= weeks[j + 1].high]
                if highs:
                    base.latest_high = (highs[-1].high, highs[-1].t)
            _event(base, "mature_frozen", w.available_at, c.lower, candle=w.t, timeframe="W1")
            _snapshot(base, w.available_at, "base_mature", cfg)
            bases.append(base)
            # Competing starts describe the same observed epoch, not new
            # cycles becoming "new" months later after an upper breakout.
            candidates.clear()
            break
        candidates = candidates[-cfg.max_candidates:]
    for base in bases:
        revision = base.revisions[-1]
        result["structures"].append({"structure_key": base.key, "rules_version": RULES_VERSION,
            "state": base.state, "eligible": True, "geometry": {k: revision[k] for k in (
                "lower", "upper", "mid", "width", "base_start_open_time", "base_end_open_time", "available_at_ms")},
            "revisions": base.revisions, "children": [], "probes": [],
            "scenario_state": deepcopy(revision["scenario_state"])})
    if bases:
        result["status"] = "ok"
    return result
