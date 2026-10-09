"""Один проход загрузки. Сеть снаружи транзакции, ошибка не останавливает H1."""
from __future__ import annotations

import hashlib
import logging
from decimal import Decimal

from ..adapters.coinglass import (
    AuthError,
    CapabilityError,
    CoinglassClient,
    CoinglassError,
    as_decimal,
)
from .evaluate import EvaluationInput, FundingBar, LiqBar, OiBar, PriceBar, evaluate
from .mathutil import DAY_MS
from .store import apply_snapshot, upsert_bucket

log = logging.getLogger(__name__)

# Проверка ключа 2026-10-09: Hobbyist отдаёт 1d/4h/8h и отказывает 1h/30m.
# Повторно 1h не запрашиваем: каждый отказ тоже расходует минутный лимит.
CAPABILITY = {
    "checked_on": "2026-10-09",
    "intervals_ok": ["1d", "4h", "8h"],
    "intervals_denied": ["1h", "30m"],
    "funding_unit": "percent",
    "rate_kind": "indicative",
    "bucket_time": "start",
    "end_time": "exclusive",
    "h1_available": False,
}


def scope_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


LIQ_SCOPE = scope_id("coinglass", "liq-aggregated-v4", "BTC", "Binance", "usd")
OI_COIN_SCOPE = scope_id("coinglass", "oi-aggregated-v4", "BTC", "coin")
OI_USD_SCOPE = scope_id("coinglass", "oi-aggregated-v4", "BTC", "usd")
FUND_SCOPE = scope_id("coinglass", "funding-ohlc-v4", "Binance", "BTCUSDT", "indicative")


def run_cycle(db, settings, *, fetcher=None, now_ms: int | None = None) -> dict:
    from ..models import now_ms as clock

    now = now_ms if now_ms is not None else clock()
    if not getattr(settings, "events_enabled", True):
        health = {"quality": "disabled"}
        db.set_meta("events:health", _tiny(health))
        return health
    key = getattr(settings, "coinglass_api_key", "") or ""
    if fetcher is None and not key:
        health = {"quality": "auth_error", "detail": "COINGLASS_API не задан"}
        db.set_meta("events:health", _tiny(health))
        return health
    try:
        payload = fetcher() if fetcher else _fetch(settings)
    except AuthError:
        health = {"quality": "auth_error", "detail": "ключ Coinglass отклонён"}
        db.set_meta("events:health", _tiny(health))
        return health
    except CapabilityError as exc:
        health = {"quality": "capability_error", "detail": str(exc)}
        db.set_meta("events:health", _tiny(health))
        return health
    except CoinglassError as exc:
        health = {"quality": "unavailable", "detail": str(exc)}
        db.set_meta("events:health", _tiny(health))
        return health
    _store_payload(db, payload, now)
    snapshot = _evaluate_stored(db, settings, now, payload.get("price_source"))
    notify = bool(getattr(settings, "events_notify_enabled", False))
    # Флаг включает статус pending, но отправитель Telegram сознательно не подключён:
    # сначала тень. pending здесь не уходит в бот.
    apply_snapshot(db, snapshot, notify=notify)
    health = {
        "quality": "ok",
        "stale": False,
        "plan": (payload.get("plan") or {}).get("level"),
        "capability": CAPABILITY,
        "limits": payload.get("limits") or {},
        "evaluated_at": now,
        "notify": "shadow" if not notify else "pending_not_sent",
    }
    db.set_meta("events:health", _tiny(health))
    return {"health": health, "market_line": snapshot["market_line"]}


def _fetch(settings) -> dict:
    client = CoinglassClient(settings.coinglass_api_key, settings.coinglass_base_url)
    liq = client.liquidations("BTC", "Binance", "1d", 150)
    oi_coin = client.open_interest("BTC", "coin", "1d", 150)
    oi_usd = client.open_interest("BTC", "usd", "1d", 150)
    funding = client.funding("Binance", "BTCUSDT", "1d", 150)
    plan = client.subscription()
    return {
        "liq": liq,
        "oi_coin": oi_coin,
        "oi_usd": oi_usd,
        "funding": funding,
        "plan": plan,
        "limits": dict(client.last_limits),
    }


def _store_payload(db, payload: dict, now: int) -> None:
    for row in payload.get("liq") or []:
        _put_liq(db, row, now)
    for row in payload.get("oi_coin") or []:
        _put_oi(db, OI_COIN_SCOPE, "oi_coin", row, now)
    for row in payload.get("oi_usd") or []:
        _put_oi(db, OI_USD_SCOPE, "oi_usd", row, now)
    for row in payload.get("funding") or []:
        _put_funding(db, row, now)
    db._commit()


