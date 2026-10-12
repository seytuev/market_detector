"""Финальная приёмочная волна: карта покрытия AC01–AC22 (спека
docs/Events_Time_Market_Direction_Spec_RU_2026_10_11.md §12) и тесты критериев,
не доказанных напрямую в прежних волнах. В конце — примеры исходов §13.6.

Карта покрытия (id → тест):

  AC01 ЭТОТ ФАЙЛ :: test_ac01_scope_is_crypto_market_regardless_of_selected_coin,
       test_ac01_alt_html_mounts_shared_card_static
       (+ test_events_api_v2::test_overview_market_block_and_legacy_keys)
  AC02 test_events_market (test_ac02_liq_fact_alone_never_assigns_short,
       test_ac02_pressure_fact_gives_short, test_ac02_reinterpreted_fact_gives_long,
       test_family_dedup_single_vote_per_liq_family)
       + test_events_runner_v2::test_liq_pressure_episode_gives_short_assessment
  AC03 test_events_market::test_ac03_missing_data_is_unknown_not_neutral
       + test_events_runner_v2::test_ac19_coinglass_failure_does_not_stop_calendar
       (unknown_dependencies видны в снимке)
  AC04 test_events_market::test_ac04_low_coverage_market_line_names_btc_background
       + test_events_api_v2::test_ac04_low_coverage_does_not_sell_btc_as_market
       + test_events_basket (покрытие/медиана — сторона данных)
  AC05 test_events_memory::test_fact_active_until_exclusive_240h_boundary
       + test_events_memory::test_repeat_begin_fact_does_not_extend_memory
  AC06 test_events_memory::test_child_fact_has_own_lifetime_and_parent_untouched
  AC07 test_events_memory::test_invalidate_and_expire_are_distinguishable
       + test_events_publish::test_ac07_expired_and_invalidated_are_distinct
  AC08 test_events_timeutil (границы дня/будни МСК против известных UTC-моментов)
       + test_events_market::test_ac08_weekday_flips_at_msk_midnight (чистая оценка)
       + ЭТОТ ФАЙЛ :: test_ac08_run_cycle_msk_midnight_turnover (полный цикл)
  AC09 ЭТОТ ФАЙЛ :: test_ac09_utc_daily_bars_make_msk_day_pattern_unknown
       + test_ac09_no_code_path_relabels_provider_d1_as_msk_day
  AC10 ЭТОТ ФАЙЛ :: test_ac10_forming_window_provisional_observation,
       test_ac10_provisional_sum_without_baseline_has_no_relative_output,
       test_ac10_fact_from_unclosed_window_is_provisional
       (+ test_events_memory: состояние provisional в жизненном цикле факта)
  AC11 test_events_market (test_ac11_conflict_both_reasons_and_consistent_arrow,
       test_ac11_advantage_with_conflict_note)
  AC12 test_events_market::test_ac12_known_neutral_state
  AC13 test_events_market::test_ac13_insufficient_stats_no_invented_percent
       + test_events_stats (гейт min-n, интервал Вильсона)
       + test_events_api_v2::test_rules_registry_and_statistics
  AC14 test_events_stats (контракт frequency/insufficient)
       + ЭТОТ ФАЙЛ :: test_ac14_statistics_contract_fields_and_basket_scope
  AC15 test_events_store_v2 (assessment_as_of без будущих данных)
       + test_events_api_v2 (test_ac15_fact_times_separate_in_assessment,
       test_journal_bundles_separate_occurred_published_delivered,
       test_market_history_known_then_vs_current)
  AC16 test_events_publish (test_ac16_flush_deliveries_feed_sent_telegram_unavailable,
       test_ac16_telegram_pending_only_when_wired_and_enabled)
       + test_events_delivery (лента «sent» всегда)
       + ЭТОТ ФАЙЛ :: test_ac16_every_publication_kind_lands_in_feed_including_night,
       test_publication_payload_rule_id_scope_seam
  AC17 test_events_publish::test_ac17_changes_within_60s_share_one_bundle_without_extension
       + test_events_publish::test_same_change_twice_creates_single_publication
       + test_events_delivery::test_ac17_repeat_and_reput_do_not_duplicate_packet
       + test_events_store_v2 (дедуп pub_key)
       + test_events_runner_v2::test_idempotent_rerun_same_instant
  AC18 test_events_publish::test_ac18_startup_digest_once_after_gap
       + test_events_publish::test_startup_digest_no_assessment_or_small_gap_is_noop
  AC19 test_events_runner_v2::test_ac19_coinglass_failure_does_not_stop_calendar
       + ЭТОТ ФАЙЛ :: test_example_source_failure_coinglass_down (§13.6)
  AC20 ЭТОТ ФАЙЛ :: test_ac20_screener_mount_and_api_without_events_keys
       + tests/test_alt_api.py (весь модуль — регрессия Скринера)
       + test_events_ui_contract::test_statics_served_and_screener_mounts_card
  AC21 tests/test_acceptance.py + tests/test_acceptance_r01_r48.py (ядро H1
       BOS/SMS, зоны, отмены — прежний движок; весь прогон suite — регрессия)
  AC22 test_events_delivery::test_ac22_pending_is_not_delivery_until_real_send
       + test_events_publish::test_ac16_telegram_pending_only_when_wired_and_enabled

Примеры §13.6 — ЭТОТ ФАЙЛ: test_example_long, test_example_short,
test_example_mixed, test_example_neutral, test_example_unknown,
test_example_source_failure_coinglass_down.
"""
from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.coinglass import CoinglassError
from app.config import Settings
from app.db import Database
from app.events import memory, publish, runner, store
from app.events.basket import basket_from_static, msk_day_return
from app.events.market import evaluate_market
from app.events.rules import ENGINE_RULE_VERSION, RULE_REGISTRY, get_rule
from app.events.runner import run_cycle
from app.events.timeutil import (
    MSK,
    completed_msk_days,
    msk_day_end_ms,
    msk_day_start_ms,
    msk_hour,
    msk_weekday,
)
from app.web import events_api
from app.web.api import create_app
from tests.test_events_api_v2 import _liq_rows as _liq_rows_at
from tests.test_events_api_v2 import make_fetcher as _make_fetcher_at
from tests.test_events_market import (
    _fact,
    _inp,
    _registry_with,
    _weekday_rule,
)
from tests.test_events_publish import _asmt
from tests.test_events_runner_v2 import make_fetcher as _runner_fetcher

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DAY = 86_400_000
HOUR = 3_600_000
W4 = 14_400_000
# Четверг 14:00 МСК — как в test_events_runner_v2 / test_events_api_v2
NOW = int(datetime(2026, 10, 8, 14, 0, tzinfo=MSK).timestamp() * 1000)
# Граница московского дня: четверг 23:59:59 → пятница 00:00:01
THU_END = int(datetime(2026, 10, 8, 23, 59, 59, tzinfo=MSK).timestamp() * 1000)
FRI_START = int(datetime(2026, 10, 9, 0, 0, 1, tzinfo=MSK).timestamp() * 1000)
# Ночь по МСК: тихих часов у модуля нет (§9)
NIGHT = int(datetime(2026, 10, 9, 3, 0, tzinfo=MSK).timestamp() * 1000)

