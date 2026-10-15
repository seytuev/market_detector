"""Рыночная корзина движка «События и время» (§3 ТЗ).

Проектная корзина v1: 20 крупнейших доступных самостоятельных активов по
капитализации — без стейблкоинов, обёрток и дубликатов (правила
исключений переиспользуют classify_exclusion модуля alt, не дублируются).
Состав фиксируется на начало дня МСК с basket_version; причины исключений
хранятся. Отсутствующий или просроченный актив никогда не считается
нулевой доходностью — он исключается из медианы/ширины и уменьшает
покрытие.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.adapters.coinmarketcap import CmcListing
from app.alt.universe import classify_exclusion
from app.events.mathutil import DAY_MS, D, median
from .validation import HOUR_MS, complete_window, hourly_bars

DEFAULT_SIZE = 20
COVERAGE_OK = 0.8  # проектный порог достаточности покрытия (вызывающий может переопределить)


@dataclass
class BasketMember:
    symbol: str          # "BTC"
    cmc_id: int          # 0 для статической корзины
    rank: int
    pair: str            # "BTCUSDT"


@dataclass
class Basket:
    version: str         # basket_version
    day_start_ms: int    # начало дня МСК, на которое зафиксирован состав
    source: str          # "cmc_snapshot" | "static" | "unavailable"
    members: list[BasketMember]
    exclusions: list[dict]   # [{"symbol","cmc_id","rank","reason"}]


def _rank_of(item) -> int:
    if isinstance(item, dict):
        return int(item.get("rank", item.get("cmc_rank", 0)) or 0)
    return int(getattr(item, "rank", getattr(item, "cmc_rank", 0)) or 0)


def _as_listing(item) -> CmcListing:
    """Нормализация строки рейтинга (CmcListing, dict или duck-typed объект)."""
    if isinstance(item, CmcListing):
        return item
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    return CmcListing(
        cmc_id=int(get("cmc_id", get("id", 0)) or 0),
        name=str(get("name", "") or ""),
        symbol=str(get("symbol", "") or ""),
        rank=_rank_of(item),
        market_cap=float(get("market_cap", 0.0) or 0.0),
        tags=[str(t) for t in (get("tags") or [])],
    )


def _basket_version(day_start_ms: int, members: list[BasketMember]) -> str:
    # cmc_id=0 (статическая корзина) — ключ по символу.
    key = f"{day_start_ms}:" + ",".join(
        str(m.cmc_id) if m.cmc_id else m.symbol for m in members
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def build_basket(
    listings,
    available_pairs: set[str],
    *,
    size: int = DEFAULT_SIZE,
    day_start_ms: int,
) -> Basket:
    """Корзина из снапшота CMC: сортировка по рангу, исключения с причинами.

    Сканируются все переданные строки: исключённые (stable/wrapped/
    duplicate по classify_exclusion, либо нет пары USDT у биржи)
    записываются в exclusions с причиной; в состав берутся первые
    `size` прошедших фильтры.
    """
    members: list[BasketMember] = []
    exclusions: list[dict] = []
    seen_symbols, seen_ids = set(), set()
    for item in sorted(listings, key=_rank_of):
        listing = _as_listing(item)
        reason = classify_exclusion(listing)
        if listing.symbol in seen_symbols or (listing.cmc_id and listing.cmc_id in seen_ids):
            reason = "duplicate"
        pair = f"{listing.symbol}USDT"
        if reason is None and pair not in available_pairs:
            reason = "no_binance_pair"
        if reason is not None:
            exclusions.append({
                "symbol": listing.symbol,
                "cmc_id": listing.cmc_id,
                "rank": listing.rank,
                "reason": reason,
            })
            continue
        members.append(BasketMember(
            symbol=listing.symbol, cmc_id=listing.cmc_id,
            rank=listing.rank, pair=pair,
        ))
        seen_symbols.add(listing.symbol)
        if listing.cmc_id:
            seen_ids.add(listing.cmc_id)
    members = members[:size]
    return Basket(
        version=_basket_version(day_start_ms, members),
        day_start_ms=day_start_ms,
        source="cmc_snapshot",
        members=members,
        exclusions=exclusions,
    )


def basket_from_static(symbols: list[str], *, day_start_ms: int) -> Basket:
    """Статическая корзина из явного списка символов (cmc_id=0)."""
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    members = [
        BasketMember(symbol=s, cmc_id=0, rank=i + 1, pair=f"{s}USDT")
        for i, s in enumerate(symbols)
    ]
    return Basket(
        version=_basket_version(day_start_ms, members),
        day_start_ms=day_start_ms,
        source="static",
        members=members,
        exclusions=[],
    )


def basket_unavailable(*, day_start_ms: int, reason: str) -> Basket:
    """Пустая корзина: состав недоступен, причина хранится в exclusions."""
    return Basket(
        version=_basket_version(day_start_ms, []),
        day_start_ms=day_start_ms,
        source="unavailable",
        members=[],
        exclusions=[{"symbol": None, "cmc_id": 0, "rank": 0, "reason": reason}],
    )


def _ok_bars(bars_by_symbol: dict[str, list[dict]], symbol: str) -> list[dict]:
    """Бары пригодного качества, отсортированные по end."""
    return sorted(
        (b for b in bars_by_symbol.get(symbol, []) if b.get("quality") == "ok"),
        key=lambda b: b["end"],
    )


def basket_metrics(
    basket: Basket,
    bars_by_symbol: dict[str, list[dict]],
    *,
    now_ms: int,
    max_age_ms: int,
    window_ms: int = DAY_MS,
) -> dict:
    """Покрытие, медианная доходность и ширина роста корзины за окно.

    ok-участник — есть свежий завершённый бар (end <= now_ms,
    end >= now_ms - max_age_ms). Доходность участника —
    close последнего завершённого бара / close на начало окна - 1.
    Участник без свежих/полных данных не считается нулём: он не входит
    в медиану и ширину и уменьшает покрытие.
    """
    members_out: list[dict] = []
    returns: list[float] = []
    ok = 0
    for m in basket.members:
        indexed = hourly_bars(bars_by_symbol.get(m.symbol, []))
        completed = [b for b in indexed.values() if b["end"] <= now_ms]
        last = max(completed, key=lambda b: b["end"]) if completed else None
        target_end = now_ms // HOUR_MS * HOUR_MS
        ret = None
        if not completed:
            status = "missing"
        elif last["end"] < now_ms - max_age_ms:
            status = "stale"
        else:
            segment = complete_window(indexed, target_end - window_ms, target_end)
            status = "ok" if segment else "incomplete"
            if segment:
                ret = float(segment[-1]["close"]) / float(segment[0]["open"]) - 1.0
                returns.append(ret)
                ok += 1
        members_out.append({
            "symbol": m.symbol, "pair": m.pair, "status": status, "return": ret,
        })
    total = len(basket.members)
    med = median([D(r) for r in returns])
    return {
        "window_ms": window_ms,
        "coverage": {
            "ok": ok,
            "total": total,
            "ratio": ok / total if total else 0.0,
        },
        "median_return": float(med) if med is not None else None,
        "breadth_up": (
            sum(1 for r in returns if r > 0) / len(returns) if returns else None
        ),
        "members": members_out,
    }


def msk_day_return(
    basket: Basket,
    bars_by_symbol: dict[str, list[dict]],
    *,
    day_start_ms: int,
    day_end_ms: int,
) -> dict:
    """Медианная доходность корзины за московский день [day_start, day_end).

    Участник ok — есть завершённые бары полностью внутри окна; доходность
    — close последнего / open первого - 1. Участники без данных не
    считаются нулём. complete — все участники корзины ok.
    """
    returns: list[float] = []
    for m in basket.members:
        bars = complete_window(bars_by_symbol.get(m.symbol, []), day_start_ms, day_end_ms)
        if not bars:
            continue
        returns.append(float(bars[-1]["close"]) / float(bars[0]["open"]) - 1.0)
    total = len(basket.members)
    med = median([D(r) for r in returns])
    return {
        "median_return": float(med) if med is not None else None,
        "members_ok": len(returns),
        "members_total": total,
        "complete": total > 0 and len(returns) == total,
    }


def day_sign(median_return) -> int:
    """Знак дня: +1 бычий (строго > 0), -1 медвежий, 0 — ровно ноль (§7).

    Нулевая медианная доходность — отдельное состояние, не бычий день.
    None (нет данных) знака не имеет.
    """
    if median_return is None:
        raise ValueError("median_return=None: знак дня не определён")
    if median_return > 0:
        return 1
    if median_return < 0:
        return -1
    return 0
