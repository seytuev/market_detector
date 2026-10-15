"""Рыночная корзина: исключения с причинами, версия, покрытие, доходность."""
from __future__ import annotations

import pytest

from app.adapters.coinmarketcap import CmcListing
from app.events.basket import (
    DEFAULT_SIZE,
    Basket,
    BasketMember,
    basket_from_static,
    basket_metrics,
    basket_unavailable,
    build_basket,
    day_sign,
    msk_day_return,
)
from app.events.mathutil import DAY_MS

HOUR_MS = 3_600_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % DAY_MS)
NOW = T0 + 15 * HOUR_MS
DAY_START = T0
DAY_END = T0 + DAY_MS


def listing(symbol, rank, cmc_id=None, tags=()) -> CmcListing:
    return CmcListing(
        cmc_id=cmc_id if cmc_id is not None else 1000 + rank,
        name=f"{symbol} name",
        symbol=symbol,
        rank=rank,
        market_cap=1e9,
        tags=list(tags),
    )


def bar(start, end, open_, close, quality="ok") -> dict:
    return {"start": start, "end": end, "open": float(open_),
            "close": float(close), "quality": quality}


def day_bars(start, end, final):
    return [bar(t, t + HOUR_MS, 100, final if t + HOUR_MS == end else 100)
            for t in range(start, end, HOUR_MS)]


def test_build_basket_records_exclusion_reasons():
    listings = [
        listing("BTC", 1),
        listing("USDT", 2, tags=["stablecoin"]),
        listing("WBTC", 3),
        listing("NOPE", 4),
        listing("ETH", 5),
    ]
    pairs = {"BTCUSDT", "ETHUSDT"}  # NOPE без пары у биржи
    basket = build_basket(listings, pairs, day_start_ms=DAY_START)
    assert basket.source == "cmc_snapshot"
    assert [m.symbol for m in basket.members] == ["BTC", "ETH"]
    assert basket.members[0].pair == "BTCUSDT"
    reasons = {e["symbol"]: e["reason"] for e in basket.exclusions}
    assert reasons == {"USDT": "stable", "WBTC": "wrapped", "NOPE": "no_binance_pair"}
    assert all({"symbol", "cmc_id", "rank", "reason"} <= set(e) for e in basket.exclusions)


def test_build_basket_accepts_dicts():
    listings = [
        {"symbol": "BTC", "cmc_rank": 1, "id": 1, "tags": []},
        {"symbol": "FDUSD", "cmc_rank": 2, "id": 2, "tags": []},
    ]
    basket = build_basket(listings, {"BTCUSDT"}, day_start_ms=DAY_START)
    assert [m.symbol for m in basket.members] == ["BTC"]
    assert basket.exclusions[0]["reason"] == "stable"


def test_build_basket_size_limit():
    listings = [listing(f"A{i:02d}", i + 1) for i in range(25)]
    pairs = {f"A{i:02d}USDT" for i in range(25)}
    basket = build_basket(listings, pairs, day_start_ms=DAY_START)
    assert len(basket.members) == DEFAULT_SIZE
    assert [m.symbol for m in basket.members][:3] == ["A00", "A01", "A02"]


def test_basket_version_deterministic_and_sensitive():
    listings = [listing("BTC", 1), listing("ETH", 2)]
    pairs = {"BTCUSDT", "ETHUSDT"}
    v1 = build_basket(listings, pairs, day_start_ms=DAY_START).version
    v2 = build_basket(listings, pairs, day_start_ms=DAY_START).version
    assert v1 == v2 and len(v1) == 16
    # Другой день — другая версия.
    v3 = build_basket(listings, pairs, day_start_ms=DAY_START + DAY_MS).version
    assert v3 != v1
    # Другой состав — другая версия.
    v4 = build_basket(listings[:1], pairs, day_start_ms=DAY_START).version
    assert v4 != v1


def test_basket_from_static():
    basket = basket_from_static(["BTC", "ETH"], day_start_ms=DAY_START)
    assert basket.source == "static"
    assert basket.members == [
        BasketMember(symbol="BTC", cmc_id=0, rank=1, pair="BTCUSDT"),
        BasketMember(symbol="ETH", cmc_id=0, rank=2, pair="ETHUSDT"),
    ]
    assert len(basket.version) == 16


