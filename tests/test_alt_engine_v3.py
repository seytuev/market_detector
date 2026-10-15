"""Agreed NEAR example, causal W1 rules, and transfer to other assets."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from app.alt.engine_v3 import DAY, RULES_VERSION, aggregate_weeks, analyze
from app.alt.store_v3 import persist_result, select_frame
from app.db import Database

FIXTURES = Path(__file__).parent / "fixtures/alt_v2/ohlc"


def stamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()*1000)


@pytest.fixture(scope="module")
def candles():
    return json.loads((FIXTURES / "NEAR.json").read_text())


@pytest.fixture(scope="module")
def replay(candles):
    return analyze(candles)


def near_base(result):
    return next(b for b in result["structures"] if b["geometry"]["base_start_open_time"] == stamp("2025-04-07"))


def test_near_base_geometry_and_independent_cycles(replay):
    b = near_base(replay)
    assert b["geometry"]["lower"] == 1.550
    assert b["geometry"]["upper"] == 3.383
    assert b["geometry"]["base_end_open_time"] == stamp("2026-09-20")
    assert b["revisions"][-1]["base_end_confirmed_at_ms"] == stamp("2026-09-21")
    # Old rally/decline boxes are no longer admissible historical cycles.
    assert all(b["revisions"][0]["upper"] <= b["revisions"][0]["quality_metrics"]["admission_ceiling"] for b in replay["structures"])
    # Completion freezes geometry even when subsequent weeks make new highs.
    complete = next(r for r in b["revisions"] if r["state"] == "completed")
    assert all((r["lower"], r["upper"]) == (1.55, 3.383) for r in b["revisions"] if r["available_at_ms"] >= complete["available_at_ms"])


def test_age_not_reset_by_boundary_refinements(replay):
    b = near_base(replay)
    assert b["revisions"][0]["available_at_ms"] == stamp("2025-07-21")
    assert b["revisions"][0]["available_at_ms"]-b["geometry"]["base_start_open_time"] >= 101*DAY
    assert b["revisions"][0]["lower"] == 1.793
    expanded = next(r for r in b["revisions"] if r["lower"] == 1.55)
    assert expanded["available_at_ms"] == stamp("2025-10-13")
    assert {r["base_start_open_time"] for r in b["revisions"]} == {stamp("2025-04-07")}


def test_bos_and_full_candle_ob(replay):
    state = near_base(replay)["scenario_state"]
    s = state["scenarios"][0]
    assert s["bos_level"] == 1.894
    assert s["bos_anchor"] == stamp("2026-01-12")
    assert s["minimum"] == .841
    assert s["bos_at_ms"] == stamp("2026-05-25")
    ob = next(z for z in state["pd_zones"] if z["kind"] == "OB")
    assert (ob["lower"], ob["upper"]) == (1.469, 1.692)
    assert ob["formed_at"] == stamp("2026-05-11")
    assert ob["available_at_ms"] == stamp("2026-05-25")
    assert ob["invalidated_at_ms"] is None


def test_depression_does_not_expand_before_bos(candles):
    b = near_base(analyze(candles, as_of_ms=stamp("2026-05-24")))
    assert b["geometry"]["lower"] == 1.55
    assert b["scenario_state"]["depression"]["minimum"] == .841
    assert not b["scenario_state"]["scenarios"]
    assert not any(e["event_type"] == "cancelled" for e in b["scenario_state"]["events"])


def test_partial_bos_week_does_not_confirm(candles):
    before = near_base(analyze(candles, as_of_ms=stamp("2026-05-24")))
    after = near_base(analyze(candles, as_of_ms=stamp("2026-05-25")))
    assert not before["scenario_state"]["scenarios"]
    assert after["scenario_state"]["scenarios"][0]["bos_at_ms"] == stamp("2026-05-25")


def test_fvg_available_only_after_third_week_closes(candles, replay):
    before = near_base(analyze(candles, as_of_ms=stamp("2026-05-31")))
    assert not any(z["lower"] == 1.692 for z in before["scenario_state"]["pd_zones"])
    z = next(z for z in near_base(replay)["scenario_state"]["pd_zones"] if z["lower"] == 1.692)
    assert (z["lower"], z["upper"]) == (1.692, 2.215)
    assert z["available_at_ms"] == stamp("2026-06-01")
    assert z["invalidated_at_ms"] == stamp("2026-08-10")


def test_repeated_fvg_touches_and_wick_below_not_invalidation(candles, replay):
    b = near_base(replay)
    z = next(z for z in b["scenario_state"]["pd_zones"] if z["lower"] == 1.692)
    touches = [e for e in b["scenario_state"]["events"] if e["event_type"] == "retest" and e["payload"]["zone_id"] == z["id"]]
    assert len(touches) >= 2
    assert all(z["available_at_ms"] <= e["event_time_ms"] <= z["invalidated_at_ms"] for e in touches)
    before_close = near_base(analyze(candles, as_of_ms=stamp("2026-08-09")))
    fvg = next(z for z in before_close["scenario_state"]["pd_zones"] if z["lower"] == 1.692)
    assert fvg["invalidated_at_ms"] is None


def test_scenario_cancel_only_on_closed_w1_below_manipulation(candles):
    prefix = [r for r in candles if r["open_time"] < stamp("2026-08-17")]
    extra = [{"open_time": stamp("2026-08-17")+i*DAY, "open": 1.6, "high": 1.65, "low": .7, "close": .8} for i in range(7)]
    rows = prefix+extra
    before = near_base(analyze(rows, as_of_ms=stamp("2026-08-23")))
    assert before["scenario_state"]["scenarios"][0]["cancelled_at_ms"] is None
    after = near_base(analyze(rows, as_of_ms=stamp("2026-08-24")))
    assert after["scenario_state"]["scenarios"][0]["cancelled_at_ms"] == stamp("2026-08-24")
    assert after["geometry"]["lower"] == 1.55


def test_same_week_confirms_bos_and_completes_base(candles):
    prefix = [r for r in candles if r["open_time"] < stamp("2026-05-18")]
    jump = [{"open_time": stamp("2026-05-18")+i*DAY, "open": 1.7,
             "high": 4.2, "low": 1.65, "close": 4.0} for i in range(7)]
    base = near_base(analyze(prefix+jump))
    assert base["state"] == "completed"
    assert base["scenario_state"]["scenarios"][0]["bos_at_ms"] == stamp("2026-05-25")
    assert {e["event_type"] for e in base["scenario_state"]["events"]
            if e["event_time_ms"] == stamp("2026-05-25")} >= {"bos_confirmed", "breakout"}
    assert (base["geometry"]["lower"], base["geometry"]["upper"]) == (1.55, 3.383)


@pytest.mark.parametrize("date", ["2025-07-21", "2025-10-13", "2026-02-09", "2026-05-25", "2026-06-01", "2026-08-10", "2026-09-21"])
def test_prefix_invariance(candles, replay, date):
    at = stamp(date)
    expected = [(b["structure_key"], r) for b in replay["structures"] for r in b["revisions"] if r["available_at_ms"] <= at]
    prefix = analyze(candles, as_of_ms=at)
    actual = [(b["structure_key"], r) for b in prefix["structures"] for r in b["revisions"]]
    assert actual == expected


def test_store_stable_ids_no_backfill_notifications_and_as_of(replay):
    db = Database(":memory:")
    persist_result(db, 1, 2, replay, backfill=True)
    f = select_frame(db, 1, 2)
    assert f["lower"] == 1.55 and f["upper"] == 3.383
    count = db.conn.execute("SELECT COUNT(*) FROM alt_v3_event").fetchone()[0]
    persist_result(db, 1, 2, replay)
    assert select_frame(db, 1, 2)["structure_id"] == f["structure_id"]
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event").fetchone()[0] == count
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0
    early = select_frame(db, 1, 2, stamp("2026-05-24"))
    assert early["scenario_state"]["depression"] and not early["scenario_state"]["scenarios"]
    assert not early["scenario_state"]["pd_zones"]
    assert db.conn.execute("SELECT COUNT(*) FROM alt_event").fetchone()[0] == 0
    db.close()


def test_input_order_duplicates_gaps_and_sources(candles):
    original = analyze(candles)
    assert analyze(list(reversed(candles))+[candles[0]]) == original
    broken = candles[:100]+candles[101:]
    assert analyze(broken)["status"] == "data_pending"
    with pytest.raises(ValueError, match="Conflicting"):
        analyze(candles+[dict(candles[0], close=candles[0]["open"])])
    with pytest.raises(ValueError, match="Mixed"):
        analyze([dict(candles[0], source_id=1), dict(candles[1], source_id=2)])


@pytest.mark.parametrize("ticker", ["LINK", "HBAR", "AAVE", "SUI", "ENA", "PUMP", "TAO", "QNT", "ONDO"])
def test_transfer_and_revision_age(ticker):
    result = analyze(json.loads((FIXTURES/f"{ticker}.json").read_text()))
    assert result["status"] in {"ok", "no_structure", "insufficient_history", "data_pending"}
    for b in result["structures"]:
        assert b["rules_version"] == RULES_VERSION
        assert b["revisions"][0]["available_at_ms"]-b["geometry"]["base_start_open_time"] >= 101*DAY
        assert b["geometry"]["upper"] > b["geometry"]["lower"] > 0