STATIC_ROOT = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


@pytest.fixture(autouse=True)
def _unwire():
    publish._WIRED = False
    yield
    publish._WIRED = False


def _db() -> Database:
    return Database(":memory:")


def _settings() -> Settings:
    settings = Settings()
    settings.auth_token = TOKEN
    settings.events_basket_static = "BTC,ETH,SOL"
    settings.events_notify_enabled = False
    return settings


# --- AC01: подпись области не зависит от выбранной монеты --------------------


def test_ac01_scope_is_crypto_market_regardless_of_selected_coin():
    db, settings = _db(), _settings()
    run_cycle(db, settings, fetcher=_make_fetcher_at(NOW), now_ms=NOW)
    client = TestClient(create_app(db, settings))
    # выбор монеты — прерогатива Скринера/графика; обзор рынка её не принимает
    # и не переименовывает направление
    for query in ("", "?symbol=ETHUSDT", "?coin=SOL"):
        body = client.get("/api/events/overview" + query, headers=AUTH).json()
        market = body["market"]
        assert market is not None
        assert market["scope"] == "crypto_market"
        assert market["timezone"] == "Europe/Moscow"
        assert market["subtitle"] == "Направление по событиям и времени"
        assert market["basket_version"]


def test_ac01_alt_html_mounts_shared_card_static():
    html = (STATIC_ROOT / "alt.html").read_text(encoding="utf-8")
    assert 'id="h1-events-context"' in html
    assert "Рыночный контекст показан отдельно" in html
    assert '<script src="/events.js"></script>' in html


