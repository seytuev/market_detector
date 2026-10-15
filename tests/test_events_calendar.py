"""Calendar statistics: causal conditioning, missing days and history ingestion."""
import json
from datetime import datetime

import pytest

from app.events import store
from app.events.calendar import (
    BACKFILL_DAYS, BTC_HISTORY_START, BTC_SERIES, CALENDAR_VERSION,
    calendar_statistics, daily_prices,
)
from app.events.calendar_history import STATE_KEY, update_calendar_history
from app.events.revalidation import MODEL_VERSION
from app.events.timeutil import MSK, msk_day_start_ms
from app.web import events_api
from tests.test_events_api_v2 import _client, _get, _settings

DAY, HOUR = 86_400_000, 3_600_000
NOW = int(datetime(2026, 10, 15, 14, tzinfo=MSK).timestamp() * 1000)
TODAY = msk_day_start_ms(NOW)


def _days(returns, start=None, *, ranges=None):
    start = start if start is not None else TODAY - len(returns) * DAY
    return [{"bucket_start": start + i * DAY, "return": ret,
             "range": ranges[i] if ranges else 0.04, "complete": True,
             "method_version": CALENDAR_VERSION} for i, ret in enumerate(returns)]


def _bars(start, end, *, ret=0.01):
    return [{"start": t, "end": t + HOUR, "open": 100, "close": 100 * (1 + ret),
             "high": 105, "low": 95, "quality": "ok"} for t in range(start, end, HOUR)]


def _pattern(result, name):
    return next(r for r in result["patterns"] if r["id"] == name)


def test_counts_match_hand_calculated_previous_day_sample():
    result = calendar_statistics(_days([.01, -.02, .03, -.04, -.05]), target=TODAY, now=NOW, min_n=1)
    sample = _pattern(result, "previous_day")
    assert (sample["n"], sample["up"], sample["down"], sample["flat"]) == (2, 1, 1, 0)
    assert sample["up_frequency"] == .5
    assert sample["mean_return"] == pytest.approx(-.01)
    assert sample["mean_range"] == .04
    assert sample["up_interval"]["low"] < .5 < sample["up_interval"]["high"]


def test_target_and_later_returns_cannot_change_historical_forecast():
    rows = _days(([.01, -.02, -.01, .03] * 60))
    target = TODAY - 30 * DAY
    first = calendar_statistics(rows, target=target, now=NOW, min_n=3)
    for row in rows:
        if row["bucket_start"] >= target:
            row.update({"return": 100, "range": 500})
    second = calendar_statistics(rows, target=target, now=NOW, min_n=3)
    assert first["patterns"] == second["patterns"]
    assert first["baseline"] == second["baseline"]
    assert first["mode"] == "reconstruction"
    assert all(not p["period"] or p["period"]["to"] < target for p in first["patterns"])


def test_missing_days_do_not_bridge_previous_day_or_streak():
    rows = _days([-.01, -.01, .02, -.03, -.04])
    del rows[-3]
    result = calendar_statistics(rows, target=TODAY, now=NOW, min_n=1)
    assert not _pattern(result, "streak")["condition_known"]
    assert _pattern(result, "previous_day")["condition_known"]
    rows.pop()
    result = calendar_statistics(rows, target=TODAY, now=NOW, min_n=1)
    assert not _pattern(result, "previous_day")["condition_known"]


@pytest.mark.parametrize("length", [1, 2, 3, 4, 7])
def test_streak_length_is_exact_until_four_or_more(length):
    rows = _days([.01] + [-.01] * length)
    sample = _pattern(calendar_statistics(rows, target=TODAY, now=NOW, min_n=1), "streak")
    assert sample["condition_known"]
    assert ("не менее 4" if length >= 4 else f"{length} дней") in sample["label"]


def test_future_day_cannot_use_forming_day_even_if_marked_complete():
    rows = _days([-.01] * 20) + _days([9.0], start=TODAY)
    result = calendar_statistics(rows, target=TODAY + DAY, now=NOW, min_n=1)
    assert result["mode"] == "future"
    assert not _pattern(result, "previous_day")["condition_known"]
    assert result["baseline"]["n"] == 20


