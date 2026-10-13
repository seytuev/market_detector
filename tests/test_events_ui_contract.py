"""Контракт UI «События и время · Крипторынок» (ТЗ 11.10.2026 §8).

Проверяет, что статики раздаются, что Скринер подключает компактную карточку,
и что ответы /api/events/* содержат ключи, которые читает events.js.
После run_cycle с фейковыми источниками overview ОБЯЗАН содержать
ненулевой блок ``market`` с полным набором ключей рыночного снимка.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.events.runner import run_cycle
from app.events.timeutil import MSK
from app.web.api import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DAY = 86_400_000
HOUR = 3_600_000
# Четверг 14:00 МСК — рабочий момент без границ дня (как в test_events_runner_v2)
NOW = int(datetime(2026, 10, 8, 14, 0, tzinfo=MSK).timestamp() * 1000)

MARKET_KEYS = {
    "assessment_id", "scope", "timezone", "evaluated_at", "rule_version",
    "direction", "events_direction", "time_direction", "contradictory",
    "quality", "coverage", "market_line", "subtitle", "conflict_note",
    "arrow", "reasons", "unknown_dependencies", "active_facts",
    "time_context", "statistics", "btc_background", "factor_agreement",
    "next_review_at",
}


@pytest.fixture()
def client() -> TestClient:
    settings = Settings()
    settings.auth_token = TOKEN
    settings.events_notify_enabled = False
    return TestClient(create_app(Database(":memory:"), settings))


def _hour_bars(pair: str, now: int, *, days: int = 35, start_price: float = 100.0):
    end = now - (now % HOUR)
    bars = []
    t, price = end - days * DAY, start_price
    while t < end:
        bars.append({"start": t, "end": t + HOUR, "open": price,
                     "close": price, "quality": "ok"})
        t += HOUR
    return bars


def _fetcher(now: int) -> dict:
    """Минимальные фейковые источники (по образцу tests/test_events_runner_v2)."""
    day = (now - 2 * DAY) // DAY * DAY
    w4 = 14_400_000
    current = now - (now % w4)

    def liq(_cg, symbol, interval, limit):
        if interval == "1d":
            return [{
                "time": day,
                "aggregated_long_liquidation_usd": "10",
                "aggregated_short_liquidation_usd": "5",
            }]
        return [{
            "time": current - i * w4,
            "aggregated_long_liquidation_usd": "10",
            "aggregated_short_liquidation_usd": "5",
        } for i in range(4)]

    return {
        "pairs": lambda _settings: {"BTCUSDT", "ETHUSDT", "SOLUSDT"},
        "prices": lambda _s, pairs, start_ms, end_ms: (
            {p: _hour_bars(p, end_ms) for p in pairs}, [],
        ),
        "liq": liq,
        "oi": lambda _cg, symbol, unit, interval, limit: [{
            "time": day, "open": "10", "high": "12", "low": "9", "close": "11",
        }],
        "funding": lambda _cg, exchange, symbol, interval, limit: [{
            "time": day, "open": "0.01", "high": "0.01",
            "low": "0.01", "close": "0.002",
        }],
        "cmc_listings": lambda: None,
    }


@pytest.fixture()
def client_with_market() -> TestClient:
    """Клиент после реального run_cycle: overview.market обязан быть."""
    db = Database(":memory:")
    settings = Settings()
    settings.auth_token = TOKEN
    settings.events_basket_static = "BTC,ETH,SOL"
    settings.events_notify_enabled = False
    run_cycle(db, settings, fetcher=_fetcher(NOW), now_ms=NOW)
    return TestClient(create_app(db, settings))


def test_statics_served_and_screener_mounts_card(client: TestClient):
    events_html = client.get("/events.html")
    assert events_html.status_code == 200
    assert 'section=sources#settings' in events_html.text
    assert 'href="/#help"' in events_html.text
    assert 'data-mode="sources"' not in events_html.text

    alt_html = client.get("/alt.html")
    assert alt_html.status_code == 200
    assert 'id="h1-events-context"' in alt_html.text
    assert '<script src="/events.js"></script>' in alt_html.text
    assert "Рыночный контекст показан отдельно" in alt_html.text

    events_js = client.get("/events.js")
    assert events_js.status_code == 200
    for marker in (
        "События и время · Крипторынок",
        "Согласованность факторов",
        "Статистика недостаточна",
        "Открыть рынок",
        "is-unknown",
        "is-mixed",
        "калиброванная вероятность",
        "историческая частота",
        "Фон BTC",
    ):
        assert marker in events_js.text, marker

    style = client.get("/style.css")
    assert style.status_code == 200
    for marker in (
        ".events-side-state .side-state.is-mixed",
        ".events-side-state .side-state.is-unknown",
        ".events-facts",
        ".events-bundle",
        ".events-src-row",
        ".alt-market-note",
    ):
        assert marker in style.text, marker


def test_overview_contract(client_with_market: TestClient):
    body = client_with_market.get("/api/events/overview", headers=AUTH)
    assert body.status_code == 200
    data = body.json()
    # legacy-ключи, которые читает запасной рендер
    assert "ready" in data and "market_line" in data and "health" in data
    # бэкенд отдаёт рыночный снимок: market обязателен, не None
    market = data["market"]
    assert market is not None
    assert MARKET_KEYS <= set(market), MARKET_KEYS - set(market)
    assert market["scope"] == "crypto_market"
    assert market["direction"] in {"long", "short", "neutral", "mixed", "unknown"}
    assert market["arrow"] in {"up", "down", "flat", "conflict", "unknown"}
    assert isinstance(market["reasons"], list)
    assert isinstance(market["active_facts"], list)
    assert "market_age_seconds" in data and "market_stale" in data


def test_feed_calendar_rules_health_contract(client_with_market: TestClient):
    journal = client_with_market.get("/api/events/journal", headers=AUTH)
    assert journal.status_code == 200
    payload = journal.json()
    assert isinstance(payload.get("items"), list)
    if "bundles" in payload:
        assert isinstance(payload["bundles"], list)
        for bundle in payload["bundles"]:
            assert {"id", "kind", "publications", "deliveries"} <= set(bundle)

    calendar = client_with_market.get("/api/events/calendar", headers=AUTH)
    assert calendar.status_code == 200
    market = calendar.json()["market"]
    assert market is not None
    assert "time_context" in market and "upcoming" in market

    rules = client_with_market.get("/api/events/rules", headers=AUTH)
    assert rules.status_code == 200
    payload = rules.json()
    assert isinstance(payload.get("rules"), list)
    if payload.get("registry"):
        for rule in payload["registry"]:
            assert {"rule_id", "rule_class", "confirmation", "direction"} <= set(rule)

    health = client_with_market.get("/api/events/health", headers=AUTH)
    assert health.status_code == 200
    assert "quality" in health.json()