# --- AC08: граница дня МСК в полном цикле -------------------------------------


def test_ac08_run_cycle_msk_midnight_turnover():
    # точные миллисекунды границы — через timeutil; момент 00:00 принадлежит
    # новому дню (полуоткрытый интервал)
    assert msk_weekday(THU_END) == 3 and msk_hour(THU_END) == 23
    assert msk_weekday(FRI_START) == 4 and msk_hour(FRI_START) == 0
    assert msk_day_start_ms(FRI_START) == msk_day_end_ms(THU_END)

    db, settings = _db(), _settings()
    first = run_cycle(db, settings, fetcher=_make_fetcher_at(THU_END), now_ms=THU_END)
    second = run_cycle(
        db, settings, fetcher=_make_fetcher_at(FRI_START), now_ms=FRI_START,
    )
    thursday = store.get_assessment(db, first["assessment_id"])
    friday = store.get_assessment(db, second["assessment_id"])
    # день недели меняется сразу на 00:00 МСК
    assert thursday["time_context"]["weekday"] == 3
    assert friday["time_context"]["weekday"] == 4
    assert friday["time_context"]["day_start_ms"] == msk_day_start_ms(FRI_START)

    # Прежний временной фактор не остаётся на десять дней: временные правила
    # голосуют из живого контекста цикла и вообще не пишутся в десятидневную
    # память — lingering-факта с valid_until +240h не существует.
    time_rule_ids = {
        r.rule_id for r in RULE_REGISTRY
        if r.rule_class in ("time_condition", "combination")
    }
    assert time_rule_ids  # реестр временных правил не пуст
    stored = {f["rule_id"] for f in store.query_facts(db)}
    assert not (stored & time_rule_ids)
    active = {f["rule_id"] for f in store.query_facts(db, active_now_ms=FRI_START)}
    assert not (active & time_rule_ids)


# --- AC09: UTC-D1 не выдаётся за московский D1 --------------------------------


