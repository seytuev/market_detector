"""HTTP API событий v2 (спека 2026-10-11, §8, §10): рыночный блок обзора,
здоровье источников с возрастом, пакеты журнала с раздельными временами,
календарь реальных переключений, реестр правил, «известно тогда».

AC01, AC03, AC04, AC13–AC15 и обратная совместимость старых клиентов.
"""
from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.events import store
from app.events.runner import run_cycle
from app.events.timeutil import MSK
from app.web import events_api
from app.web.api import create_app

DAY = 86_400_000
HOUR = 3_600_000
W4 = 14_400_000
# Четверг 14:00 МСК — рабочий момент без границ дня (как в test_events_runner_v2)
NOW = int(datetime(2026, 10, 8, 14, 0, tzinfo=MSK).timestamp() * 1000)
TOKEN = "test-token"
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# --- Фейковые источники (по образцу tests/test_events_runner_v2.py) --------


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


def _legacy_day(now: int, back: int = 2) -> int:
    return (now - back * DAY) // DAY * DAY


def make_fetcher(now: int, *, liq_rows=None, drift: float = 0.0,
                 ok_pairs: set[str] | None = None) -> dict:
    def prices(_settings, pairs, start_ms, end_ms):
        pairs = list(pairs)
        wanted = pairs if ok_pairs is None else [p for p in pairs if p in ok_pairs]
        bars = {p: _hour_bars(p, end_ms, drift=drift) for p in wanted}
        failed = [p for p in pairs if p not in bars]
        return bars, failed

    def liq(_cg, symbol, interval, limit):
        if interval == "1d":
            return [{
                "time": _legacy_day(now),
                "aggregated_long_liquidation_usd": "10",
                "aggregated_short_liquidation_usd": "5",
            }]
        return liq_rows if liq_rows is not None else _liq_rows(now)

    def oi(_cg, symbol, unit, interval, limit):
        if symbol == "BTC" and interval == "1d" and limit == 150 and unit == "coin":
            return [{"time": _legacy_day(now), "open": "1", "high": "1.2",
                     "low": "0.9", "close": "1.1"}]
        return [{"time": _legacy_day(now), "open": "10", "high": "12",
                 "low": "9", "close": "11"}]

    def funding(_cg, exchange, symbol, interval, limit):
        return [{"time": _legacy_day(now), "open": "0.01", "high": "0.01",
                 "low": "0.01", "close": "0.002"}]

    return {
        "pairs": lambda _settings: set(PAIRS),
        "prices": prices,
        "liq": liq,
        "oi": oi,
        "funding": funding,
        "cmc_listings": lambda: None,
    }


# --- Окружение --------------------------------------------------------------


def _settings(tmp_path) -> Settings:
    settings = Settings()
    settings.auth_token = TOKEN
    settings.db_path = str(tmp_path / "unused.db")
    settings.events_basket_static = "BTC,ETH,SOL"
    settings.events_notify_enabled = False
    return settings


def _client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


def _get(client: TestClient, path: str):
    return client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})


def _assessment(*, aid: str, evaluated: int) -> dict:
    return {
        "assessment_id": aid,
        "scope": "crypto_market",
        "timezone": "Europe/Moscow",
        "evaluated_at": evaluated,
        "available_at": evaluated,
        "rule_version": "events-time-2.0",
        "basket_version": "b1",
        "direction": "neutral",
        "events_direction": "neutral",
        "time_direction": "neutral",
        "contradictory": False,
        "quality": "ok",
        "quality_by_dependency": {},
        "coverage": {"ok": 3, "total": 3, "ratio": 1.0, "sufficient": True},
        "market_line": "Нейтрально",
        "subtitle": "Направление по событиям и времени",
        "conflict_note": None,
        "arrow": "flat",
        "reasons": [],
        "unknown_dependencies": [],
        "active_facts": [],
        "time_context": {"weekday": 3, "day_start_ms": evaluated - (evaluated % DAY)},
        "statistics": None,
        "btc_background": None,
        "factor_agreement": {"long": 0, "short": 0, "label": "Согласованность факторов"},
        "next_review_at": None,
    }


# --- Обзор ------------------------------------------------------------------


