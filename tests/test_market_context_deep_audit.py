"""Adversarial cases: causality, historical revisions and revoking an interpretation."""
import json

import pytest

from app.events import memory, runner, store
from app.events.basket import basket_from_static, basket_metrics, build_basket, msk_day_return
from app.events.btc_context import btc_liquidation_oi
from app.events.market import evaluate_market
from app.events.revalidation import MODEL_VERSION
from app.services.runtime import claim_owner, release_owner
from tests.test_events_market import _fact, _inp
from tests.test_events_runner_v2 import DAY, HOUR, NOW, W4, _settings, _hour_bars, make_fetcher
from tests.test_market_context_quality import _scenario


def _run(db, rows, bars, *, now=NOW):
    fetcher = make_fetcher(liq_rows=rows)
    fetcher["prices"] = lambda _s, pairs, start_ms, end_ms: ({p: bars for p in pairs}, [])
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=now)
    return store.latest_assessment(db), fetcher


def test_recovered_price_does_not_hide_a_historical_break_of_long_basis(db):
    rows, bars, _, end = _scenario(days_ago=5)
    # One H1 closes below the cascade, then price returns above it before detection.
    broken_at = end + HOUR
    for b in bars:
        if b["end"] == broken_at:
            b["close"] = 50
    market, fetcher = _run(db, rows, bars)
    assert market["direction"] == "neutral"
    fact = next(f for f in market["active_facts"] if f["rule_id"] == "LIQ_PRESSURE")
    assert fact["direction"] is None
    assert fact["context_status"] == "invalidated"
    assert fact["invalidated_at"] == broken_at
    # Polling the earlier recovery windows again cannot resurrect it.
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW + 60_000)
    assert store.latest_assessment(db)["direction"] == "neutral"


def test_revision_removing_the_cascade_cannot_leave_its_long_vote(db):
    rows, bars, _, _ = _scenario(days_ago=5)
    market, fetcher = _run(db, rows, bars)
    assert market["direction"] == "long"
    for row in rows:
        row["aggregated_long_liquidation_usd"] = "100"
        row["aggregated_short_liquidation_usd"] = "60"
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW + 60_000)
    market = store.latest_assessment(db)
    assert market["direction"] == "unknown"
    assert "event_history" in market["unknown_dependencies"]
    assert market["active_facts"][0]["context_status"] == "unverified"


def test_missing_current_prices_retains_event_without_a_direction_vote(db):
    rows, bars, _, _ = _scenario(days_ago=5)
    _, fetcher = _run(db, rows, bars)
    fetcher["prices"] = lambda *args: ({}, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW + 60_000)
    market = store.latest_assessment(db)
    assert market["direction"] == "unknown"
    assert not any(f["used_for_direction"] for f in market["active_facts"])
    assert market["factor_agreement"]["long"] == 0
    assert any(f["direction"] == "long" for f in market["active_facts"])


def _saved_fact(db, *, occurred, direction, phase, basis=None):
    return memory.begin_fact(db, rule_id="LIQ_PRESSURE", rule_version="events-time-2.0",
        scope="crypto_market", episode_id=f"saved-{occurred}", direction=direction,
        occurred_at=occurred, detected_at=occurred, available_at=occurred, state="confirmed",
        payload={"phase_at": phase, "context_model": MODEL_VERSION,
                 "price_basis": basis or {"BTC": {"open": 100, "min_close": 90}}})[0]


