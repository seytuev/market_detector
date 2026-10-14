from dataclasses import replace

from app.models import Direction
from app.services.d1_structure import DAY_MS, daily_alignment, daily_structure, project_daily
from app.services.asset_state import project_asset
from tests.conftest import make_candle

T0 = 1_780_012_800_000


def daily_bars():
    highs = [9, 10, 12, 10, 9, 8, 10, 12, 15, 12, 11, 10, 11, 12, 13, 11, 10, 9, 7]
    lows = [7, 8, 9, 7, 6, 5, 7, 9, 11, 9, 8, 7, 8, 9, 10, 8, 7.5, 4.5, 3]
    return [make_candle(T0 + i * DAY_MS, (h + l) / 2, h, l,
                        6 if i == 17 else 4 if i == 18 else (h + l) / 2)
            for i, (h, l) in enumerate(zip(highs, lows))]


def test_daily_sms_then_bos_requires_strict_daily_close():
    bars = daily_bars()
    before = daily_structure(bars, bars[16].close_time)
    sms = daily_structure(bars, bars[17].close_time)
    bos = daily_structure(bars, bars[18].close_time)
    assert not before["events"]
    assert sms["direction"] == "bull"
    assert sms["warning"]["direction"] == "bear"
    assert sms["last_event"]["kind"] == "SMS"
    assert sms["last_event"]["level"] == 7
    assert bos["last_event"]["kind"] == "BOS"
    assert bos["last_event"]["level"] == 5
    assert bos["warning"] is None
    assert bos["last_event"]["anchor_at"] == bars[5].open_time
    assert all(c["timeframe"] == "D1" for side in bos["expected"].values() for c in side.values())


def test_no_lookahead_and_no_forming_daily_break():
    bars = daily_bars()
    at = bars[16].close_time
    assert daily_structure(bars, at) == daily_structure(bars[:17], at)
    forming = bars[:18] + [replace(bars[18], closed=False)]
    assert daily_structure(forming, bars[18].close_time)["last_event"]["kind"] == "SMS"
    equal = bars[:18] + [replace(bars[18], close=5)]
    assert daily_structure(equal, bars[18].close_time)["last_event"]["kind"] == "SMS"


def test_bull_mirror_and_alignment():
    bars = [replace(c, open=30-c.open, high=30-c.low, low=30-c.high, close=30-c.close)
            for c in daily_bars()]
    daily = daily_structure(bars, bars[-1].close_time)
    assert daily["direction"] == "bull"
    assert daily_alignment(daily, "long")["relation"] == "aligned"
    assert daily_alignment(daily, Direction.BEAR)["relation"] == "countertrend"
    assert daily_alignment(daily, None)["relation"] == "unknown"


def test_confirmed_daily_pair_establishes_initial_structure():
    bars = daily_bars()
    result = daily_structure(bars, bars[13].close_time)
    assert result["direction"] == "bull"
    assert result["basis"] == "pivot_pair"
    assert not result["events"]


def test_missing_stale_and_gapped_daily_history():
    bars = daily_bars()
    assert daily_structure([], bars[-1].close_time)["data_quality"] == "no_data"
    stale = daily_structure(bars, bars[-1].close_time + 3 * DAY_MS)
    assert stale["data_quality"] == "stale"
    assert stale["direction"] is None
    gap = daily_structure(bars[:6] + bars[7:], bars[-1].close_time)
    assert gap["direction"] is None
    assert gap["data_quality"] == "gap"


def test_asset_carries_daily_structure_without_writing_h1(db, instrument_id):
    bars = [replace(c, instrument_id=instrument_id) for c in daily_bars()]
    db.insert_candles(bars)
    at = bars[-1].close_time
    seq = db.get_state_seq()
    asset = project_asset(db, instrument_id, at)
    assert asset["daily_structure"] == project_daily(db, instrument_id, at)
    assert asset["daily_structure"]["direction"] == "bear"
    assert any(row["k"] == "Структура D1" for row in asset["compact_rows"])
    assert "daily_alignment" in asset["trade_setup"]
    assert db.get_state_seq() == seq


def test_htf_long_scenario_is_classified_against_bearish_daily_structure(db, instrument_id):
    from tests.test_asset_state import _zone, _obs, _scenario, _leg, _open

    db.insert_candles([replace(c, instrument_id=instrument_id) for c in daily_bars()])
    zone = _zone(db, instrument_id, Direction.BULL, 3, 8)
    obs = _obs(db, instrument_id, zone, Direction.BULL, "active", _open(1))
    _scenario(db, obs, Direction.BULL)
    _leg(db, instrument_id, "bull", 10, 20, origin=3, end=15)
    asset = project_asset(db, instrument_id, daily_bars()[-1].close_time)
    assert asset["trade_setup"]["direction"] == "long"
    assert asset["trade_setup"]["daily_alignment"]["relation"] == "countertrend"
    assert any("Контртрендовый" in row["v"] for row in asset["compact_rows"])


def test_versioned_chart_carries_daily_structure_and_preserves_legacy_shape(db, instrument_id, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.web.api import create_app

    bars = [replace(c, instrument_id=instrument_id) for c in daily_bars()]
    db.insert_candles(bars)
    monkeypatch.setattr("app.web.api.now_ms", lambda: bars[-1].close_time)
    settings = Settings(auth_token="daily-test", db_path=str(tmp_path / "test.db"))
    client = TestClient(create_app(db, settings))
    url = f"/api/candles?instrument_id={instrument_id}&timeframe=D1"
    assert client.get(url + "&with_version=true").status_code == 401
    headers = {"Authorization": "Bearer daily-test"}
    seq = db.get_state_seq()
    snapshot = client.get(url + "&with_version=true", headers=headers).json()
    assert snapshot["daily_structure"]["direction"] == "bear"
    assert snapshot["state_version"] == seq
    assert client.get(url, headers=headers).json() == snapshot["candles"]
    assert db.get_state_seq() == seq
