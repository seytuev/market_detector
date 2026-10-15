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
from dataclasses import asdict
from decimal import Decimal

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
from .market import MarketInput, evaluate_market
from .mathutil import DAY_MS, median
from .rules import ENGINE_RULE_VERSION, get_rule, registry_for_eval
from .stats import frequency, insufficient
from .store import apply_snapshot, load_buckets, upsert_bucket
from .timeutil import (
    WEEKDAY_RU,
    completed_msk_days,
    msk_day_end_ms,
    msk_day_start_ms,
    msk_hour,
    msk_weekday,
)

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
_LIQ_FRESH_MS = 8 * HOUR_MS  # свежесть оконных данных ликвидаций (§4)

_CG_ERRORS = (AuthError, CapabilityError, CoinglassError)


def run_cycle(db, settings, *, fetcher=None, now_ms: int | None = None) -> dict:
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
    transitions_marker = _transitions_marker(db)
    memory.expire_due(db, now)

    # --- Корзина дня МСК и пары Binance ---
    day_start = msk_day_start_ms(now)
    pairs, pairs_state = _binance_pairs(db, settings, fetcher, day_start)
    basket = _basket_of_day(db, settings, fetcher, day_start, pairs, pairs_state)

    # --- Цены участников корзины (1h, 35 дней) ---
    bars_by_symbol, metrics = _prices_cycle(db, settings, fetcher, basket, now)
    _store_day_metrics(db, basket, bars_by_symbol, now)

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
    if liq is not None and liq.get("available"):
        _facts_cycle(db, settings, liq, metrics, bars_by_symbol, basket, now)

    # --- Временной контекст МСК и статистика корзины ---
    time_ctx = _time_ctx(basket, bars_by_symbol, now)
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
        }

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
    publish.flush_due(db, settings, now)

    sources_state = store.get_source_states(db)
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
    if latest and latest.get("day_start_ms") == day_start:
        return _basket_from_payload(latest)
    listings = _cmc_listings(db, fetcher)
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
        bars = bars_by_pair.get(member.pair) or []
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
    if failed and len(failed) == len(pairs):
        quality = "unavailable"
    elif failed:
        quality = "partial"
    else:
        quality = "ok"
    _set_source(
        db, "price", quality,
        f"нет данных: {', '.join(failed)}" if failed else None, now,
        payload={"failed": list(failed)},
    )
    metrics = basket_metrics(
        basket, bars_by_symbol, now_ms=now,
        max_age_ms=int(settings.events_price_max_age_seconds) * 1000,
    )
    return bars_by_symbol, metrics