def test_cancelling_or_expiring_latest_phase_never_resurrects_older_short(db):
    basket = basket_from_static(["BTC"], day_start_ms=NOW // DAY * DAY)
    old = _saved_fact(db, occurred=NOW - 8 * DAY, direction="short", phase=NOW - 8 * DAY)
    new = _saved_fact(db, occurred=NOW - 9 * DAY, direction="long", phase=NOW - 3 * HOUR)
    bars = _hour_bars("BTCUSDT", NOW, start_price=110)
    for b in bars:
        if b["end"] == NOW // HOUR * HOUR - HOUR:
            b["close"] = 80
    runner._revalidate_facts(db, _settings(), {"BTC": bars}, basket, NOW)
    assert store.get_fact(db, old["fact_key"])["payload"]["superseded_by"] == new["fact_key"]
    assert store.get_fact(db, new["fact_key"])["payload"]["context_invalidated"]
    future = NOW + DAY + 1
    memory.expire_due(db, future)
    runner._revalidate_facts(db, _settings(), {"BTC": bars}, basket, future)
    market = evaluate_market(_inp(now_ms=future, facts=store.query_facts(db, active_now_ms=future)))
    assert market["direction"] == "neutral"
    assert market["factor_agreement"]["short"] == 0


def test_price_reclaim_removes_old_short_without_inventing_long(db):
    fact = _saved_fact(db, occurred=NOW - DAY, direction="short", phase=NOW - DAY)
    basket = basket_from_static(["BTC"], day_start_ms=0)
    bars = _hour_bars("BTCUSDT", NOW, start_price=110)
    runner._revalidate_facts(db, _settings(), {"BTC": bars}, basket, NOW)
    current = store.get_fact(db, fact["fact_key"])
    assert current["state"] == "confirmed"
    assert current["direction"] is None
    assert current["payload"]["pressure_released"] is True


@pytest.mark.parametrize("change", [
    {"scope": "BTC"}, {"state": "provisional"}, {"state": "expired"},
    {"state": "invalidated"}, {"available_at": NOW + HOUR},
    {"occurred_at": NOW + HOUR}, {"valid_until": NOW}, {"rule_id": "UNKNOWN"},
    {"rule_id": "SRC_DEGRADED"},
    {"rule_id": "LIQ_FACT"},
])
def test_ineligible_facts_cannot_vote(change):
    fact = _fact("LIQ_PRESSURE", "short", occurred=NOW - HOUR)
    fact.update(change)
    market = evaluate_market(_inp(now_ms=NOW, facts=[fact]))
    assert market["direction"] == "neutral"
    assert not market["active_facts"][0]["used_for_direction"]


def test_future_phase_and_missing_baseline_do_not_imply_neutral():
    fact = _fact("LIQ_PRESSURE", "short", occurred=NOW - HOUR)
    fact["payload"]["phase_at"] = NOW + HOUR
    assert evaluate_market(_inp(now_ms=NOW, facts=[fact]))["direction"] == "neutral"
    liq = _inp().liq
    liq["baseline_ok"] = False
    market = evaluate_market(_inp(liq=liq))
    assert market["direction"] == "unknown"
    assert "liquidations_baseline" in market["unknown_dependencies"]


def test_stale_asset_and_a_missing_hour_cannot_pollute_market_median():
    basket = basket_from_static(["BTC", "ETH", "SOL"], day_start_ms=0)
    good = _hour_bars("BTCUSDT", NOW, drift=0.001)
    stale = _hour_bars("ETHUSDT", NOW - DAY, drift=-0.1)
    missing = [b for b in good if b["start"] != NOW // HOUR * HOUR - 3 * HOUR]
    market = basket_metrics(basket, {"BTC": good, "ETH": stale, "SOL": missing},
                            now_ms=NOW, max_age_ms=2 * HOUR)
    assert market["coverage"]["ok"] == 1
    assert market["median_return"] > 0
    assert market["breadth_up"] == 1
    assert {r["symbol"]: r["status"] for r in market["members"]} == {
        "BTC": "ok", "ETH": "stale", "SOL": "incomplete"}


def test_duplicate_hours_cannot_fill_a_missing_hour():
    basket = basket_from_static(["BTC"], day_start_ms=0)
    start = NOW // DAY * DAY - DAY
    bars = [b for b in _hour_bars("BTCUSDT", NOW) if start <= b["start"] < start + DAY]
    assert len(bars) == 24
    broken = bars[:-1] + [bars[0]]
    result = msk_day_return(basket, {"BTC": broken}, day_start_ms=start, day_end_ms=start + DAY)
    assert not result["complete"]
    assert result["median_return"] is None


def test_empty_basket_recovers_within_same_day(db):
    settings = _settings()
    settings.events_basket_static = ""
    day = NOW // DAY * DAY
    empty = runner._basket_of_day(db, settings, {"cmc_listings": lambda: None}, day, set(), "error")
    assert not empty.members
    settings.events_basket_static = "BTC, BTC,eth"
    recovered = runner._basket_of_day(db, settings, {"cmc_listings": lambda: None}, day,
                                     {"BTCUSDT", "ETHUSDT"}, "ok")
    assert [m.symbol for m in recovered.members] == ["BTC", "ETH"]
    assert store.latest_basket(db)["version"] == recovered.version


def test_history_not_rewritten_by_new_basket_or_revised_prices(db):
    settings = _settings()
    first = basket_from_static(["BTC"], day_start_ms=0)
    bars = {"BTC": _hour_bars("BTCUSDT", NOW, drift=0.001)}
    runner._store_day_metrics(db, first, bars, NOW)
    frozen = store.metric_series(db, "basket_day_return")
    other = basket_from_static(["ETH"], day_start_ms=DAY)
    runner._store_day_metrics(db, other, {"ETH": _hour_bars("ETHUSDT", NOW, drift=-0.01)}, NOW)
    assert store.metric_series(db, "basket_day_return") == frozen


def test_unavailable_composition_cannot_be_counted_as_historical_statistics(db):
    basket = basket_from_static(["BTC"], day_start_ms=0)
    basket.source = "cmc_snapshot"
    runner._store_day_metrics(db, basket, {"BTC": _hour_bars("BTCUSDT", NOW)}, NOW)
    settings = _settings()
    settings.events_stats_min_n = 1
    assert runner._stats(db, settings)["available"] is False


def test_old_cmc_snapshot_is_not_evidence_of_constituents_on_a_missing_day(db):
    from app.events.timeutil import msk_day_start_ms
    today = msk_day_start_ms(NOW)
    basket = basket_from_static(["BTC"], day_start_ms=today - 5 * DAY)
    basket.source = "cmc_snapshot"
    store.put_basket(db, version=basket.version, day_start_ms=basket.day_start_ms,
                     payload={"source": basket.source, "members": [{"symbol": "BTC", "cmc_id": 0, "rank": 1, "pair": "BTCUSDT"}]})
    runner._store_day_metrics(db, basket, {"BTC": _hour_bars("BTCUSDT", NOW)}, NOW)
    metric = next(m for m in store.metric_series(db, "basket_day_return")
                  if m["bucket_start"] == today - 2 * DAY)
    assert metric["historical_composition_known"] is False


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", None])
def test_one_invalid_liquidation_row_does_not_stop_other_sources(db, bad):
    fetcher = make_fetcher()
    original = fetcher["liq"]
    def liq(cg, symbol, interval, limit):
        rows = original(cg, symbol, interval, limit)
        if interval == "4h":
            rows.append({"time": NOW // W4 * W4 - W4,
                "aggregated_long_liquidation_usd": bad, "aggregated_short_liquidation_usd": "1"})
        return rows
    fetcher["liq"] = liq
    runner.run_cycle(db, _settings(), fetcher=fetcher, now_ms=NOW)
    states = store.get_source_states(db)
    assert states["liquidations"]["quality"] in ("partial", "stale", "unavailable")
    assert store.latest_assessment(db)["direction"] == "unknown"
    assert states["calendar"]["quality"] == "ok"
    assert states["funding"]["quality"] == "ok"
    json.dumps(store.latest_assessment(db), allow_nan=False)


def test_cycle_lease_prevents_parallel_mutation_and_releases_after_exception(db, monkeypatch):
    claim = claim_owner(db, "events:cycle_lease", {"instance_id": "other-process"}, lease_ms=60_000)
    result = runner.run_cycle(db, _settings(), fetcher=make_fetcher(), now_ms=NOW)
    assert result["in_progress"] is True
    assert store.latest_assessment(db) is None
    release_owner(db, "events:cycle_lease", claim["owner"])
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(runner, "_run_cycle", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        runner.run_cycle(db, _settings(), now_ms=NOW)
    assert json.loads(db.get_meta("events:cycle_lease"))["expires_at"] == 0


@pytest.mark.parametrize("operation", ["confirm", "reinterpret"])
def test_terminal_fact_cannot_be_revived_without_expire_due(db, operation):
    fact = _saved_fact(db, occurred=NOW - 11 * DAY, direction="short", phase=NOW - 11 * DAY)
    if operation == "confirm":
        result = memory.confirm_fact(db, fact["fact_key"], NOW)
    else:
        result = memory.reinterpret_fact(db, fact["fact_key"], new_direction="long", at_ms=NOW, reason="late")
    assert result["state"] == "expired"
    assert result["direction"] == "short"


def test_btc_oi_uses_same_closed_day_and_keeps_older_cascades():
    latest = NOW // DAY * DAY - DAY
    liq = [{"t": latest - i * DAY, "quality": "ok", "long_usd": 100, "short_usd": 100}
           for i in range(45)]
    for row in liq:
        if row["t"] in (latest, latest - 5 * DAY):
            row.update(long_usd=1000, short_usd=100)
    oi = [{"t": latest, "quality": "ok", "open": 100, "close": 110},
          {"t": latest - 5 * DAY, "quality": "ok", "open": 100, "close": 90},
          {"t": latest + DAY, "quality": "ok", "open": 100, "close": 1}]
    result = btc_liquidation_oi(liq, oi, now=NOW)
    assert result["state"] == "cascade_oi_rising"
    assert len(result["recent_events"]) == 2
    assert not result["used_for_market_direction"]
    oi = [row for row in oi if row["t"] != latest]
    assert btc_liquidation_oi(liq, oi, now=NOW)["state"] == "unknown"


def test_history_writes_are_batched_not_one_roundtrip_per_bucket(db, monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("scalar source-history write")
    monkeypatch.setattr(runner, "upsert_bucket", reject)
    runner.run_cycle(db, _settings(), fetcher=make_fetcher(), now_ms=NOW)
    assert store.latest_assessment(db)["direction"] == "neutral"


def test_interrupted_publication_rolls_back_assessment_and_retries_transitions(db, monkeypatch):
    from app.events import publish
    original = publish.process_cycle
    def fail(*args, **kwargs):
        raise RuntimeError("publication interrupted")
    monkeypatch.setattr(publish, "process_cycle", fail)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        runner.run_cycle(db, _settings(), fetcher=make_fetcher(), now_ms=NOW)
    assert store.latest_assessment(db) is None
    assert db.get_meta("events:transitions_processed") is None
    pending = store.fact_transitions_since(db, 0)
    assert pending
    monkeypatch.setattr(publish, "process_cycle", original)
    runner.run_cycle(db, _settings(), fetcher=make_fetcher(), now_ms=NOW)
    assert store.latest_assessment(db) is not None
    assert int(db.get_meta("events:transitions_processed")) == pending[-1]["id"]


def test_assessment_was_not_known_while_its_cycle_was_still_calculating(db, monkeypatch):
    import app.models as models
    stamps = iter([NOW, NOW + 50_000])
    monkeypatch.setattr(models, "now_ms", lambda: next(stamps, NOW + 50_000))
    runner.run_cycle(db, _settings(), fetcher=make_fetcher())
    market = store.latest_assessment(db)
    assert market["available_at"] > market["evaluated_at"]
    assert store.assessment_as_of(db, NOW + 1_000) is None


def test_historical_statistics_from_old_unchecked_method_are_excluded(db):
    settings = _settings()
    settings.events_stats_min_n = 1
    store.upsert_metric(db, series="basket_day_return", bucket_start=NOW - DAY,
                        payload={"complete": True, "median_return": 0.1, "basket_version": "old"})
    assert runner._stats(db, settings)["available"] is False


def test_recent_bars_at_different_hours_are_not_a_shared_market_window():
    basket = basket_from_static(["BTC", "ETH"], day_start_ms=0)
    bars = {"BTC": _hour_bars("BTCUSDT", NOW), "ETH": _hour_bars("ETHUSDT", NOW - HOUR)}
    result = basket_metrics(basket, bars, now_ms=NOW, max_age_ms=2 * HOUR)
    assert result["coverage"]["ok"] == 1


def test_fading_can_still_be_above_baseline_and_a_new_spike_starts_new_episode():
    from app.events.episodes import liquidation_history
    from app.events.rules import get_rule
    rows, _, _, end = _scenario()
    for r in rows:
        if end - 3 * W4 <= r["time"] < end:
            r["aggregated_long_liquidation_usd"] = "400"
            r["aggregated_short_liquidation_usd"] = "40"
    data = {r["time"]: {"long_usd": r["aggregated_long_liquidation_usd"],
                         "short_usd": r["aggregated_short_liquidation_usd"]} for r in rows}
    history = liquidation_history({"BTC": data}, now=NOW, baseline_days=30,
        thresholds=get_rule("LIQ_PRESSURE").thresholds, memory_ms=10 * DAY, min_coverage=0.8)
    assert history[-1]["episode"]["phase"] == "fading"
    assert history[-1]["episode"]["active"] is True
    data[end] = {"long_usd": "800", "short_usd": "50"}
    history = liquidation_history({"BTC": data}, now=end + W4 + 1, baseline_days=30,
        thresholds=get_rule("LIQ_PRESSURE").thresholds, memory_ms=10 * DAY, min_coverage=0.8)
    assert history[-1]["episode"]["started_at"] == end
    assert history[-1]["episode"]["phase"] == "intensifying"