def test_small_sample_shows_counts_without_percentages():
    result = calendar_statistics(_days([.01, 0, -.01]), target=TODAY, now=NOW)
    assert result["baseline"]["n"] == 3
    assert result["baseline"]["flat"] == 1
    for sample in [result["baseline"], *result["patterns"]]:
        assert not sample["available"]
        assert sample["up_frequency"] is None
        assert sample["mean_return"] is None
        assert sample["up_interval"] is None


def test_zero_is_separate_from_red_and_nonfinite_rows_are_excluded():
    rows = _days([.01, 0, -.01, 0, .01, 0, float("nan"), float("inf")])
    result = calendar_statistics(rows, target=TODAY - 2 * DAY, now=NOW, min_n=1)
    sample = _pattern(result, "previous_day")
    assert sample["n"] == 2 and sample["up"] == 1 and sample["down"] == 1
    assert result["baseline"]["n"] == 6
    assert len(result["completed_days"]) == 6


def test_monday_tuesday_pair_uses_same_target_weekday_only():
    rows = _days(([.02, -.01, .03, -.02, .01, -.01, .01] * 30))
    result = calendar_statistics(rows, target=TODAY, now=NOW, min_n=1)
    sample = _pattern(result, "mon_tue")  # TODAY is Thursday.
    assert sample["condition_known"]
    assert sample["n"] == 29
    assert sample["up"] == 29


def test_range_classification_uses_prior_baseline_and_never_future_ranges():
    rows = _days([.01, -.01] * 60)
    rows[-1]["range"] = .12
    result = calendar_statistics(rows, target=TODAY, now=NOW, min_n=1)
    assert "широким" in _pattern(result, "previous_range")["label"]
    assert _pattern(result, "red_wide")["condition_known"]
    assert _pattern(result, "red_wide")["n"] == 0  # yesterday's outcome is still unknown
    assert "1,5" in result["range_note"]


def test_daily_ohlc_requires_all_24_hours_and_excludes_current_day():
    bars = _bars(TODAY - 2 * DAY, TODAY + 12 * HOUR)
    rows = daily_prices(bars, now=NOW)
    assert len(rows) == 2
    assert rows[0]["return"] == pytest.approx(.01)
    assert rows[0]["range"] == pytest.approx(.10)
    # Remove an H1 in the prior Moscow day, not a UTC day.
    bars = [b for b in bars if b["start"] != TODAY - DAY + 5 * HOUR]
    assert [r["bucket_start"] for r in daily_prices(bars, now=NOW)] == [TODAY - 2 * DAY]


def test_missing_high_low_does_not_invent_an_intraday_range():
    bars = _bars(TODAY - DAY, TODAY)
    del bars[3]["high"]
    del bars[3]["low"]
    assert daily_prices(bars, now=NOW)[0]["range"] is None


def test_incremental_backfill_is_bounded_and_keeps_progress(db):
    calls = []
    def fetch(settings, pairs, start, end):
        calls.append((start, end))
        return {"BTCUSDT": _bars(start, end)}, []
    settings = object()
    update_calendar_history(db, settings, {"calendar_prices": fetch}, {}, now=NOW)
    assert calls == [(TODAY - BACKFILL_DAYS * DAY, TODAY)]
    assert len(store.metric_series(db, BTC_SERIES)) == BACKFILL_DAYS
    assert json.loads(db.get_meta(STATE_KEY))["backfill_before"] == calls[0][0]
    update_calendar_history(db, settings, {"calendar_prices": fetch}, {}, now=NOW)
    assert len(calls) == 2
    assert calls[1] == (TODAY - 2 * BACKFILL_DAYS * DAY, TODAY - BACKFILL_DAYS * DAY)
    assert len(store.metric_series(db, BTC_SERIES)) == 2 * BACKFILL_DAYS


def test_failed_history_does_not_advance_cursor_or_hide_saved_days(db):
    def fail(*args):
        raise RuntimeError("outage")
    update_calendar_history(db, object(), {"calendar_prices": fail}, {"BTC": _bars(TODAY - DAY, TODAY)}, now=NOW)
    state = json.loads(db.get_meta(STATE_KEY))
    assert state["backfill_before"] == TODAY
    assert state["quality"] == "partial"
    assert len(store.metric_series(db, BTC_SERIES)) == 1