def _store_day_metrics(db, basket: Basket, bars_by_symbol: dict, now: int) -> None:
    """Медианная доходность корзины за последние 10 завершённых дней МСК."""
    if not basket.members:
        return
    for day_start, day_end in completed_msk_days(now, 10):
        ret = msk_day_return(
            basket, bars_by_symbol, day_start_ms=day_start, day_end_ms=day_end,
        )
        store.upsert_metric(
            db, series="basket_day_return", bucket_start=day_start,
            payload={**ret, "basket_version": basket.version},
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
        for row in rows:
            _put_liq_window(db, symbol, row, now)
        fetched.append(symbol)
    try:
        for row in fn(cg, "BTC", "1d", 150):
            _put_liq(db, row, now)
        legacy["loaded"] = True
    except _CG_ERRORS as exc:
        errors.append(exc)
        log.warning("events: legacy liq 1d недоступен: %s", exc)
    db._commit()
    if errors and not fetched:
        _set_source(db, "liquidations", _cg_error_quality(errors[0]),
                    str(errors[0]), now)
        return None, symbols
    _set_source(
        db, "liquidations",
        "partial" if errors else "ok",
        "; ".join(str(e) for e in errors) if errors else None,
        now, payload={"symbols": fetched},
    )
    thresholds = get_rule("LIQ_PRESSURE").thresholds
    liq = _market_liq(db, fetched, now, int(settings.events_liq_baseline_days),
                      thresholds)
    return liq, symbols


def _put_liq_window(db, symbol: str, row: dict, now: int) -> None:
    start = int(row["time"])
    long_usd = as_decimal(row["aggregated_long_liquidation_usd"])
    short_usd = as_decimal(row["aggregated_short_liquidation_usd"])
    quality = "integrity_error" if long_usd < 0 or short_usd < 0 else "ok"
    upsert_bucket(
        db, scope_id=scope_id("coinglass", "liq-4h", symbol), series="liq",
        interval="4h", bucket_start=start,
        payload={"long_usd": long_usd, "short_usd": short_usd},
        quality=quality, available_at=now,
    )


def _market_liq(db, symbols: list[str], now: int, baseline_days: int,
                thresholds: dict) -> dict:
    """Рыночные метрики ликвидаций из сохранённых бакетов 4h (§4).

    Отсутствующий участник окна не считается нулём: окно помечается
    неполным через coverage, сумма — только по реальным данным.
    baseline участника — медиана long_usd окон того же часа UTC за
    baseline_days; без совместимого baseline относительный вывод unknown.
    """
    window_ms = int(thresholds["window_ms"])
    per_symbol = {}
    for symbol in symbols:
        rows = load_buckets(
            db, scope_id("coinglass", "liq-4h", symbol), "liq", interval="4h",
        )
        per_symbol[symbol] = {
            r["t"]: r for r in rows if r.get("quality") == "ok"
        }

    def window_sum(start: int):
        ok = [s for s in symbols if start in per_symbol[s]]
        long_sum = sum(float(per_symbol[s][start]["long_usd"]) for s in ok)
        short_sum = sum(float(per_symbol[s][start]["short_usd"]) for s in ok)
        return ok, long_sum, short_sum

    def baseline(symbol: str, start: int) -> float | None:
        values = []
        for k in range(1, baseline_days + 1):
            row = per_symbol[symbol].get(start - k * DAY_MS)
            if row is not None:
                values.append(float(row["long_usd"]))
        med = median([Decimal(str(v)) for v in values])
        return float(med) if med is not None else None

    current_start = now - (now % window_ms)
    last_start = current_start - window_ms
    ok_syms, sum_long, sum_short = window_sum(last_start)
    fresh = bool(ok_syms) and (last_start + window_ms) >= now - _LIQ_FRESH_MS

    long_share = (
        sum_long / (sum_long + sum_short) if sum_long + sum_short > 0 else None
    )
    baselines = {s: baseline(s, last_start) for s in ok_syms}
    baseline_total = sum(b for b in baselines.values() if b is not None)
    baseline_ok = (
        bool(ok_syms)
        and all(b is not None for b in baselines.values())
        and baseline_total > 0
    )
    multiple = sum_long / baseline_total if baseline_ok else None

    # Эпизод: непрерывные завершённые окна с кратностью и долей выше порогов
    episode_windows = []
    cursor = last_start
    while True:
        ok, long_k, short_k = window_sum(cursor)
        if not ok:
            break
        base = {s: baseline(s, cursor) for s in ok}
        base_total = sum(b for b in base.values() if b is not None)
        if not base_total or any(b is None for b in base.values()):
            break
        share = long_k / (long_k + short_k) if long_k + short_k > 0 else None
        if (
            long_k / base_total >= float(thresholds["episode_multiple"])
            and share is not None
            and share >= float(thresholds["long_share"])
        ):
            episode_windows.append((cursor, long_k))
            cursor -= window_ms
        else:
            break
    active = bool(episode_windows) and episode_windows[0][0] == last_start
    started_at = episode_windows[-1][0] if episode_windows else None
    phase = None
    if active:
        peak = max(long_k for _, long_k in episode_windows)
        phase = (
            "fading" if episode_windows[0][1] < float(thresholds["fade_ratio"]) * peak
            else "intensifying"
        )

    ok_prov, prov_long, _prov_short = window_sum(current_start)
    return {
        "available": fresh,
        "baseline_ok": baseline_ok,
        "window": (
            {"start": last_start, "end": last_start + window_ms, "closed": True}
            if ok_syms else None
        ),
        "sum_long_usd": sum_long,
        "sum_short_usd": sum_short,
        "long_share": long_share,
        "multiple": multiple,
        "coverage": {"ok": len(ok_syms), "total": len(symbols)},
        "episode": {"active": active, "phase": phase, "started_at": started_at},
        # формирующееся окно: только сумма, относительного вывода нет (§4/AC10)
        "provisional": (
            {"sum_long_usd": prov_long, "window_start": current_start}
            if ok_prov else None
        ),
    }


def _oi_cycle(db, settings, cg, fetcher: dict, symbols: list[str], now: int,
              legacy: dict) -> None:
    """OI: legacy BTC 1d (coin/usd) + рыночный OI в USD по символам цикла."""
    fn = _oi_fn(fetcher)
    errors = []
    try:
        for row in fn(cg, "BTC", "coin", "1d", 150):
            _put_oi(db, OI_COIN_SCOPE, "oi_coin", row, now)
        for row in fn(cg, "BTC", "usd", "1d", 150):
            _put_oi(db, OI_USD_SCOPE, "oi_usd", row, now)
        legacy["loaded"] = True
    except _CG_ERRORS as exc:
        errors.append(exc)
        log.warning("events: legacy OI недоступен: %s", exc)
    market_total, covered = 0.0, 0
    for symbol in symbols:
        try:
            rows = fn(cg, symbol, "usd", "1d", 40)
        except _CG_ERRORS as exc:
            errors.append(exc)
            continue
        latest = None
        for row in rows:
            _put_oi(db, scope_id("coinglass", "oi-usd-1d", symbol), "oi_usd",
                    row, now)
            if latest is None or int(row["time"]) > int(latest["time"]):
                latest = row
        if latest is not None:
            market_total += float(as_decimal(latest["close"]))
            covered += 1
    db._commit()
    if errors and not covered and not legacy["loaded"]:
        _set_source(db, "oi", _cg_error_quality(errors[0]), str(errors[0]), now)
        return
    _set_source(
        db, "oi", "partial" if errors else "ok",
        "; ".join(str(e) for e in errors) if errors else None, now,
        payload={
            "market_oi_usd": market_total if covered else None,
            "coverage": {"ok": covered, "total": len(symbols)},
        },
    )


def _funding_cycle(db, cg, fetcher: dict, now: int, legacy: dict) -> None:
    fn = _funding_fn(fetcher)
    try:
        for row in fn(cg, "Binance", "BTCUSDT", "1d", 150):
            _put_funding(db, row, now)
        legacy["loaded"] = True
    except _CG_ERRORS as exc:
        _set_source(db, "funding", _cg_error_quality(exc), str(exc), now)
        return
    db._commit()
    _set_source(db, "funding", "ok", None, now)


# --- Факты памяти из состояния ликвидаций ---------------------------------


def _facts_cycle(db, settings, liq: dict, metrics: dict | None,
                 bars_by_symbol: dict, basket: Basket, now: int) -> None:
    """Фазы эпизода → факты: давление SHORT, затухание+восстановление LONG,
    только объём — факт без направления (§6). Недоступные ликвидации не
    создают и не отменяют фактов (вызывающий это гарантирует)."""
    window = liq.get("window")
    if window is None:
        return
    rule = get_rule("LIQ_PRESSURE")
    th = rule.thresholds
    episode = liq["episode"]
    memory_hours = int(getattr(settings, "events_memory_hours", 240))
    price_known = metrics is not None and metrics.get("median_return") is not None
    falling = (
        price_known
        and metrics["median_return"] < 0
        and (metrics.get("breadth_up") or 0) < 0.5
    )
    episode_id = f"liq-{episode['started_at']}"
    if episode.get("active") and episode.get("phase") == "intensifying" and falling:
        occurred = episode["started_at"] + int(th["window_ms"])
        key = memory.make_fact_key(
            rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
            scope="crypto_market", episode_id=episode_id, occurrence=str(occurred),
        )
        existing = store.get_fact(db, key)
        if (
            existing is not None
            and existing.get("direction") == "long"
            and window["end"] > existing["occurred_at"]
        ):
            # Новый каскад после затухания — дочерний факт со своим сроком (AC06)
            children = [
                f for f in store.query_facts(db, active_now_ms=now)
                if f.get("parent_key") == key
            ]
            if children:
                child = children[-1]
                memory.begin_fact(
                    db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                    scope="crypto_market", episode_id=episode_id,
                    parent_key=key, direction="short",
                    occurred_at=child["occurred_at"], detected_at=now,
                    available_at=now, time_precision="window",
                    payload=_pressure_payload(liq, metrics),
                    state="confirmed", memory_hours=memory_hours,
                )
            else:
                memory.child_fact(
                    db, key, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                    scope="crypto_market", episode_id=episode_id, direction="short",
                    occurred_at=window["end"], detected_at=now, available_at=now,
                    time_precision="window",
                    payload=_pressure_payload(liq, metrics),
                    state="confirmed", memory_hours=memory_hours,
                )
        else:
            memory.begin_fact(
                db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                scope="crypto_market", episode_id=episode_id, direction="short",
                occurred_at=occurred, detected_at=now, available_at=now,
                time_precision="window", payload=_pressure_payload(liq, metrics),
                state="confirmed" if window["closed"] else "provisional",
                memory_hours=memory_hours,
            )
        return
    if episode.get("active") and episode.get("phase") == "fading":
        recovery = _recovery_ok(th, metrics, bars_by_symbol, basket, now)
        if recovery:
            occurred = episode["started_at"] + int(th["window_ms"])
            key = memory.make_fact_key(
                rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
                scope="crypto_market", episode_id=episode_id, occurrence=str(occurred),
            )
            existing = store.get_fact(db, key)
            if (
                existing is not None
                and existing.get("direction") == "short"
                and existing.get("state") in ("provisional", "confirmed")
            ):
                memory.reinterpret_fact(
                    db, key, new_direction="long", at_ms=now,
                    reason="затухание каскада, восстановление цены/ширины",
                    payload={
                        "recovery_rule": "LIQ_RECOVERY",
                        "summary": (
                            "Каскад лонг-ликвидаций затухает, цена и ширина "
                            "рынка восстанавливаются — контекст LONG"
                        ),
                    },
                )
                return
    # Эпизодные условия не выполнены: чистый факт объёма без направления (§6)
    memory.begin_fact(
        db, rule_id="LIQ_FACT", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id=f"liqfact-{window['start']}",
        direction=None, occurred_at=window["end"], detected_at=now,
        available_at=now, time_precision="window",
        payload={
            "summary": (
                "Зафиксирован объём ликвидаций: "
                f"лонг {liq['sum_long_usd']:.0f} USD, "
                f"шорт {liq['sum_short_usd']:.0f} USD за окно 4ч"
            ),
            "sum_long_usd": liq["sum_long_usd"],
            "sum_short_usd": liq["sum_short_usd"],
            "long_share": liq.get("long_share"),
            "multiple": liq.get("multiple"),
            "coverage": liq.get("coverage"),
            "window_ms": int(th["window_ms"]),
        },
        state="confirmed", memory_hours=memory_hours,
    )


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


def _recovery_ok(th: dict, metrics: dict | None,
                 bars_by_symbol: dict, basket: Basket, now: int) -> bool:
    """Затухание + восстановление: медиана доходностей корзины за последние
    recovery_windows окон 4h > 0 либо ширина роста >= 0.5 (проектные критерии)."""
    breadth = (metrics or {}).get("breadth_up")
    if breadth is not None and breadth >= 0.5:
        return True
    window_ms = int(th["window_ms"])
    count = int(th["recovery_windows"])
    current = now - (now % window_ms)
    medians = []
    for i in range(1, count + 1):
        start, end = current - i * window_ms, current - (i - 1) * window_ms
        returns = []
        for member in basket.members:
            bars = sorted(
                (b for b in bars_by_symbol.get(member.symbol, [])
                 if b.get("quality") == "ok"),
                key=lambda b: b["end"],
            )
            before = [b for b in bars if b["end"] <= start]
            inside = [b for b in bars if b["start"] >= start and b["end"] <= end]
            if before and inside:
                returns.append(inside[-1]["close"] / before[-1]["close"] - 1.0)
        med = median([Decimal(str(r)) for r in returns])
        if med is not None:
            medians.append(med)
    med3 = median(medians)
    return med3 is not None and med3 > 0


# --- Время и статистика ----------------------------------------------------


def _time_ctx(basket: Basket, bars_by_symbol: dict, now: int) -> dict:
    weekday = msk_weekday(now)
    day_start = msk_day_start_ms(now)
    completed = []
    if basket.members:
        for start, end in completed_msk_days(now, 7):
            ret = msk_day_return(
                basket, bars_by_symbol, day_start_ms=start, day_end_ms=end,
            )
            med = ret["median_return"]
            completed.append({
                "day_start_ms": start,
                "weekday": msk_weekday(start),
                "median_return": med,
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
        "completed_days": completed,
        "week_seq": week_seq,
    }


def _stats(db, settings) -> dict:
    """Частота роста медианы корзины за московский день (§7, AC13/AC14)."""
    min_n = int(settings.events_stats_min_n)
    samples = [
        s for s in store.metric_series(db, "basket_day_return")
        if s.get("complete") and s.get("median_return") is not None
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
    return json.dumps(payload, ensure_ascii=False)
