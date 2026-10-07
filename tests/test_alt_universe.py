"""Тесты модуля вселенной альткоинов: адаптер CMC (respx), select_universe,
refresh_universe (успех / stale по последнему снапшоту / пусто без снапшота).
"""
from __future__ import annotations

import json

import pytest
import respx

from app.adapters.base import AdapterError
from app.adapters.coinmarketcap import CoinMarketCapAdapter, CmcListing
from app.alt import universe
from app.alt.universe import (
    CANONICAL_MAP,
    classify_exclusion,
    refresh_universe,
    select_universe,
)
from app.config import AltConfig

CMC = "https://test.cmc"


def _cmc_row(
    cmc_id: int,
    symbol: str,
    rank: int,
    name: str = "",
    tags: list[str] | None = None,
    market_cap: float = 1_000_000.0,
    platform: dict | None = None,
) -> dict:
    return {
        "id": cmc_id,
        "name": name or f"Coin {symbol}",
        "symbol": symbol,
        "cmc_rank": rank,
        "tags": tags or [],
        "date_added": "2020-01-01T00:00:00.000Z",
        "platform": platform,
        "quote": {"USD": {"market_cap": market_cap}},
    }


def _listing(
    cmc_id: int,
    symbol: str,
    rank: int,
    name: str = "",
    tags: list[str] | None = None,
) -> CmcListing:
    return CmcListing(
        cmc_id=cmc_id, name=name or f"Coin {symbol}", symbol=symbol,
        rank=rank, market_cap=1_000_000.0, tags=tags or [],
        date_added="2020-01-01T00:00:00.000Z", platform_info=None,
    )


def _cfg(min_rank: int = 11, max_rank: int = 300) -> AltConfig:
    return AltConfig(cmc_rank_min=min_rank, cmc_rank_max=max_rank)


# ---------------- CoinMarketCapAdapter ----------------


async def test_cmc_listings_latest_parses_rows():
    adapter = CoinMarketCapAdapter(CMC, api_key="test-key")
    payload = {
        "status": {"error_code": 0},
        "data": [
            _cmc_row(1, "BTC", 1, name="Bitcoin", tags=["pow"],
                     market_cap=2e12),
            _cmc_row(1027, "ETH", 2, name="Ethereum",
                     tags=["smart-contracts"], market_cap=4e11,
                     platform={"name": "Ethereum", "symbol": "ETH"}),
        ],
    }
    with respx.mock:
        route = respx.get(f"{CMC}/v1/cryptocurrency/listings/latest").respond(
            200, json=payload
        )
        listings = await adapter.listings_latest(limit=300)
    assert len(listings) == 2
    btc = listings[0]
    assert (btc.cmc_id, btc.symbol, btc.rank) == (1, "BTC", 1)
    assert btc.market_cap == 2e12
    assert btc.tags == ["pow"]
    assert btc.date_added == "2020-01-01T00:00:00.000Z"
    eth = listings[1]
    assert eth.platform_info == {"name": "Ethereum", "symbol": "ETH"}
    # Ключ уходит заголовком, параметры start/limit/convert на месте
    request = route.calls[0].request
    assert request.headers["X-CMC_PRO_API_KEY"] == "test-key"
    assert "start=1" in str(request.url) and "limit=300" in str(request.url)
    assert "convert=USD" in str(request.url)
    await adapter.aclose()


async def test_cmc_http_error_raises_adapter_error():
    adapter = CoinMarketCapAdapter(CMC, api_key="bad-key")
    with respx.mock:
        respx.get(f"{CMC}/v1/cryptocurrency/listings/latest").respond(
            401, json={"status": {"error_message": "Invalid API key"}}
        )
        with pytest.raises(AdapterError, match="401"):
            await adapter.listings_latest()
    await adapter.aclose()


async def test_cmc_empty_api_key_raises_on_call_not_on_init():
    adapter = CoinMarketCapAdapter(CMC, api_key="")  # конструктор не падает
    with pytest.raises(AdapterError, match="CMC_API_KEY not configured"):
        await adapter.listings_latest()
    await adapter.aclose()


async def test_cmc_malformed_payload_raises_adapter_error():
    adapter = CoinMarketCapAdapter(CMC, api_key="k")
    with respx.mock:
        respx.get(f"{CMC}/v1/cryptocurrency/listings/latest").respond(
            200, json={"status": {"error_code": 0}}  # нет поля data
        )
        with pytest.raises(AdapterError):
            await adapter.listings_latest()
    await adapter.aclose()


