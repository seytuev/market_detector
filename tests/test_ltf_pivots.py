"""§5.1: pivots H1 — подтверждение по 3 правым свечам, плато, ambiguous."""
from __future__ import annotations

from app.engine.ltf import confirmed_pivots, find_h1_pivots
from tests.conftest import H1_MS, make_h1_candles

T0 = 1_780_000_000_000


def _bars(hl: list[tuple[float, float]]) -> list[tuple[float, float, float, float]]:
    """(h, l) → (o, h, l, c) с телом посередине."""
    return [((h + l) / 2, h, l, (h + l) / 2) for h, l in hl]


def test_pivot_at_vs_confirmed_at():
    # строгий high-pivot на свече 3: highs 10,11,12,[13],12,11,10
    candles = make_h1_candles(
        _bars([(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]), T0
    )
    pivots = find_h1_pivots(candles, left=3, right=3)
    assert len(pivots) == 1
    p = pivots[0]
    assert p.kind == "high" and p.price == 13
    assert p.pivot_at == T0 + 3 * H1_MS
    # известен после закрытия свечи i+r = 6, а не в момент открытия i (§5.1)
    assert p.confirmed_at == candles[6].close_time
    # до подтверждения недоступен, после — доступен (приёмка п.6)
    assert confirmed_pivots(pivots, p.confirmed_at - 1) == []
    assert confirmed_pivots(pivots, p.confirmed_at) == [p]


def test_plateau_no_pivot():
    # равные пики (плато) pivot не образуют — строгие неравенства (§5.1)
    candles = make_h1_candles(
        _bars([(10, 9), (11, 9), (12, 9), (12, 9), (12, 9), (11, 9), (10, 9)]), T0
    )
    assert find_h1_pivots(candles, 3, 3) == []


def test_ambiguous_candle_is_both_high_and_low_pivot():
    # свеча 3 — одновременно high- и low-pivot (внешний бар)
    candles = make_h1_candles(
        _bars([(12, 10), (12, 10), (12, 10), (15, 5), (12, 10), (12, 10), (12, 10)]),
        T0,
    )
    pivots = find_h1_pivots(candles, 3, 3)
    assert {p.kind for p in pivots} == {"high", "low"}
    assert all(p.state == "ambiguous" for p in pivots)
    # ambiguous в сигналах не участвует (§3.5)
    assert confirmed_pivots(pivots, pivots[0].confirmed_at) == []


def test_only_closed_candles_and_window_edges():
    hl = [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]
    candles = make_h1_candles(_bars(hl), T0)
    # незакрытая правая свеча окна — pivot не подтверждается
    candles[6].closed = False
    assert find_h1_pivots(candles, 3, 3) == []
    # без полного правого окна (всего 5 свечей при 3+3) pivot не существует
    candles = make_h1_candles(_bars(hl[:5]), T0)
    assert find_h1_pivots(candles, 3, 3) == []
