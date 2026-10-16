import pytest

from app.models import Zone, ZoneType, ZoneStatus, Direction
from app.services.liquidity_anchors import refresh_liquidity_anchors
from app.web.api import zone_to_dict
from tests.conftest import make_candle, H1_MS

T = 1_800_000_000_000 // H1_MS * H1_MS


@pytest.mark.parametrize("tf,n", [("D1", 24), ("W1", 168)])
@pytest.mark.parametrize("kind", [ZoneType.BSL, ZoneType.SSL])
def test_exact_first_extreme_and_backfill(db, instrument_id, tf, n, kind):
    level = 120 if kind == ZoneType.BSL else 80
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=kind, direction=Direction.BEAR,
        timeframe=tf, lower=level, upper=level, formed_at=T,
        confirmed_at=T + n * 4 * H1_MS, status=ZoneStatus.ACTIVE,
    ))
    rows = [make_candle(T + i * H1_MS, 100,
                        120 if kind == ZoneType.BSL and i in (7, 14) else 110,
                        80 if kind == ZoneType.SSL and i in (7, 14) else 90,
                        100, timeframe="H1", instrument_id=instrument_id) for i in range(n)]
    db.insert_candles(rows[8:])
    refresh_liquidity_anchors(db, instrument_id)
    assert db.get_zone(zid).display_from == T  # earlier equal maximum may be missing
    assert zone_to_dict(db.get_zone(zid))["extreme_anchor_quality"] == "incomplete"
    db.insert_candles(rows[:8])
    refresh_liquidity_anchors(db, instrument_id)
    z = db.get_zone(zid)
    assert z.display_from == T + 7 * H1_MS
    assert z.formed_at == T and z.confirmed_at == T + n * 4 * H1_MS
    assert z.evidence["extreme_at"] == T + 7 * H1_MS
    before = db.get_state_seq()
    refresh_liquidity_anchors(db, instrument_id)
    assert db.get_state_seq() == before


def test_cluster_uses_price_setting_member_and_first_equal_extreme(db, instrument_id):
    ids = []
    for offset, price in [(0, 120), (24, 121), (48, 121)]:
        zid = db.insert_zone(Zone(
            id=None, instrument_id=instrument_id, type=ZoneType.BSL, direction=Direction.BEAR,
            timeframe="D1", lower=price, upper=price, formed_at=T + offset * H1_MS,
            confirmed_at=T + (offset + 96) * H1_MS, status=ZoneStatus.ACTIVE,
        ))
        ids.append(zid)
        db.insert_candles([make_candle(T + (offset + i) * H1_MS, 100,
                                      price if i == 5 else 110, 90, 100,
                                      timeframe="H1", instrument_id=instrument_id) for i in range(24)])
    for zid in ids:
        db.update_zone(zid, evidence={"group_members": ids, "group_level": 121})
    refresh_liquidity_anchors(db, instrument_id)
    for zid in ids:
        assert db.get_zone(zid).evidence["group_extreme_at"] == T + 29 * H1_MS


@pytest.mark.parametrize("preloaded", [False, True])
def test_shared_h1_sample_preserves_individual_windows(db, instrument_id, monkeypatch, preloaded):
    """An adjacent day's maximum must not supply an anchor for a gapped day."""
    rows = [make_candle(T + i * H1_MS, 100, 120 if i in (7, 31) else 110,
                        90, 100, "H1", instrument_id)
            for i in range(48) if i != 25]
    # An unfinished bar must not fill the missing hour in the closed sample.
    db.insert_candles(rows + [make_candle(T + 25 * H1_MS, 100, 120, 90, 100,
                                         "H1", instrument_id, closed=False)])
    ids = [db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.BSL,
        direction=Direction.BEAR, timeframe="D1", lower=120, upper=120,
        formed_at=T + offset * H1_MS, confirmed_at=T + (offset + 96) * H1_MS,
        status=ZoneStatus.ACTIVE,
    )) for offset in (0, 24)]
    original = db.get_candles
    reads = []

    def read(*args, **kwargs):
        reads.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "get_candles", read)
    refresh_liquidity_anchors(db, instrument_id, **({"candles": rows} if preloaded else {}))
    assert len(reads) == (0 if preloaded else 1)
    first, second = map(db.get_zone, ids)
    assert first.evidence["extreme_at"] == T + 7 * H1_MS
    assert second.evidence["extreme_at"] is None
    assert second.display_from == T + 24 * H1_MS