# ---------------- classify_exclusion / select_universe ----------------


def test_classify_exclusion_categories():
    assert classify_exclusion(_listing(1, "USDT", 5)) == "stable"
    assert classify_exclusion(_listing(2, "XYZ", 50, tags=["usd-stablecoin"])) == "stable"
    assert classify_exclusion(_listing(3, "WBTC", 15)) == "wrapped"
    assert classify_exclusion(_listing(4, "FOO", 20, tags=["wrapped"])) == "wrapped"
    assert classify_exclusion(_listing(5, "BAR", 25, tags=["binance-peg"])) == "duplicate"
    assert classify_exclusion(_listing(8085, "STETH", 30)) == "duplicate"  # CANONICAL_MAP
    assert classify_exclusion(_listing(6, "SOL", 12)) is None


def test_select_universe_rank_window_no_backfill():
    cfg = _cfg(11, 13)
    listings = [
        _listing(10, "TOP", 10),          # выше окна — не трек-аем
        _listing(11, "AAA", 11),
        _listing(12, "USDT", 12),         # стейбл — исключён
        _listing(13, "BBB", 13),
        _listing(14, "CCC", 14),          # ниже окна — НЕ добираем (no backfill)
    ]
    included, excluded = select_universe(listings, cfg)
    assert [a.symbol for a in included] == ["AAA", "BBB"]
    assert [a.symbol for a in excluded] == ["USDT"]
    assert excluded[0].exclusion_category == "stable"
    # выборка меньше окна после исключений — добора из 14+ не произошло
    assert all(a.cmc_rank <= 13 for a in included)


def test_select_universe_excludes_wrapped_and_duplicate():
    cfg = _cfg(11, 20)
    listings = [
        _listing(101, "AAA", 11),
        _listing(102, "WXYZ", 12, tags=["wrapped"]),
        _listing(103, "PEG", 13, tags=["binance-peg"]),
    ]
    included, excluded = select_universe(listings, cfg)
    assert [a.symbol for a in included] == ["AAA"]
    assert {a.symbol: a.exclusion_category for a in excluded} == {
        "WXYZ": "wrapped", "PEG": "duplicate",
    }


def test_select_universe_same_symbol_different_cmc_id_mapping_pending():
    cfg = _cfg(11, 20)
    listings = [
        _listing(201, "DUP", 11, name="Dup One"),
        _listing(202, "DUP", 12, name="Dup Two"),  # тот же тикер, другой cmc_id
        _listing(203, "OK", 13),
    ]
    included, _ = select_universe(listings, cfg)
    assert len(included) == 3  # не сливаем
    by_symbol = {a.cmc_id: a for a in included}
    assert by_symbol[201].mapping_status == "mapping_pending"
    assert by_symbol[201].mapping_reason == "same symbol, different CMC id"
    assert by_symbol[202].mapping_status == "mapping_pending"
    assert by_symbol[203].mapping_status == "pending"


def test_select_universe_duplicate_canonical_keeps_lower_rank(monkeypatch):
    # Две записи одного экономического актива (alias, не исключаются)
    monkeypatch.setitem(
        CANONICAL_MAP, 301,
        {"canonical_asset_id": "ALI", "kind": "alias", "note": "Alias One"},
    )
    monkeypatch.setitem(
        CANONICAL_MAP, 302,
        {"canonical_asset_id": "ALI", "kind": "alias", "note": "Alias Two"},
    )
    cfg = _cfg(11, 20)
    listings = [
        _listing(302, "ALI2", 11),   # ранг лучше, но проверим оба порядка
        _listing(301, "ALI1", 12),
        _listing(303, "OK", 13),
    ]
    included, excluded = select_universe(listings, cfg)
    assert [a.cmc_id for a in included] == [302, 303]  # меньший ранг остался
    assert [a.cmc_id for a in excluded] == [301]
    assert excluded[0].exclusion_category == "duplicate"
    assert "kept cmc_id=302" in excluded[0].mapping_reason


# ---------------- refresh_universe ----------------


class _StubAdapter:
    """Подмена CoinMarketCapAdapter: listings_latest из памяти или ошибка."""

    def __init__(self, listings: list[CmcListing] | None = None, error: str | None = None):
        self._listings = listings or []
        self._error = error

    async def listings_latest(self, limit: int = 300) -> list[CmcListing]:
        if self._error:
            raise AdapterError(self._error)
        return self._listings


