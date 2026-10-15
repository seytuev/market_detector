"""Цикл движка «События и время» (спека 2026-10-11, §11).

Каждый загрузчик независим: ошибка источника фиксируется в его
source_state и не останавливает остальные (AC19) — общего раннего
выхода нет. Рыночное направление считает market.evaluate_market;
прежний BTC-расчёт (_evaluate_stored + apply_snapshot) сохранён как
помеченный legacy-путь и подключается к оценке как «Фон BTC».
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
import threading
from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from ..adapters.coinglass import (
    AuthError,
    CapabilityError,
    CoinglassError,
    as_decimal,
)
from . import memory, publish, sources, store
from .basket import (
    Basket,
    BasketMember,
    basket_from_static,
    basket_metrics,
    basket_unavailable,
    build_basket,
    day_sign,
    msk_day_return,
)
from .evaluate import EvaluationInput, FundingBar, LiqBar, OiBar, PriceBar, evaluate, market_direction
from .episodes import liquidation_history
from .btc_context import btc_liquidation_oi
from .calendar_history import update_calendar_history
from .market import MarketInput, evaluate_market
from .mathutil import DAY_MS, median
from .rules import ENGINE_RULE_VERSION, get_rule, registry_for_eval
from .stats import frequency, insufficient
from .store import apply_snapshot, load_buckets, upsert_bucket
from .timeutil import (
    CONTEXT_STALE_AFTER_MS,
    WEEKDAY_RU,
    completed_msk_days,
    msk_day_end_ms,
    msk_day_start_ms,
    msk_hour,
    msk_weekday,
)
from .validation import complete_window, finite_number, hourly_bars
from .revalidation import MODEL_VERSION, basis_for_window, broken_since, merge_basis, phase_order, price_check

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

HOUR_MS = 3_600_000
_PRICE_DAYS = 35          # окно часовых баров корзины (покрывает baseline 30 дней)

_CG_ERRORS = (AuthError, CapabilityError, CoinglassError)
_DATA_ERRORS = (ValueError, TypeError, KeyError, InvalidOperation, OverflowError)


def _ingest_buckets(db, *, scope: str, series: str, interval: str, rows,
                    parse, now: int, errors: list) -> int:
    """Validate one response, quarantine bad timestamps and pipeline history writes."""
    buckets = {}
    accepted = 0
    width = {"1d": DAY_MS, "4h": 4 * HOUR_MS}[interval]
    if not isinstance(rows, (list, tuple)):
        errors.append(CoinglassError("источник не вернул список наблюдений"))
        return 0
    for row in rows:
        try:
            bucket = parse(row, now)
            buckets[bucket["start"]] = bucket
            accepted += 1
        except _DATA_ERRORS:
            errors.append(CoinglassError("непригодная строка источника"))
            try:
                start = _source_time(row, width, now)
                buckets[start] = {"start": start, "quality": "integrity_error", "payload": {"invalid": True}}
            except _DATA_ERRORS:
                pass
    store.upsert_buckets(db, scope_id=scope, series=series, interval=interval,
                         buckets=buckets.values(), available_at=now)
    return accepted


def _liquidation_bucket(row, now: int, *, interval_ms: int = DAY_MS) -> dict:
    start = _source_time(row, interval_ms, now)
    long_usd, short_usd = (as_decimal(row[k]) for k in
                          ("aggregated_long_liquidation_usd", "aggregated_short_liquidation_usd"))
    if not finite_number(long_usd) or not finite_number(short_usd):
        raise ValueError("invalid liquidation volume")
    return {"start": start, "quality": "ok", "payload": {"long_usd": long_usd, "short_usd": short_usd}}


def _oi_bucket(row, now: int) -> dict:
    start = _source_time(row, DAY_MS, now)
    open_, high, low, close = (as_decimal(row[k]) for k in ("open", "high", "low", "close"))
    if (not all(finite_number(v) for v in (open_, high, low, close))
            or min(open_, close) < low or max(open_, close) > high):
        raise ValueError("invalid OI OHLC")
    return {"start": start, "quality": "ok", "payload": {"open": open_, "high": high, "low": low, "close": close}}


def _funding_bucket(row, now: int) -> dict:
    start = _source_time(row, DAY_MS, now)
    close = as_decimal(row["close"])
    if not close.is_finite():
        raise ValueError("invalid funding")
    return {"start": start, "quality": "ok", "payload": {
        "close_percent": close, "rate_kind": "indicative", "unit": "percent"}}


def _source_time(row, interval_ms: int, now: int) -> int:
    start = int(row["time"])
    if start < 0 or start % interval_ms or start > now:
        raise ValueError("invalid bucket time")
    return start


def run_cycle(db, settings, *, fetcher=None, now_ms: int | None = None) -> dict:
    # A manual refresh and the background worker may run in different processes.
    from ..services.runtime import assert_owner, claim_owner, release_owner, renew_owner
    key = "events:cycle_lease"
    claim = claim_owner(db, key, {"instance_id": uuid.uuid4().hex, "role": "events"}, lease_ms=120_000)
    if not claim["owned"]:
        saved = store.latest_assessment(db) or {}
        return {"in_progress": True, "assessment_id": saved.get("assessment_id"),
                "direction": saved.get("direction", "unknown"), "market_line": saved.get("market_line"),
                "quality": "in_progress"}
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(20):
            try:
                renew_owner(db, key, claim["owner"], lease_ms=120_000)
            except Exception:
                log.exception("events: не удалось продлить владение циклом")
                return
    keeper = threading.Thread(target=heartbeat, name="events-lease", daemon=True)
    keeper.start()
    try:
        return _run_cycle(db, settings, fetcher=fetcher, now_ms=now_ms,
                          ownership_check=lambda: assert_owner(db, key, claim["owner"]))
    finally:
        stopped.set()
        keeper.join(timeout=1)
        release_owner(db, key, claim["owner"])


def _run_cycle(db, settings, *, fetcher=None, now_ms: int | None = None, ownership_check=None) -> dict:
    from ..models import now_ms as clock

    now = now_ms if now_ms is not None else clock()
    if not getattr(settings, "events_enabled", True):
        health = {"quality": "disabled"}
        db.set_meta("events:health", _tiny(health))
        return health
    fetcher = fetcher or {}

    # Сводка после простоя — до начала работы цикла, самозащищённая (§9)
    publish.startup_digest(db, settings, now)

    # Календарь не зависит от внешних источников; сначала гасим просроченное
    _set_source(db, "calendar", "ok", None, now)
    # Retry transitions left by an interrupted calculation; publication keys deduplicate them.
    transitions_marker = int(db.get_meta("events:transitions_processed") or 0)
    memory.expire_due(db, now)

    # --- Корзина дня МСК и пары Binance ---
    day_start = msk_day_start_ms(now)
    pairs, pairs_state = _binance_pairs(db, settings, fetcher, day_start)
    basket = _basket_of_day(db, settings, fetcher, day_start, pairs, pairs_state)

    # --- Цены участников корзины (1h, 35 дней) ---
    bars_by_symbol, metrics = _prices_cycle(db, settings, fetcher, basket, now)
    _store_day_metrics(db, basket, bars_by_symbol, now)
    update_calendar_history(db, settings, fetcher, bars_by_symbol, now=now)

    # --- Coinglass: ликвидации 4h рынка, затем legacy BTC 1d / OI / funding ---
    cg = _coinglass_arg(settings, fetcher)
    plan, limits = None, {}
    legacy = {"loaded": False}
    if cg is None:
        for src in ("liquidations", "oi", "funding"):
            _set_source(db, src, "auth_error", "COINGLASS_API не задан", now)
        liq = None
    else:
        if not fetcher:
            try:
                plan = cg.subscription()
                limits = dict(cg.last_limits)
            except _CG_ERRORS as exc:
                log.warning("events: subscription: %s", exc)
        liq, liq_symbols = _liq_cycle(db, settings, cg, fetcher, basket, now, legacy)
        _oi_cycle(db, settings, cg, fetcher, liq_symbols, now, legacy)
        _funding_cycle(db, cg, fetcher, now, legacy)

    # --- Факты памяти из состояния ликвидаций (§5, §6) ---
    if ownership_check:
        ownership_check()
    observed_keys = None
    if liq is not None and liq.get("available"):
        observed_keys = _facts_cycle(db, settings, liq, metrics, bars_by_symbol, basket, now)
    _revalidate_facts(db, settings, bars_by_symbol, basket, now, observed_keys=observed_keys)

    # --- Временной контекст МСК и статистика корзины ---
    time_ctx = _time_ctx(basket, bars_by_symbol, now, min_coverage=float(settings.events_basket_min_coverage))
    stats = _stats(db, settings)

    # --- Оценка рынка ---
    facts = store.query_facts(db, active_now_ms=now)
    assessment = evaluate_market(MarketInput(
        now_ms=now,
        basket=store.latest_basket(db),
        metrics=metrics,
        liq=liq,
        facts=facts,
        time_ctx=time_ctx,
        stats=stats,
        source_states=store.get_source_states(db),
        registry=registry_for_eval(),
        coverage_threshold=float(getattr(settings, "events_basket_min_coverage", 0.8)),
        memory_hours=int(settings.events_memory_hours),
    ))

    # --- Legacy BTC-путь: прежний расчёт, помеченный как фон BTC ---
    snapshot = None
    if legacy["loaded"]:
        snapshot = _evaluate_stored(db, settings, now, None)
        notify = bool(getattr(settings, "events_notify_enabled", False))
        apply_snapshot(db, snapshot, notify=notify)
    else:
        snapshot = store.latest_snapshot(db, "BTC")
    if snapshot:
        assessment["btc_background"] = {
            "direction_note": market_direction(snapshot),
            "label": "Фон BTC",
            "evaluated_at": snapshot.get("evaluated_at"),
            "stale": now - int(snapshot.get("evaluated_at") or 0) > CONTEXT_STALE_AFTER_MS,
        }
    evidence = btc_liquidation_oi(_load(db, LIQ_SCOPE, "liq"), _load(db, OI_COIN_SCOPE, "oi_coin"), now=now)
    if snapshot or evidence["current"] or evidence["recent_events"]:
        assessment["btc_background"] = {**(assessment["btc_background"] or {}), "evidence": evidence,
                                        "data_stale": evidence["state"] == "unknown"}

    # Assessment, publications and their checkpoint become visible together.
    with db.batch_writes():
        if ownership_check:
            ownership_check()
        assessment["available_at"] = now if now_ms is not None else clock()
        prev = store.latest_assessment(db)
        assessment["assessment_id"] = _assessment_id(assessment, now)
        if store.get_assessment(db, assessment["assessment_id"]) is None:
            store.insert_assessment(db, assessment)

        # --- Публикации: переходы цикла + существенные изменения (§9) ---
        transitions = [
            t for t in store.fact_transitions_since(db, 0) if t["id"] > transitions_marker
        ]
        publish.process_cycle(
            db, settings, prev=prev, curr=assessment, transitions=transitions, now_ms=now,
        )
        db.set_meta("events:transitions_processed", str(_transitions_marker(db)))

        sources_state = store.get_source_states(db)
        limits = dict(getattr(cg, "last_limits", {}) or limits) if cg is not None else limits
        bad = [f"{s}: {v['quality']}" for s, v in sources_state.items()
               if v["quality"] not in ("ok", "partial")]
        health = {
            "quality": assessment["quality"],
            "detail": "; ".join(bad) if bad else None,
            "plan": plan,
            "capability": CAPABILITY,
            "limits": limits,
            "evaluated_at": now,
            "sources": sources_state,
            "cycle_seconds": getattr(settings, "events_poll_seconds", 60),
            "basket_version": assessment["basket_version"],
        }
        if not legacy["loaded"]:
            health["legacy_stale"] = True
        db.set_meta("events:health", _tiny(health))
    publish.flush_due(db, settings, now)
    return {
        "health": health,
        "market_line": assessment["market_line"],
        "assessment_id": assessment["assessment_id"],
        "direction": assessment["direction"],
    }


def _transitions_marker(db) -> int:
    row = db.conn.execute(
        "SELECT COALESCE(MAX(id),0) AS m FROM events_fact_transition"
    ).fetchone()
    return int(row["m"])


def _set_source(db, source: str, quality: str, detail, at_ms: int,
                payload: dict | None = None) -> None:
    """source_state с сохранением payload.notified_quality (его ведёт publish)."""
    current = store.get_source_states(db).get(source) or {}
    merged = dict(current.get("payload") or {})
    if payload:
        merged.update(payload)
    store.set_source_state(
        db, source=source, quality=quality, detail=detail, at_ms=at_ms, payload=merged,
    )


def _cg_error_quality(exc: Exception) -> str:
    if isinstance(exc, AuthError):
        return "auth_error"
    if isinstance(exc, CapabilityError):
        return "capability_error"
    return "unavailable"


# --- Корзина и пары -------------------------------------------------------


def _binance_pairs(db, settings, fetcher: dict, day_start: int) -> tuple[set, str]:
    """Пары Binance с кэшем на московский день; при ошибке — старый кэш."""
    cached = None
    raw = db.get_meta("events:binance_pairs")
    if raw:
        try:
            cached = json.loads(raw)
        except json.JSONDecodeError:
            cached = None
    if cached and cached.get("day") == day_start:
        return set(cached.get("pairs") or []), "cache"
    fn = fetcher.get("pairs")
    try:
        pairs = set(fn(settings) if fn else sources.fetch_binance_pairs(settings))
    except Exception as exc:
        log.warning("events: каталог Binance недоступен: %s", exc)
        if cached:
            return set(cached.get("pairs") or []), "stale"
        return set(), "error"
    db.set_meta(
        "events:binance_pairs",
        json.dumps({"day": day_start, "pairs": sorted(pairs)}, ensure_ascii=False),
    )
    return pairs, "ok"


def _cmc_listings(db, fetcher: dict):
    fn = fetcher.get("cmc_listings")
    if fn is not None:
        return fn()
    snap = db.get_latest_alt_universe_snapshot()
    if snap is None:
        return None
    try:
        return json.loads(snap["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def _basket_of_day(db, settings, fetcher: dict, day_start: int,
                   pairs: set, pairs_state: str) -> Basket:
    """Состав фиксирован на начало дня МСК: корзина дня переиспользуется (§3)."""
    latest = store.latest_basket(db)
    if latest and latest.get("day_start_ms") == day_start and latest.get("members"):
        return _basket_from_payload(latest)
    try:
        listings = _cmc_listings(db, fetcher)
    except Exception:
        log.warning("events: состав CMC недоступен")
        listings = None
    if listings:
        basket = build_basket(
            listings, pairs, size=int(settings.events_basket_size),
            day_start_ms=day_start,
        )
    else:
        static = [
            s.strip() for s in str(getattr(settings, "events_basket_static", "") or "").split(",")
            if s.strip()
        ]
        if static:
            basket = basket_from_static(static, day_start_ms=day_start)
        else:
            basket = basket_unavailable(
                day_start_ms=day_start,
                reason="нет снапшота CMC и EVENTS_BASKET_STATIC",
            )
    store.put_basket(
        db, version=basket.version, day_start_ms=day_start,
        payload={
            "source": basket.source,
            "members": [asdict(m) for m in basket.members],
            "exclusions": basket.exclusions,
        },
    )
    quality = {
        "cmc_snapshot": "ok",
        "static": "partial",
        "unavailable": "unavailable",
    }[basket.source]
    if quality == "ok" and pairs_state not in ("ok", "cache"):
        quality = "partial"
    _set_source(
        db, "basket", quality,
        None if basket.members else basket.exclusions[0]["reason"],
        day_start, payload={"pairs": pairs_state, "members": len(basket.members)},
    )
    return basket


def _basket_from_payload(payload: dict) -> Basket:
    return Basket(
        version=payload.get("version", ""),
        day_start_ms=int(payload.get("day_start_ms") or 0),
        source=payload.get("source", ""),
        members=[BasketMember(**m) for m in payload.get("members") or []],
        exclusions=payload.get("exclusions") or [],
    )


# --- Цены -----------------------------------------------------------------


def _prices_cycle(db, settings, fetcher: dict, basket: Basket,
                  now: int) -> tuple[dict, dict | None]:
    """Часовые бары участников → бакеты 1h; метрики корзины за 24ч."""
    bars_by_symbol: dict[str, list[dict]] = {}
    if not basket.members:
        _set_source(db, "price", "unavailable", "корзина без участников", now)
        return bars_by_symbol, None
    pairs = [m.pair for m in basket.members]
    fn = fetcher.get("prices")
    try:
        if fn is not None:
            bars_by_pair, failed = fn(settings, pairs, now - _PRICE_DAYS * DAY_MS, now)
        else:
            bars_by_pair, failed = sources.fetch_prices(
                settings, pairs, start_ms=now - _PRICE_DAYS * DAY_MS, end_ms=now,
            )
    except Exception as exc:
        log.warning("events: цены корзины недоступны: %s", exc)
        _set_source(db, "price", "unavailable", str(exc), now)
        return bars_by_symbol, None
    for member in basket.members:
        raw_bars = bars_by_pair.get(member.pair) or []
        bars = sorted(hourly_bars(raw_bars).values(), key=lambda b: b["start"])
        bars_by_symbol[member.symbol] = bars
        # A 20-member basket has ~17,000 historical H1 rows. Individual
        # autocommit round trips made a nominal 60-second cycle take minutes.
        store.upsert_buckets(
            db, scope_id=scope_id("binance", "klines", member.pair, "1h"),
            series="price", interval="1h", available_at=now,
            buckets=({"start": bar["start"],
                      "payload": {"open": bar["open"], "close": bar["close"]},
                      "quality": bar.get("quality", "ok")} for bar in bars),
        )
    db._commit()
    metrics = basket_metrics(
        basket, bars_by_symbol, now_ms=now,
        max_age_ms=int(settings.events_price_max_age_seconds) * 1000,
    )
    if not metrics["coverage"]["ok"]:
        quality = "unavailable"
    elif failed or metrics["coverage"]["ratio"] < 1:
        quality = "partial"
    else:
        quality = "ok"
    _set_source(
        db, "price", quality,
        f"нет данных: {', '.join(failed)}" if failed else
        "неполные, непригодные или устаревшие ценовые окна" if quality != "ok" else None, now,
        payload={"failed": list(failed)},
    )
    return bars_by_symbol, metrics


def _store_day_metrics(db, basket: Basket, bars_by_symbol: dict, now: int) -> None:
    """Медианная доходность корзины за последние 10 завершённых дней МСК."""
    if not basket.members:
        return
    existing = {m["bucket_start"]: m for m in store.metric_series(db, "basket_day_return")}
    for day_start, day_end in completed_msk_days(now, 10):
        # A new day's constituents must not rewrite a previously completed day.
        previous = existing.get(day_start) or {}
        if previous.get("complete") and previous.get("method_version") == MODEL_VERSION:
            continue
        historical = store.basket_at(db, day_start)
        if (historical and historical["day_start_ms"] != day_start
                and historical.get("source") != "static"):
            historical = None
        day_basket = _basket_from_payload(historical) if historical else basket
        ret = msk_day_return(
            day_basket, bars_by_symbol, day_start_ms=day_start, day_end_ms=day_end,
        )
        known = bool(historical) or basket.source == "static"
        if previous.get("complete") and (not ret["complete"] or not known):
            continue  # preserve legacy evidence, exclude it from the new statistics
        store.upsert_metric(
            db, series="basket_day_return", bucket_start=day_start,
            payload={**ret, "basket_version": day_basket.version,
                     "historical_composition_known": known, "method_version": MODEL_VERSION},
        )


# --- Coinglass ------------------------------------------------------------


def _coinglass_arg(settings, fetcher: dict):
    """Первый аргумент fetcher-функций: клиент, если он есть, иначе settings."""
    if any(k in fetcher for k in ("liq", "oi", "funding", "client")):
        return fetcher.get("client") or settings
    if not (getattr(settings, "coinglass_api_key", "") or ""):
        return None
    try:
        return sources.make_coinglass(settings)
    except AuthError:
        return None


def _liq_fn(fetcher: dict):
    fn = fetcher.get("liq")
    if fn is not None:
        return fn
    return lambda cg, symbol, interval, limit: sources.fetch_liq(
        cg, symbol, interval=interval, limit=limit,
    )


def _oi_fn(fetcher: dict):
    fn = fetcher.get("oi")
    if fn is not None:
        return fn
    return lambda cg, symbol, unit, interval, limit: cg.open_interest(
        symbol, unit, interval, limit,
    )


def _funding_fn(fetcher: dict):
    fn = fetcher.get("funding")
    if fn is not None:
        return fn
    return lambda cg, exchange, symbol, interval, limit: cg.funding(
        exchange, symbol, interval, limit,
    )


def _liq_rotation(db, members: list, budget: int) -> list[str]:
    """BTC всегда + участники с самым старым available_at бакетов 4h."""
    candidates = []
    for member in members:
        if member.symbol == "BTC":
            continue
        rows = load_buckets(
            db, scope_id("coinglass", "liq-4h", member.symbol), "liq", interval="4h",
        )
        oldest = max((r["available_at"] for r in rows), default=0)
        candidates.append((oldest, member.symbol))
    candidates.sort()
    picked = [symbol for _, symbol in candidates[:budget]]
    return ["BTC"] + [s for s in picked if s != "BTC"]


def _liq_cycle(db, settings, cg, fetcher: dict, basket: Basket, now: int,
               legacy: dict) -> tuple[dict | None, list[str]]:
    """Ликвидации 4h по ротации символов + legacy BTC 1d; метрики эпизода."""
    fn = _liq_fn(fetcher)
    symbols = _liq_rotation(db, basket.members, int(settings.events_liq_symbol_budget))
    fetched, errors = [], []
    for symbol in symbols:
        try:
            rows = fn(cg, symbol, "4h", 180)
        except _CG_ERRORS as exc:
            errors.append(exc)
            continue
        if _ingest_buckets(db, scope=scope_id("coinglass", "liq-4h", symbol), series="liq",
                           interval="4h", rows=rows,
                           parse=lambda row, stamp: _liquidation_bucket(row, stamp, interval_ms=4 * HOUR_MS),
                           now=now, errors=errors):
            fetched.append(symbol)
        else:
            errors.append(CoinglassError("источник не вернул пригодных окон ликвидаций"))
    try:
        count = _ingest_buckets(db, scope=LIQ_SCOPE, series="liq", interval="1d",
            rows=fn(cg, "BTC", "1d", 150), parse=_liquidation_bucket, now=now, errors=errors)
        legacy["loaded"] = bool(count)
    except _CG_ERRORS as exc:
        errors.append(exc)
        log.warning("events: legacy liq 1d недоступен: %s", exc)
    db._commit()
    if errors and not fetched:
        _set_source(db, "liquidations", _cg_error_quality(errors[0]),
                    str(errors[0]), now)
        return None, symbols
    thresholds = get_rule("LIQ_PRESSURE").thresholds
    # Rotation limits network requests, not the scope of the market aggregate.
    all_symbols = [m.symbol for m in basket.members]
    liq = _market_liq(
        db, all_symbols, now, int(settings.events_liq_baseline_days), thresholds,
        memory_hours=int(settings.events_memory_hours),
        min_coverage=float(settings.events_basket_min_coverage),
    )
    coverage = liq["coverage"]
    partial = bool(errors) or coverage["ok"] < coverage["total"] or not liq["baseline_ok"]
    detail = "; ".join(str(e) for e in errors) if errors else None
    if partial and detail is None:
        detail = f"Ликвидации: {coverage['ok']} из {coverage['total']} монет; baseline: {'достаточен' if liq['baseline_ok'] else 'недостаточен'}"
    quality = ("unavailable" if errors else "stale") if coverage["ok"] == 0 else "partial" if partial else "ok"
    _set_source(db, "liquidations", quality, detail, now,
                payload={"symbols": all_symbols, "fetched_symbols": fetched,
                         "coverage": coverage, "baseline_ok": liq["baseline_ok"]})
    return liq, symbols


def _put_liq_window(db, symbol: str, row: dict, now: int) -> None:
    start = _source_time(row, 4 * HOUR_MS, now)
    long_usd = as_decimal(row["aggregated_long_liquidation_usd"])
    short_usd = as_decimal(row["aggregated_short_liquidation_usd"])
    if not finite_number(long_usd) or not finite_number(short_usd):
        raise ValueError("invalid liquidation volume")
    quality = "ok"
    upsert_bucket(
        db, scope_id=scope_id("coinglass", "liq-4h", symbol), series="liq",
        interval="4h", bucket_start=start,
        payload={"long_usd": long_usd, "short_usd": short_usd},
        quality=quality, available_at=now,
    )


def _market_liq(db, symbols: list[str], now: int, baseline_days: int,
                thresholds: dict, *, memory_hours: int = 240,
                min_coverage: float = 0.8) -> dict:
    """Aggregate the entire basket and retain the aftermath of closed cascades."""
    window_ms = int(thresholds["window_ms"])
    per_symbol = {}
    for symbol in symbols:
        rows = load_buckets(db, scope_id("coinglass", "liq-4h", symbol), "liq", interval="4h",
                            since_ms=now - memory_hours * HOUR_MS - (baseline_days + 1) * DAY_MS)
        per_symbol[symbol] = {r["t"]: r for r in rows if r.get("quality") == "ok"}
    history = liquidation_history(
        per_symbol, now=now, baseline_days=baseline_days, thresholds=thresholds,
        memory_ms=memory_hours * HOUR_MS, min_coverage=min_coverage,
    )
    current_start = now // window_ms * window_ms
    last_start = current_start - window_ms
    latest = next((w for w in reversed(history) if w["window"]["start"] == last_start), None)
    if latest is None:
        latest = {
            "available": False, "baseline_ok": False, "baseline_samples": 0,
            "window": None, "sum_long_usd": 0, "sum_short_usd": 0,
            "long_share": None, "multiple": None,
            "coverage": {"ok": 0, "total": len(symbols), "ratio": 0.0},
            "episode": {"active": False, "phase": None, "started_at": None},
        }
    forming = [rows[current_start] for rows in per_symbol.values() if current_start in rows]
    return {**latest, "history": history, "provisional": (
        {"sum_long_usd": sum(float(r["long_usd"]) for r in forming),
         "window_start": current_start} if forming else None
    )}


def _oi_cycle(db, settings, cg, fetcher: dict, symbols: list[str], now: int,
              legacy: dict) -> None:
    """OI: legacy BTC 1d (coin/usd) + рыночный OI в USD по символам цикла."""
    fn = _oi_fn(fetcher)
    errors = []
    try:
        count = _ingest_buckets(db, scope=OI_COIN_SCOPE, series="oi_coin", interval="1d",
            rows=fn(cg, "BTC", "coin", "1d", 150), parse=_oi_bucket, now=now, errors=errors)
        count += _ingest_buckets(db, scope=OI_USD_SCOPE, series="oi_usd", interval="1d",
            rows=fn(cg, "BTC", "usd", "1d", 150), parse=_oi_bucket, now=now, errors=errors)
        legacy["loaded"] = legacy["loaded"] or bool(count)
    except _CG_ERRORS as exc:
        errors.append(exc)
        log.warning("events: legacy OI недоступен: %s", exc)
    for symbol in symbols:
        try:
            rows = fn(cg, symbol, "usd", "1d", 40)
        except _CG_ERRORS as exc:
            errors.append(exc)
            continue
        _ingest_buckets(db, scope=scope_id("coinglass", "oi-usd-1d", symbol), series="oi_usd",
            interval="1d", rows=rows, parse=_oi_bucket, now=now, errors=errors)
    db._commit()
    all_symbols = [m["symbol"] for m in (store.latest_basket(db) or {}).get("members", [])]
    expected = now // DAY_MS * DAY_MS - DAY_MS
    matched = []
    for symbol in all_symbols:
        rows = load_buckets(db, scope_id("coinglass", "oi-usd-1d", symbol), "oi_usd",
                            since_ms=expected)
        matched.extend(r for r in rows if r["t"] == expected and r["quality"] == "ok")
    market_total = sum(float(r["close"]) for r in matched)
    covered = len(matched)
    quality = "stale" if not covered else "partial" if errors or covered < len(all_symbols) else "ok"
    if errors and not covered:
        quality = _cg_error_quality(errors[0])
    _set_source(
        db, "oi", quality,
        "; ".join(str(e) for e in errors) if errors else None, now,
        payload={
            "market_oi_usd": market_total if covered else None,
            "coverage": {"ok": covered, "total": len(all_symbols)},
            "window": {"start": expected, "end": expected + DAY_MS, "closed": True},
        },
    )


def _funding_cycle(db, cg, fetcher: dict, now: int, legacy: dict) -> None:
    fn = _funding_fn(fetcher)
    errors = []
    try:
        count = _ingest_buckets(db, scope=FUND_SCOPE, series="funding", interval="1d",
            rows=fn(cg, "Binance", "BTCUSDT", "1d", 150), parse=_funding_bucket, now=now, errors=errors)
        legacy["loaded"] = legacy["loaded"] or bool(count)
    except _CG_ERRORS as exc:
        _set_source(db, "funding", _cg_error_quality(exc), str(exc), now)
        return
    db._commit()
    _set_source(db, "funding", "partial" if errors or not count else "ok",
                "неполные или непригодные данные funding" if errors or not count else None, now)


# --- Факты памяти из состояния ликвидаций ---------------------------------


def _facts_cycle(db, settings, liq: dict, metrics: dict | None,
                 bars_by_symbol: dict, basket: Basket, now: int) -> set[str]:
    """Replay closed windows with prices known at their close, including after downtime.

    Detection stays at now; neither occurrence nor the original expiry is moved.
    History is reconstructed from today's basket, explicitly labelled in the output.
    """
    history = liq.get("history")
    if history is None:  # compatibility for callers supplying a single observation
        key = _facts_at_window(db, settings, liq, metrics, bars_by_symbol, basket, now)
        return {key} if key else set()
    threshold = float(settings.events_basket_min_coverage)
    observed = set()
    for observation in history:
        if not observation["available"]:
            continue
        end = observation["window"]["end"]
        cohort = observation["episode"].get("symbols") or list(bars_by_symbol)
        observed_bars = {s: bars_by_symbol[s] for s in cohort if s in bars_by_symbol}
        prices = _closed_window_prices(basket, observed_bars, end,
                                       int(get_rule("LIQ_PRESSURE").thresholds["window_ms"]), threshold)
        key = _facts_at_window(db, settings, observation, prices, observed_bars, basket, now,
                         record_volume=observation["window"] == liq.get("window"))
        if key:
            observed.add(key)
    return observed


def _facts_at_window(db, settings, liq: dict, metrics: dict | None,
                     bars_by_symbol: dict, basket: Basket, now: int, *,
                     record_volume: bool = True) -> str | None:
    window = liq.get("window")
    if window is None:
        return
    th = get_rule("LIQ_PRESSURE").thresholds
    episode = liq["episode"]
    memory_hours = int(getattr(settings, "events_memory_hours", 240))
    if record_volume:
        _volume_fact(db, liq, now, memory_hours)
    price_known = metrics is not None and metrics.get("median_return") is not None
    falling = (price_known and metrics["median_return"] < 0
               and metrics.get("breadth_up") is not None and metrics["breadth_up"] < 0.5)
    started = episode.get("started_at")
    existing = None
    if started is not None:
        cohort = episode.get("symbols") or [m.symbol for m in basket.members]
        cohort_key = scope_id(*sorted(cohort))[:10]
        episode_id = f"liq-{started}-{cohort_key}"
        occurred = started + int(th["window_ms"])
        key = memory.make_fact_key(rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                                  scope="crypto_market", episode_id=episode_id, occurrence=str(occurred))
        existing = store.get_fact(db, key)
        # Older keys can be adopted only after their episode is verified again.
        if existing is None:
            legacy_id = f"liq-{started}"
            legacy_key = memory.make_fact_key(rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                scope="crypto_market", episode_id=legacy_id, occurrence=str(occurred))
            legacy_fact = store.get_fact(db, legacy_key)
            if legacy_fact is not None and not (legacy_fact["payload"].get("basis_symbols")):
                existing, key = legacy_fact, legacy_key
        if episode.get("active") and episode.get("phase") == "intensifying" and falling:
            # Replaying earlier windows must not overwrite a later reinterpretation.
            if existing is None:
                memory.begin_fact(
                    db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                    scope="crypto_market", episode_id=episode_id, direction="short",
                    occurred_at=occurred, detected_at=now, available_at=now,
                    time_precision="window", payload={**_pressure_payload(liq, metrics),
                        "phase_at": window["end"], "basket_version": basket.version,
                        "price_basis": basis_for_window(metrics), "basis_symbols": sorted(cohort),
                        "context_model": MODEL_VERSION},
                    state="confirmed" if window["closed"] else "provisional",
                    memory_hours=memory_hours,
                )
            elif (existing["direction"] == "short" and existing["state"] in ("confirmed", "provisional")
                  and existing["valid_until"] > now and not existing["payload"].get("context_invalidated")):
                old = existing["payload"]
                basis = basis_for_window(metrics)
                if basis:
                    merged = merge_basis(old.get("price_basis") or basis, basis)
                    payload = {**old, "price_basis": merged, "basis_symbols": sorted(cohort),
                               "context_model": MODEL_VERSION}
                    if payload != old:
                        store.upsert_fact(db, {**existing, "payload": payload, "updated_at": now})
            return key
        if (episode.get("phase") == "fading" and existing is not None
                and existing["direction"] in ("short", None)
                and not existing["payload"].get("context_invalidated")
                and window["end"] > existing["payload"].get("interpretation_evaluated_through", 0)
                and existing["state"] in ("provisional", "confirmed")
                and existing["valid_until"] > now
                and _recovery_ok(th, metrics, bars_by_symbol, basket, window["end"],
                                 min_coverage=float(settings.events_basket_min_coverage))
                and _holds_basis(existing, bars_by_symbol, window["end"], settings)):
            memory.reinterpret_fact(
                db, key, new_direction="long", at_ms=now,
                reason="каскад завершён: три тихих окна и восстановление цены и ширины",
                payload={"recovery_rule": "LIQ_RECOVERY", "phase_at": window["end"],
                         "pressure_released": False, "context_model": MODEL_VERSION,
                         "interpretation_evaluated_through": window["end"],
                         "summary": "После каскада ликвидации затихли; цена и ширина рынка восстанавливаются — контекст LONG (пороги проектные)"},
            )
            return key


def _volume_fact(db, liq: dict, now: int, memory_hours: int) -> None:
    window = liq["window"]
    th = get_rule("LIQ_PRESSURE").thresholds
    memory.begin_fact(
        db, rule_id="LIQ_FACT", rule_version=ENGINE_RULE_VERSION, scope="crypto_market",
        episode_id=f"liqfact-{window['start']}", direction=None,
        occurred_at=window["end"], detected_at=now, available_at=now, time_precision="window",
        payload={"summary": ("Зафиксирован объём ликвидаций: "
                 f"лонг {liq['sum_long_usd']:.0f} USD, шорт {liq['sum_short_usd']:.0f} USD за окно 4ч"),
                 "sum_long_usd": liq["sum_long_usd"], "sum_short_usd": liq["sum_short_usd"],
                 "long_share": liq.get("long_share"), "multiple": liq.get("multiple"),
                 "coverage": liq.get("coverage"), "window_ms": int(th["window_ms"])},
        state="confirmed" if window["closed"] else "provisional", memory_hours=memory_hours,
    )


def _holds_basis(fact: dict, bars_by_symbol: dict, end: int, settings) -> bool:
    check = price_check(fact["payload"].get("price_basis") or {}, bars_by_symbol,
                        now=end, max_age_ms=HOUR_MS - 1,
                        min_coverage=float(settings.events_basket_min_coverage))
    return check["status"] == "verified" and not check["below_floor"]


def _revalidate_facts(db, settings, bars_by_symbol: dict, basket: Basket, now: int, *,
                      observed_keys: set[str] | None = None) -> None:
    """Keep history, permanently supersede older phases, revoke a broken recovery."""
    facts = [f for f in store.query_facts(db) if f["scope"] == "crypto_market"
             and f["rule_id"] in ("LIQ_PRESSURE", "LIQ_RECOVERY")
             and f["occurred_at"] <= now and f["available_at"] <= now]
    if not facts:
        return
    eligible = [f for f in facts if not f["payload"].get("superseded_by")
                and f["state"] != "provisional" and phase_order(f)[0] <= now]
    if not eligible:
        return
    latest = max(eligible, key=phase_order)
    for fact in facts:
        if phase_order(fact) < phase_order(latest) and not fact["payload"].get("superseded_by"):
            store.upsert_fact(db, {**fact, "updated_at": now, "payload": {
                **fact["payload"], "superseded_by": latest["fact_key"],
            }})
    if latest["state"] not in ("provisional", "confirmed") or latest["valid_until"] <= now:
        return
    payload = latest["payload"]
    basis = payload.get("price_basis") or {}
    current_symbols = {m.symbol for m in basket.members}
    overlap = len(current_symbols.intersection(basis)) / len(basis) if basis else 0
    check = price_check(basis, bars_by_symbol, now=now,
                        max_age_ms=int(settings.events_price_max_age_seconds) * 1000,
                        min_coverage=float(settings.events_basket_min_coverage))
    if overlap < float(settings.events_basket_min_coverage):
        check = {"status": "unverified", "reason": "composition_or_basis_missing"}
    if observed_keys is not None and latest["fact_key"] not in observed_keys:
        check = {"status": "unverified", "reason": "event_not_reproduced_in_history"}
    if latest["direction"] == "long" and basis:
        path = broken_since(basis, bars_by_symbol, start=payload.get("phase_at") or latest["occurred_at"],
                            end=now, min_coverage=float(settings.events_basket_min_coverage))
        if path["broken_at"] is not None:
            memory.reinterpret_fact(db, latest["fact_key"], new_direction=None, at_ms=now,
                reason="закрытия H1 опустились ниже основания восстановления",
                payload={"context_invalidated": True, "invalidated_at": path["broken_at"],
                         "invalidation_reason": "Закрытия H1 ниже основания восстановления",
                         "current_check": check,
                         "summary": "Каскад сохранён в истории; трактовка восстановления опровергнута закрытиями H1"})
            return
        if not path["path_complete"]:
            check = {"status": "unverified", "reason": "price_path_incomplete"}
    if (latest["direction"] == "short" and check.get("above_origin")
            and check["status"] == "verified"):
        memory.reinterpret_fact(db, latest["fact_key"], new_direction=None, at_ms=now,
            reason="цены вернулись выше начала каскада; прежнее давление не подтверждается",
            payload={"pressure_released": True, "current_check": check,
                     "interpretation_evaluated_through": check["as_of"],
                     "summary": "Каскад сохранён; цены вернулись к началу снижения, прежнее давление снято"})
        return
    if check != payload.get("current_check"):
        store.upsert_fact(db, {**latest, "updated_at": now, "payload": {**payload, "current_check": check}})


def _pressure_payload(liq: dict, metrics: dict) -> dict:
    return {
        "summary": (
            "Усиление лонг-ликвидаций при падающих цене и ширине рынка — "
            "давление на рынок, SHORT (пороги проектные)"
        ),
        "sum_long_usd": liq["sum_long_usd"],
        "sum_short_usd": liq["sum_short_usd"],
        "long_share": liq.get("long_share"),
        "multiple": liq.get("multiple"),
        "coverage": liq.get("coverage"),
        "median_return": metrics.get("median_return"),
        "breadth_up": metrics.get("breadth_up"),
        "window_ms": int(get_rule("LIQ_PRESSURE").thresholds["window_ms"]),
    }


def _closed_window_prices(basket: Basket, bars_by_symbol: dict, end: int,
                          window_ms: int, min_coverage: float) -> dict | None:
    """Require all hourly bars within each included member's closed window."""
    start = end - window_ms
    returns = []
    members = {}
    for member in basket.members:
        segment = complete_window(bars_by_symbol.get(member.symbol, []), start, end)
        if not segment:
            continue
        ret = float(segment[-1]["close"]) / float(segment[0]["open"]) - 1.0
        returns.append(ret)
        members[member.symbol] = {"open": float(segment[0]["open"]),
            "close": float(segment[-1]["close"]), "min_close": min(float(b["close"]) for b in segment)}
    ratio = len(returns) / len(basket.members) if basket.members else 0.0
    if not returns or ratio < min_coverage:
        return None
    return {"median_return": float(median([Decimal(str(r)) for r in returns])),
            "breadth_up": sum(r > 0 for r in returns) / len(returns),
            "coverage": {"ok": len(returns), "total": len(basket.members), "ratio": ratio},
            "members": members}


