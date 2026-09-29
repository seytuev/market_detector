"""§3: FVG — бычий/медвежий, равенство границ, подтверждение 3-й свечой, эталон."""
from __future__ import annotations

from app.engine.fvg import scan_fvgs
from app.models import Direction, TIMEFRAME_MINUTES

from .conftest import (
    ETALON_FVG_EXTERNAL,
    ETALON_FVG_INTERNAL_1,
    ETALON_FVG_INTERNAL_2,
    load_etalon_candles,
    make_candle,
)

H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
T0 = 1780272000000  # 2026-06-01 00:00 UTC


def test_bull_fvg():
    candles = [
        make_candle(T0 + 0 * H4_MS, 99.0, 100.0, 98.0, 99.5, "H4"),
        make_candle(T0 + 1 * H4_MS, 99.5, 102.0, 99.0, 101.5, "H4"),
        make_candle(T0 + 2 * H4_MS, 101.5, 103.0, 101.0, 102.5, "H4"),
    ]
    fvgs = scan_fvgs(candles, "H4")
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.direction == Direction.BULL
    assert (f.lower, f.upper) == (100.0, 101.0)  # L=High₁, U=Low₃
    assert f.formed_at == T0 + 2 * H4_MS          # open_time 3-й свечи
    assert f.confirmed_at == T0 + 3 * H4_MS       # граница следующего интервала
    assert f.candle_open_times == (T0, T0 + H4_MS, T0 + 2 * H4_MS)


def test_bear_fvg():
    candles = [
        make_candle(T0 + 0 * H4_MS, 102.0, 103.0, 101.0, 102.5, "H4"),
        make_candle(T0 + 1 * H4_MS, 102.5, 102.8, 99.0, 99.5, "H4"),
        make_candle(T0 + 2 * H4_MS, 99.5, 100.0, 98.0, 98.5, "H4"),
    ]
    fvgs = scan_fvgs(candles, "H4")
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.direction == Direction.BEAR
    assert (f.lower, f.upper) == (100.0, 101.0)  # L=High₃, U=Low₁


def test_equal_bounds_no_fvg():
    # High₁ == Low₃ — равенство границ не создаёт ненулевой FVG (§3)
    bull_eq = [
        make_candle(T0 + 0 * H4_MS, 99.0, 100.0, 98.0, 99.5, "H4"),
        make_candle(T0 + 1 * H4_MS, 99.5, 101.0, 99.0, 100.5, "H4"),
        make_candle(T0 + 2 * H4_MS, 100.5, 102.0, 100.0, 101.5, "H4"),
    ]
    assert scan_fvgs(bull_eq, "H4") == []
    # Low₁ == High₃ — зеркально
    bear_eq = [
        make_candle(T0 + 0 * H4_MS, 102.0, 103.0, 101.0, 102.5, "H4"),
        make_candle(T0 + 1 * H4_MS, 102.5, 102.8, 100.5, 101.0, "H4"),
        make_candle(T0 + 2 * H4_MS, 101.0, 101.0, 99.0, 99.5, "H4"),
    ]
    assert scan_fvgs(bear_eq, "H4") == []


def test_no_confirmation_before_third_candle_closes():
    # до закрытия третьей свечи FVG для алгоритма не существует (§3, §13.1)
    candles = [
        make_candle(T0 + 0 * H4_MS, 99.0, 100.0, 98.0, 99.5, "H4"),
        make_candle(T0 + 1 * H4_MS, 99.5, 102.0, 99.0, 101.5, "H4"),
        make_candle(T0 + 2 * H4_MS, 101.5, 103.0, 101.0, 102.5, "H4", closed=False),
    ]
    assert scan_fvgs(candles, "H4") == []
    candles[2].closed = True
    assert len(scan_fvgs(candles, "H4")) == 1


def test_etalon_three_bear_fvgs():
    """Эталон §13.19: три медвежьих FVG после базы, точные границы и formed_at."""
    fvgs = scan_fvgs(load_etalon_candles(), "H4")
    bear = {(round(f.lower, 2), round(f.upper, 2), f.formed_at): f for f in fvgs
            if f.direction == Direction.BEAR}
    for expected in (ETALON_FVG_INTERNAL_1, ETALON_FVG_INTERNAL_2, ETALON_FVG_EXTERNAL):
        assert expected in bear, f"FVG {expected} не найден"
    ext = bear[(ETALON_FVG_EXTERNAL[0], ETALON_FVG_EXTERNAL[1], ETALON_FVG_EXTERNAL[2])]
    # подтверждение — на границе 2026-06-04 04:00 UTC
    assert ext.confirmed_at == 1780545600000
    assert ext.candle_open_times == (1780502400000, 1780516800000, 1780531200000)
