"""Scenario header and consumed price bands share the canonical snapshot."""
from app.config import Settings
from app.models import Direction
from app.services.h1_chart import _CandleIndex, _tested_slices
from app.services.overview import instrument_current, instruments_overview
from tests.conftest import H1_MS, make_candle
from tests.test_asset_state import _zone, _obs, _scenario, _leg, _open, _close, LEVEL


def test_nearest_waiting_zone_does_not_replace_confirmed_asset_scenario(db, instrument_id, monkeypatch):
    near = _zone(db, instrument_id, Direction.BEAR, 83000, 84000)
    near_obs = _obs(db, instrument_id, near, Direction.BEAR, "waiting_structure", _open(1))
    far = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    far_obs = _obs(db, instrument_id, far, Direction.BULL, "active", _open(2), cycle=2)
    scenario = _scenario(db, far_obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    db.set_meta(f"ltf:selected_context:{instrument_id}", str(near_obs.id))
    monkeypatch.setattr("app.services.overview.now_ms", lambda: _close(40))
    current = instrument_current(db, Settings(), instrument_id)
    row = next(r for r in instruments_overview(db, Settings()) if r["instrument"]["id"] == instrument_id)
    assert current["selected_context_id"] == near_obs.id
    assert current["current_scenario"] is None  # This is navigation only.
    summary = current["scenario_summary"]
    assert summary == row["scenario_summary"] == current["asset"]["scenario_summary"]
    assert summary["confirmed"] and summary["id"] == scenario.id
    assert summary["direction"] == "bull"
    assert summary["detail"]


def test_structure_alone_does_not_confirm_a_trade(db, instrument_id, monkeypatch):
    _leg(db, instrument_id, "bull", LEVEL, 20)
    monkeypatch.setattr("app.services.overview.now_ms", lambda: _close(40))
    current = instrument_current(db, Settings(), instrument_id)
    assert current["asset"]["structure"]["proven"]
    assert current["scenario_summary"]["confirmed"] is False
    assert current["scenario_summary"]["state"] == "context_missing"


def test_same_direction_sms_keeps_the_active_scenario(db, instrument_id, monkeypatch):
    zone = _zone(db, instrument_id, Direction.BEAR, 84000, 86000)
    obs = _obs(db, instrument_id, zone, Direction.BEAR, "active", _open(1))
    scenario = _scenario(db, obs, Direction.BEAR, created=_close(11))
    _leg(db, instrument_id, "bear", 84000, 10, origin=87000, end=82000, superseded=_close(20))
    _leg(db, instrument_id, "bear", 82972.74, 20, origin=84352, end=80393,
         key=f"sms:secondary:LL:12:82972.74@{_close(20)}")
    monkeypatch.setattr("app.services.overview.now_ms", lambda: _close(40))
    current = instrument_current(db, Settings(), instrument_id)
    assert current["scenario_summary"]["confirmed"]
    assert current["scenario_summary"]["id"] == scenario.id
    assert current["asset"]["trade_setup"]["state"] != "scenario_not_opened"


def test_old_scenario_is_not_carried_across_an_opposite_leg(db, instrument_id, monkeypatch):
    zone = _zone(db, instrument_id, Direction.BEAR, 84000, 86000)
    obs = _obs(db, instrument_id, zone, Direction.BEAR, "active", _open(1))
    _scenario(db, obs, Direction.BEAR, created=_close(6))
    _leg(db, instrument_id, "bear", 84000, 5, origin=87000, end=82000, superseded=_close(10))
    _leg(db, instrument_id, "bull", 85000, 10, origin=82000, end=87000, superseded=_close(20))
    _leg(db, instrument_id, "bear", 82972.74, 20, origin=84352, end=80393)
    monkeypatch.setattr("app.services.overview.now_ms", lambda: _close(40))
    current = instrument_current(db, Settings(), instrument_id)
    assert not current["scenario_summary"]["confirmed"]


def test_trade_can_precede_the_first_saved_local_pd_of_the_direction(db, instrument_id, monkeypatch):
    zone = _zone(db, instrument_id, Direction.BEAR, 84000, 86000)
    obs = _obs(db, instrument_id, zone, Direction.BEAR, "active", _open(1))
    scenario = _scenario(db, obs, Direction.BEAR, created=_close(11))
    _leg(db, instrument_id, "bull", 85000, 10, origin=82000, end=87000, superseded=_close(20))
    _leg(db, instrument_id, "bear", 82972.74, 20, origin=84352, end=80393)
    monkeypatch.setattr("app.services.overview.now_ms", lambda: _close(40))
    current = instrument_current(db, Settings(), instrument_id)
    assert current["scenario_summary"]["confirmed"]
    assert current["scenario_summary"]["id"] == scenario.id


def test_price_bands_stop_at_each_deeper_test_and_ignore_future_candles():
    start = _open(1)
    candles = [
        make_candle(start + i * H1_MS, 110, high, low, 110, "H1", 1)
        for i, (high, low) in enumerate([(112, 108), (110, 106), (111, 107), (109, 103), (106, 99)])
    ]
    index = _CandleIndex(candles, candles[3].close_time)
    bands = _tested_slices(index, "FVG", Direction.BULL, 100, 110, start, start)
    assert bands == [
        {"lower": 108, "upper": 110, "tested_at": start},
        {"lower": 106, "upper": 108, "tested_at": start + H1_MS},
        {"lower": 103, "upper": 106, "tested_at": start + 3 * H1_MS},
    ]
    assert _tested_slices(index, "OB", Direction.BULL, 100, 110, start, start) == bands
    assert _tested_slices(index, "FVG", Direction.BULL, 100, 110, None, start) == []
    full = _CandleIndex(candles, candles[-1].close_time)
    assert _tested_slices(full, "FVG", Direction.BULL, 100, 110, start, start)[-1]["lower"] == 100


def test_bearish_zone_is_consumed_from_the_lower_boundary():
    start = _open(1)
    candles = [make_candle(start + i * H1_MS, 100, high, 98, 100, "H1", 1)
               for i, high in enumerate([102, 105, 103, 111])]
    bands = _tested_slices(_CandleIndex(candles, candles[-1].close_time),
                           "OB", Direction.BEAR, 100, 110, start, start)
    assert [(b["lower"], b["upper"]) for b in bands] == [(100, 102), (102, 105), (105, 110)]