def _utc_daily_bars(symbols: list[str], now: int, *, days: int = 40) -> dict:
    """«Провайдерский D1»: бары на границах 00:00 UTC, а не 00:00 МСК (21:00 UTC)."""
    end = (now // DAY) * DAY
    out = {}
    for symbol in symbols:
        bars, t, price = [], end - days * DAY, 100.0
        while t < end:
            bars.append({"start": t, "end": t + DAY, "open": price,
                         "close": price * 1.01, "quality": "ok"})
            price *= 1.01
            t += DAY
        out[symbol] = bars
    return out


def test_ac09_utc_daily_bars_make_msk_day_pattern_unknown():
    basket = basket_from_static(["BTC", "ETH"], day_start_ms=msk_day_start_ms(NOW))
    bars = _utc_daily_bars(["BTC", "ETH"], NOW)

    # UTC-бар [00:00, 24:00) не лежит целиком внутри московского дня
    # [21:00, 21:00): данных о московском дне нет, а не «день известен»
    day_start, day_end = completed_msk_days(NOW, 1)[0]
    ret = msk_day_return(
        basket, bars, day_start_ms=day_start, day_end_ms=day_end,
    )
    assert ret["complete"] is False
    assert ret["members_ok"] == 0
    assert ret["median_return"] is None

    # контекст раннера: ни знака дня, ни недельной комбинации не выдумывается
    time_ctx = runner._time_ctx(basket, bars, NOW)
    assert time_ctx["completed_days"]  # дни перечислены, но…
    assert all(d["sign"] is None for d in time_ctx["completed_days"])
    assert time_ctx["week_seq"] is None

    # evaluate_market: даже включённая валидированная «комбинация недели»
    # молчит — придуманного знака/направления нет
    seq_rule = replace(
        get_rule("SEQ_WEEK_START"), enabled=True,
        confirmation="validated", direction="long",
    )
    out = evaluate_market(_inp(time_ctx=time_ctx, registry=_registry_with(seq_rule)))
    assert out["time_context"]["week_seq"] is None
    assert out["time_direction"] == "neutral"
    assert out["direction"] == "neutral"  # не LONG по мнимым бычьим пн+вт


def test_ac09_no_code_path_relabels_provider_d1_as_msk_day():
    # Московский день корзины считается только по часовым klines: 35 дней 1h
    # запрашиваются и складываются в бакеты interval="1h"; msk_day_return
    # получает границы от completed_msk_days. Переименования D1 провайдера
    # в «московский день» в коде нет — отдельный source_day_note не нужен.
    assert '"1h"' in inspect.getsource(runner._prices_cycle)
    day_code = (
        inspect.getsource(runner._store_day_metrics)
        + inspect.getsource(runner._time_ctx)
    )
    assert "msk_day_return" in day_code
    assert "completed_msk_days" in day_code
    # Единственный потребитель D1 источника — legacy BTC-снимок (interval="1d",
    # scope BTC), подписанный как прежняя методика (§11.8), — не рыночный день.
    assert '"1d"' in inspect.getsource(runner._put_liq)
    assert events_api.LEGACY_METHODOLOGY == "legacy BTC / прежняя методика"


# --- AC10: предварительное наблюдение формирующегося окна ---------------------


def _cycle_liq_metrics(db, settings, fetcher) -> dict:
    run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    return runner._market_liq(
        db, ["BTC", "ETH", "SOL"], NOW,
        int(settings.events_liq_baseline_days),
        get_rule("LIQ_PRESSURE").thresholds,
    )


def test_ac10_forming_window_provisional_observation():
    db, settings = _db(), _settings()
    # _liq_rows включает формирующееся окно (i=0, текущий 4h-старт)
    liq = _cycle_liq_metrics(db, settings, _make_fetcher_at(NOW))
    current_start = NOW - (NOW % W4)

    # наблюдение доступно до закрытия окна: формирующий бакет сохранён
    rows = store.load_buckets(
        db, runner.scope_id("coinglass", "liq-4h", "BTC"), "liq", interval="4h",
    )
    forming = [r for r in rows if r["t"] == current_start]
    assert len(forming) == 1
    assert forming[0]["quality"] == "ok"
    assert forming[0]["available_at"] == NOW

    # предварительный блок: сумма показана, относительный вывод неизвестен
    prov = liq["provisional"]
    assert prov is not None
    assert prov["window_start"] == current_start
    assert prov["sum_long_usd"] == pytest.approx(300.0)  # 3 символа × 100
    assert set(prov) == {"sum_long_usd", "window_start"}


def test_ac10_provisional_sum_without_baseline_has_no_relative_output():
    db, settings = _db(), _settings()
    # истории 5 окон — совместимого baseline (30 дней того же часа) нет
    liq = _cycle_liq_metrics(
        db, settings, _make_fetcher_at(NOW, liq_rows=_liq_rows_at(NOW, count=5)),
    )
    assert liq["available"] is True
    assert liq["baseline_ok"] is False
    assert liq["multiple"] is None  # кратность не выдумывается
    assert liq["provisional"]["sum_long_usd"] > 0


def test_ac10_fact_from_unclosed_window_is_provisional():
    # Шов _facts_cycle: наблюдение из незакрытого окна может породить только
    # предварительный факт. run_cycle сегодня передаёт сюда только закрытые
    # окна (_market_liq помечает window closed=True), поэтому путь проверяется
    # напрямую — формирующееся «горячее» окно эпизода.
    db, settings = _db(), _settings()
    basket = basket_from_static(["BTC"], day_start_ms=msk_day_start_ms(NOW))
    metrics = {"median_return": -0.01, "breadth_up": 0.3}  # цена и ширина падают
    liq = {
        "available": True,
        "window": {"start": NOW - W4, "end": NOW, "closed": False},
        "sum_long_usd": 400.0, "sum_short_usd": 100.0,
        "long_share": 0.8, "multiple": 2.0,
        "coverage": {"ok": 1, "total": 1},
        "episode": {"active": True, "phase": "intensifying",
                    "started_at": NOW - 2 * W4},
    }
    runner._facts_cycle(db, settings, liq, metrics, {}, basket, NOW)
    pressure = [
        f for f in store.query_facts(db, active_now_ms=NOW)
        if f["rule_id"] == "LIQ_PRESSURE"
    ]
    assert len(pressure) == 1
    assert pressure[0]["state"] == "provisional"


# --- AC14: поля статистики и область «корзина, МСК» ----------------------------


def test_ac14_statistics_contract_fields_and_basket_scope():
    db, settings = _db(), _settings()
    settings.events_stats_min_n = 3  # 10 завершённых дней корзины ≥ порога
    run_cycle(db, settings, fetcher=_make_fetcher_at(NOW), now_ms=NOW)
    stats = store.latest_assessment(db)["statistics"]

    assert stats["available"] is True
    # исход/горизонт, выборка, период, методика и версия доступны для процента
    for key in ("outcome", "horizon", "method", "version", "period",
                "n", "numerator", "denominator", "min_n", "uncertainty",
                "label", "text"):
        assert key in stats, key
    assert stats["outcome"] == "median_return>0"
    assert stats["horizon"] == "msk_day"
    assert stats["version"] == ENGINE_RULE_VERSION
    assert stats["period"]["from"] < stats["period"]["to"]
    assert stats["n"] == 10

    # область — корзина за московский день; BTC-only статистика общерыночной
    # не подписывается (§7)
    assert "basket" in stats["method"] and "msk" in stats["method"]
    assert "корзин" in stats["label"] and "московск" in stats["label"]
    assert "BTC" not in stats["label"] and "BTC" not in stats["method"]


# --- AC16: все виды публикаций в ленте, включая ночь --------------------------


_COV_LOW = {"ok": 1, "total": 3, "ratio": 0.33, "sufficient": False}
_COV_OK = {"ok": 3, "total": 3, "ratio": 1.0, "sufficient": True}
_STATS_LOW = {"available": False, "value": None, "n": 5,
              "text": "Статистика недостаточна"}
_STATS_OK = {"available": True, "value": 0.61, "n": 120, "text": "… 61% …"}


def test_ac16_every_publication_kind_lands_in_feed_including_night():
    db, settings = _db(), _settings()
    settings.events_group_seconds = 60
    assert msk_hour(NIGHT) == 3  # ночь по МСК: тихих часов у модуля нет

    # переход факта памяти → fact_appeared
    memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-night", direction="short",
        occurred_at=NIGHT - W4, detected_at=NIGHT, available_at=NIGHT,
        time_precision="window", payload={"summary": "ночное давление"},
        state="confirmed", memory_hours=240,
    )
    # деградация источника → source_degraded
    runner._set_source(db, "liquidations", "unavailable", "таймаут", NIGHT)

    prev = _asmt(direction="neutral", weekday=3, aid="a0",
                 evaluated=NIGHT - DAY, coverage=_COV_LOW, stats=_STATS_LOW)
    curr = _asmt(direction="long", contradictory=True, weekday=4, aid="a1",
                 evaluated=NIGHT, coverage=_COV_OK, stats=_STATS_OK)
    created = publish.process_cycle(
        db, settings, prev=prev, curr=curr,
        transitions=store.fact_transitions_since(db, 0), now_ms=NIGHT,
    )
    assert {"direction", "conflict", "time_boundary", "coverage", "stats",
            "fact_appeared", "source_degraded"} <= {c["kind"] for c in created}

    # восстановление источника → source_recovered (тем же ночным окном)
    runner._set_source(db, "liquidations", "ok", None, NIGHT + 1_000)
    curr2 = _asmt(direction="long", contradictory=True, weekday=4, aid="a2",
                  evaluated=NIGHT + 1_000, coverage=_COV_OK, stats=_STATS_OK)
    created2 = publish.process_cycle(
        db, settings, prev=curr, curr=curr2, transitions=[], now_ms=NIGHT + 1_000,
    )
    assert [c["kind"] for c in created2] == ["source_recovered"]

    # сводка после простоя → downtime_digest (свой пакет, публикуется сразу)
    settings.events_digest_gap_seconds = 600
    store.insert_assessment(db, {
        "assessment_id": "old-night", "evaluated_at": NIGHT - 3_600_000,
        "available_at": NIGHT - 3_600_000, "direction": "short",
        "market_line": "↓ SHORT",
    })
    publish.startup_digest(db, settings, NIGHT + 2_000)

    published = publish.flush_due(db, settings, NIGHT + 61_000)
    assert len(published) == 1  # пакет изменений; дайджест уже опубликован

    # каждый опубликованный класс доступен в ленте журнала
    client = TestClient(create_app(db, settings))
    body = client.get("/api/events/journal", headers=AUTH).json()
    pubs = [p for b in body["bundles"] for p in b["publications"]]
    kinds_in_feed = {p["kind"] for p in pubs}
    assert {
        "direction", "conflict", "time_boundary", "coverage", "stats",
        "fact_appeared", "source_degraded", "source_recovered",
        "downtime_digest",
    } <= kinds_in_feed
    # шов rule_id закрыт для всех видов
    assert all(p["rule_id"] for p in pubs)
    # лента доставлена даже ночью; quiet-фильтра нет
    for bundle in body["bundles"]:
        feed = next(d for d in bundle["deliveries"] if d["channel"] == "feed")
        assert feed["status"] == "sent"
        assert feed["delivered_at"] is not None
    assert publish._validate_packet(None, None) is None


