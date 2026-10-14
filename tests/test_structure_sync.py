from dataclasses import replace

import pytest

from app.models import Direction, ZoneStatus, ZoneType
from app.services.structure_sync import nearest_daily_obstacle, synchronize_scenario
from app.services.asset_state import project_asset
from tests.test_asset_state import _zone, _obs, _scenario, _leg, _open
from tests.test_d1_structure import daily_bars

AT = daily_bars()[-1].close_time


@pytest.mark.parametrize("kind", [ZoneType.OB, ZoneType.FVG, ZoneType.BREAKER, ZoneType.PRB])
def test_target_is_near_boundary_of_nearest_opposite_daily_zone(db, instrument_id, kind):
    farther = _zone(db, instrument_id, Direction.BEAR, 20, 22, kind=kind)
    nearest = _zone(db, instrument_id, Direction.BEAR, 12, 14, kind=kind)
    _zone(db, instrument_id, Direction.BEAR, 11, 13, kind=kind, tf="W1")
    _zone(db, instrument_id, Direction.BULL, 10, 11, kind=kind)
    _zone(db, instrument_id, Direction.BEAR, 4, 6, kind=kind)
    result = nearest_daily_obstacle(db, instrument_id, "long", 10, AT)
    assert result["zone_id"] == nearest.id and result["zone_id"] != farther.id
    assert result["price"] == 12
    assert nearest_daily_obstacle(db, instrument_id, "long", 13, AT)["status"] == "inside"


def test_short_target_and_exclusions_have_no_liquidity_fallback(db, instrument_id):
    _zone(db, instrument_id, Direction.BULL, 8, 9, kind=ZoneType.OB, until=AT)
    _zone(db, instrument_id, Direction.BULL, 8, 9, kind=ZoneType.FVG, status=ZoneStatus.REJECTED)
    future = _zone(db, instrument_id, Direction.BULL, 8, 9, kind=ZoneType.PRB)
    db.update_zone(future.id, confirmed_at=AT + 1)
    _zone(db, instrument_id, Direction.BULL, 8, 9, kind=ZoneType.BSL)
    assert nearest_daily_obstacle(db, instrument_id, "short", 10, AT) is None
    nearest = _zone(db, instrument_id, Direction.BULL, 5, 7, kind=ZoneType.BREAKER)
    _zone(db, instrument_id, Direction.BULL, 3, 4, kind=ZoneType.OB)
    assert nearest_daily_obstacle(db, instrument_id, "short", 10, AT)["zone_id"] == nearest.id
    assert nearest_daily_obstacle(db, instrument_id, "short", 10, AT)["price"] == 7
    db.update_zone(nearest.id, needs_replay=True)
    assert nearest_daily_obstacle(db, instrument_id, "short", 10, AT)["price"] == 4


def test_target_lifecycle_is_time_bounded(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BEAR, 12, 14, until=AT+1, status=ZoneStatus.ARCHIVED)
    assert nearest_daily_obstacle(db, instrument_id, "long", 10, AT)["zone_id"] == zone.id
    assert nearest_daily_obstacle(db, instrument_id, "long", 10, AT+1) is None


def test_new_countertrend_requires_h1_and_htf_but_d1_flip_keeps_entries(db, instrument_id):
    daily = {"direction": "bear", "warning": None}
    new = synchronize_scenario(db, instrument_id, daily, "long", AT, reference=10,
                               h1_confirmed=True, has_htf_source=True)
    assert new["confirmation_complete"] and new["priority"] == "countertrend"
    assert new["target"] is None and "не найдена" in new["target_text"]
    for h1, source in [(False, True), (True, False)]:
        pending = synchronize_scenario(db, instrument_id, daily, "long", AT,
                                       h1_confirmed=h1, has_htf_source=source)
        assert not pending["confirmation_complete"]
        assert "ждём H1" in pending["status"]
    main = synchronize_scenario(db, instrument_id, {"direction": "bull"}, "long", AT)
    assert main["priority"] == "main"
    assert new["preserve_entry_zones"]


def test_asset_has_target_and_d1_flip_does_not_cancel_stored_scenario(db, instrument_id):
    db.insert_candles([replace(c, instrument_id=instrument_id) for c in daily_bars()])
    parent = _zone(db, instrument_id, Direction.BULL, 3, 8)
    obs = _obs(db, instrument_id, parent, Direction.BULL, "active", _open(1))
    sid = _scenario(db, obs, Direction.BULL)
    _leg(db, instrument_id, "bull", 10, 20, origin=3, end=15)
    target = _zone(db, instrument_id, Direction.BEAR, 12, 14, kind=ZoneType.FVG)
    seq = db.get_state_seq()
    before = project_asset(db, instrument_id, daily_bars()[17].close_time, price=10)
    assert before["trade_setup"]["daily_alignment"]["priority"] == "main"
    assert before["daily_structure"]["warning"]["direction"] == "bear"
    asset = project_asset(db, instrument_id, AT, price=10)
    sync = asset["trade_setup"]["daily_alignment"]
    assert sync["priority"] == "countertrend"
    assert sync["confirmation_complete"]
    assert sync["target"]["zone_id"] == target.id
    assert any(row["kind"] == "target" for row in asset["compact_rows"])
    assert asset["compact_rows"][0]["kind"] == "daily_structure"
    assert db.get_ltf_scenario(sid.id).state == "monitoring_entries"
    assert db.get_state_seq() == seq
