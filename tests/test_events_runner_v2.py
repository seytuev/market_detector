"""Цикл runner v2 (спека §11): независимые источники, корзина, эпизоды
ликвидаций, оценка рынка, идемпотентность. AC19 в том числе."""
from __future__ import annotations

from datetime import datetime

from app.adapters.coinglass import CoinglassError
from app.config import Settings
from app.db import Database
from app.events import store
from app.events.runner import run_cycle
from app.events.timeutil import MSK

DAY = 86_400_000
HOUR = 3_600_000
W4 = 14_400_000
# Четверг 14:00 МСК — рабочий момент без границ дня
NOW = int(datetime(2026, 10, 8, 14, 0, tzinfo=MSK).timestamp() * 1000)

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _db() -> Database:
    return Database(":memory:")


def _settings() -> Settings:
    settings = Settings()
    settings.events_basket_static = "BTC,ETH,SOL"
    settings.events_notify_enabled = False
    return settings


def _hour_bars(pair: str, now: int, *, days: int = 35, start_price: float = 100.0,
               drift: float = 0.0) -> list[dict]:
    end = now - (now % HOUR)
    bars = []
    t, price = end - days * DAY, start_price
    while t < end:
        close = price * (1 + drift)
        bars.append({"start": t, "end": t + HOUR, "open": price,
                     "close": close, "quality": "ok"})
        price = close
        t += HOUR
    return bars


def _liq_rows(now: int, *, count: int = 180, long_usd: float = 100.0,
              short_usd: float = 60.0, hot_windows: int = 0,
              hot_long: float = 400.0, hot_short: float = 100.0) -> list[dict]:
    current = now - (now % W4)
    rows = []
    for i in range(count):
        start = current - i * W4
        hot = 0 < i <= hot_windows
        forming_hot = i == 0 and hot_windows > 0
        rows.append({
            "time": start,
            "aggregated_long_liquidation_usd": str(
                hot_long if (hot or forming_hot) else long_usd),
            "aggregated_short_liquidation_usd": str(
                hot_short if (hot or forming_hot) else short_usd),
        })
    return rows


def _legacy_day(now: int, back: int = 1) -> int:
    return (now - back * DAY) // DAY * DAY


def make_fetcher(*, liq_rows=None, liq_exc: Exception | None = None,
                 oi_exc: Exception | None = None,
                 funding_exc: Exception | None = None,
                 drift: float = 0.0) -> dict:
    def prices(_settings, pairs, start_ms, end_ms):
        return {p: _hour_bars(p, end_ms, drift=drift) for p in pairs}, []

    def liq(_cg, symbol, interval, limit):
        if liq_exc is not None:
            raise liq_exc
        if interval == "1d":
            return [{
                "time": _legacy_day(NOW),
                "aggregated_long_liquidation_usd": "10",
                "aggregated_short_liquidation_usd": "5",
            }]
        return liq_rows if liq_rows is not None else _liq_rows(NOW)

    def oi(_cg, symbol, unit, interval, limit):
        if oi_exc is not None:
            raise oi_exc
        if symbol == "BTC" and interval == "1d" and limit == 150:
            if unit == "coin":
                return [{"time": _legacy_day(NOW), "open": "1", "high": "1.2",
                         "low": "0.9", "close": "1.1"}]
                return []
            return [{"time": _legacy_day(NOW), "open": "10", "high": "12",
                     "low": "9", "close": "11"}]
        return [{"time": _legacy_day(NOW), "open": "10", "high": "12",
                 "low": "9", "close": "11"}]

    def funding(_cg, exchange, symbol, interval, limit):
        if funding_exc is not None:
            raise funding_exc
        return [{"time": _legacy_day(NOW), "open": "0.01", "high": "0.01",
                 "low": "0.01", "close": "0.002"}]

    return {
        "pairs": lambda _settings: set(PAIRS),
        "prices": prices,
        "liq": liq,
        "oi": oi,
        "funding": funding,
        "cmc_listings": lambda: None,
    }


def _count(db, table: str) -> int:
    return db.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


CONTRACT_KEYS = {
    "assessment_id", "scope", "timezone", "evaluated_at", "available_at",
    "rule_version", "basket_version", "direction", "events_direction",
    "time_direction", "contradictory", "quality", "quality_by_dependency",
    "coverage", "market_line", "subtitle", "conflict_note", "arrow",
    "reasons", "unknown_dependencies", "active_facts", "time_context",
    "statistics", "btc_background", "factor_agreement", "next_review_at",
}