def test_publication_payload_rule_id_scope_seam():
    """Каждый payload add_publication несёт rule_id и scope (шов events_api)."""
    db, settings = _db(), _settings()
    memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=NIGHT, detected_at=NIGHT, available_at=NIGHT,
    )
    created = publish.process_cycle(
        db, settings, prev=_asmt(direction="neutral", aid="a0", evaluated=NIGHT - DAY),
        curr=_asmt(direction="long", aid="a1", evaluated=NIGHT),
        transitions=store.fact_transitions_since(db, 0), now_ms=NIGHT,
    )
    assert created
    for pub in created:
        assert pub["payload"]["rule_id"]
        assert pub["payload"]["scope"] == "crypto_market"

    # публикация сводки простоя — тоже с явными rule_id и scope
    settings.events_digest_gap_seconds = 600
    store.insert_assessment(db, {
        "assessment_id": "old", "evaluated_at": NIGHT - 3_600_000,
        "available_at": NIGHT - 3_600_000, "direction": "short",
        "market_line": "↓ SHORT",
    })
    publish.startup_digest(db, settings, NIGHT)
    digest = next(b for b in store.list_bundles(db) if b["kind"] == "downtime_digest")
    pubs = store.bundle_publications(db, digest["id"])
    assert [p["kind"] for p in pubs] == ["downtime_digest"]
    assert pubs[0]["payload"]["rule_id"] == "DOWNTIME_DIGEST"
    assert pubs[0]["payload"]["scope"] == "crypto_market"


