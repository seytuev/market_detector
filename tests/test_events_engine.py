"""Приёмка модуля «События»: формулы, гейт и контракт Coinglass без живого ключа."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.adapters.coinglass import CapabilityError, interpret_body, redact
from app.config import Settings
from app.db import Database
from app.events.evaluate import (
    EvaluationInput,
    FundingBar,
    LiqBar,
    OiBar,
    PriceBar,
    _liq_stats,
    evaluate,
    market_direction,
    median,
)
from app.events.mathutil import DAY_MS, percentile
from app.events.runner import run_cycle
from app.events.store import list_journal
from app.models import Candle, Instrument
from app.web.api import create_app

T0 = 1_700_000_000_000 - (1_700_000_000_000 % DAY_MS)
# Полдень UTC, не тихий час Москвы и не 09:00.
NOW = T0 + 120 * DAY_MS + 12 * 3_600_000
TOKEN = "test-token"


def D(value) -> Decimal:
    return Decimal(str(value))


def price(i, o, h, l, c, closed=True) -> PriceBar:
    return PriceBar(T0 + i * DAY_MS, D(o), D(h), D(l), D(c), closed)


def liq(i, long, short, closed=True) -> LiqBar:
    return LiqBar(T0 + i * DAY_MS, D(long), D(short), closed)


def oi(i, open_, close, closed=True) -> OiBar:
    return OiBar(T0 + i * DAY_MS, D(open_), D(close), closed)


def fund(i, percent, closed=True) -> FundingBar:
    return FundingBar(T0 + i * DAY_MS, D(percent), closed)


def downtrend(n, start=200) -> list[PriceBar]:
    bars = []
    px = D(start)
    for i in range(n):
        nxt = px - D(1)
        bars.append(PriceBar(T0 + i * DAY_MS, px, px, nxt, nxt, True))
        px = nxt
    return bars


def flat_liq(n, long=100, short=100) -> list[LiqBar]:
    return [liq(i, long, short) for i in range(n)]


def gate(snap, setup):
    return next(item for item in snap["strategy_gates"] if item["setup"] == setup)


def test_median_even_and_percentile():
    assert median([D(1), D(2), D(3), D(4)]) == D("2.5")
    assert percentile([D(10), D(30)], D("0.5")) == D(20)


def test_share_and_multiple_boundaries():
    base = flat_liq(90)
    exact = _liq_stats(base + [liq(90, 60, 40)], T0 + 90 * DAY_MS)
    assert exact["liq01"] is False
    above = _liq_stats(base + [liq(90, 61, 39)], T0 + 90 * DAY_MS)
    assert above["liq01"] is True
    mult = _liq_stats(base + [liq(90, 190, 40)], T0 + 90 * DAY_MS)
    assert mult["long_multiple"] == D("1.9")
    assert mult["liq02"] is True


def test_total_median_is_not_sum_of_medians():
    rows = []
    for i in range(90):
        kind = i % 3
        if kind == 0:
            rows.append(liq(i, 1, 100))
        elif kind == 1:
            rows.append(liq(i, 2, 0))
        else:
            rows.append(liq(i, 100, 0))
    stats = _liq_stats(rows + [liq(90, 10, 10)], T0 + 90 * DAY_MS)
    assert stats["m_t"] != stats["m_l"] + stats["m_s"]


def test_unclosed_day_does_not_extend_streak():
    bars = [price(i, 10, 11, 9, 9) for i in range(3)]
    bars.append(price(3, 9, 10, 8, 8, closed=False))
    snap = evaluate(EvaluationInput(symbol="BTC", now_ms=NOW, price=bars))
    assert snap["streak"]["length"] == 3
    assert snap["streak"]["provisional"] == "red"
    assert gate(snap, "V")["status"] == "not_applicable"


def test_gap_makes_streak_unknown():
    bars = [price(0, 10, 11, 8, 9), price(2, 9, 10, 7, 8)]
    snap = evaluate(EvaluationInput(symbol="BTC", now_ms=NOW, price=bars))
    assert snap["streak"]["status"] == "unknown"
    assert "пропуском" in snap["market_line"]


def test_doji_breaks_streak():
    bars = [
        price(0, 10, 11, 8, 9),
        price(1, 9, 10, 8, 8),
        price(2, 8, 9, 7, 8),
        price(3, 8, 9, 7, 7),
    ]
    snap = evaluate(EvaluationInput(symbol="BTC", now_ms=NOW, price=bars))
    assert snap["streak"]["length"] == 1


def test_oi_high_equality_and_gap_and_position():
    from app.events.evaluate import oi_range30

    flat = [oi(i, 10, 10) for i in range(30)]
    same = oi_range30(flat + [oi(30, 10, 10)], T0 + 30 * DAY_MS)
    assert same["new_high30"] is False
    assert same["at_boundary"] == "at_boundary"
    assert same["position30"] is None
    varied = [oi(i, 10, D(10) + D(i % 5)) for i in range(30)]
    higher = oi_range30(varied + [oi(30, 10, 30)], T0 + 30 * DAY_MS)
    assert higher["new_high30"] is True
    assert higher["position30"] > 1
    short = oi_range30([oi(i, 10, 10) for i in range(29)] + [oi(29, 10, 12)], T0 + 29 * DAY_MS)
    assert short["quality"] == "gap"


def test_funding_percent_boundaries_are_not_a_settlement():
    bars = downtrend(2)
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=bars,
        funding=[fund(1, "0.01")],
        rate_kind="indicative",
    ))
    assert snap["funding"]["fraction"] == "0.0001"
    assert snap["funding"]["sign"] == "positive"
    assert snap["funding"]["above_high_indicative"] is False
    assert snap["study_percents_on_card"] is False
    assert gate(snap, "A")["status"] != "passed"


def test_negative_indicative_funding_does_not_pass_gate():
    bars = downtrend(2)
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=bars,
        funding=[fund(1, "-0.002485")],
    ))
    assert snap["funding"]["sign"] == "negative"
    assert gate(snap, "G")["status"] == "unknown"
    assert "h1_bull_confirmation" in gate(snap, "G")["unknown_dependencies"]
    assert all(item["status"] != "passed" for item in snap["strategy_gates"])


def test_ac42_rising_oi_blocks_even_with_negative_funding():
    n = 91
    fresh = T0 + n * DAY_MS + 3_600_000
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=fresh, price=downtrend(n),
        liq=flat_liq(90) + [liq(90, 400, 50)],
        oi_coin=[oi(90, 100, 110)],
        funding=[fund(90, "-0.004")],
        h1_available=True, h1_hold=True,
        rate_kind="settled",
    ))
    item = gate(snap, "A")
    assert item["status"] == "blocked"
    assert "CASCADE_OI_RISING" in item["blocking_reasons"]
    assert "funding_settlement" in item["unknown_dependencies"] or item["status"] == "blocked"


def test_ac43_falling_oi_meets_oi_check_and_can_match_watch():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=downtrend(91),
        liq=flat_liq(90) + [liq(90, 400, 50)],
        oi_coin=[oi(90, 110, 100)],
        funding=[fund(90, "0.005")],
        h1_available=True, h1_hold=True,
        rate_kind="settled",
    ))
    item = gate(snap, "A")
    oi_check = next(c for c in item["checks"] if c["code"] == "oi")
    assert oi_check["status"] == "met"
    assert item["status"] == "passed"
    assert item["state"] == "watch_matched"
    assert "разрешение" not in item["title"]


def test_ac44_flat_oi_is_not_falling():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=downtrend(91),
        liq=flat_liq(90) + [liq(90, 400, 50)],
        oi_coin=[oi(90, 100, 100)],
        funding=[fund(90, "0.005")],
        h1_available=True, h1_hold=True,
        rate_kind="settled",
    ))
    item = gate(snap, "A")
    oi_check = next(c for c in item["checks"] if c["code"] == "oi")
    assert oi_check["status"] == "unmet"
    assert item["status"] != "passed"


def test_ac46_wide_day_does_not_require_falling_oi_and_oi02_blocks():
    prices = []
    px = D(100)
    for i in range(91):
        # узкие зелёные, последний день красный и широкий
        if i < 90:
            prices.append(PriceBar(T0 + i * DAY_MS, px, px + D("0.2"), px, px + D("0.1"), True))
            px = px + D("0.1")
        else:
            prices.append(PriceBar(T0 + i * DAY_MS, px, px + D(1), px - D(20), px - D(1), True))
    quiet = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=prices,
        liq=flat_liq(91, 10, 10),
        rate_kind="settled", funding=[fund(90, "0.001")],
    ))
    assert gate(quiet, "B")["status"] == "unknown"
    assert "wide_experimental" in gate(quiet, "B")["unknown_dependencies"]
    blocked = evaluate(EvaluationInput(
        symbol="BTC", now_ms=T0 + 91 * DAY_MS + 3_600_000, price=prices,
        liq=flat_liq(90, 10, 10) + [liq(90, 400, 40)],
        oi_coin=[oi(90, 100, 130)],
        rate_kind="settled", funding=[fund(90, "0.001")],
        h1_available=True, h1_hold=False,
    ))
    assert blocked["oi02"]["active"] is True
    assert gate(blocked, "B")["status"] == "blocked"


def test_ac47_misaligned_oi_is_unknown():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=downtrend(91),
        liq=flat_liq(90) + [liq(90, 400, 50)],
        oi_coin=[OiBar(T0 + 90 * DAY_MS + 3_600_000, D(110), D(100), True)],
        h1_available=True, h1_hold=True, rate_kind="settled",
        funding=[fund(90, "0.001")],
    ))
    assert snap["oi_coin"]["quality"] == "unknown"
    item = gate(snap, "A")
    assert next(c for c in item["checks"] if c["code"] == "oi")["status"] == "unknown"
    assert item["status"] != "passed"


def test_ac61_quiet_falling_oi_does_not_release():
    liqs = flat_liq(90) + [liq(90, 400, 50), liq(91, 10, 10)]
    coins = [oi(90, 100, 120), oi(91, 120, 90)]
    now = T0 + 91 * DAY_MS + 12 * 3_600_000
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=now, price=downtrend(92),
        liq=liqs, oi_coin=coins, h1_available=True, h1_hold=True,
    ))
    assert snap["oi02"]["active"] is True


def test_ac62_stale_expiry_is_not_passed():
    liqs = flat_liq(90) + [liq(90, 400, 50)] + [liq(i, 10, 10) for i in range(91, 100)]
    coins = [oi(90, 100, 120)]
    until = T0 + 90 * DAY_MS + 8 * DAY_MS
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=until + 1000, price=downtrend(100),
        liq=liqs, oi_coin=coins, data_stale=True,
        h1_available=True, h1_hold=True, rate_kind="settled",
        funding=[fund(99, "0.001")],
    ))
    assert snap["oi02"]["stale_expiry"] is True
    assert all(item["status"] != "passed" for item in snap["strategy_gates"])
    assert "устарели" in snap["service_line"]


def test_disabled_gate_is_unknown_and_risk_is_not_multiplied():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=downtrend(5), gate_enabled=False,
    ))
    assert gate(snap, "A")["status"] == "unknown"
    assert "FILTER_DISABLED" in gate(snap, "A")["unknown_dependencies"]
    assert snap["risk_multiplier"] is None


def test_foreign_scope_is_rejected():
    with pytest.raises(ValueError):
        evaluate(EvaluationInput(
            symbol="ETH", now_ms=NOW, asset_profile="ETH", scope_asset="BTC",
        ))


def test_derivatives_without_price_stay_visible():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=T0 + 91 * DAY_MS + 3_600_000,
        liq=flat_liq(90) + [liq(90, 400, 50)],
        oi_coin=[oi(90, 100, 120)],
        funding=[fund(90, "-0.002")],
    ))
    assert snap["oi02"]["active"] is True
    assert snap["liquidation"]["quality"] == "ok"
    assert snap["oi_coin"]["direction"] == "rising"
    assert snap["funding"]["sign"] == "negative"
    assert all(item["status"] != "passed" for item in snap["strategy_gates"])


def test_zero_funding_is_not_negative():
    snap = evaluate(EvaluationInput(
        symbol="BTC", now_ms=NOW, price=downtrend(2), funding=[fund(1, "0")],
    ))
    assert snap["funding"]["sign"] == "zero"
    assert gate(snap, "G")["status"] == "not_applicable"


def test_code_403_on_http_200_is_capability():
    with pytest.raises(CapabilityError):
        interpret_body(200, {"code": "403", "msg": "The requested interval is not available for your current API plan."})
    assert interpret_body(200, {"code": "0", "data": []})["data"] == []


def test_redact_removes_secret():
    assert "<redacted>" in redact("key=abc", "abc")
    assert "abc" not in redact("key=abc", "abc")


def test_runner_is_idempotent_and_api_hides_the_key(tmp_path):
    db = Database(":memory:")
    ins = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    db.insert_candles([
        Candle(ins, "D1", T0 + i * DAY_MS, T0 + (i + 1) * DAY_MS - 1,
               100, 101, 99, 100 - i, True, "test")
        for i in range(5)
    ])
    settings = Settings()
    settings.auth_token = TOKEN
    settings.db_path = str(tmp_path / "unused.db")
    settings.coinglass_api_key = "should-not-leak"
    settings.events_notify_enabled = False

    def fetcher():
        return {
            "liq": [{
                "time": T0 + 4 * DAY_MS,
                "aggregated_long_liquidation_usd": "10",
                "aggregated_short_liquidation_usd": "5",
            }],
            "oi_coin": [{"time": T0 + 4 * DAY_MS, "open": "1", "high": "1.2",
                         "low": "0.9", "close": "1.1"}],
            "oi_usd": [{"time": T0 + 4 * DAY_MS, "open": "10", "high": "12",
                        "low": "9", "close": "11"}],
            "funding": [{"time": T0 + 4 * DAY_MS, "open": "0.01", "high": "0.01",
                         "low": "0.01", "close": "0.002"}],
            "plan": {"level": "HOBBYIST"},
            "limits": {"api-key-max-limit": "30"},
        }

    first = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    written = len(list_journal(db, "BTC"))
    second = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    assert "should-not-leak" not in str(first) + str(second)
    assert len(list_journal(db, "BTC")) == written
    client = TestClient(create_app(db, settings))
    response = client.get("/api/events/overview", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["strategy_gates"]
    assert body["study_percents_on_card"] is False
    assert "should-not-leak" not in response.text
    assert "60%" not in body["market_line"]
    missing = client.get(
        "/api/ltf/events/999/context",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert missing.status_code == 404


def test_market_direction_priorities():
    # каскад/запрет разворота — давление вниз, наблюдения отскока — вверх
    assert market_direction({})["side"] is None
    assert market_direction({"post_high": {"active": True}})["side"] == "short"
    assert market_direction({"oi02": {"active": True}})["side"] == "short"
    assert market_direction({"liquidation": {"cascade": True}})["side"] == "short"
    # пост-высокий сильнее каскадной ситуации OI
    both_down = market_direction({"post_high": {"active": True},
                                  "oi02": {"active": True}})
    assert both_down["rule_id"] == "POST_HIGH_LONG_CASCADE"
    # наблюдения отскока — сторона лонга
    streak = market_direction({
        "situations": [{"rule_id": "RED_STREAK_REBOUND_WATCH"}],
        "streak": {"length": 5},
    })
    assert streak["side"] == "long"
    assert streak["text"].startswith("Серия снижения: 5")
    assert market_direction({
        "situations": [{"rule_id": "RED_WIDE_DAY_REBOUND_WATCH"}],
    })["side"] == "long"
    assert market_direction({
        "situations": [{"rule_id": "FUNDING_REBOUND_WATCH"}],
    })["side"] == "long"
    # направление событий не зависит от направления графика: поля H1 не читаются
    chart_says_bull = {"direction": "bull", "market_stage": "LONG · ждёт откат"}
    assert market_direction(chart_says_bull)["side"] is None