def test_custom_price_fetcher_never_makes_implicit_network_request(db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected network request")
    monkeypatch.setattr("app.events.sources.fetch_prices", forbidden)
    update_calendar_history(db, object(), {"prices": forbidden}, {"BTC": _bars(TODAY - DAY, TODAY)}, now=NOW)
    assert len(store.metric_series(db, BTC_SERIES)) == 1


def test_partial_backfill_keeps_gaps_visible_instead_of_zero_days(db):
    def fetch(settings, pairs, start, end):
        return {"BTCUSDT": _bars(start + DAY, end)}, []
    update_calendar_history(db, object(), {"calendar_prices": fetch}, {}, now=NOW)
    state = json.loads(db.get_meta(STATE_KEY))
    assert state["missing_days"] == 1
    assert state["days"] == BACKFILL_DAYS - 1


def test_history_uses_bulk_metric_writes(db, monkeypatch):
    def scalar(*args, **kwargs):
        pytest.fail("history must not write one database request per day")
    monkeypatch.setattr(store, "upsert_metric", scalar)
    update_calendar_history(db, object(), {"prices": scalar}, {"BTC": _bars(TODAY - 35 * DAY, TODAY)}, now=NOW)
    assert len(store.metric_series(db, BTC_SERIES)) == 35


def test_missing_days_are_retried_after_initial_backfill(db):
    db.set_meta(STATE_KEY, json.dumps({"backfill_before": BTC_HISTORY_START}))
    calls = []
    def fetch(settings, pairs, start, end):
        calls.append((start, end))
        return {"BTCUSDT": _bars(start, end)}, []
    update_calendar_history(db, object(), {"calendar_prices": fetch}, {}, now=NOW)
    assert (BTC_HISTORY_START, BTC_HISTORY_START + DAY) in calls
    assert json.loads(db.get_meta(STATE_KEY))["missing_days"] > 0


def test_impossible_returns_and_negative_ranges_are_not_statistics():
    rows = _days([-.01, .01, -2])
    rows[0]["range"] = -1
    result = calendar_statistics(rows, target=TODAY, now=NOW, min_n=1)
    assert result["baseline"]["n"] == 2
    assert result["baseline"]["range_n"] == 1


def test_calendar_api_scope_date_and_history_do_not_mix(tmp_path, db, monkeypatch):
    monkeypatch.setattr(events_api, "now_ms", lambda: NOW)
    settings = _settings(tmp_path)
    settings.events_stats_min_n = 1
    for row in _days([.01, -.02, .03, -.04, -.05]):
        store.upsert_metric(db, series=BTC_SERIES, bucket_start=row["bucket_start"], payload=row)
        store.upsert_metric(db, series="basket_day_return", bucket_start=row["bucket_start"],
                            payload={"median_return": .02, "complete": True,
                                     "historical_composition_known": True, "method_version": MODEL_VERSION})
    client = _client(db, settings)
    btc = _get(client, "/api/events/calendar?day=2026-10-15&scope=BTC").json()["market"]
    basket = _get(client, "/api/events/calendar?day=2026-10-15&scope=basket").json()["market"]
    assert btc["forecast"]["baseline"]["up"] == 2
    assert basket["forecast"]["baseline"]["up"] == 5
    assert not btc["forecast"]["used_for_market_direction"]
    assert btc["time_context"]["completed_days"][-1]["sign"] == -1
    old = _get(client, "/api/events/calendar?day=2026-10-13").json()["market"]["forecast"]
    assert old["baseline"]["n"] == 3
    assert old["mode"] == "reconstruction"


@pytest.mark.parametrize("query", ["day=2026-02-31", "day=oops", "day=2026-1-1", "day=1000-01-01", "scope=ETH"])
def test_calendar_rejects_invalid_query(tmp_path, db, query):
    assert _get(_client(db, _settings(tmp_path)), f"/api/events/calendar?{query}").status_code == 400