def _put_liq(db, row: dict, now: int) -> None:
    start = int(row["time"])
    long_usd = as_decimal(row["aggregated_long_liquidation_usd"])
    short_usd = as_decimal(row["aggregated_short_liquidation_usd"])
    quality = "integrity_error" if long_usd < 0 or short_usd < 0 else "ok"
    upsert_bucket(
        db, scope_id=LIQ_SCOPE, series="liq", interval="1d", bucket_start=start,
        payload={"long_usd": long_usd, "short_usd": short_usd},
        quality=quality, available_at=now,
    )


def _put_oi(db, scope: str, series: str, row: dict, now: int) -> None:
    start = int(row["time"])
    open_, high, low, close = (as_decimal(row[k]) for k in ("open", "high", "low", "close"))
    quality = "ok"
    if min(open_, close) < low or max(open_, close) > high or low < 0 or open_ < 0:
        quality = "integrity_error"
    upsert_bucket(
        db, scope_id=scope, series=series, interval="1d", bucket_start=start,
        payload={"open": open_, "high": high, "low": low, "close": close},
        quality=quality, available_at=now,
    )


def _put_funding(db, row: dict, now: int) -> None:
    start = int(row["time"])
    close = as_decimal(row["close"])
    upsert_bucket(
        db, scope_id=FUND_SCOPE, series="funding", interval="1d", bucket_start=start,
        payload={"close_percent": close, "rate_kind": "indicative", "unit": "percent"},
        quality="ok", available_at=now,
    )


def _evaluate_stored(db, settings, now: int, price_source: str | None) -> dict:
    ins = _btc(db)
    price = []
    source = price_source or ""
    if ins is not None:
        source = f"{ins.venue} {ins.symbol} {ins.market_type}"
        start = now - 160 * DAY_MS
        for candle in db.get_candles(ins.id, "D1", start_ms=start, closed_only=False):
            price.append(PriceBar(
                t=candle.open_time,
                o=Decimal(str(candle.open)),
                h=Decimal(str(candle.high)),
                l=Decimal(str(candle.low)),
                c=Decimal(str(candle.close)),
                closed=bool(candle.closed),
            ))
    liq = [_liq_bar(row, now) for row in _load(db, LIQ_SCOPE, "liq")]
    oi_coin = [_oi_bar(row, now) for row in _load(db, OI_COIN_SCOPE, "oi_coin")]
    oi_usd = [_oi_bar(row, now) for row in _load(db, OI_USD_SCOPE, "oi_usd")]
    funding = [_fund_bar(row, now) for row in _load(db, FUND_SCOPE, "funding")]
    return evaluate(EvaluationInput(
        symbol="BTC",
        now_ms=now,
        price=price,
        liq=[b for b in liq if b],
        oi_coin=[b for b in oi_coin if b],
        oi_usd=[b for b in oi_usd if b],
        funding=[b for b in funding if b],
        h1_available=False,
        h1_hold=None,
        funding_unit="percent",
        rate_kind="indicative",
        gate_enabled=bool(getattr(settings, "events_strategy_gate_enabled", True)),
        price_source=source,
        liq_scope=LIQ_SCOPE,
        oi_scope=OI_COIN_SCOPE,
        funding_scope=FUND_SCOPE,
        data_stale=False,
        asset_profile="BTC",
        scope_asset="BTC",
    ))


def _load(db, scope: str, series: str) -> list[dict]:
    from .store import load_buckets
    return load_buckets(db, scope, series)


def _closed(start: int, now: int) -> bool:
    return start + DAY_MS <= now


def _liq_bar(row: dict, now: int):
    if row.get("quality") != "ok":
        return None
    return LiqBar(
        t=row["t"],
        long_usd=Decimal(str(row["long_usd"])),
        short_usd=Decimal(str(row["short_usd"])),
        closed=_closed(row["t"], now),
    )


def _oi_bar(row: dict, now: int):
    if row.get("quality") != "ok":
        return None
    return OiBar(
        t=row["t"],
        open=Decimal(str(row["open"])),
        close=Decimal(str(row["close"])),
        closed=_closed(row["t"], now),
    )


def _fund_bar(row: dict, now: int):
    return FundingBar(
        t=row["t"],
        close_percent=Decimal(str(row["close_percent"])),
        closed=_closed(row["t"], now),
    )


def _btc(db):
    for ins in db.get_instruments(enabled_only=True):
        if ins.venue == "binance" and ins.symbol == "BTCUSDT":
            return ins
    return None


def _tiny(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)
