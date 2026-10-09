"""Чистая оценка ситуаций. Сеть и SQLite сюда не входят.

Историческая частота вида «так было у 60% минимумов» не становится
разрешением. Состояние совпадения называется watch_matched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from .mathutil import DAY_MS, RULE_VERSION, D, median, percentile

MSK = timezone(timedelta(hours=3))
P90 = Decimal("0.90")
P75 = Decimal("0.75")
P25 = Decimal("0.25")
P05 = Decimal("0.05")
SHARE_LIQ01 = Decimal("0.60")
MULT_LIQ02 = Decimal("1.9")
FUNDING_HIGH_FRACTION = Decimal("0.0001")  # +0.01%
OI02_DAYS = 7


@dataclass(frozen=True)
class PriceBar:
    t: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    closed: bool


@dataclass(frozen=True)
class LiqBar:
    t: int
    long_usd: Decimal
    short_usd: Decimal
    closed: bool


@dataclass(frozen=True)
class OiBar:
    t: int
    open: Decimal
    close: Decimal
    closed: bool


@dataclass(frozen=True)
class FundingBar:
    t: int
    close_percent: Decimal
    closed: bool


@dataclass
class EvaluationInput:
    symbol: str
    now_ms: int
    price: list[PriceBar] = field(default_factory=list)
    liq: list[LiqBar] = field(default_factory=list)
    oi_coin: list[OiBar] = field(default_factory=list)
    oi_usd: list[OiBar] = field(default_factory=list)
    funding: list[FundingBar] = field(default_factory=list)
    h1_available: bool = False
    h1_hold: Optional[bool] = None
    funding_unit: str = "percent"
    rate_kind: str = "indicative"
    gate_enabled: bool = True
    price_source: str = ""
    liq_scope: str = "binance"
    oi_scope: str = "oi_aggregate_coin_v1"
    funding_scope: str = "funding_indicative_ohlc_pct_v1"
    data_stale: bool = False
    asset_profile: str = "BTC"
    scope_asset: str = "BTC"


def _closed(items):
    return [x for x in items if x.closed]


def _by_t(items) -> dict:
    return {x.t: x for x in items}


def color_of(bar: PriceBar) -> str:
    if bar.c < bar.o:
        return "red"
    if bar.c > bar.o:
        return "green"
    return "doji"


def return_pct(bar: PriceBar) -> Optional[Decimal]:
    if bar.o <= 0:
        return None
    return (bar.c - bar.o) / bar.o * Decimal(100)


def range_pct(bar: PriceBar) -> Optional[Decimal]:
    if bar.o <= 0:
        return None
    return (bar.h - bar.l) / bar.o * Decimal(100)


def continuous(days: list, day_t: int, n: int):
    """n календарных дней строго до day_t. Пропуск — None."""
    have = _by_t(days)
    out = []
    for k in range(n, 0, -1):
        t = day_t - DAY_MS * k
        bar = have.get(t)
        if bar is None:
            return None
        out.append(bar)
    return out


def streak_of(price: list[PriceBar]) -> dict:
    """Серия только по закрытым дням. Незакрытый день её не удлиняет.

    Пропуск между закрытыми днями делает серию неизвестной.
    Doji рвёт и красную, и зелёную серию.
    """
    ordered = sorted(price, key=lambda b: b.t)
    closed = [b for b in ordered if b.closed]
    provisional = None
    if ordered and not ordered[-1].closed:
        provisional = color_of(ordered[-1])
    if len(closed) >= 2:
        for prev, cur in zip(closed, closed[1:]):
            if cur.t - prev.t != DAY_MS:
                return {
                    "status": "unknown",
                    "color": None,
                    "length": None,
                    "provisional": provisional,
                    "reason": "gap",
                }
    length = 0
    color = None
    start_t = None
    for bar in reversed(closed):
        c = color_of(bar)
        if c == "doji":
            break
        if color is None:
            color = c
            length = 1
            start_t = bar.t
        elif c == color:
            length += 1
            start_t = bar.t
        else:
            break
    return {
        "status": "ok",
        "color": color,
        "length": length,
        "start_t": start_t,
        "provisional": provisional,
    }


def causal_legs(price: list[PriceBar], pct: Decimal) -> dict:
    """Разворот по закрытию. Пивот известен на баре подтверждения, не в экстремуме."""
    closed = sorted((b for b in price if b.closed), key=lambda b: b.t)
    pivots = []
    if not closed:
        return {"pivots": pivots, "leg": None}
    trough = closed[0]
    peak = closed[0]
    mode = "seek"
    for bar in closed[1:]:
        if mode == "seek":
            if bar.c < trough.c:
                trough = bar
            if bar.c > peak.c:
                peak = bar
            if trough.c > 0 and peak.c >= trough.c * (1 + pct) and peak.t > trough.t:
                pivots.append(_pivot("low", trough, bar, pct))
                mode = "up"
                peak = bar
            elif peak.c > 0 and trough.c <= peak.c * (1 - pct) and trough.t > peak.t:
                pivots.append(_pivot("high", peak, bar, pct))
                mode = "down"
                trough = bar
        elif mode == "up":
            if bar.c >= peak.c:
                peak = bar
            elif peak.c > 0 and bar.c <= peak.c * (1 - pct):
                pivots.append(_pivot("high", peak, bar, pct))
                mode = "down"
                trough = bar
        else:
            if bar.c <= trough.c:
                trough = bar
            elif trough.c > 0 and bar.c >= trough.c * (1 + pct):
                pivots.append(_pivot("low", trough, bar, pct))
                mode = "up"
                peak = bar
    leg = None
    if mode == "down":
        leg = {
            "kind": "low_candidate",
            "extreme_t": trough.t,
            "extreme": str(trough.c),
            "confirmed": False,
        }
    elif mode == "up":
        leg = {
            "kind": "high_candidate",
            "extreme_t": peak.t,
            "extreme": str(peak.c),
            "confirmed": False,
        }
    return {"pivots": pivots, "leg": leg}


def _pivot(kind: str, extreme: PriceBar, known: PriceBar, pct: Decimal) -> dict:
    return {
        "kind": kind,
        "extreme_t": extreme.t,
        "extreme": str(extreme.c),
        "known_at": known.t,
        "threshold": str(pct),
        "confirmed": True,
    }


def _share(bar: LiqBar) -> Optional[Decimal]:
    if bar.long_usd < 0 or bar.short_usd < 0:
        return None
    total = bar.long_usd + bar.short_usd
    if total <= 0:
        return None
    return bar.long_usd / total


def oi_direction(bar: OiBar) -> dict:
    if bar.open < 0 or bar.close < 0 or bar.open == 0:
        return {"direction": None, "quality": "integrity_error", "delta": None}
    delta = bar.close - bar.open
    if delta < 0:
        direction = "falling"
    elif delta > 0:
        direction = "rising"
    else:
        direction = "flat"
    pct = delta / bar.open * Decimal(100)
    return {"direction": direction, "quality": "ok", "delta": delta, "pct": pct}


def oi_range30(bars: list[OiBar], day_t: int) -> dict:
    prior = continuous(_closed(bars), day_t, 30)
    today = _by_t(bars).get(day_t)
    if prior is None or today is None or not today.closed:
        return {"quality": "gap" if prior is None else "pending", "new_high30": None}
    closes = [b.close for b in prior]
    if any(v < 0 for v in closes) or today.close < 0:
        return {"quality": "integrity_error", "new_high30": None}
    high = max(closes)
    low = min(closes)
    new_high = today.close > high
    new_low = today.close < low
    if high == low:
        position = None
    else:
        position = (today.close - low) / (high - low)
    if today.close == high or today.close == low:
        edge = "at_boundary"
    else:
        edge = None
    return {
        "quality": "ok",
        "new_high30": new_high,
        "new_low30": new_low,
        "at_boundary": edge,
        "position30": position,
        "high": high,
        "low": low,
    }


def funding_view(bar: Optional[FundingBar], unit: str, rate_kind: str) -> dict:
    if bar is None:
        return {"quality": "gap", "sign": None, "fraction": None}
    if unit != "percent":
        return {
            "quality": "semantics_unverified",
            "sign": None,
            "fraction": None,
            "raw": str(bar.close_percent),
            "rate_kind": "unknown",
        }
    fraction = bar.close_percent / Decimal(100)
    if fraction < 0:
        sign = "negative"
    elif fraction > 0:
        sign = "positive"
    else:
        sign = "zero"
    above = fraction > FUNDING_HIGH_FRACTION
    return {
        "quality": "ok",
        "sign": sign,
        "fraction": fraction,
        "raw_percent": bar.close_percent,
        "above_high": above,
        "rate_kind": rate_kind,
        "closed": bar.closed,
        "t": bar.t,
    }


def _status(not_applicable: bool, blocking: list, unknown: list, pending: bool) -> str:
    if not_applicable:
        return "not_applicable"
    if blocking:
        return "blocked"
    if unknown:
        return "unknown"
    if pending:
        return "pending"
    return "passed"


def _check(code: str, label: str, status: str, value=None, threshold=None,
           unit=None, time_ms=None) -> dict:
    words = {
        "met": "выполнено",
        "unmet": "не выполнено",
        "pending": "ожидается закрытие",
        "unknown": "неизвестно",
        "na": "неприменимо",
    }
    return {
        "code": code,
        "label": label,
        "status": status,
        "text": words[status],
        "value": None if value is None else str(value),
        "threshold": None if threshold is None else str(threshold),
        "unit": unit,
        "time": time_ms,
    }


def _liq_stats(liq: list[LiqBar], day_t: int) -> dict:
    closed = _closed(liq)
    bar = _by_t(liq).get(day_t)
    if bar is None:
        return {"quality": "gap"}
    if bar.long_usd < 0 or bar.short_usd < 0:
        return {"quality": "integrity_error"}
    share = _share(bar)
    base = continuous(closed, day_t, 90)
    out = {
        "t": day_t,
        "long": bar.long_usd,
        "short": bar.short_usd,
        "total": bar.long_usd + bar.short_usd,
        "share": share,
        "closed": bar.closed,
    }
    if not bar.closed:
        out["quality"] = "pending"
        return out
    if base is None:
        out["quality"] = "warming_up"
        return out
    longs = [b.long_usd for b in base]
    shorts = [b.short_usd for b in base]
    totals = [b.long_usd + b.short_usd for b in base]
    m_l = median(longs)
    m_s = median(shorts)
    m_t = median(totals)
    out["m_l"] = m_l
    out["m_s"] = m_s
    out["m_t"] = m_t
    if m_l is None or m_l <= 0 or m_t is None or m_t <= 0:
        out["quality"] = "baseline_zero"
        return out
    out["long_multiple"] = bar.long_usd / m_l
    out["total_multiple"] = (bar.long_usd + bar.short_usd) / m_t
    out["p90"] = percentile(longs, P90)
    out["quality"] = "ok"
    out["liq01"] = share is not None and share > SHARE_LIQ01
    out["liq02"] = out["long_multiple"] >= MULT_LIQ02
    out["cascade"] = bool(out["liq01"] and out["liq02"])
    return out


def _oi02_episode(liq, oi_coin, h1_hold, now_ms, data_stale) -> dict:
    """Запрет живёт до конца c+7. Досрочное снятие требует известного удержания H1."""
    closed_liq = sorted(_closed(liq), key=lambda b: b.t)
    oi = _by_t(_closed(oi_coin))
    active = None
    for bar in closed_liq:
        stats = _liq_stats(liq, bar.t)
        if stats.get("quality") != "ok" or not stats.get("cascade"):
            continue
        coin = oi.get(bar.t)
        if coin is None:
            continue
        direction = oi_direction(coin)
        if direction["quality"] != "ok":
            continue
        until = bar.t + DAY_MS * (OI02_DAYS + 1)
        if direction["direction"] == "rising":
            if active is None or active.get("released"):
                active = {
                    "start_t": bar.t,
                    "until": until,
                    "released": False,
                    "extended_to": bar.t,
                }
            else:
                active["until"] = until
                active["extended_to"] = bar.t
        elif (
            active
            and not active["released"]
            and direction["direction"] == "falling"
            and h1_hold is True
            and bar.t > active["start_t"]
        ):
            active["released"] = True
            active["released_at"] = bar.t
    if active is None or active.get("released"):
        return {"active": False, "episode": active}
    if now_ms >= active["until"]:
        return {
            "active": False,
            "expired": True,
            "stale_expiry": bool(data_stale),
            "episode": active,
        }
    return {"active": True, "episode": active}


def _wide(price: list[PriceBar], day_t: int) -> dict:
    bar = _by_t(price).get(day_t)
    if bar is None or not bar.closed:
        return {"status": "pending"}
    width = range_pct(bar)
    if width is None:
        return {"status": "unknown"}
    base = continuous(_closed(price), day_t, 90)
    if base is None:
        return {"status": "warming_up", "range_pct": width}
    ranges = []
    for b in base:
        w = range_pct(b)
        if w is None:
            return {"status": "unknown", "range_pct": width}
        ranges.append(w)
    p75 = percentile(ranges, P75)
    p25 = percentile(ranges, P25)
    return {
        "status": "ok",
        "range_pct": width,
        "p75": p75,
        "p25": p25,
        "wide": width >= p75,
        "narrow": width <= p25,
    }


def _post_high(price, liq, legs5) -> dict:
    highs = []
    leg = legs5.get("leg")
    if leg and leg["kind"] == "high_candidate":
        highs.append((leg["extreme_t"], D(leg["extreme"])))
    for pivot in legs5.get("pivots") or []:
        if pivot["kind"] == "high":
            highs.append((pivot["extreme_t"], D(pivot["extreme"])))
    if not highs:
        return {"active": False}
    high_t, high_px = max(highs, key=lambda item: item[0])
    prior_lows = [
        p for p in legs5.get("pivots") or []
        if p["kind"] == "low" and p["extreme_t"] < high_t
    ]
    if prior_lows:
        base = D(prior_lows[-1]["extreme"])
    else:
        earlier = [b.c for b in _closed(price) if b.t < high_t]
        base = min(earlier) if earlier else None
    if base is None or base <= 0 or high_px < base * Decimal("1.05"):
        return {"active": False}
    for bar in sorted(_closed(price), key=lambda b: b.t):
        if bar.t <= high_t or bar.t > high_t + 7 * DAY_MS:
            continue
        if color_of(bar) != "red" or bar.c >= high_px:
            continue
        stats = _liq_stats(liq, bar.t)
        if stats.get("quality") != "ok":
            continue
        share_ok = stats.get("share") is not None and stats["share"] > SHARE_LIQ01
        p90 = stats.get("p90")
        p90_ok = p90 is not None and stats["long"] > p90
        if share_ok and p90_ok:
            return {
                "active": True,
                "high_t": high_t,
                "day_t": bar.t,
                "extra_1_9": bool(stats.get("liq02")),
            }
    return {"active": False}


def _aligned(bars, day_t: int):
    """Точное совпадение границы. Чужой timestamp внутри тех же суток — несовместимое окно."""
    exact = _by_t(bars).get(day_t)
    if exact is not None:
        return exact, True
    for bar in bars:
        if bar.t // DAY_MS == day_t // DAY_MS:
            return bar, False
    return None, True


def week_of_month(day: datetime) -> int:
    d = day.day
    if d <= 7:
        return 1
    if d <= 14:
        return 2
    if d <= 21:
        return 3
    if d <= 28:
        return 4
    return 5


def calendar_block(now_ms: int, price: list[PriceBar]) -> dict:
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    week = week_of_month(now)
    month_end = now.day >= 22
    chain = {"status": "na", "text": "Сезонная цепочка не активна"}
    if now.month == 6:
        # июньская цепочка только после финализации последнего дня мая
        may_last = datetime(now.year, 5, 31, tzinfo=timezone.utc)
        may_t = int(may_last.timestamp() * 1000)
        bar = _by_t(price).get(may_t)
        if bar is None or not bar.closed:
            chain = {
                "status": "pending",
                "text": "Фон зависит от закрытия месяца",
            }
        else:
            ret = return_pct(bar)  # это день, не месяц; честный признак дня, не реплика исследования
            chain = {
                "status": "background",
                "text": "Июнь после мая. Месячная статистика источника на нашем ряду не проверена",
                "may_last_closed": True,
                "may_last_color": color_of(bar),
                "may_last_return": None if ret is None else str(ret),
            }
    return {
        "utc_date": now.date().isoformat(),
        "week_of_month": week,
        "month_end_card": month_end,
        "chain": chain,
        "note": "Календарь не задаёт направление сделки",
    }


def _msk_hour(now_ms: int) -> int:
    return datetime.fromtimestamp(now_ms / 1000, tz=MSK).hour


def quiet_hours(now_ms: int) -> bool:
    hour = _msk_hour(now_ms)
    return hour >= 23 or hour < 8


def evaluate(inp: EvaluationInput) -> dict:
    if inp.asset_profile not in ("BTC", "ETH", "SOL"):
        raise ValueError("неизвестный профиль актива")
    if inp.scope_asset != inp.asset_profile:
        raise ValueError("scope другого актива")

    price = sorted(inp.price, key=lambda b: b.t)
    price_t = {b.t for b in price}
    legs = causal_legs(price, Decimal("0.05"))
    legs20 = causal_legs(price, Decimal("0.20"))
    streak = streak_of(price)
    last_closed = next((b for b in reversed(price) if b.closed), None)
    last_t = last_closed.t if last_closed else None
    # Производные не пропадают, если ценового ряда ещё нет: берём последний закрытый день ликвидаций.
    deriv_t = last_t
    if deriv_t is None:
        closed_liq = sorted((bar for bar in inp.liq if bar.closed), key=lambda bar: bar.t)
        deriv_t = closed_liq[-1].t if closed_liq else None

    liq_today = _liq_stats(inp.liq, deriv_t) if deriv_t else {"quality": "gap"}
    coin_today = None
    usd_today = None
    if deriv_t is not None:
        coin_bar, coin_aligned = _aligned(inp.oi_coin, deriv_t)
        usd_bar, _usd_aligned = _aligned(inp.oi_usd, deriv_t)
        if coin_bar is not None and not coin_aligned:
            coin_today = {"quality": "unknown", "reason": "window_mismatch"}
        elif coin_bar:
            coin_today = oi_direction(coin_bar)
            coin_today["closed"] = coin_bar.closed
        else:
            coin_today = {"quality": "gap"}
        if usd_bar is not None and not _usd_aligned:
            usd_today = {"quality": "unknown", "reason": "window_mismatch"}
        elif usd_bar:
            usd_today = oi_direction(usd_bar)
        else:
            usd_today = {"quality": "gap"}
    fund_bar = None
    if deriv_t is not None:
        fund_bar = _by_t(inp.funding).get(deriv_t)
        if fund_bar and price_t and fund_bar.t not in price_t:
            fund_bar = None
    fund = funding_view(fund_bar, inp.funding_unit, inp.rate_kind)
    rng30 = oi_range30(inp.oi_coin, deriv_t) if deriv_t else {"quality": "gap"}
    wide = _wide(price, last_t) if last_t else {"status": "unknown"}
    oi02 = _oi02_episode(inp.liq, inp.oi_coin, inp.h1_hold if inp.h1_available else None,
                         inp.now_ms, inp.data_stale)
    post = _post_high(price, inp.liq, legs)

    # Каскад ищется в [d0-3, d0] кандидата минимума, один и тот же день.
    cascade_day = None
    d0 = legs["leg"]["extreme_t"] if legs["leg"] and legs["leg"]["kind"] == "low_candidate" else None
    if d0 is not None:
        for k in range(0, 4):
            t = d0 - DAY_MS * k
            stats = _liq_stats(inp.liq, t)
            if stats.get("cascade"):
                cascade_day = stats
                break

    situations = []
    if streak["status"] == "ok" and streak["color"] == "red" and (streak["length"] or 0) >= 4:
        situations.append({
            "rule_id": "RED_STREAK_REBOUND_WATCH",
            "episode_id": str(streak["start_t"]),
            "state": "observing",
            "title": f"Серия снижения: {streak['length']} закрытых дней",
        })
    if last_closed and color_of(last_closed) == "red" and wide.get("wide"):
        situations.append({
            "rule_id": "RED_WIDE_DAY_REBOUND_WATCH",
            "episode_id": str(last_closed.t),
            "state": "observing",
            "title": "Красный широкий день. Наблюдение отскока экспериментальное",
            "experimental": True,
        })
    if fund.get("sign") == "negative":
        situations.append({
            "rule_id": "FUNDING_REBOUND_WATCH",
            "episode_id": str(fund.get("t")),
            "state": "candidate",
            "title": "Шорты платят по наблюдаемой ставке. Это не выплата и не вход",
        })
    if oi02.get("active"):
        situations.append({
            "rule_id": "CASCADE_OI_RISING_RISK",
            "episode_id": str(oi02["episode"]["start_t"]),
            "state": "observing",
            "title": "Каскад при росте OI в монетах. Разворотное наблюдение блокируется",
            "until": oi02["episode"]["until"],
        })
    if post.get("active"):
        situations.append({
            "rule_id": "POST_HIGH_LONG_CASCADE",
            "episode_id": str(post["high_t"]),
            "state": "observing",
            "title": "После локального максимума красный день и лонг-ликвидации. Не команда закрыть позицию",
        })

    gates = [
        _gate_a(inp, d0, cascade_day, coin_today, oi02, rng30, fund, legs),
        _gate_b(inp, last_closed, wide, oi02, fund, coin_today),
        _gate_v(inp, streak, oi02, fund),
        _gate_g(inp, fund, liq_today, coin_today, oi02),
    ]
    for gate in gates:
        if gate["status"] == "passed" and (gate["blocking_reasons"] or gate["unknown_dependencies"]):
            raise RuntimeError("passed при непустых зависимостях")

    market = _market_line(post, oi02, streak, liq_today, situations)
    service = _service_line(inp, liq_today, fund)
    digest = _msk_hour(inp.now_ms) == 9
    return {
        "symbol": inp.symbol,
        "rule_version": RULE_VERSION,
        "evaluated_at": inp.now_ms,
        "available_at": inp.now_ms,
        "price_source": inp.price_source,
        "market_line": market,
        "service_line": service,
        "lines": {"market": market, "service": service},
        "calendar": calendar_block(inp.now_ms, price),
        "streak": streak,
        "leg5": legs["leg"],
        "leg20": legs20["leg"],
        "pivots5": legs["pivots"],
        "liquidation": _public_liq(liq_today),
        "oi_coin": _public_oi(coin_today),
        "oi_usd": _public_oi(usd_today),
        "oi_range30": _public_range(rng30),
        "funding": _public_funding(fund),
        "oi02": {
            "active": bool(oi02.get("active")),
            "expired": bool(oi02.get("expired")),
            "stale_expiry": bool(oi02.get("stale_expiry")),
            "until": None if not oi02.get("episode") else oi02["episode"].get("until"),
        },
        "post_high": {"active": bool(post.get("active"))},
        "wide": {
            "status": wide.get("status"),
            "wide": wide.get("wide"),
            "experimental": True,
        },
        "strategy_gates": gates,
        "situations": situations,
        "risk_multiplier": None,
        "risk_note": "Коэффициенты риска не перемножаются и размер позиции не меняют",
        "morning_digest_due": digest,
        "quiet_hours": quiet_hours(inp.now_ms),
        "study_percents_on_card": False,
        "effect_on_h1_structure": "none",
    }


def _public_liq(stats: dict) -> dict:
    return {
        "quality": stats.get("quality"),
        "share": _s(stats.get("share")),
        "long_multiple": _s(stats.get("long_multiple")),
        "total_multiple": _s(stats.get("total_multiple")),
        "cascade": stats.get("cascade"),
        "closed": stats.get("closed"),
        "t": stats.get("t"),
    }


def _public_oi(info) -> dict:
    if not info:
        return {"quality": "gap"}
    return {
        "quality": info.get("quality"),
        "direction": info.get("direction"),
        "pct": _s(info.get("pct")),
        "closed": info.get("closed"),
    }


def _public_range(info: dict) -> dict:
    return {
        "quality": info.get("quality"),
        "new_high30": info.get("new_high30"),
        "new_low30": info.get("new_low30"),
        "at_boundary": info.get("at_boundary"),
        "position30": _s(info.get("position30")),
    }


def _public_funding(info: dict) -> dict:
    return {
        "quality": info.get("quality"),
        "sign": info.get("sign"),
        "fraction": _s(info.get("fraction")),
        "raw_percent": _s(info.get("raw_percent")),
        "rate_kind": info.get("rate_kind"),
        "above_high_indicative": info.get("above_high") if info.get("rate_kind") == "indicative" else None,
        "closed": info.get("closed"),
    }


def _s(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _market_line(post, oi02, streak, liq_today, situations) -> str:
    if post.get("active"):
        return ("После локального максимума закрылся красный день ниже него, "
                "а лонг-ликвидации выше своей нормы. Это предупреждение, не команда.")
    if oi02.get("active"):
        return "Каскад прошёл при росте открытого интереса в монетах. Разворотное наблюдение под запретом."
    if streak.get("status") == "ok" and streak.get("color") == "red" and (streak.get("length") or 0) >= 4:
        return f"Серия снижения: {streak['length']} закрытых дней."
    if liq_today.get("cascade"):
        return "В последнем закрытом дне каскад лонг-ликвидаций."
    if any(s["rule_id"] == "RED_WIDE_DAY_REBOUND_WATCH" for s in situations):
        return "Последний закрытый день красный и широкий. Детектор ширины экспериментальный."
    if streak.get("status") == "unknown":
        return "Ценовая серия прервана пропуском дня. Длину серии назвать нельзя."
    return "Отдельного ценового эпизода нет."


def _service_line(inp: EvaluationInput, liq_today, fund) -> str:
    parts = []
    if not inp.h1_available:
        parts.append("Часовой интервал Coinglass недоступен. Удержание минимума тремя часами не проверяется.")
    if inp.rate_kind != "settled" or fund.get("quality") == "semantics_unverified":
        parts.append("Funding — наблюдение ставки, не подтверждённая выплата.")
    if liq_today.get("quality") == "warming_up":
        parts.append("Нормы ликвидаций ещё прогреваются: в 90 днях есть пропуск или истории мало.")
    if inp.data_stale:
        parts.append("Данные устарели.")
    if not parts:
        parts.append("Обязательные ряды на месте. Это не прогноз сделки.")
    return " ".join(parts)


def _unknown_funding(inp: EvaluationInput, fund: dict) -> list[str]:
    unknown = []
    if not inp.gate_enabled:
        return unknown
    if inp.rate_kind != "settled" or fund.get("quality") != "ok":
        unknown.append("funding_settlement")
    return unknown


def _gate_shell(setup: str, *, not_applicable=False, blocking=None, unknown=None,
                pending=False, checks=None, advisories=None) -> dict:
    blocking = list(blocking or [])
    unknown = list(unknown or [])
    advisories = list(advisories or [])
    status = _status(not_applicable, blocking, unknown, pending)
    return {
        "setup": setup,
        "status": status,
        "blocking_reasons": blocking,
        "unknown_dependencies": unknown,
        "advisories": advisories,
        "checks": checks or [],
        "effect_on_h1_structure": "none",
    }


def _gate_a(inp, d0, cascade_day, coin_today, oi02, rng30, fund, legs) -> dict:
    if not inp.gate_enabled:
        return _gate_shell("A", unknown=["FILTER_DISABLED"],
                           checks=[_check("gate", "Фильтр стратегии", "unknown")])
    if d0 is None:
        return _gate_shell("A", not_applicable=True,
                           checks=[_check("low", "Живой кандидат минимума 5%", "unmet")])
    checks = []
    blocking = []
    unknown = []
    pending = False
    checks.append(_check("low", "Живой кандидат минимума 5%", "met", time_ms=d0))
    if cascade_day is None:
        checks.append(_check("cascade", "Один день с LIQ01 и LIQ02 в окне 4 дней", "unmet"))
    else:
        checks.append(_check(
            "cascade", "Один день с LIQ01 и LIQ02", "met",
            value=cascade_day.get("long_multiple"), threshold=MULT_LIQ02,
            unit="кратность лонгов", time_ms=cascade_day.get("t"),
        ))
    if not inp.h1_available or inp.h1_hold is None:
        unknown.append("h1_hold")
        checks.append(_check("hold", "Удержание минимума тремя закрытыми H1", "unknown"))
    elif inp.h1_hold:
        checks.append(_check("hold", "Удержание минимума тремя закрытыми H1", "met"))
    else:
        checks.append(_check("hold", "Удержание минимума тремя закрытыми H1", "unmet"))
    if cascade_day is None:
        checks.append(_check("oi", "OI в монетах того же дня снижается", "na"))
    else:
        coin, aligned = _aligned(inp.oi_coin, cascade_day["t"])
        if coin is not None and not aligned:
            unknown.append("oi_window")
            checks.append(_check("oi", "OI в монетах того же дня снижается", "unknown"))
        elif coin is None:
            unknown.append("oi_coin")
            checks.append(_check("oi", "OI в монетах того же дня снижается", "unknown"))
        else:
            direction = oi_direction(coin)
            if direction["quality"] != "ok":
                unknown.append("oi_coin")
                checks.append(_check("oi", "OI в монетах того же дня снижается", "unknown"))
            elif not coin.closed:
                pending = True
                checks.append(_check("oi", "OI в монетах того же дня снижается", "pending"))
            elif direction["direction"] == "falling":
                checks.append(_check("oi", "OI в монетах того же дня снижается", "met",
                                     value=direction["pct"], unit="%"))
            else:
                checks.append(_check("oi", "OI в монетах того же дня снижается", "unmet",
                                     value=direction["direction"]))
    if oi02.get("active"):
        blocking.append("CASCADE_OI_RISING")
    if oi02.get("stale_expiry"):
        unknown.append("oi02_expiry_stale")
    if rng30.get("new_high30"):
        # Для А высокий OI не запрещает полный набор, только напоминает.
        pass
    unknown.extend(_unknown_funding(inp, fund))
    if fund.get("rate_kind") == "indicative" and fund.get("above_high"):
        pass
    advisories = []
    if fund.get("rate_kind") == "indicative" and fund.get("above_high"):
        advisories.append("funding_indicative_above_0_01pct")
    if rng30.get("new_high30"):
        advisories.append("oi_high30_smaller_risk_reminder")
    # passed только когда нет блокировок и неизвестного и все обязательные checks met
    required = [c for c in checks if c["code"] in {"low", "cascade", "hold", "oi"}]
    if any(c["status"] == "pending" for c in required):
        pending = True
    if any(c["status"] in {"unmet", "na"} for c in required):
        # не passed: есть невыполненное. Если уже есть blocking/unknown, приоритет у них.
        if not blocking and not unknown and not pending:
            blocking.append("setup_A_incomplete")
    gate = _gate_shell("A", blocking=blocking, unknown=unknown, pending=pending,
                       checks=checks, advisories=advisories)
    if gate["status"] == "passed":
        gate["state"] = "watch_matched"
        gate["title"] = "Условия наблюдения А совпали. Это не допуск к сделке."
    return gate


def _gate_b(inp, last_closed, wide, oi02, fund, coin_today) -> dict:
    if not inp.gate_enabled:
        return _gate_shell("B", unknown=["FILTER_DISABLED"])
    if last_closed is None or color_of(last_closed) != "red" or not wide.get("wide"):
        return _gate_shell("B", not_applicable=True)
    blocking = []
    unknown = ["wide_experimental"]
    advisories = ["wide_day_experimental"]
    if oi02.get("active"):
        blocking.append("CASCADE_OI_RISING")
    if oi02.get("stale_expiry"):
        unknown.append("oi02_expiry_stale")
    # Активный расчётный FR03, не вчерашний каскад.
    if inp.rate_kind == "settled" and fund.get("above_high"):
        blocking.append("funding_long_restricted")
    unknown.extend(x for x in _unknown_funding(inp, fund) if x not in unknown)
    if any_high30_blocks_non_a(inp):
        blocking.append("oi_high30_requires_full_A")
    return _gate_shell("B", blocking=blocking, unknown=unknown, advisories=advisories,
                       checks=[_check("price", "Красный широкий день", "met")])


def any_high30_blocks_non_a(inp: EvaluationInput) -> bool:
    if not inp.price:
        return False
    last = next((b for b in reversed(sorted(inp.price, key=lambda x: x.t)) if b.closed), None)
    if last is None:
        return False
    return bool(oi_range30(inp.oi_coin, last.t).get("new_high30"))


def _gate_v(inp, streak, oi02, fund) -> dict:
    if not inp.gate_enabled:
        return _gate_shell("V", unknown=["FILTER_DISABLED"])
    ok = streak.get("status") == "ok" and streak.get("color") == "red" and (streak.get("length") or 0) >= 4
    if streak.get("status") == "unknown":
        return _gate_shell("V", unknown=["price_gap"])
    if not ok:
        return _gate_shell("V", not_applicable=True)
    blocking = []
    unknown = []
    advisories = []
    if oi02.get("active"):
        blocking.append("CASCADE_OI_RISING")
    if oi02.get("stale_expiry"):
        unknown.append("oi02_expiry_stale")
    if fund.get("sign") == "negative":
        advisories.append("funding_negative_amplifier")
    if inp.rate_kind == "settled" and fund.get("above_high"):
        blocking.append("funding_long_restricted")
    unknown.extend(_unknown_funding(inp, fund))
    if any_high30_blocks_non_a(inp):
        blocking.append("oi_high30_requires_full_A")
    return _gate_shell(
        "V", blocking=blocking, unknown=unknown, advisories=advisories,
        checks=[_check("streak", "Четыре непрерывных красных дня", "met",
                       value=streak.get("length"), threshold=4, unit="дней")],
    )


def _gate_g(inp, fund, liq_today, coin_today, oi02) -> dict:
    if not inp.gate_enabled:
        return _gate_shell("G", unknown=["FILTER_DISABLED"])
    if fund.get("quality") == "semantics_unverified":
        return _gate_shell("G", unknown=["funding_unit"])
    if fund.get("sign") != "negative":
        return _gate_shell("G", not_applicable=True,
                           checks=[_check("sign", "Наблюдаемая ставка отрицательна", "unmet")])
    unknown = ["h1_bull_confirmation"]
    if oi02.get("stale_expiry"):
        unknown.append("oi02_expiry_stale")
    if liq_today.get("quality") in {None, "gap"} or coin_today is None or coin_today.get("quality") == "gap":
        unknown.append("liq_or_oi")
    if oi02.get("active"):
        # Г без обязательного каскада, но активный OI02 не даёт passed.
        pass
    unknown.extend(_unknown_funding(inp, fund))
    # дедуп
    unknown = list(dict.fromkeys(unknown))
    blocking = ["CASCADE_OI_RISING"] if oi02.get("active") else []
    return _gate_shell(
        "G", blocking=blocking, unknown=unknown,
        checks=[_check("sign", "Наблюдаемая ставка отрицательна", "met"),
                _check("h1", "Подтверждённый бычий сценарий H1", "unknown")],
        advisories=["funding_is_indicative"],
    )


def relation_to_h1(snapshot: dict, direction: Optional[str]) -> str:
    if not snapshot:
        return "недостаточно данных"
    bull = direction == "bull"
    bear = direction == "bear"
    oi02 = snapshot.get("oi02", {}).get("active")
    post = snapshot.get("post_high", {}).get("active")
    if bull and (oi02 or post):
        return "противоречит"
    matched = any(
        g.get("status") == "passed" and g.get("setup") == "A"
        for g in snapshot.get("strategy_gates", [])
    )
    if bull and matched:
        return "согласован"
    if bull or bear:
        return "нейтрален"
    return "недостаточно данных"


RULES = [
    {
        "id": "LIQ01",
        "class": "strategy_rule",
        "text": "Доля лонг-ликвидаций строго выше 60%. Ровно 60% условие не выполняет.",
    },
    {
        "id": "LIQ02",
        "class": "engineering_default",
        "text": "Лонг-ликвидации не ниже 1.9 своей медианы за 90 предшествующих дней. Это не доказанное дно.",
    },
    {
        "id": "OI02",
        "class": "strategy_rule",
        "text": "Каскад и рост OI в монетах держат запрет до конца дня c+7. Без часовых данных досрочно он не снимается.",
    },
    {
        "id": "FR03",
        "class": "strategy_rule",
        "text": "Порог +0.01% относится к выплаченной 8-часовой ставке. Наблюдаемое OHLC его не включает: ставка показывается как справка.",
    },
    {
        "id": "A",
        "class": "strategy_rule",
        "text": "Наблюдение А — кандидат минимума, каскад в том же четырёхдневном окне, удержание, падение OI и пустой запрет. Совпадение не является вероятностью из исследования.",
    },
    {
        "id": "study",
        "class": "source_fact",
        "text": "Проценты и t-статистика источника на нашем ряду не проверены и на карточку не выводятся.",
    },
]
