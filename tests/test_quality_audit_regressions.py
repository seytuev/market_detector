"""Регрессии семи воспроизведений аудита 08.10.2026 (пакет A01–A03, A06, A07).

Цепочка кластеров alt v2 (A04) здесь только зафиксирована как известный
дефект: геометрию план велит менять после нового отчёта измерителя.
"""
from __future__ import annotations

import pytest

from app.alt.engine_v2 import cluster_pivots
from app.config import DetectorConfig
from app.engine.fvg import scan_fvgs
from app.engine.liquidity import PivotRecord, find_pivots
from app.engine.ltf.context import find_htf_fvg50_test
from app.engine.ltf.pivots import find_h1_pivots
from app.models import Candle, Direction, Zone, ZoneStatus, ZoneType
from app.models_alt import AltRangeEpisode
from app.services.alt_overview import select_current_episode
from app.services.htf_parent import parent_decision

DAY = 86_400_000
H1 = 3_600_000
T0 = 1_767_225_600_000  # 2026-01-01 UTC


def _candle(tf: str, open_time: int, step: int, o, h, l, c) -> Candle:
    return Candle(1, tf, open_time, open_time + step - 1, o, h, l, c)


def test_fvg_does_not_cross_a_missing_day():
    candles = [
        _candle("D1", T0 + d * DAY, DAY, o, h, low, c)
        for d, o, h, low, c in (
            (0, 10, 11, 9, 10), (1, 11, 13, 10, 12), (4, 14, 16, 13, 15),
        )
    ]
    assert scan_fvgs(candles, "D1") == []


def test_h1_pivot_window_stops_at_a_gap():
    step = H1
    gapped = [
        _candle("H1", T0 + n * step, step, 10, 20 if n == 2 else 11, 9, 10)
        for n in (0, 1, 2, 4, 5, 6)
    ]
    assert find_h1_pivots(gapped, left=1, right=1) == []
    solid = [
        _candle("H1", T0 + n * step, step, 10, 20 if n == 2 else 11, 9, 10)
        for n in range(5)
    ]
    assert find_h1_pivots(solid, left=1, right=1)


def test_htf_pivot_window_stops_at_a_gap():
    cfg = DetectorConfig()
    gapped = [
        _candle("D1", T0 + d * DAY, DAY, 10, 11, 9 if d != 3 else 8, 10)
        for d in (0, 1, 2, 3, 5, 6, 7)
    ]
    assert find_pivots(gapped, "D1", cfg) == []


def test_fvg_context_waits_for_confirmation(db, instrument_id):
    db.insert_zone(Zone(
        None, instrument_id, ZoneType.FVG, Direction.BULL, "D1",
        10, 12, T0, T0 + DAY, ZoneStatus.ACTIVE,
    ))
    before = find_htf_fvg50_test(
        db, instrument_id, 10.5, Direction.BEAR,
        as_of=T0 + DAY // 2, event_time=T0 + DAY // 2,
    )
    assert before is None
    at_close = find_htf_fvg50_test(
        db, instrument_id, 10.5, Direction.BEAR,
        as_of=T0 + DAY, event_time=T0 + DAY,
    )
    assert at_close is not None and at_close["confirmed"] is True


def test_later_archive_does_not_rewrite_history(db, instrument_id):
    zid = db.insert_zone(Zone(
        None, instrument_id, ZoneType.FVG, Direction.BULL, "D1",
        10, 12, T0, T0 + DAY, ZoneStatus.ACTIVE,
    ))
    as_of = T0 + 2 * DAY
    before = find_htf_fvg50_test(
        db, instrument_id, 10.5, Direction.BEAR, as_of=as_of, event_time=as_of,
    )
    db.update_zone(zid, status=ZoneStatus.ARCHIVED)
    after = find_htf_fvg50_test(
        db, instrument_id, 10.5, Direction.BEAR, as_of=as_of, event_time=as_of,
    )
    assert before == after and before is not None
    db.update_zone(zid, display_until=as_of)
    ended = find_htf_fvg50_test(
        db, instrument_id, 10.5, Direction.BEAR, as_of=as_of, event_time=as_of,
    )
    assert ended is None


def test_needs_replay_is_visible_and_not_trusted():
    zone = Zone(
        None, 1, ZoneType.OB, Direction.BULL, "D1", 10, 12,
        T0, T0 + DAY, ZoneStatus.ACTIVE, needs_replay=True,
    )
    decision = parent_decision(zone, DetectorConfig(), T0 + 2 * DAY)
    assert decision.eligible is True
    assert decision.reason == "needs_replay"
    assert decision.history_trusted is False


def test_manual_future_confirmation_is_not_available_early():
    zone = Zone(
        None, 1, ZoneType.OB, Direction.BULL, "D1", 10, 12,
        T0, T0 + 10 * DAY, ZoneStatus.ACTIVE, source="manual",
    )
    decision = parent_decision(zone, DetectorConfig(), T0 + DAY)
    assert decision.eligible is False
    assert decision.reason == "confirmation_not_yet"


def test_manual_known_at_blocks_replay_before_the_mark():
    known = T0 + 50 * DAY
    zone = Zone(
        None, 1, ZoneType.MANUAL, Direction.BULL, "D1",
        80, 82, T0, None, ZoneStatus.ACTIVE,
        source="manual", zone_type="ob", created_at=known,
    )
    early = parent_decision(zone, DetectorConfig(), known - 1)
    assert early.eligible is False and early.reason == "not_yet_known"
    current = parent_decision(zone, DetectorConfig(), known)
    assert current.eligible is True and current.history_trusted is True


def test_expired_base_does_not_hide_a_recent_breakout():
    old = AltRangeEpisode(
        id=1, asset_id=1, source_id=1, origin_key="old",
        anchor_start_open_time=T0, base_start_open_time=T0,
        lower=10, upper=20, width=10, mid=15, state="accompaniment",
        base_end_open_time=T0 + 10 * DAY, base_end_reason="breakout_confirmed",
        base_end_confirmed_at_ms=T0 + 11 * DAY,
    )
    new = AltRangeEpisode(
        id=2, asset_id=1, source_id=1, origin_key="new",
        anchor_start_open_time=T0 + 350 * DAY,
        base_start_open_time=T0 + 350 * DAY,
        lower=11, upper=14, width=3, mid=12.5, state="accompaniment",
        base_end_open_time=T0 + 390 * DAY, base_end_reason="breakout_confirmed",
        base_end_confirmed_at_ms=T0 + 391 * DAY,
    )
    selected, reason, alternatives = select_current_episode(
        [old, new], 15, T0 + 400 * DAY,
    )
    assert selected is new
    assert reason == "recent_breakout_accompaniment"
    assert alternatives == []


@pytest.mark.xfail(
    reason="A04: цепочный кластер alt v2 исправляется после отчёта измерителя",
    strict=False,
)
def test_alt_cluster_chain_does_not_exceed_tolerance():
    pivots = [
        PivotRecord(ZoneType.SSL, price, T0 + i * DAY, T0 + (i + 3) * DAY, "D1", ())
        for i, price in enumerate((100, 100.9, 101.8, 102.7))
    ]
    clusters = cluster_pivots(pivots, 1.0)
    span = max(
        max(p.price for p in cl.pivots) - min(p.price for p in cl.pivots)
        for cl in clusters
    )
    assert span <= 1.0


