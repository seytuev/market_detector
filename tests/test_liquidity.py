"""§7: SSL/BSL — pivot 3+3, кластеризация 2%, частичное снятие группы (§13.4, §13.17)."""
from __future__ import annotations

from app.engine.liquidity import cluster_prices, find_pivots
from app.engine.scanner import Scanner
from app.models import EventKind, TIMEFRAME_MINUTES, ZoneStatus, ZoneType

from .conftest import make_candle

D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000
T0 = 1780272000000


def _pivot_candles(center_high=100.0, n=7, closed_last=True):
    """7 свечей: центр (idx 3) со строгим максимумом, остальные ниже."""
    candles = []
    for i in range(n):
        h = center_high if i == 3 else 95.0 + i * 0.1
        candles.append(make_candle(
            T0 + i * D1_MS, 90.0, h, 89.0, 91.0, "D1",
            closed=(i < n - 1 or closed_last),
        ))
    return candles


def test_pivot_not_exists_before_third_right_candle(cfg):
    # только 2 правые свечи после центра — пивота для алгоритма нет (§7, §13.4)
    assert find_pivots(_pivot_candles(n=6), "D1", cfg) == []
    # 3 правые свечи — пивот появляется, confirmed_at = граница 3-й правой
    pivots = find_pivots(_pivot_candles(n=7), "D1", cfg)
    assert len(pivots) == 1
    p = pivots[0]
    assert p.kind == ZoneType.BSL and p.price == 100.0
    assert p.formed_at == T0 + 3 * D1_MS
    assert p.confirmed_at == T0 + 7 * D1_MS


def test_pivot_scanner_gating(db, cfg, instrument_id):
    scanner = Scanner(db, cfg)
    for c in _pivot_candles(n=6):
        scanner.on_closed_candle(c)
    assert db.get_zones(instrument_id, types=[ZoneType.BSL]) == []
    scanner.on_closed_candle(make_candle(T0 + 6 * D1_MS, 90.0, 95.6, 89.0, 91.0, "D1"))
    zones = db.get_zones(instrument_id, types=[ZoneType.BSL])
    assert len(zones) == 1
    assert zones[0].confirmed_at == T0 + 7 * D1_MS


def test_ssl_pivot(cfg):
    candles = []
    for i in range(7):
        l = 90.0 if i == 3 else 95.0 + i * 0.1
        candles.append(make_candle(T0 + i * D1_MS, 100.0, 101.0, l, 100.5, "D1"))
    pivots = find_pivots(candles, "D1", cfg)
    assert len(pivots) == 1
    assert pivots[0].kind == ZoneType.SSL and pivots[0].price == 90.0


def test_cluster_tolerance_and_no_chain():
    # 2% допуск по всей группе: 100 и 101.5 вместе, 104 — отдельно
    groups = cluster_prices([100.0, 101.5, 104.0], 0.02)
    assert sorted(sorted(g) for g in groups) == [[0, 1], [2]]
    # без цепочного расширения: 103.7 близко к 101.9, но не к диапазону всей группы
    groups = cluster_prices([100.0, 101.9, 103.7], 0.02)
    assert sorted(sorted(g) for g in groups) == [[0, 1], [2]]


def _two_bsl_group_candles():
    """Два BSL-пивота одной группы: 100.0 (idx 3) и 101.5 (idx 7); прочие High ниже."""
    highs = {3: 100.0, 7: 101.5}
    candles = []
    for i in range(11):
        h = highs.get(i, 90.0 + i * 0.05)
        candles.append(make_candle(T0 + i * D1_MS, 85.0, h, 84.0, 86.0, "D1"))
    return candles


def test_partial_take_keeps_extreme_active(db, cfg, instrument_id):
    """§7/§13.17: снятие части группы не деактивирует непересечённую крайнюю
    точку; снятые участники не реактивируются при перестроении группы."""
    scanner = Scanner(db, cfg)
    for c in _two_bsl_group_candles():
        scanner.on_closed_candle(c)
    zones = db.get_zones(instrument_id, types=[ZoneType.BSL])
    assert len(zones) == 2
    low_member = next(z for z in zones if z.lower == 100.0)
    extreme = next(z for z in zones if z.lower == 101.5)
    assert extreme.evidence["is_extreme"] is True
    assert low_member.evidence["group_id"] == extreme.evidence["group_id"]
    assert extreme.evidence["group_level"] == 101.5

    # свеча пересекает только нижнего участника (100.0 < high < 101.5)
    scanner.on_closed_candle(make_candle(T0 + 11 * D1_MS, 99.0, 101.0, 98.0, 100.5, "D1"))
    low_member = db.get_zone(low_member.id)
    extreme = db.get_zone(extreme.id)
    assert low_member.status == ZoneStatus.TAKEN
    assert extreme.status == ZoneStatus.ACTIVE
    assert extreme.evidence["part_taken"] is True  # качественный контекст, без баллов
    events = db.get_events(low_member.id)
    assert [e.kind for e in events] == [EventKind.LEVEL_TAKEN]

    # повторная обработка/перестроение не реактивирует снятый уровень (§13.4)
    scanner.on_closed_candle(make_candle(T0 + 12 * D1_MS, 100.0, 100.8, 99.0, 100.2, "D1"))
    assert db.get_zone(low_member.id).status == ZoneStatus.TAKEN
    assert db.get_zone(extreme.id).status == ZoneStatus.ACTIVE

    # снятие крайней точки — тоже TAKEN
    scanner.on_closed_candle(make_candle(T0 + 13 * D1_MS, 100.0, 102.0, 99.0, 101.8, "D1"))
    assert db.get_zone(extreme.id).status == ZoneStatus.TAKEN