def _recovery_ok(th: dict, metrics: dict | None, bars_by_symbol: dict,
                 basket: Basket, now: int, *, min_coverage: float = 0.8) -> bool:
    """Every required closed window must confirm both price and breadth recovery."""
    window_ms = int(th["window_ms"])
    end = now // window_ms * window_ms
    for i in range(int(th["recovery_windows"])):
        prices = _closed_window_prices(basket, bars_by_symbol, end - i * window_ms,
                                       window_ms, min_coverage)
        if (prices is None or prices["median_return"] <= 0 or prices["breadth_up"] < 0.5):
            return False
    return True


# --- Время и статистика ----------------------------------------------------


def _time_ctx(basket: Basket, bars_by_symbol: dict, now: int, *, min_coverage: float = 0.8) -> dict:
    weekday = msk_weekday(now)
    day_start = msk_day_start_ms(now)
    completed = []
    if basket.members:
        for start, end in completed_msk_days(now, 7):
            ret = msk_day_return(
                basket, bars_by_symbol, day_start_ms=start, day_end_ms=end,
            )
            med = ret["median_return"]
            if ret["members_ok"] / ret["members_total"] < min_coverage:
                med = None
            completed.append({
                "day_start_ms": start,
                "weekday": msk_weekday(start),
                "median_return": med,
                "complete": ret["complete"],
                "sign": None if med is None else day_sign(med),
            })
    week_seq = None
    monday = day_start - weekday * DAY_MS
    if basket.members and weekday >= 2:
        # оба дня текущей недели завершены — берём их знаки
        signs = {}
        for start, end in completed_msk_days(now, 7):
            if start in (monday, monday + DAY_MS):
                ret = msk_day_return(
                    basket, bars_by_symbol, day_start_ms=start, day_end_ms=end,
                )
                med = ret["median_return"]
                if ret["members_ok"] / ret["members_total"] < min_coverage:
                    med = None
                signs[start] = None if med is None else day_sign(med)
        mon_sign = signs.get(monday)
        tue_sign = signs.get(monday + DAY_MS)
        if mon_sign is not None and tue_sign is not None:
            week_seq = {
                "mon_sign": mon_sign,
                "tue_sign": tue_sign,
                "condition_met": mon_sign == 1 and tue_sign == 1,
            }
    return {
        "weekday": weekday,
        "weekday_name": WEEKDAY_RU[weekday],
        "hour_msk": msk_hour(now),
        "day_start_ms": day_start,
        "next_day_start_ms": msk_day_end_ms(now),
        "next_h1_close_ms": (now // HOUR_MS + 1) * HOUR_MS,
        "next_liquidation_close_ms": (now // (4 * HOUR_MS) + 1) * (4 * HOUR_MS),
        "completed_days": completed,
        "week_seq": week_seq,
    }


def _stats(db, settings) -> dict:
    """Частота роста медианы корзины за московский день (§7, AC13/AC14)."""
    min_n = int(settings.events_stats_min_n)
    samples = [
        s for s in store.metric_series(db, "basket_day_return")
        if s.get("complete") and s.get("median_return") is not None
        and s.get("historical_composition_known", False) and s.get("method_version") == MODEL_VERSION
    ]
    n = len(samples)
    if n < min_n:
        return insufficient("n<min", n=n)
    positive = sum(1 for s in samples if float(s["median_return"]) > 0)
    result = frequency(
        positive, n, min_n=min_n,
        label="рост медианы корзины за московский день",
    )
    result.update({
        "method": "median_basket_msk_day",
        "version": ENGINE_RULE_VERSION,
        "period": {"from": samples[0]["bucket_start"], "to": samples[-1]["bucket_start"]},
        "outcome": "median_return>0",
        "horizon": "msk_day",
    })
    return result


# --- Оценка ---------------------------------------------------------------


def _assessment_id(assessment: dict, now: int) -> str:
    """Детерминированный ключ снимка: повтор того же содержимого не дублируется."""
    body = json.dumps(
        {k: v for k, v in assessment.items() if k != "assessment_id"},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return "asmt_" + hashlib.sha256(f"{now}|{body}".encode("utf-8")).hexdigest()[:20]


# --- Legacy BTC-путь (спека §11 п.1/п.8: прежняя методика сохранена) ------


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
    start = _source_time(row, DAY_MS, now)
    long_usd = as_decimal(row["aggregated_long_liquidation_usd"])
    short_usd = as_decimal(row["aggregated_short_liquidation_usd"])
    if not finite_number(long_usd) or not finite_number(short_usd):
        raise ValueError("invalid liquidation volume")
    quality = "ok"
    upsert_bucket(
        db, scope_id=LIQ_SCOPE, series="liq", interval="1d", bucket_start=start,
        payload={"long_usd": long_usd, "short_usd": short_usd},
        quality=quality, available_at=now,
    )


def _put_oi(db, scope: str, series: str, row: dict, now: int) -> None:
    start = _source_time(row, DAY_MS, now)
    open_, high, low, close = (as_decimal(row[k]) for k in ("open", "high", "low", "close"))
    if (not all(finite_number(v) for v in (open_, high, low, close))
            or min(open_, close) < low or max(open_, close) > high):
        raise ValueError("invalid OI OHLC")
    quality = "ok"
    upsert_bucket(
        db, scope_id=scope, series=series, interval="1d", bucket_start=start,
        payload={"open": open_, "high": high, "low": low, "close": close},
        quality=quality, available_at=now,
    )


def _put_funding(db, row: dict, now: int) -> None:
    start = _source_time(row, DAY_MS, now)
    close = as_decimal(row["close"])
    if not close.is_finite():
        raise ValueError("invalid funding")
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
    expected = now // DAY_MS * DAY_MS - DAY_MS
    data_stale = any(not any(b and b.closed and b.t == expected for b in series)
                     for series in (liq, oi_coin, funding))
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
        data_stale=data_stale,
        asset_profile="BTC",
        scope_asset="BTC",
    ))


def _load(db, scope: str, series: str) -> list[dict]:
    return load_buckets(db, scope, series)


def _closed(start: int, now: int) -> bool:
    return start + DAY_MS <= now


def _liq_bar(row: dict, now: int):
    if row.get("quality") != "ok" or not all(finite_number(row.get(k)) for k in ("long_usd", "short_usd")):
        return None
    return LiqBar(
        t=row["t"],
        long_usd=Decimal(str(row["long_usd"])),
        short_usd=Decimal(str(row["short_usd"])),
        closed=_closed(row["t"], now),
    )


def _oi_bar(row: dict, now: int):
    if row.get("quality") != "ok" or not all(finite_number(row.get(k)) for k in ("open", "close")):
        return None
    return OiBar(
        t=row["t"],
        open=Decimal(str(row["open"])),
        close=Decimal(str(row["close"])),
        closed=_closed(row["t"], now),
    )


def _fund_bar(row: dict, now: int):
    if row.get("quality") != "ok":
        return None
    try:
        if not as_decimal(row.get("close_percent")).is_finite():
            return None
    except InvalidOperation:
        return None
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
    return json.dumps(payload, ensure_ascii=False)
