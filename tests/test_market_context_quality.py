"""Regression cases from the market-context audit: aftermath, gaps and scope."""
from dataclasses import replace

import pytest

from app.events import runner, store
from app.events.basket import basket_from_static
from app.events.market import evaluate_market
from app.events.episodes import liquidation_history
from app.events.rules import get_rule
from tests.test_events_market import _fact, _inp, _registry_with
from tests.test_events_runner_v2 import (
    DAY, HOUR, NOW, W4, _hour_bars, _liq_rows, _settings, make_fetcher,
)


def _scenario(*, days_ago=0, recover=True):
    current = NOW // W4 * W4
    # Two hot windows, then three quiet windows below both baseline and half peak.
    end = current - days_ago * DAY
    hot = {end - 5 * W4, end - 4 * W4}
    rows = _liq_rows(NOW, count=240)
    for row in rows:
        if row["time"] in hot:
            row["aggregated_long_liquidation_usd"] = "1000"
            row["aggregated_short_liquidation_usd"] = "100"
    first_hot = min(hot)
    price = 100.0
    bars = []
    for bar in _hour_bars("BTCUSDT", NOW, days=40):
        drift = -0.01 if bar["start"] in range(first_hot, end - 3 * W4, HOUR) else 0.0
        if recover and end - 3 * W4 <= bar["start"] < end:
            drift = 0.01
        close = price * (1 + drift)
        bars.append({**bar, "open": price, "close": close})
        price = close
    return rows, bars, first_hot + W4, end


def _run_scenario(db, *, days_ago=0, recover=True):
    rows, bars, occurred, end = _scenario(days_ago=days_ago, recover=recover)
    fetcher = make_fetcher(liq_rows=rows)
    fetcher["prices"] = lambda _s, pairs, start_ms, end_ms: ({p: bars for p in pairs}, [])
    result = runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW)
    return result, fetcher, occurred, end


@pytest.mark.parametrize("days_ago", [0, 5, 9])
def test_old_cascade_is_recovered_after_downtime_without_resetting_age(db, days_ago):
    result, fetcher, occurred, end = _run_scenario(db, days_ago=days_ago)
    assert result["direction"] == "long"
    fact = next(f for f in store.query_facts(db) if f["rule_id"] == "LIQ_PRESSURE")
    assert fact["occurred_at"] == occurred
    assert fact["valid_until"] == occurred + 10 * DAY
    assert fact["detected_at"] == NOW
    assert fact["direction"] == "long"
    assert fact["payload"]["phase_at"] == end
    before = store.fact_transitions_since(db, 0)
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW + 60_000)
    assert store.fact_transitions_since(db, 0) == before
    assert store.get_fact(db, fact["fact_key"])["direction"] == "long"
    market = store.latest_assessment(db)
    selected = market["active_facts"][0]
    assert selected["effective_rule_id"] == "LIQ_RECOVERY"
    assert selected["used_for_direction"] is True
    assert market["reasons"][0]["rule_id"] == "LIQ_RECOVERY"


def test_quiet_liquidations_alone_do_not_mean_price_recovery(db):
    result, _, _, _ = _run_scenario(db, recover=False)
    assert result["direction"] == "short"


def test_recovery_rejects_missing_hours_and_future_bars():
    _, bars, _, end = _scenario()
    basket = basket_from_static(["BTC"], day_start_ms=0)
    th = get_rule("LIQ_PRESSURE").thresholds
    assert runner._recovery_ok(th, None, {"BTC": bars}, basket, end)
    missing = [b for b in bars if b["start"] != end - 5 * HOUR]
    assert not runner._recovery_ok(th, {"breadth_up": 1}, {"BTC": missing}, basket, end)
    # Only one rising window cannot substitute for the required three.
    assert not runner._recovery_ok(th, {"breadth_up": 1},
                                  {"BTC": bars[-4:]}, basket, end)
    assert not runner._recovery_ok(th, {"breadth_up": 1},
                                  {"BTC": [b for b in bars if b["start"] >= end]}, basket, end)


def test_rotation_aggregates_cached_members_and_reports_entire_basket(db):
    settings = _settings()
    settings.events_liq_symbol_budget = 1
    first = runner.run_cycle(db, settings, fetcher=make_fetcher(), now_ms=NOW)
    assert first["direction"] == "unknown"  # two out of three is below 80%
    src = store.get_source_states(db)["liquidations"]
    assert src["quality"] == "partial"
    assert src["payload"]["coverage"]["total"] == 3
    runner.run_cycle(db, settings, fetcher=make_fetcher(), now_ms=NOW + 60_000)
    market = store.latest_assessment(db)
    assert market["liquidations"]["coverage"]["ok"] == 3
    assert market["liquidations"]["coverage"]["total"] == 3
    assert market["direction"] == "neutral"