def test_price_history_uses_bulk_writes_and_retains_partial_source_quality(db, monkeypatch):
    from app.events import runner
    from app.events.basket import basket_from_static

    def scalar_write(*args, **kwargs):
        raise AssertionError("price history must not write one request per candle")

    monkeypatch.setattr(runner, "upsert_bucket", scalar_write)
    basket = basket_from_static(["BTC", "ETH"], day_start_ms=NOW // DAY * DAY)
    source = _hour_bars("BTCUSDT", NOW)
    prices = lambda *args: ({"BTCUSDT": source}, ["ETHUSDT"])
    rows, metrics = runner._prices_cycle(db, _settings(), {"prices": prices}, basket, NOW)
    saved = store.load_buckets(db, runner.scope_id("binance", "klines", "BTCUSDT", "1h"), "price", "1h")
    assert len(saved) == len(source) == len(rows["BTC"])
    assert saved[-1]["close"] == source[-1]["close"]
    assert saved[-1]["available_at"] == NOW
    assert store.get_source_states(db)["price"]["quality"] == "partial"


def test_full_cycle_happy_path():
    db, settings = _db(), _settings()
    result = run_cycle(db, settings, fetcher=make_fetcher(), now_ms=NOW)
    assert result["market_line"] == "Нейтрально"
    assert result["assessment_id"].startswith("asmt_")

    assessment = store.latest_assessment(db)
    assert assessment is not None
    assert CONTRACT_KEYS <= set(assessment)
    assert assessment["direction"] == "neutral"
    assert assessment["events_direction"] == "neutral"
    assert assessment["quality"] == "ok"
    assert assessment["basket_version"]
    assert assessment["coverage"]["sufficient"] is True
    assert assessment["btc_background"]["label"] == "Фон BTC"
    assert assessment["btc_background"]["direction_note"]
    assert assessment["statistics"]["available"] is False
    assert assessment["statistics"]["text"] == "Статистика недостаточна"

    basket = store.latest_basket(db)
    assert [m["symbol"] for m in basket["members"]] == ["BTC", "ETH", "SOL"]
    assert basket["source"] == "static"

    day_metrics = store.metric_series(db, "basket_day_return")
    assert len(day_metrics) == 10
    assert all(m["complete"] for m in day_metrics)

    states = store.get_source_states(db)
    assert states["calendar"]["quality"] == "ok"
    assert states["price"]["quality"] == "ok"
    assert states["liquidations"]["quality"] == "ok"
    assert states["oi"]["quality"] == "ok"
    assert states["funding"]["quality"] == "ok"
    assert states["oi"]["payload"]["market_oi_usd"] > 0

    # факт объёма без направления (§6): эпизодных условий нет
    facts = store.query_facts(db, active_now_ms=NOW)
    assert len(facts) == 1
    assert facts[0]["rule_id"] == "LIQ_FACT"
    assert facts[0]["direction"] is None

    health = result["health"]
    assert health["quality"] == "ok"
    assert "legacy_stale" not in health
    assert health["sources"]["calendar"]["quality"] == "ok"
    assert health["basket_version"] == assessment["basket_version"]
    assert _count(db, "events_snapshot") == 1  # legacy BTC-снимок


def test_ac19_coinglass_failure_does_not_stop_calendar():
    db, settings = _db(), _settings()
    fetcher = make_fetcher(
        liq_exc=CoinglassError("сбой Coinglass"),
        oi_exc=CoinglassError("сбой Coinglass"),
        funding_exc=CoinglassError("сбой Coinglass"),
    )
    result = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    states = store.get_source_states(db)
    assert states["calendar"]["quality"] == "ok"
    assert states["price"]["quality"] == "ok"
    assert states["liquidations"]["quality"] == "unavailable"

    assessment = store.latest_assessment(db)
    assert assessment["events_direction"] == "unknown"
    assert "liquidations" in assessment["unknown_dependencies"]
    assert assessment["direction"] == "unknown"
    assert assessment["time_direction"] == "neutral"
    # связанные правила неизвестны, но факты не выдумываются
    assert assessment["active_facts"] == []

    assert result["health"]["legacy_stale"] is True
    assert _count(db, "events_snapshot") == 0  # legacy-снимок не тронут


def test_idempotent_rerun_same_instant():
    db, settings = _db(), _settings()
    fetcher = make_fetcher()
    first = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    counts = {t: _count(db, t) for t in (
        "events_fact", "events_fact_transition", "events_assessment",
        "events_publication", "events_bundle", "events_bucket",
    )}
    second = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    assert second["assessment_id"] == first["assessment_id"]
    for table, before in counts.items():
        assert _count(db, table) == before, table


def test_liq_pressure_episode_gives_short_assessment():
    db, settings = _db(), _settings()
    fetcher = make_fetcher(
        liq_rows=_liq_rows(NOW, hot_windows=3),
        drift=-0.002,
    )
    result = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    assert result["direction"] == "short"
    assessment = store.latest_assessment(db)
    assert assessment["market_line"] == "↓ SHORT"
    assert assessment["arrow"] == "down"
    facts = store.query_facts(db, active_now_ms=NOW)
    pressure = [f for f in facts if f["rule_id"] == "LIQ_PRESSURE"]
    assert len(pressure) == 1
    assert pressure[0]["direction"] == "short"
    assert pressure[0]["time_precision"] == "window"
    # появление факта опубликовано в открытом пакете
    bundle = store.open_bundle(db)
    kinds = [p["kind"] for p in store.bundle_publications(db, bundle["id"])]
    assert "fact_appeared" in kinds
    # второй проход — факт обновляется, а не дублируется
    run_cycle(db, settings, fetcher=fetcher, now_ms=NOW + 60_000)
    facts = store.query_facts(db, active_now_ms=NOW + 60_000)
    assert len([f for f in facts if f["rule_id"] == "LIQ_PRESSURE"]) == 1


def test_assessment_id_changes_with_content():
    db, settings = _db(), _settings()
    run_cycle(db, settings, fetcher=make_fetcher(), now_ms=NOW)
    first = store.latest_assessment(db)["assessment_id"]
    run_cycle(db, settings, fetcher=make_fetcher(), now_ms=NOW + 60_000)
    second = store.latest_assessment(db)["assessment_id"]
    assert first != second
    assert store.get_assessment(db, first) is not None