async def test_refresh_universe_success_writes_snapshot_and_assets(db):
    cfg = _cfg(11, 15)
    adapter = _StubAdapter([
        _listing(11, "AAA", 11),
        _listing(12, "USDT", 12),
        _listing(13, "BBB", 13),
        _listing(99, "OUT", 99),  # вне окна
    ])
    result = await refresh_universe(db, adapter, cfg, now_ms=1_000_000)
    assert result["stale"] is False and result["empty"] is False
    assert (result["included"], result["excluded"]) == (2, 1)
    assert result["included_cmc_ids"] == [11, 13]

    snap = db.get_latest_alt_universe_snapshot()
    assert snap is not None and snap["id"] == result["snapshot_id"]
    assert snap["stale"] == 0
    payload = json.loads(snap["payload_json"])
    assert len(payload) == 4 and payload[0]["symbol"] == "AAA"

    aaa = db.get_alt_asset_by_cmc_id(11)
    assert aaa is not None and aaa.symbol == "AAA" and aaa.cmc_rank == 11
    assert aaa.exclusion_category is None and aaa.enabled is True
    usdt = db.get_alt_asset_by_cmc_id(12)
    assert usdt.exclusion_category == "stable"
    assert db.get_alt_asset_by_cmc_id(99) is None  # вне окна — не трек-аем

    # Повторный прогон идемпотентен по UNIQUE(cmc_id)
    result2 = await refresh_universe(db, adapter, cfg, now_ms=2_000_000)
    assert result2["included"] == 2
    assert db.get_alt_asset_by_cmc_id(11).id == aaa.id


async def test_refresh_universe_preserves_manual_override(db):
    cfg = _cfg(11, 15)
    adapter = _StubAdapter([_listing(11, "AAA", 11)])
    await refresh_universe(db, adapter, cfg, now_ms=1_000_000)

    # Ручной разбор: exclusion выставлен вручную (convention mapping_status="manual")
    row = db.get_alt_asset_by_cmc_id(11)
    row.mapping_status = "manual"
    row.mapping_reason = "manual review: scam clone"
    row.exclusion_category = "manual_exclude"
    row.enabled = False
    db.upsert_alt_asset(row)

    await refresh_universe(db, adapter, cfg, now_ms=2_000_000)
    after = db.get_alt_asset_by_cmc_id(11)
    assert after.mapping_status == "manual"
    assert after.mapping_reason == "manual review: scam clone"
    assert after.exclusion_category == "manual_exclude"
    assert after.enabled is False
    assert after.cmc_rank == 11  # справочные поля обновляются


async def test_refresh_universe_cmc_failure_reuses_last_snapshot_stale(db):
    cfg = _cfg(11, 15)
    ok_adapter = _StubAdapter([
        _listing(11, "AAA", 11),
        _listing(12, "USDT", 12),
        _listing(13, "BBB", 13),
    ])
    await refresh_universe(db, ok_adapter, cfg, now_ms=1_000_000)

    failing = _StubAdapter(error="cmc: HTTP 429")
    result = await refresh_universe(db, failing, cfg, now_ms=2_000_000)
    assert result["stale"] is True and result["empty"] is False
    assert result["error"] == "cmc: HTTP 429"
    assert (result["included"], result["excluded"]) == (2, 1)
    assert result["included_cmc_ids"] == [11, 13]
    assert result["snapshot_taken_ms"] == 1_000_000
    # Новых снапшотов и перезаписи активов на stale-пути нет
    assert db.get_latest_alt_universe_snapshot()["taken_ms"] == 1_000_000


async def test_refresh_universe_failure_without_snapshot_is_empty(db):
    cfg = _cfg(11, 15)
    failing = _StubAdapter(error="CMC_API_KEY not configured")
    result = await refresh_universe(db, failing, cfg, now_ms=1_000_000)
    assert result == {
        "stale": True, "empty": True, "error": "CMC_API_KEY not configured",
        "snapshot_id": None, "included": 0, "excluded": 0,
        "included_cmc_ids": [],
    }
    assert db.get_latest_alt_universe_snapshot() is None


def test_listing_from_dict_roundtrip():
    original = _listing(42, "AAA", 11, tags=["pow"])
    restored = universe._listing_from_dict(
        json.loads(json.dumps(original.__dict__))
    )
    assert restored == original