# --- AC20: Скринер не тронут ---------------------------------------------------


def test_ac20_screener_mount_and_api_without_events_keys():
    # alt.html: интеграция — только монт общей карточки, подпись и script;
    # alt.js байт-в-байт не менялся (git diff пустой), в pytest это проверяется
    # отсутствием ключей рыночного модуля в ответе Скринера
    html = (STATIC_ROOT / "alt.html").read_text(encoding="utf-8")
    assert 'id="h1-events-context"' in html

    db, settings = _db(), _settings()
    client = TestClient(create_app(db, settings))
    resp = client.get("/api/alt/setups?bucket=all", headers=AUTH)
    assert resp.status_code == 200

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    forbidden = {
        "assessment_id", "events_direction", "time_direction",
        "btc_background", "factor_agreement", "time_context",
        "active_facts", "market_age_seconds",
    }
    assert not (set(walk(resp.json())) & forbidden)


# --- §13.6: примеры исходов ----------------------------------------------------


def test_example_long():
    out = evaluate_market(_inp(facts=[_fact(
        "LIQ_RECOVERY", "long",
        summary="Каскад затухает, цена и ширина восстанавливаются",
    )]))
    assert out["direction"] == "long"
    assert out["market_line"] == "↑ LONG"
    assert out["arrow"] == "up"


