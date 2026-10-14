"""Shared range queries for causal zone lifecycle projections.

Build once per closed H1 sample, then query each zone without scanning its
entire lifetime. The scalar zone_at path remains the reference implementation.
"""
from bisect import bisect_left
from dataclasses import replace

from ...models import Direction
from ..depth import reaches_depth
from .entries import test_depth_of
from .liquidity import resolve_sweep

HOUR = 3_600_000


class LifecycleIndex:
    def __init__(self, candles, as_of):
        self.rows = [c for c in candles if c.closed and c.close_time <= as_of]
        self.opens = [c.open_time for c in self.rows]
        self.as_of = as_of
        self.size = 1 << max(0, (len(self.rows) - 1).bit_length())
        self.low = [float('inf')] * (2 * self.size)
        self.high = [float('-inf')] * (2 * self.size)
        self.min_close = self.low.copy()
        self.max_close = self.high.copy()
        self.min_high = self.low.copy()
        self.max_low = self.high.copy()
        self.gaps = [0]
        for i, c in enumerate(self.rows):
            at = self.size + i
            self.low[at], self.high[at] = c.low, c.high
            self.min_close[at] = self.max_close[at] = c.close
            self.min_high[at], self.max_low[at] = c.high, c.low
            self.gaps.append(self.gaps[-1] + int(i > 0 and c.open_time != self.opens[i-1] + HOUR))
        for i in range(self.size - 1, 0, -1):
            self.low[i] = min(self.low[2*i], self.low[2*i+1])
            self.high[i] = max(self.high[2*i], self.high[2*i+1])
            self.min_close[i] = min(self.min_close[2*i], self.min_close[2*i+1])
            self.max_close[i] = max(self.max_close[2*i], self.max_close[2*i+1])
            self.min_high[i] = min(self.min_high[2*i], self.min_high[2*i+1])
            self.max_low[i] = max(self.max_low[2*i], self.max_low[2*i+1])

    def tests(self, lower, upper, direction, after_open):
        """First interval touch and deepest touching wick; a gap is no touch."""
        from bisect import bisect_right
        start = bisect_right(self.opens, after_open)
        bull = direction == Direction.BULL
        first = extreme = None
        def visit(node, left, right):
            nonlocal first, extreme
            if right <= start or left >= len(self.rows) or self.high[node] < lower or self.low[node] > upper:
                return
            if left >= start and right <= len(self.rows) and self.min_high[node] >= lower and self.max_low[node] <= upper:
                if first is None:
                    first = self.opens[left]
                value = self.low[node] if bull else self.high[node]
                extreme = value if extreme is None else (min(extreme, value) if bull else max(extreme, value))
                return
            if right - left > 1:
                mid = (left + right) // 2
                visit(node*2, left, mid); visit(node*2+1, mid, right)
        visit(1, 0, self.size)
        return first, extreme

    def extreme(self, start, bull):
        tree, pick = (self.low, min) if bull else (self.high, max)
        result = float('inf') if bull else float('-inf')
        left, right = start + self.size, len(self.rows) + self.size
        while left < right:
            if left & 1:
                result = pick(result, tree[left]); left += 1
            if right & 1:
                right -= 1; result = pick(result, tree[right])
            left //= 2; right //= 2
        return result

    def first(self, start, possible):
        def visit(node, left, right):
            if right <= start or left >= len(self.rows) or not possible(node):
                return None
            if right - left == 1:
                return left
            mid = (left + right) // 2
            found = visit(node * 2, left, mid)
            return found if found is not None else visit(node * 2 + 1, mid, right)
        return visit(1, 0, self.size)

    def project(self, zone, cfg):
        from .relevance import relevance_reason
        evidence = dict(zone.evidence)
        for key in ('lifecycle_reason', 'excluded_at'):
            evidence.pop(key, None)
        z = replace(zone, first_test_at=None, max_test_depth=0.0,
                    test_extreme=None, validity='fresh', evidence=evidence)
        if zone.confirmed_at is None or zone.confirmed_at > self.as_of:
            return z, dict(relevant=False, reason='unconfirmed', data_quality='pending', excluded_at=None)
        start = zone.formed_at + HOUR if zone.is_level else zone.confirmed_at
        if start % HOUR == HOUR - 1:
            start += 1
        pos = bisect_left(self.opens, start)
        n = len(self.rows)
        gap = ((pos < n and self.opens[pos] != start)
               or self.gaps[n] - self.gaps[min(pos + 1, n)] > 0)
        expected = self.opens[-1] + HOUR if pos < n else start
        gap = gap or expected + HOUR - 1 <= self.as_of
        excluded = reason = None
        if z.is_level:
            bsl = z.type == 'BSL'
            touch = self.first(pos, lambda i: self.high[i] >= z.lower if bsl else self.low[i] <= z.lower)
            crossed = self.first(pos, lambda i: (
                (self.high[i] > z.lower if bsl else self.low[i] < z.lower)
                and (self.min_close[i] != z.lower or self.max_close[i] != z.lower)))
            if crossed is not None:
                outcome = resolve_sweep(z.type, z.lower, self.rows[crossed])
                reason = 'swept_level' if outcome == 'confirmed' else 'level_broken'
                z.evidence['lifecycle_reason'] = reason
                excluded = self.rows[crossed].close_time
        else:
            bull = z.direction == Direction.BULL
            touch = self.first(pos, lambda i: self.low[i] <= z.upper if bull else self.high[i] >= z.lower)
            if touch is not None:
                z.test_extreme = self.extreme(pos, bull)
                z.max_test_depth = test_depth_of(z, z.test_extreme)
                threshold = min(cfg.entry_reuse_max_depth, 1.0) if z.type == 'FVG' else cfg.entry_reuse_max_depth
                crossed = self.first(touch, lambda i: reaches_depth(
                    z, self.low[i] if bull else self.high[i], threshold))
                if crossed is not None:
                    excluded = self.rows[crossed].close_time
        if touch is not None:
            z.first_test_at = self.rows[touch].close_time
            z.validity = 'tested'
        reason = relevance_reason(z, cfg) or reason
        invalid_at = zone.evidence.get('invalidated_at')
        if zone.validity == 'invalid' and (invalid_at is None or invalid_at <= self.as_of):
            z.validity, reason = 'invalid', 'invalid'
            excluded = invalid_at or excluded
        z.evidence.update(lifecycle_reason=reason, excluded_at=excluded)
        return z, dict(relevant=reason is None and not gap,
                       reason=reason or ('data_gap' if gap else 'ok'),
                       data_quality='gap' if gap else 'complete', excluded_at=excluded)
