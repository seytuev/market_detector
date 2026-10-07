"""Тесты дневного runner'а «Altcoins D1 accumulation» (ТЗ §3, §4, §15, §18).

Покрытие: выбор пары (binance USDT → binance USDC → bybit, причина пропуска),
первичная загрузка с пагинацией и backfill-антиспамом (события delivered без
отправки, meta-флаг), инкрементальный прогон (live, события pending),
stale/no_universe при ошибке CMC, непрерывность наблюдения выпавших активов
(universe_eligible=0, без активного сетапа — пропуск), блокировка прогона
(skipped_locked / interrupted), только закрытые свечи, идемпотентность.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.adapters.base import AdapterError
from app.adapters.coinmarketcap import CmcListing
from app.alt.runner import AltRunner
from app.config import AltConfig, Settings
from app.models import Instrument, now_ms
from app.models_alt import AltRun
from tests.conftest import make_candle
from tests.test_alt_engine import build_accumulation

DAY_MS = 86_400_000
MSK = ZoneInfo("Europe/Moscow")


class FakeVenue:
    """In-memory площадка: каталог из quotes_by_base, свечи из candles.

    page_size ограничивает «страницу» ответа — проверка пагинации runner'а.
    Параметр include_forming намеренно игнорируется (симуляция небрежного
    адаптера): runner обязан сам отсечь незакрытую свечу.
    """

    def __init__(self, venue, quotes_by_base=None, candles=None,
                 page_size=None, catalog_error=None):
        self.venue = venue
        self.quotes_by_base = quotes_by_base or {}
        self.candles = candles or {}
        self.page_size = page_size
        self.catalog_error = catalog_error
        self.klines_calls = []

    async def catalog(self):
        if self.catalog_error:
            raise AdapterError(self.catalog_error)
        return [
            Instrument(id=None, asset=base, venue=self.venue,
                       market_type="spot", symbol=f"{base}{quote}",
                       quote_asset=quote)
            for base, quotes in self.quotes_by_base.items()
            for quote in quotes
        ]

    async def klines(self, symbol, timeframe, start_ms, end_ms,
                     include_forming=False):
        self.klines_calls.append((symbol, start_ms, end_ms))
        cs = [c for c in self.candles.get(symbol, [])
              if start_ms <= c.open_time <= end_ms]
        if self.page_size is not None:
            cs = cs[: self.page_size]
        return [replace(c) for c in cs]


class FakeCmc:
    """Подмена CoinMarketCapAdapter: снапшот из памяти или ошибка."""

    def __init__(self, listings=None, error=None):
        self.listings = listings or []
        self.error = error

    async def listings_latest(self, limit=300):
        if self.error:
            raise AdapterError(self.error)
        return list(self.listings)


def _listing(cmc_id, symbol, rank):
    return CmcListing(
        cmc_id=cmc_id, name=f"Coin {symbol}", symbol=symbol, rank=rank,
        market_cap=1_000_000.0, tags=[], date_added="2020-01-01T00:00:00.000Z",
        platform_info=None,
    )


def _to_candles(alt_candles):
    return [make_candle(c.open_time, c.open, c.high, c.low, c.close,
                        instrument_id=0, source="fake")
            for c in alt_candles]


def make_runner(db, venues, cmc, cfg=None):
    settings = Settings()
    settings.alt_config = cfg or AltConfig()
    return AltRunner(db, settings, {v.venue: v for v in venues}, cmc)


def _alt_event_count(db):
    return db.conn.execute("SELECT COUNT(*) c FROM alt_event").fetchone()["c"]


# ---------------------------------------------------------------------------
# Выбор пары (§3/§4)
# ---------------------------------------------------------------------------


async def test_pair_selection_prefers_binance_usdt(db):
    binance = FakeVenue("binance", {"AAA": ["USDT"]})
    bybit = FakeVenue("bybit", {"AAA": ["USDT"]})
    r = make_runner(db, [binance, bybit], FakeCmc([_listing(11, "AAA", 11)]))
    res = await r.run_daily("test")
    assert res["status"] == "ok"
    asset = db.get_alt_asset_by_cmc_id(11)
    src = db.get_alt_instrument_source(asset.id)
    assert (src.venue, src.symbol, src.quote, src.source_version) == (
        "binance", "AAAUSDT", "USDT", 1,
    )


async def test_pair_selection_usdc_fallback_within_binance_first(db):
    # USDT нет на binance, но есть USDC — binance USDC важнее bybit USDT
    binance = FakeVenue("binance", {"AAA": ["USDC"]})
    bybit = FakeVenue("bybit", {"AAA": ["USDT"]})
    r = make_runner(db, [binance, bybit], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")
    src = db.get_alt_instrument_source(db.get_alt_asset_by_cmc_id(11).id)
    assert (src.venue, src.symbol, src.quote) == ("binance", "AAAUSDC", "USDC")


async def test_pair_selection_bybit_fallback_when_missing_on_binance(db):
    binance = FakeVenue("binance", {"OTHER": ["USDT"]})
    bybit = FakeVenue("bybit", {"AAA": ["USDT"]})
    r = make_runner(db, [binance, bybit], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")
    src = db.get_alt_instrument_source(db.get_alt_asset_by_cmc_id(11).id)
    assert (src.venue, src.symbol, src.quote) == ("bybit", "AAAUSDT", "USDT")


async def test_pair_selection_no_pair_records_skip_reason(db):
    binance = FakeVenue("binance", {"OTHER": ["USDT"]})
    bybit = FakeVenue("bybit", {})
    r = make_runner(db, [binance, bybit], FakeCmc([_listing(11, "AAA", 11)]))
    res = await r.run_daily("test")
    assert res["status"] == "ok"  # пропуск одного инструмента не роняет прогон
    entry = res["summary"]["per_asset"][0]
    assert entry["status"] == "skipped"
    assert entry["reason"] == "no_spot_pair"
    asset = db.get_alt_asset_by_cmc_id(11)
    assert db.get_alt_instrument_source(asset.id) is None


async def test_existing_source_reused_venue_never_mixed(db):
    # Источник уже выбран (bybit) — повторный прогон не перескакивает на
    # binance, даже если там появилась пара (§4: одна история = один источник)
    bybit = FakeVenue("bybit", {"AAA": ["USDT"]})
    r = make_runner(db, [bybit], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")
    binance = FakeVenue("binance", {"AAA": ["USDT"]})
    r2 = make_runner(db, [binance, bybit], FakeCmc([_listing(11, "AAA", 11)]))
    await r2.run_daily("test")
    src = db.get_alt_instrument_source(db.get_alt_asset_by_cmc_id(11).id)
    assert src.venue == "bybit"


# ---------------------------------------------------------------------------
# Первичная загрузка: пагинация + backfill-антиспам (§4, §18)
# ---------------------------------------------------------------------------


async def test_first_load_paginates_full_history_backfill_no_spam(db):
    base = build_accumulation(seg_len=120)
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": _to_candles(base)}, page_size=100)
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    res = await r.run_daily("test")
    assert res["status"] == "ok"

    asset = db.get_alt_asset_by_cmc_id(11)
    src = db.get_alt_instrument_source(asset.id)
    stored = db.get_alt_candles(src.id)
    assert len(stored) == len(base)  # пагинация собрала все страницы
    assert len([c for c in venue.klines_calls if c[0] == "AAAUSDT"]) > 1
    assert src.history_scope == "full"
    assert src.earliest_available_ms == stored[0].open_time
    assert src.last_closed_ms == stored[-1].open_time

    # Backfill: события созданы движком, но помечены delivered БЕЗ отправки
    assert _alt_event_count(db) > 0
    assert db.pending_alt_events(limit=1000) == []
    entry = res["summary"]["per_asset"][0]
    assert entry["mode"] == "backfill"
    assert entry["events_marked_delivered"] == _alt_event_count(db)
    assert res["summary"]["backfill_event_ids"]
    assert db.get_meta(f"alt:loaded:{asset.id}:{src.id}") is not None


async def test_only_closed_candles_stored(db):
    base = _to_candles(build_accumulation(seg_len=30))
    today_open = (now_ms() // DAY_MS) * DAY_MS
    forming = make_candle(today_open, 1.5, 1.6, 1.4, 1.55,
                          instrument_id=0, closed=False, source="fake")
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": base + [forming]})
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")
    src = db.get_alt_instrument_source(db.get_alt_asset_by_cmc_id(11).id)
    stored = db.get_alt_candles(src.id)
    assert len(stored) == len(base)  # формирующаяся D1 отброшена
    assert all(c.open_time + DAY_MS <= now_ms() for c in stored)


# ---------------------------------------------------------------------------
# Повторный прогон: инкремент, live-режим, идемпотентность
# ---------------------------------------------------------------------------


async def test_second_run_incremental_live_mode_events_pending(db):
    base = build_accumulation(seg_len=120)
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": _to_candles(base)})
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")

    asset = db.get_alt_asset_by_cmc_id(11)
    src = db.get_alt_instrument_source(asset.id)
    last = src.last_closed_ms
    setup = db.list_alt_setups(asset.id)[0]
    lower = db.get_alt_frozen_range(setup.range_id).lower
    calls_before = len(venue.klines_calls)

    # Новая закрытая D1: прокол под L → MANIPULATION_STARTED (low > K, без отмены)
    venue.candles["AAAUSDT"].append(
        make_candle(last + DAY_MS, lower, lower * 1.01, lower * 0.5,
                    lower * 0.9, instrument_id=0, source="fake")
    )
    res2 = await r.run_daily("test")

    new_calls = venue.klines_calls[calls_before:]
    assert new_calls and new_calls[0][1] == last  # инкремент от last_closed
    assert db.get_alt_instrument_source(asset.id).last_closed_ms == last + DAY_MS
    entry = res2["summary"]["per_asset"][0]
    assert entry["mode"] == "live"
    assert entry["events_marked_delivered"] == 0

    # live: новые события остаются pending для диспетчера
    pending = db.pending_alt_events(limit=1000)
    assert pending and all(e.delivered is False for e in pending)
    assert any(e.event_type == "manipulation_started" for e in pending)


async def test_run_twice_idempotent_no_duplicate_events_or_candles(db):
    base = build_accumulation(seg_len=120)
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": _to_candles(base)})
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")
    src = db.get_alt_instrument_source(db.get_alt_asset_by_cmc_id(11).id)
    candles_n = len(db.get_alt_candles(src.id))
    events_n = _alt_event_count(db)

    res2 = await r.run_daily("test")  # тех же данных — без новых свечей
    assert len(db.get_alt_candles(src.id)) == candles_n
    assert _alt_event_count(db) == events_n
    assert db.pending_alt_events(limit=1000) == []
    assert res2["summary"]["backfill_event_ids"] == []


# ---------------------------------------------------------------------------
# Ошибка CMC: stale по снапшоту / no_universe без снапшота (§3)
# ---------------------------------------------------------------------------


async def test_universe_failure_with_snapshot_proceeds_stale(db):
    venue = FakeVenue("binance", {"AAA": ["USDT"]})
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    await r.run_daily("test")

    r.cmc_adapter = FakeCmc(error="cmc: HTTP 429")
    res = await r.run_daily("test")
    assert res["status"] == "ok"
    assert res["summary"]["universe_stale"] is True
    assert res["summary"]["universe_error"] == "cmc: HTTP 429"
    # анализ продолжается по last_good снапшоту, актив обработан
    assert res["summary"]["per_asset"][0]["status"] == "processed"
    run = db.get_alt_run(res["run_id"])
    assert run.universe_snapshot_id is not None


async def test_universe_failure_without_snapshot_is_no_universe(db):
    r = make_runner(db, [FakeVenue("binance", {"AAA": ["USDT"]})],
                    FakeCmc(error="CMC_API_KEY not configured"))
    res = await r.run_daily("test")
    assert res["status"] == "no_universe"
    run = db.get_alt_run(res["run_id"])
    assert run.status == "no_universe"
    assert run.finished_ms is not None
    # никакого выдуманного top-300: активы не созданы
    assert db.list_alt_assets() == []


# ---------------------------------------------------------------------------
# Непрерывность наблюдения (§3)
# ---------------------------------------------------------------------------


async def test_out_of_universe_with_active_setup_processed_flagged(db):
    base = build_accumulation(seg_len=120)
    venue = FakeVenue(
        "binance", {"AAA": ["USDT"], "BBB": ["USDT"]},
        {"AAAUSDT": _to_candles(base), "BBBUSDT": _to_candles(base)},
    )
    cmc = FakeCmc([_listing(11, "AAA", 11), _listing(12, "BBB", 12)])
    r = make_runner(db, [venue], cmc)
    await r.run_daily("test")
    aaa = db.get_alt_asset_by_cmc_id(11)
    assert db.list_alt_setups(aaa.id)[0].universe_eligible is True

    # AAA выпал из выборки — сетап активен: обработка продолжается
    r.cmc_adapter = FakeCmc([_listing(12, "BBB", 12)])
    res = await r.run_daily("test")
    entries = {e["symbol"]: e for e in res["summary"]["per_asset"]}
    assert entries["AAA"]["status"] == "processed"
    assert entries["AAA"]["in_universe"] is False
    assert db.list_alt_setups(aaa.id)[0].universe_eligible is False


async def test_out_of_universe_without_active_setup_skipped(db):
    # CCC был в выборке, но пары нет → сетапа нет; после выхода из выборки
    # актив не обрабатывается (новых сетапов вне вселенной нет)
    venue = FakeVenue("binance", {"AAA": ["USDT"]})
    cmc = FakeCmc([_listing(11, "AAA", 11), _listing(13, "CCC", 13)])
    r = make_runner(db, [venue], cmc)
    await r.run_daily("test")
    ccc = db.get_alt_asset_by_cmc_id(13)
    assert ccc is not None and db.get_alt_instrument_source(ccc.id) is None

    r.cmc_adapter = FakeCmc([_listing(11, "AAA", 11)])
    res = await r.run_daily("test")
    symbols = [e["symbol"] for e in res["summary"]["per_asset"]]
    assert "CCC" not in symbols
    assert db.get_alt_instrument_source(ccc.id) is None


# ---------------------------------------------------------------------------
# Блокировка прогона (§4)
# ---------------------------------------------------------------------------


async def test_run_lock_concurrent_run_skipped(db):
    running = db.insert_alt_run(AltRun(
        id=None, started_ms=now_ms(), as_of_ms=0, status="running"
    ))
    r = make_runner(db, [FakeVenue("binance", {"AAA": ["USDT"]})],
                    FakeCmc([_listing(11, "AAA", 11)]))
    res = await r.run_daily("test")
    assert res["status"] == "skipped_locked"
    assert res["locked_by_run_id"] == running.id
    assert db.get_alt_run(running.id).status == "running"  # не тронули


async def test_stale_running_marked_interrupted_and_new_run_proceeds(db):
    old = db.insert_alt_run(AltRun(
        id=None, started_ms=now_ms() - 7 * 3_600_000, as_of_ms=0,
        status="running",
    ))
    venue = FakeVenue("binance", {"AAA": ["USDT"]})
    r = make_runner(db, [venue], FakeCmc([_listing(11, "AAA", 11)]))
    res = await r.run_daily("test")
    assert res["status"] == "ok"
    assert db.get_alt_run(old.id).status == "interrupted"


# ---------------------------------------------------------------------------
# Расписание (§4, §18)
# ---------------------------------------------------------------------------


def test_next_run_time_msk(db):
    r = make_runner(db, [], FakeCmc(), AltConfig(job_hour_msk=3, job_minute_msk=15))
    # 15:00 МСК → следующий слот завтра 03:15 МСК
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    assert r.next_run_time(now) == datetime(2026, 10, 8, 3, 15, tzinfo=MSK)
    # 03:00 МСК → сегодня 03:15 МСК
    now2 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)
    assert r.next_run_time(now2) == datetime(2026, 10, 7, 3, 15, tzinfo=MSK)


def test_should_run_first_and_catchup_once(db):
    r = make_runner(db, [], FakeCmc(), AltConfig(job_hour_msk=3, job_minute_msk=15))
    now = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)  # 08:00 МСК, слот прошёл
    assert r.should_run(now) is True  # ни разу не запускали — сразу
    old_ms = int((now - timedelta(days=2)).timestamp() * 1000)
    db.insert_alt_run(AltRun(id=None, started_ms=old_ms, as_of_ms=0, status="ok"))
    # пропущенные сутки — разовый catch-up
    assert r.should_run(now) is True
    fresh_ms = int(now.timestamp() * 1000)
    db.insert_alt_run(AltRun(id=None, started_ms=fresh_ms, as_of_ms=0, status="ok"))
    # успешный прогон в текущем слоте — больше не запускаем
    assert r.should_run(now) is False


def test_should_run_no_retry_spam_after_error_in_slot(db):
    r = make_runner(db, [], FakeCmc(), AltConfig(job_hour_msk=3, job_minute_msk=15))
    now = datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)
    fresh_ms = int(now.timestamp() * 1000)
    db.insert_alt_run(AltRun(id=None, started_ms=fresh_ms, as_of_ms=0,
                             status="error"))
    # ошибка в этом слоте не перезапускает job каждый poll
    assert r.should_run(now) is False


# ---------------------------------------------------------------------------
# Этап 7: флаг версии движка, backfill без рассылки, откат v2 → v1
# ---------------------------------------------------------------------------


async def test_v2_flag_backfill_episodes_without_pending_events(db):
    """engine_version=v2 пишет эпизоды alt-0.2 и не оставляет pending-события."""
    base = build_accumulation(seg_len=120)
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": _to_candles(base)})
    r = make_runner(
        db, [venue], FakeCmc([_listing(11, "AAA", 11)]),
        AltConfig(engine_version="v2"),
    )
    res = await r.run_daily("test")
    assert res["status"] == "ok"
    asset = db.get_alt_asset_by_cmc_id(11)
    episodes = db.list_alt_range_episodes(asset.id)
    assert episodes
    assert all(e.rules_version == "alt-0.2" for e in episodes)
    assert db.list_alt_setups(asset.id) == []
    assert db.pending_alt_events(limit=1000) == []
    entry = res["summary"]["per_asset"][0]
    assert entry["mode"] == "backfill"
    assert entry["events_marked_delivered"] == 0
    src = db.get_alt_instrument_source(asset.id)
    assert db.get_meta(f"alt:engine:{asset.id}:{src.id}") == "v2"


async def test_switch_v2_to_v1_keeps_episodes_and_does_not_spam(db):
    """Откат флага на v1 не стирает эпизоды v2 и не шлёт историю v1 как новую."""
    base = build_accumulation(seg_len=120)
    venue = FakeVenue("binance", {"AAA": ["USDT"]},
                      {"AAAUSDT": _to_candles(base)})
    listings = [_listing(11, "AAA", 11)]
    r2 = make_runner(
        db, [venue], FakeCmc(listings), AltConfig(engine_version="v2"),
    )
    await r2.run_daily("test")
    asset = db.get_alt_asset_by_cmc_id(11)
    before = [
        (e.origin_key, e.lower, e.upper, e.state)
        for e in db.list_alt_range_episodes(asset.id)
    ]
    assert before

    r1 = make_runner(db, [venue], FakeCmc(listings), AltConfig())
    res = await r1.run_daily("test")
    assert res["summary"]["per_asset"][0]["mode"] == "backfill"
    after = [
        (e.origin_key, e.lower, e.upper, e.state)
        for e in db.list_alt_range_episodes(asset.id)
    ]
    assert after == before
    assert db.list_alt_setups(asset.id)
    assert db.pending_alt_events(limit=1000) == []
    src = db.get_alt_instrument_source(asset.id)
    assert db.get_meta(f"alt:engine:{asset.id}:{src.id}") == "v1"