def test_example_short():
    out = evaluate_market(_inp(facts=[_fact("LIQ_PRESSURE", "short")]))
    assert out["direction"] == "short"
    assert out["market_line"] == "↓ SHORT"
    assert out["arrow"] == "down"


def test_example_mixed():
    registry = _registry_with(_weekday_rule("short", [3]))
    out = evaluate_market(_inp(
        facts=[_fact("LIQ_RECOVERY", "long")], registry=registry,
    ))
    assert out["direction"] == "mixed"
    assert out["contradictory"] is True
    assert out["market_line"] == "Противоречивый контекст"
    assert out["arrow"] == "conflict"


def test_example_neutral():
    out = evaluate_market(_inp())
    assert out["direction"] == "neutral"
    assert out["market_line"] == "Нейтрально"
    assert out["arrow"] == "flat"
    assert out["contradictory"] is False


def test_example_unknown():
    out = evaluate_market(_inp(liq=None))
    assert out["direction"] == "unknown"
    assert out["market_line"] == "Направление неизвестно"
    assert out["arrow"] == "unknown"


def test_example_source_failure_coinglass_down():
    """Coinglass недоступен: события unknown, календарь и часы работают (§13.6)."""
    db, settings = _db(), _settings()
    fetcher = _runner_fetcher(
        liq_exc=CoinglassError("сбой Coinglass"),
        oi_exc=CoinglassError("сбой Coinglass"),
        funding_exc=CoinglassError("сбой Coinglass"),
    )
    run_cycle(db, settings, fetcher=fetcher, now_ms=NOW)
    assessment = store.latest_assessment(db)
    assert assessment["events_direction"] == "unknown"
    assert "liquidations" in assessment["unknown_dependencies"]
    assert assessment["direction"] == "unknown"
    # календарь не остановлен: временная группа посчитана по МСК
    assert assessment["time_direction"] == "neutral"
    assert assessment["time_context"]["weekday"] == 3
    states = store.get_source_states(db)
    assert states["calendar"]["quality"] == "ok"
    assert states["price"]["quality"] == "ok"
    assert states["liquidations"]["quality"] == "unavailable"
