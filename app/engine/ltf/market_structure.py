"""Close-driven BOS/SMS, shared by H1, D1 and W1.

A frame retains its main extreme until a continuation close breaks it.
The first confirmed opposite pivot after that extreme is its SMS level.
An SMS opens an opposite child frame; the parent's range remains available
for continuation until its origin is broken by a close.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ...models import Direction
from .pivots import PivotCandidate

RULE_VERSION = "structure-2026-10-14"


def opposite(direction):
    return Direction.BEAR if direction == Direction.BULL else Direction.BULL


def crosses(direction, close, level):
    return close > level if direction == Direction.BULL else close < level


@dataclass
class Frame:
    direction: Direction
    origin: PivotCandidate
    extreme: PivotCandidate | None
    started_at: int
    internal: PivotCandidate | None = None
    shifted: bool = False
    continuation_at: int | None = None
    correction_since: int | None = None
    regime: str = "BOS"
    endpoint_level: float | None = None
    observed_price: float | None = None
    forming: bool = False


class StructureMachine:
    def __init__(self):
        self.frames: list[Frame] = []
        self.last: dict[str, PivotCandidate] = {}
        self.roles: dict[tuple, str] = {}
        self.fired: set[str] = set()
        self.seen: set[tuple] = set()
        self.last_close_at = None
        self.last_event = None

    @property
    def direction(self):
        return self.frames[-1].direction if self.frames else None

    def _role(self, pivot, role):
        self.roles[(pivot.kind, pivot.pivot_at)] = role
        return replace(pivot, role=role)

    def absorb(self, pivot):
        key = (pivot.kind, pivot.pivot_at, pivot.confirmed_at, pivot.price)
        if pivot.state != "confirmed" or key in self.seen:
            return
        self.seen.add(key)
        previous = self.last.get(pivot.kind)
        other = self.last.get("low" if pivot.kind == "high" else "high")
        self.roles[(pivot.kind, pivot.pivot_at)] = "none"
        if not self.frames and previous is not None and other is not None:
            direction = Direction.BULL if pivot.kind == "high" else Direction.BEAR
            if crosses(direction, pivot.price, previous.price):
                # Initial confirmed pair supplies context, not a retrospective
                # event. Once seeded, only candle closes can move a main level.
                extreme = self._role(pivot, "HH" if direction == Direction.BULL else "LL")
                origin = self._role(other, "HL" if direction == Direction.BULL else "LH")
                self.frames = [Frame(direction, origin, extreme, pivot.pivot_at)]
        if self.frames:
            frame = self.frames[-1]
            kind = "high" if frame.direction == Direction.BULL else "low"
            if (frame.extreme is None and pivot.kind == kind and pivot.pivot_at > frame.started_at
                    and (frame.endpoint_level is None or crosses(frame.direction, pivot.price, frame.endpoint_level))
                    and (frame.observed_price is None or pivot.price == frame.observed_price
                         or crosses(frame.direction, pivot.price, frame.observed_price))):
                frame.extreme = self._role(pivot, "HH" if kind == "high" else "LL")
                if (frame.regime == "BOS" and other is not None
                        and frame.started_at < other.pivot_at < pivot.pivot_at):
                    frame.origin = self._role(other, "HL" if kind == "high" else "LH")
            elif frame.extreme is None and pivot.kind != kind:
                self._role(pivot, "HL" if kind == "high" else "LH")
                if (frame.correction_since is not None and pivot.pivot_at > frame.correction_since and frame.internal is None
                        and crosses(frame.direction, pivot.price, frame.origin.price)):
                    frame.internal = self._role(pivot, "HL" if kind == "high" else "LH")
            elif frame.extreme is not None and pivot.pivot_at > frame.extreme.pivot_at:
                if pivot.kind != kind:
                    frame.forming = False
                    role = "HL" if kind == "high" else "LH"
                    internal = self._role(pivot, role)
                    if frame.internal is None and crosses(frame.direction, pivot.price, frame.origin.price):
                        frame.internal = internal
                else:
                    if frame.forming and frame.internal is None and crosses(frame.direction, pivot.price, frame.extreme.price):
                        # The endpoint of an uninterrupted BOS/SMS impulse is
                        # still forming. Lock the main extreme at its first
                        # confirmed correction, not at an intermediate peak.
                        self._role(frame.extreme, "internal_high" if kind == "high" else "internal_low")
                        frame.extreme = self._role(pivot, "HH" if kind == "high" else "LL")
                    else:
                        self._role(pivot, "LH" if kind == "high" else "HL")
        self.last[pivot.kind] = pivot

    def _event(self, kind, direction, pivot, frame, candle, now_ms, depth, stage="primary"):
        from .breaks import StructureEventDraft, _level_key, _ref
        key = _level_key(kind, stage, pivot)
        if key in self.fired:
            return None
        self.fired.add(key)
        reversal = direction != frame.direction
        origin = frame.extreme if reversal else frame.origin
        if not reversal:
            correction = self.last.get("low" if direction == Direction.BULL else "high")
            if correction and frame.extreme and correction.pivot_at > frame.extreme.pivot_at:
                origin = correction
        refs = [origin, pivot] if origin is not None else [pivot]
        parent = frame if reversal and kind == "SMS" else self.frames[depth - 1] if depth else None
        event = StructureEventDraft(
            kind=kind, stage=stage, direction=direction,
            break_level=pivot.price, break_candle_open_time=candle.open_time,
            occurred_at=candle.close_time, detected_at=now_ms,
            level_key=key, ref_pivot_ids=[_ref(p) for p in refs],
            evidence={
                "rule_version": RULE_VERSION, "broken_pivot_id": _ref(pivot),
                "role_at_event": pivot.role, "level_price": pivot.price,
                "pivot_confirmed_at": pivot.confirmed_at,
                "candle_close_time": candle.close_time, "close_price": candle.close,
                "close": candle.close, "anchor_price": origin.price if origin else None,
                "ref_price": frame.origin.price, "structure_depth": depth,
                "origin_pivot_id": _ref(origin) if origin else None,
                "parent_direction": parent.direction.value if parent else None,
                "parent_origin_price": parent.origin.price if parent else None,
                "parent_extreme_price": parent.extreme.price if parent and parent.extreme else None,
            },
        )
        self.last_event = event
        return event

    def on_candle(self, candle, now_ms):
        if not candle.closed or candle.close_time > now_ms:
            return []
        if self.last_close_at is not None and candle.close_time <= self.last_close_at:
            return []
        self.last_close_at = candle.close_time
        for pending in self.frames:
            if pending.extreme is None:
                price = getattr(candle, "high" if pending.direction == Direction.BULL else "low", candle.close)
                if pending.observed_price is None or crosses(pending.direction, price, pending.observed_price):
                    pending.observed_price = price
        if not self.frames:
            high, low = self.last.get("high"), self.last.get("low")
            if high is None or low is None:
                return []
            direction = (Direction.BULL if candle.close > high.price else
                         Direction.BEAR if candle.close < low.price else None)
            if direction is None:
                return []
            level = high if direction == Direction.BULL else low
            origin = low if direction == Direction.BULL else high
            level = self._role(level, "HH" if direction == Direction.BULL else "LL")
            origin = self._role(origin, "HL" if direction == Direction.BULL else "LH")
            frame = Frame(direction, origin, level, level.pivot_at)
            self.frames.append(frame)
            event = self._event("BOS", direction, level, frame, candle, now_ms, 0)
            frame.extreme = None
            frame.forming = True
            frame.endpoint_level = level.price
            frame.observed_price = getattr(candle, "high" if direction == Direction.BULL else "low", candle.close)
            return [event] if event else []
        # Only the main BOS frame owns BOS boundaries. All nested ranges
        # remain SMS, however many internal pivots or closes they contain.
        for depth in range(len(self.frames) - 1, -1, -1):
            frame = self.frames[depth]
            direction = opposite(frame.direction)
            if depth == 0 and crosses(direction, candle.close, frame.origin.price):
                level = self._role(frame.origin, "HL" if frame.direction == Direction.BULL else "LH")
                event = self._event("BOS", direction, level, frame, candle, now_ms, depth)
                if event:
                    origin = frame.extreme or self.last.get("high" if direction == Direction.BEAR else "low")
                    correction = self.last.get("high" if direction == Direction.BEAR else "low")
                    if correction:
                        origin = correction
                    if origin:
                        prefix = self.frames[:depth]
                        if prefix and prefix[-1].direction == direction and prefix[-1].extreme and prefix[-1].extreme.price == level.price:
                            prefix = prefix[:-1]
                        self.frames = prefix + [Frame(direction, origin, None, level.pivot_at,
                                                      endpoint_level=level.price,
                                                      observed_price=getattr(candle, "high" if direction == Direction.BULL else "low", candle.close), forming=True)]
                        seed = self.last.get("high" if direction == Direction.BULL else "low")
                        if seed and seed.pivot_at > level.pivot_at and crosses(direction, seed.price, level.price):
                            self.frames[-1].extreme = self._role(seed, "HH" if direction == Direction.BULL else "LL")
                    self._drop_invalid_parents(candle.close)
                    return [event]
        # An outer BOS continuation can end a nested correction. An SMS
        # frame's internal continuation keeps its type and does not reopen
        # a scenario or reset the external origin.
        for depth, frame in enumerate(self.frames):
            correction = self.last.get("low" if frame.direction == Direction.BULL else "high")
            if (frame.extreme and correction and correction.pivot_at > frame.extreme.pivot_at
                    and crosses(frame.direction, candle.close, frame.extreme.price)):
                kind = frame.regime
                stage = "secondary" if depth == len(self.frames) - 1 else "primary"
                event = self._event(kind, frame.direction, frame.extreme, frame, candle, now_ms, depth, stage)
                if event is None:
                    continue
                self.frames = self.frames[:depth + 1]
                correction = self.last.get("low" if frame.direction == Direction.BULL else "high")
                if kind == "BOS" and correction and correction.pivot_at > frame.extreme.pivot_at:
                    frame.origin = self._role(correction, "HL" if frame.direction == Direction.BULL else "LH")
                previous_extreme = frame.extreme
                frame.extreme, frame.internal, frame.shifted = None, None, False
                frame.started_at = previous_extreme.pivot_at
                frame.endpoint_level = previous_extreme.price
                frame.observed_price = getattr(candle, "high" if frame.direction == Direction.BULL else "low", candle.close)
                frame.forming = True
                frame.continuation_at = candle.close_time
                frame.correction_since = None
                return [event]
        frame = self.frames[-1]
        if frame.extreme and not frame.shifted:
            direction = opposite(frame.direction)
            level = frame.internal or frame.origin
            if crosses(direction, candle.close, level.price):
                event = self._event("SMS", direction, level, frame, candle, now_ms, len(self.frames) - 1)
                frame.shifted = True
                child = Frame(direction, frame.extreme, None, candle.open_time,
                              correction_since=level.pivot_at, regime="SMS",
                              endpoint_level=level.price,
                              observed_price=getattr(candle, "high" if direction == Direction.BULL else "low", candle.close), forming=True)
                child.started_at = level.pivot_at
                candidate = self.last.get("low" if direction == Direction.BULL else "high")
                if (candidate and candidate.pivot_at > level.pivot_at
                        and crosses(direction, candidate.price, child.origin.price)):
                    child.internal = self._role(candidate, "HL" if direction == Direction.BULL else "LH")
                self.frames.append(child)
                self._drop_invalid_parents(candle.close)
                return [event] if event else []
        self._drop_invalid_parents(candle.close)
        return []

    def _drop_invalid_parents(self, close):
        # Crossing an old origin invalidates that retained range; it is not
        # a second event at an arbitrary outer boundary.
        self.frames = [f for f in self.frames[:-1]
                       if not crosses(opposite(f.direction), close, f.origin.price)] + self.frames[-1:]

    def armed(self, direction):
        levels = []
        for frame in self.frames:
            if frame is self.frames[0] and frame.direction != direction:
                levels.append(("BOS", "primary", frame.origin))
            if frame.direction == direction and frame.extreme:
                levels.append((frame.regime, "secondary" if frame.regime == "SMS" else "primary", frame.extreme))
        if self.frames:
            frame = self.frames[-1]
            if frame.direction != direction and frame.extreme and not frame.shifted and frame.internal:
                levels.append(("SMS", "primary", frame.internal or frame.origin))
        return levels

    def condition(self, direction, kind):
        levels = [(stage, p) for k, stage, p in self.armed(direction) if k == kind]
        stage, pivot = levels[-1] if levels else ("primary", None)
        occurred = (self.last_event if pivot is None and self.last_event
                    and self.last_event.kind == kind and self.last_event.direction == direction else None)
        if occurred:
            return {
                "kind": kind, "stage": occurred.stage, "timeframe": "H1",
                "requires_close": True, "strict": True,
                "side": "above" if direction == Direction.BULL else "below",
                "source": "structure_machine", "rule_version": RULE_VERSION,
                "status": "occurred", "level": occurred.break_level,
                "occurred_at": occurred.occurred_at, "opens_scenario": False,
                "pivot": {"id": occurred.evidence["broken_pivot_id"],
                          "price": occurred.break_level,
                          "confirmed_at": occurred.evidence["pivot_confirmed_at"],
                          "role": occurred.evidence["role_at_event"]},
            }
        return {
            "kind": kind, "stage": stage, "timeframe": "H1",
            "requires_close": True, "strict": True,
            "side": "above" if direction == Direction.BULL else "below",
            "source": "structure_machine", "rule_version": RULE_VERSION,
            "status": "ready" if pivot else "unavailable",
            "level": pivot.price if pivot else None,
            "pivot": ({"id": pivot.pivot_id, "price": pivot.price,
                       "pivot_at": pivot.pivot_at, "confirmed_at": pivot.confirmed_at,
                       "role": pivot.role} if pivot else None),
            "missing": None if pivot else "нет подтверждённой структурной опоры",
            "opens_scenario": pivot is not None and stage == "primary",
            "continues_structure": pivot is not None and stage == "secondary",
        }

    def main_structure(self):
        if not self.frames:
            return None
        frame = self.frames[0]
        endpoint = frame.extreme.price if frame.extreme else frame.observed_price
        lower, upper = (sorted((frame.origin.price, endpoint)) if endpoint is not None else (None, None))
        return {
            "rule_version": RULE_VERSION, "as_of": self.last_close_at,
            "direction": frame.direction.value, "kind": "BOS",
            "origin": frame.origin.price, "origin_at": frame.origin.pivot_at,
            "extreme": endpoint, "extreme_at": frame.extreme.pivot_at if frame.extreme else None,
            "range_status": "confirmed" if frame.extreme else "provisional",
            "lower": lower, "upper": upper,
            "eq": (lower + upper) / 2 if lower is not None else None,
            "reverse_bos_level": frame.origin.price,
            "continuation_bos_level": frame.extreme.price if frame.extreme else None,
            "local_direction": self.direction.value,
        }


def replay_structure(pivots, candles, as_of):
    machine = StructureMachine()
    pending = sorted((p for p in pivots if p.state == "confirmed" and p.confirmed_at <= as_of),
                     key=lambda p: (p.confirmed_at, p.pivot_at, p.kind))
    index, events = 0, []
    for candle in sorted((c for c in candles if c.closed and c.close_time <= as_of), key=lambda c: c.open_time):
        while index < len(pending) and pending[index].confirmed_at <= candle.close_time:
            machine.absorb(pending[index])
            index += 1
        events.extend(machine.on_candle(candle, as_of))
    while index < len(pending):
        machine.absorb(pending[index])
        index += 1
    return machine, events