def test_overview_market_block_and_legacy_keys(monkeypatch, tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    result = run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    monkeypatch.setattr(events_api, "now_ms", lambda: NOW + 30_000)
    body = _get(_client(db, settings), "/api/events/overview").json()

    # старые поля на месте (обратная совместимость)
    for key in ("ready", "market_line", "service_line", "direction_note",
                "strategy_gates", "situations", "health", "rule_version",
                "study_percents_on_card"):
        assert key in body, key
    assert body["ready"] is True
    assert body["health"]["quality"] == "ok"

    # рыночный блок (AC01): область и подпись методики
    market = body["market"]
    assert market["assessment_id"] == result["assessment_id"]
    assert market["scope"] == "crypto_market"
    assert market["subtitle"] == "Направление по событиям и времени"
    assert market["direction"] == "neutral"
    assert market["btc_background"]["label"] == "Фон BTC"

    # возраст и устаревание снимка (§11.4)
    assert body["market_age_seconds"] == 30
    assert body["market_stale"] is False

    # legacy BTC-снимок подписан как прежняя методика (§11.8)
    assert body["legacy_methodology"] == "legacy BTC / прежняя методика"


def test_overview_market_stale_flag(monkeypatch, tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    store.insert_assessment(db, _assessment(aid="old", evaluated=NOW - 300_000))
    monkeypatch.setattr(events_api, "now_ms", lambda: NOW)
    client = _client(db, settings)

    body = _get(client, "/api/events/overview").json()
    assert body["ready"] is True  # готовность общей оценки не зависит от legacy BTC
    assert body["legacy_methodology"] is None
    assert body["market"]["assessment_id"] == "old"
    assert body["market_age_seconds"] == 300
    assert body["market_stale"] is False  # five minutes is normal source cadence

    db.set_meta("events:health", '{"cycle_seconds": 86400}')
    for elapsed_ms, expected in ((4 * HOUR, False), (4 * HOUR + 1, True), (5 * HOUR, True)):
        monkeypatch.setattr(events_api, "now_ms", lambda elapsed_ms=elapsed_ms: NOW - 300_000 + elapsed_ms)
        body = _get(client, "/api/events/overview").json()
        assert body["market_stale"] is expected

    # свежая оценка снимает признак устаревания
    monkeypatch.setattr(events_api, "now_ms", lambda: NOW)
    store.insert_assessment(db, _assessment(aid="fresh", evaluated=NOW - 30_000))
    body = _get(client, "/api/events/overview").json()
    assert body["market"]["assessment_id"] == "fresh"
    assert body["market_age_seconds"] == 30
    assert body["market_stale"] is False


def test_overview_without_any_data(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    body = _get(_client(db, settings), "/api/events/overview").json()
    assert body["ready"] is False
    assert body["market"] is None
    assert body["market_age_seconds"] is None
    assert body["market_stale"] is None
    assert body["legacy_methodology"] is None


def test_ac04_low_coverage_does_not_sell_btc_as_market(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    # свежие цены есть только по BTC: покрытие корзины 1/3 < 0.8
    fetcher = make_fetcher(NOW, ok_pairs={"BTCUSDT"})
    run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    body = _get(_client(db, settings), "/api/events/overview").json()

    market = body["market"]
    assert market["coverage"]["sufficient"] is False
    assert market["direction"] == "unknown"
    assert "price" in market["unknown_dependencies"]
    assert market["market_line"] == (
        "Крипторынок: направление неизвестно. Доступен фон BTC"
    )
    # legacy BTC-строка не подменяет рыночное направление (AC04)
    assert body["market_line"] != market["market_line"]
    assert "Крипторынок" not in body["market_line"]
    # фон BTC — отдельной подписью
    assert market["btc_background"] is not None


# --- Здоровье ---------------------------------------------------------------


def test_health_sources_with_age(monkeypatch, tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    monkeypatch.setattr(events_api, "now_ms", lambda: NOW + 30_000)
    body = _get(_client(db, settings), "/api/events/health").json()

    assert body["quality"] == "ok"
    assert body["evaluated_at"] == NOW
    assert body["cycle_seconds"] == 60
    sources = body["sources"]
    assert sources["price"]["quality"] == "ok"
    for name in ("calendar", "price", "liquidations", "oi", "funding"):
        entry = sources[name]
        assert entry["age_seconds"] == (NOW + 30_000 - entry["updated_at"]) // 1000


def test_health_empty_db_is_honest_unknown(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    body = _get(_client(db, settings), "/api/events/health").json()
    assert body["quality"] == "unknown"
    assert body["detail"] == "Проверка ещё не запускалась"
    assert body["sources"] == {}
    assert body["evaluated_at"] is None
    assert body["cycle_seconds"] == 60


# --- Журнал -----------------------------------------------------------------


def test_journal_bundles_separate_occurred_published_delivered(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    fetcher = make_fetcher(
        NOW, liq_rows=_liq_rows(NOW, hot_windows=3), drift=-0.002,
    )
    first = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    assert first["direction"] == "short"
    # второй цикл после окна объединения 60с: пакет публикуется и доставляется
    run_cycle(db, settings, fetcher=fetcher, now_ms=NOW + 61_000)

    body = _get(_client(db, settings), "/api/events/journal").json()
    assert isinstance(body["items"], list)  # legacy-лента на месте
    bundles = body["bundles"]
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle["kind"] == "changes"
    assert bundle["status"] == "published"
    # обнаружение (окно) и публикация — раздельные поля (AC15)
    assert bundle["opened_at"] == NOW
    assert bundle["closes_at"] == NOW + 60_000
    assert bundle["published_at"] == NOW + 61_000
    assert bundle["opened_at"] < bundle["published_at"]

    pub = next(p for p in bundle["publications"] if p["kind"] == "fact_appeared")
    assert pub["text"].startswith("Новое событие:")
    assert pub["rule_id"] == "LIQ_PRESSURE"  # шов rule_id закрыт в payload
    assert pub["created_at"] == NOW  # момент обнаружения, не публикации
    assert pub["window_ms"] == W4
    assert pub["assessment_id"] == first["assessment_id"]

    deliveries = {d["channel"]: d for d in bundle["deliveries"]}
    feed, telegram = deliveries["feed"], deliveries["telegram"]
    assert feed["recipient"] == "owner"
    assert feed["status"] == "sent"
    assert feed["delivered_at"] == NOW + 61_000
    assert telegram["status"] == "unavailable"
    assert telegram["delivered_at"] is None  # недоступна ≠ доставлено (AC22)
    assert telegram["detail"] == "Доставка недоступна"


def test_ac15_fact_times_separate_in_assessment(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    fetcher = make_fetcher(
        NOW, liq_rows=_liq_rows(NOW, hot_windows=3), drift=-0.002,
    )
    result = run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    body = _get(
        _client(db, settings), f"/api/events/market/assessment/{result['assessment_id']}",
    ).json()
    fact = next(f for f in body["active_facts"] if f["rule_id"] == "LIQ_PRESSURE")
    # рыночное возникновение (граница окна) раньше обнаружения (AC15)
    assert fact["occurred_at"] < fact["detected_at"] == NOW
    assert fact["available_at"] == NOW
    assert fact["valid_until"] == fact["occurred_at"] + 240 * HOUR
    assert fact["time_precision"] == "window"


# --- Календарь --------------------------------------------------------------


def test_calendar_market_upcoming_real_switches_only(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    body = _get(_client(db, settings), "/api/events/calendar").json()

    # legacy-полезная нагрузка сохранена
    assert "utc_date" in body
    assert body["note"] == "Календарь не задаёт направление сделки"

    market = body["market"]
    assert market["time_context"]["weekday"] == 3  # четверг МСК
    upcoming = market["upcoming"]
    assert {u["kind"] for u in upcoming} <= {"msk_day_boundary", "fact_expiry", "h1_close", "liquidation_window_close"}

    day = next(u for u in upcoming if u["kind"] == "msk_day_boundary")
    assert day["label"] == "Смена дня МСК"
    assert day["at"] == market["time_context"]["next_day_start_ms"]

    expiry = next(u for u in upcoming if u["kind"] == "fact_expiry")
    assert expiry["label"] == "Событие больше не учитывается"
    assert expiry["fact_key"]

    assert [u["at"] for u in upcoming] == sorted(u["at"] for u in upcoming)
    assert market["next_review_at"] == min(u["at"] for u in upcoming)


def test_calendar_empty_db(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    body = _get(_client(db, settings), "/api/events/calendar").json()
    assert body["ready"] is False
    # Calendar conditions remain readable while the worker has no assessment.
    assert body["market"]["forecast"]["baseline"]["n"] == 0
    assert body["market"]["forecast"]["baseline"]["up_frequency"] is None


# --- Правила и статистика ---------------------------------------------------


def test_rules_registry_and_statistics(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    client = _client(db, settings)

    body = _get(client, "/api/events/rules").json()
    assert body["rules"]  # legacy-реестр на месте
    assert body["note"] == "Проценты источника на карточку не выводятся"
    assert body["rule_version"] == "events-time-2.0"
    assert body["statistics"] is None  # оценки ещё не было

    registry = {r["rule_id"]: r for r in body["registry"]}
    pressure = registry["LIQ_PRESSURE"]
    assert pressure["origin"] == "project_default"
    assert pressure["family"] == "liq"
    assert pressure["confirmation"] == "experimental"
    assert pressure["rule_class"] == "market_event"
    assert pressure["direction"] == "short"

    # после цикла — статистика последней оценки; честно недостаточна (AC13)
    run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    stats = _get(client, "/api/events/rules").json()["statistics"]
    assert isinstance(stats, dict)
    assert stats["available"] is False
    assert stats["value"] is None
    assert stats["text"] == "Статистика недостаточна"


# --- История и снимки -------------------------------------------------------


def test_market_history_known_then_vs_current(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    first = run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    assert first["direction"] == "neutral"
    second = run_cycle(
        db, settings,
        fetcher=make_fetcher(
            NOW + HOUR, liq_rows=_liq_rows(NOW + HOUR, hot_windows=3), drift=-0.002,
        ),
        now_ms=NOW + HOUR,
    )
    assert second["direction"] == "short"
    client = _client(db, settings)

    resp = _get(client, f"/api/events/market/history?at={NOW}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["captured_at"] == NOW
    # «Известно тогда» не знает будущей оценки (§8.2, AC15)
    assert body["known_then"]["assessment_id"] == first["assessment_id"]
    assert body["known_then"]["direction"] == "neutral"
    assert body["current"]["assessment_id"] == second["assessment_id"]
    assert body["current"]["direction"] == "short"

    later = _get(client, f"/api/events/market/history?at={NOW + HOUR}").json()
    assert later["known_then"]["assessment_id"] == second["assessment_id"]

    assert _get(client, "/api/events/market/history").status_code == 400
    assert _get(client, "/api/events/market/history?at=abc").status_code == 400


def test_market_assessment_by_id_and_404(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    result = run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    client = _client(db, settings)

    resp = _get(client, f"/api/events/market/assessment/{result['assessment_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["assessment_id"] == result["assessment_id"]
    assert body["scope"] == "crypto_market"
    assert body["timezone"] == "Europe/Moscow"

    missing = _get(client, "/api/events/market/assessment/asmt_unknown")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Оценка не найдена"


# --- Обратная совместимость --------------------------------------------------


def test_old_client_keys_still_served(tmp_path):
    db, settings = Database(":memory:"), _settings(tmp_path)
    run_cycle(db, settings, fetcher=make_fetcher(NOW), now_ms=NOW)
    client = _client(db, settings)

    overview = _get(client, "/api/events/overview").json()
    assert overview["ready"] is True
    assert isinstance(overview["market_line"], str)
    assert isinstance(overview["direction_note"], dict)
    assert isinstance(overview["strategy_gates"], list)
    assert isinstance(overview["situations"], list)
    assert "quality" in overview["health"]

    assert isinstance(_get(client, "/api/events/journal").json()["items"], list)

    rules = _get(client, "/api/events/rules").json()
    assert "rules" in rules and "note" in rules

    health = _get(client, "/api/events/health").json()
    assert "quality" in health and "detail" in health

    assert isinstance(_get(client, "/api/events/situations").json()["items"], list)

    calendar = _get(client, "/api/events/calendar").json()
    assert "utc_date" in calendar

    derivatives = _get(client, "/api/events/derivatives").json()
    assert "liquidation" in derivatives and "evaluated_at" in derivatives