def test_baseline_needs_more_than_one_comparable_day(db):
    runner.run_cycle(db, _settings(), fetcher=make_fetcher(liq_rows=_liq_rows(NOW, count=12)), now_ms=NOW)
    liq = store.latest_assessment(db)["liquidations"]
    assert liq["baseline_ok"] is False
    assert liq["baseline_samples"] == 1
    assert not any(f["direction"] for f in store.query_facts(db))


def test_latest_phase_wins_but_old_event_remains_in_context():
    pressure = _fact("LIQ_PRESSURE", "short", occurred=NOW - 5 * DAY)
    recovery = _fact("LIQ_RECOVERY", "long", occurred=NOW - DAY)
    out = evaluate_market(_inp(now_ms=NOW, facts=[pressure, recovery]))
    assert out["direction"] == "long"
    assert len(out["active_facts"]) == 2
    assert out["active_facts"][0]["context_status"] == "driving"
    assert out["active_facts"][1]["context_status"] == "background"


def test_disabled_recovery_does_not_vote_through_pressure_rule_id():
    fact = _fact("LIQ_PRESSURE", "long")
    fact["payload"]["recovery_rule"] = "LIQ_RECOVERY"
    out = evaluate_market(_inp(facts=[fact], registry=_registry_with(
        replace(get_rule("LIQ_RECOVERY"), enabled=False))))
    assert out["direction"] == "neutral"
    assert not out["active_facts"][0]["used_for_direction"]


def test_gap_in_liquidations_cannot_prove_three_quiet_windows():
    rows, _, _, end = _scenario()
    data = {r["time"]: {"long_usd": r["aggregated_long_liquidation_usd"],
                        "short_usd": r["aggregated_short_liquidation_usd"]} for r in rows}
    del data[end - 2 * W4]
    history = liquidation_history({"BTC": data}, now=NOW, baseline_days=30,
        thresholds=get_rule("LIQ_PRESSURE").thresholds, memory_ms=10 * DAY, min_coverage=0.8)
    assert history[-1]["episode"]["phase"] == "post_cascade"


def test_losing_a_member_cannot_be_mistaken_for_fading_volume():
    rows, _, _, end = _scenario()
    data = {r["time"]: {"long_usd": r["aggregated_long_liquidation_usd"],
                        "short_usd": r["aggregated_short_liquidation_usd"]} for r in rows}
    truncated = {t: r for t, r in data.items() if t < end - 3 * W4}
    history = liquidation_history({"BTC": data, "ETH": data, "SOL": truncated},
        now=NOW, baseline_days=30, thresholds=get_rule("LIQ_PRESSURE").thresholds,
        memory_ms=10 * DAY, min_coverage=0.6)
    assert history[-1]["available"] is True
    assert history[-1]["episode"]["phase"] == "post_cascade"


def test_new_cascade_supersedes_recovery_without_deleting_old_context(db):
    rows, bars, _, _ = _scenario(days_ago=5)
    end = NOW // W4 * W4
    for row in rows:
        if row["time"] == end - W4:
            row["aggregated_long_liquidation_usd"] = "1000"
            row["aggregated_short_liquidation_usd"] = "100"
    for bar in bars:
        if bar["start"] >= end - W4:
            bar["close"] = bar["open"] * 0.99
    fetcher = make_fetcher(liq_rows=rows)
    fetcher["prices"] = lambda _s, pairs, start_ms, end_ms: ({p: bars for p in pairs}, [])
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW)
    market = store.latest_assessment(db)
    assert market["direction"] == "short"
    pressure = [f for f in store.query_facts(db) if f["rule_id"] == "LIQ_PRESSURE"]
    assert {f["direction"] for f in pressure} == {"long", "short"}
    assert len(pressure) == 2
    assert len([f for f in market["active_facts"] if f["used_for_direction"]]) == 1


def test_expired_event_is_not_revived_by_historical_reconstruction(db):
    result, _, _, _ = _run_scenario(db, days_ago=11)
    assert result["direction"] == "neutral"
    assert not any(f["direction"] for f in store.query_facts(db, active_now_ms=NOW))