def test_basket_unavailable():
    basket = basket_unavailable(day_start_ms=DAY_START, reason="cmc_down")
    assert basket.source == "unavailable"
    assert basket.members == []
    assert basket.exclusions[0]["reason"] == "cmc_down"
    metrics = basket_metrics(basket, {}, now_ms=NOW, max_age_ms=HOUR_MS)
    assert metrics["coverage"] == {"ok": 0, "total": 0, "ratio": 0.0}
    assert metrics["median_return"] is None
    assert metrics["breadth_up"] is None


def test_basket_metrics_median_breadth_coverage():
    ws = NOW - DAY_MS  # начало окна
    basket = basket_from_static(["AAA", "BBB", "CCC", "DDD"], day_start_ms=DAY_START)
    bars = {
        "AAA": day_bars(ws, NOW, 110),
        "BBB": day_bars(ws, NOW, 90),
        # Свежий бар непригодного качества не считается; старый ok-бар → stale.
        "CCC": [bar(NOW - 5 * HOUR_MS, NOW - 4 * HOUR_MS, 100, 100),
                bar(NOW - HOUR_MS, NOW, 100, 105, quality="bad")],
    }
    metrics = basket_metrics(basket, bars, now_ms=NOW, max_age_ms=HOUR_MS)
    assert metrics["window_ms"] == DAY_MS
    assert metrics["coverage"] == {"ok": 2, "total": 4, "ratio": 0.5}
    # Просроченный/отсутствующий участники не считаются нулевой доходностью.
    assert metrics["median_return"] == pytest.approx(0.0, abs=1e-9)
    assert metrics["breadth_up"] == pytest.approx(0.5)
    status = {m["symbol"]: (m["status"], m["return"]) for m in metrics["members"]}
    assert status["AAA"] == ("ok", pytest.approx(0.1))
    assert status["BBB"] == ("ok", pytest.approx(-0.1))
    assert status["CCC"] == ("stale", None)
    assert status["DDD"] == ("missing", None)


def test_basket_metrics_fresh_bar_without_window_start_reference():
    basket = basket_from_static(["AAA"], day_start_ms=DAY_START)
    bars = {"AAA": [bar(NOW - HOUR_MS, NOW, 100, 110)]}
    metrics = basket_metrics(basket, bars, now_ms=NOW, max_age_ms=HOUR_MS)
    assert metrics["members"][0]["status"] == "incomplete"
    assert metrics["members"][0]["return"] is None
    assert metrics["median_return"] is None
    assert metrics["coverage"]["ratio"] == 0.0


def test_msk_day_return_complete_and_incomplete():
    basket = basket_from_static(["AAA", "BBB"], day_start_ms=DAY_START)
    bars_full = {
        "AAA": day_bars(DAY_START, DAY_END, 103),
        "BBB": day_bars(DAY_START, DAY_END, 97),
    }
    res = msk_day_return(basket, bars_full, day_start_ms=DAY_START, day_end_ms=DAY_END)
    assert res == {
        "median_return": pytest.approx(0.0, abs=1e-9),
        "members_ok": 2,
        "members_total": 2,
        "complete": True,
    }
    # Бар BBB, выходящий за границу дня, не считается: участник без данных.
    res = msk_day_return(
        basket, {"AAA": bars_full["AAA"], "BBB": [bar(DAY_START - 1000, DAY_START, 100, 97)]},
        day_start_ms=DAY_START, day_end_ms=DAY_END,
    )
    assert res["median_return"] == pytest.approx(0.03)
    assert res["members_ok"] == 1
    assert res["complete"] is False


def test_msk_day_return_empty_basket():
    basket = basket_unavailable(day_start_ms=DAY_START, reason="cmc_down")
    res = msk_day_return(basket, {}, day_start_ms=DAY_START, day_end_ms=DAY_END)
    assert res == {"median_return": None, "members_ok": 0,
                   "members_total": 0, "complete": False}


def test_day_sign():
    assert day_sign(0.03) == 1
    assert day_sign(1e-12) == 1
    assert day_sign(0.0) == 0
    assert day_sign(-0.03) == -1
    with pytest.raises(ValueError):
        day_sign(None)
