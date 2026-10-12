"""Структурные базы alt-0.4.

Общие правила для любого актива: причинная опора 3+3, допуск по ATR
периода реакции, кластер с ограничением диаметра, линия по внешнему краю
подтверждённых реакций. Тикер, календарное окно и целевая цена в расчёт
не входят. Время доступности ревизии — закрытие свечи подтверждения,
а не момент запуска прогона.

Параметры ниже — проектная калибровка (ТЗ 12.10.2026 §5–§7), не отдельные
правила пользователя. Их снимок хранится вместе с результатом.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..config import DetectorConfig
from ..engine.liquidity import PivotRecord, find_pivots
from ..models import Candle, ZoneType, close_boundary_ms
from .engine import DAY_MS, ALT_TIMEFRAME, median, validate_ohlc

ALT_RULE_VERSION_V4 = "alt-0.4"
RULES_VERSION = ALT_RULE_VERSION_V4

# Статусы, при которых ревизию можно выбрать в effective_range.
ELIGIBLE_STATES = ("forming", "mature", "active", "completed")


@dataclass(frozen=True)
class RulesV04Params:
    """Проектные пороги. Одинаковы для всех активов."""

    atr_window: int = 30
    min_atr_bars: int = 14
    atr_mult: float = 0.5
    price_pct: float = 0.02
    tick_mult: float = 2.0
    # Уход от экстремума, который делает реакцию значимой, и дистанция
    # принятого выхода за внешнюю зону.
    move_atr: float = 2.0
    # Выход между касаниями, после которого следующий возврат — новая реакция.
    contact_exit_atr: float = 1.0
    # Насколько новая опора должна быть внешнее уже выбранной, чтобы
    # заменить её, а не остаться шумом той же полки.
    material_atr: float = 1.0
    exit_closes: int = 3
    accept_bars: int = 10
    balance_window: int = 30
    balance_efficiency: float = 0.35
    trend_efficiency: float = 0.45
    max_base_efficiency: float = 0.55
    min_containment: float = 0.55
    forming_days: int = 51
    mature_days: int = 101
    # Новая внешняя опора становится границей, только если за это окно
    # не начался принятый выход в противоположную сторону (терминальный вынос).
    reclaim_window_days: int = 70
    # Выход из базы: после трёх закрытий за снимком границы ждём окно.
    # База закрывается, только если доля закрытий за снимком не ниже порога.
    # Один возвратный день окно не обнуляет.
    departure_confirm_days: int = 120
    departure_share: float = 0.70
    # Сколько дней до спокойного окна смотреть, чтобы взять кульминацию тренда,
    # а не экстремум короткого отскока у самого дна.
    trend_lookback_days: int = 180
    gap_days: int = 5
    pivot_left: int = 3
    pivot_right: int = 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "atr_window": self.atr_window,
            "min_atr_bars": self.min_atr_bars,
            "atr_mult": self.atr_mult,
            "price_pct": self.price_pct,
            "tick_mult": self.tick_mult,
            "move_atr": self.move_atr,
            "contact_exit_atr": self.contact_exit_atr,
            "material_atr": self.material_atr,
            "exit_closes": self.exit_closes,
            "accept_bars": self.accept_bars,
            "balance_window": self.balance_window,
            "balance_efficiency": self.balance_efficiency,
            "trend_efficiency": self.trend_efficiency,
            "max_base_efficiency": self.max_base_efficiency,
            "min_containment": self.min_containment,
            "forming_days": self.forming_days,
            "mature_days": self.mature_days,
            "reclaim_window_days": self.reclaim_window_days,
            "departure_confirm_days": self.departure_confirm_days,
            "departure_share": self.departure_share,
            "trend_lookback_days": self.trend_lookback_days,
            "gap_days": self.gap_days,
            "pivot_left": self.pivot_left,
            "pivot_right": self.pivot_right,
            "rules_version": RULES_VERSION,
        }


@dataclass
class _Pivot:
    kind: str                     # low | high
    price: float
    formed_at: int
    confirmed_at: int
    formed_i: int
    confirm_i: int
    atr_ref: Optional[float]
    median_price: Optional[float]
    tolerance: Optional[float]
    atr_status: str               # ok | fallback | insufficient
    record_id: int


@dataclass
class _Cluster:
    kind: str
    pivots: list[_Pivot]
    reactions: int
    edge: float
    confirmed_at: int
    atr_ref: float
    tolerance: float
    method: str
    confirmation: str

    @property
    def first_formed_at(self) -> int:
        return min(p.formed_at for p in self.pivots)


@dataclass
class _SideChoice:
    cluster: _Cluster
    excluded: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    uncertainty: Optional[str] = None


def _efficiency(closes: Sequence[float]) -> float:
    if len(closes) < 2:
        return 1.0
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path <= 0:
        return 0.0
    return abs(closes[-1] - closes[0]) / path


def infer_tick(candles: Sequence[Any]) -> float:
    """Шаг цены по фактической записи OHLC. Не константа тикера."""
    decimals = 0
    for candle in candles:
        for value in (candle.open, candle.high, candle.low, candle.close):
            text = f"{float(value):.8f}".rstrip("0")
            if "." in text:
                decimals = max(decimals, len(text.split(".")[1]))
    decimals = min(max(decimals, 2), 8)
    return 10 ** (-decimals)


def _prepare(candles: Sequence[Any], as_of_ms: Optional[int], params: RulesV04Params):
    ordered = sorted(candles, key=lambda c: c.open_time)
    deduped: list[Any] = []
    seen: set[int] = set()
    duplicate = False
    for candle in ordered:
        if candle.open_time in seen:
            duplicate = True
            continue
        seen.add(candle.open_time)
        close_at = close_boundary_ms(candle.open_time, ALT_TIMEFRAME)
        if as_of_ms is not None and close_at > as_of_ms:
            continue
        deduped.append(candle)
    invalid = []
    for candle in deduped:
        reason = validate_ohlc(candle.open, candle.high, candle.low, candle.close)
        if reason:
            invalid.append({"open_time": candle.open_time, "reason": reason})
    gaps = []
    for prev, nxt in zip(deduped, deduped[1:]):
        missing = (nxt.open_time - prev.open_time) // DAY_MS - 1
        if missing >= params.gap_days:
            gaps.append({
                "after_open_time": prev.open_time,
                "before_open_time": nxt.open_time,
                "missing_days": int(missing),
            })
    return deduped, {
        "duplicate_open_time": duplicate,
        "invalid_ohlc": invalid,
        "gaps": gaps,
    }


def _atr_table(candles: Sequence[Any], params: RulesV04Params):
    n = len(candles)
    tr = [None] * n
    for i in range(1, n):
        high, low = candles[i].high, candles[i].low
        prev_close = candles[i - 1].close
        tr[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    atr = [None] * n
    med = [None] * n
    status = ["insufficient"] * n
    window = params.atr_window
    for i in range(1, n):
        sample = [x for x in tr[max(1, i - window + 1): i + 1] if x is not None]
        closes = [c.close for c in candles[max(0, i - window + 1): i + 1]]
        med[i] = median(closes) if closes else None
        if not sample or med[i] is None:
            continue
        atr[i] = median(sample)
        status[i] = "ok" if len(sample) >= params.min_atr_bars else "fallback"
    return atr, med, status


def _tolerance(atr_value: float, median_price: float, tick: float, params: RulesV04Params) -> float:
    return max(params.tick_mult * tick, params.atr_mult * atr_value, params.price_pct * median_price)


def _to_engine(candles: Sequence[Any]) -> list[Candle]:
    out = []
    for candle in candles:
        out.append(Candle(
            instrument_id=getattr(candle, "source_id", 0) or 0,
            timeframe=ALT_TIMEFRAME,
            open_time=candle.open_time,
            close_time=close_boundary_ms(candle.open_time, ALT_TIMEFRAME) - 1,
            open=candle.open, high=candle.high, low=candle.low, close=candle.close,
            closed=True, source="alt",
        ))
    return out


def _collect_pivots(candles, atr, med, atr_status, tick, params: RulesV04Params) -> list[_Pivot]:
    cfg = DetectorConfig(pivot_left=params.pivot_left, pivot_right=params.pivot_right)
    raw = find_pivots(_to_engine(candles), ALT_TIMEFRAME, cfg)
    index = {c.open_time: i for i, c in enumerate(candles)}
    out: list[_Pivot] = []
    for n, pivot in enumerate(raw):
        formed_i = index.get(pivot.formed_at)
        confirm_ot = pivot.confirmed_at - DAY_MS
        confirm_i = index.get(confirm_ot)
        if formed_i is None or confirm_i is None:
            continue
        kind = "low" if pivot.kind == ZoneType.SSL else "high"
        atr_value = atr[confirm_i]
        median_price = med[confirm_i]
        tol = None
        if atr_value is not None and median_price is not None and atr_value > 0:
            tol = _tolerance(atr_value, median_price, tick, params)
        out.append(_Pivot(
            kind=kind, price=pivot.price, formed_at=pivot.formed_at,
            confirmed_at=pivot.confirmed_at, formed_i=formed_i, confirm_i=confirm_i,
            atr_ref=atr_value, median_price=median_price, tolerance=tol,
            atr_status=atr_status[confirm_i], record_id=n,
        ))
    return out


def _max_high(candles, left: int, right: int) -> float:
    if right <= left:
        return candles[left].high
    return max(c.high for c in candles[left: right + 1])


def _min_low(candles, left: int, right: int) -> float:
    if right <= left:
        return candles[left].low
    return min(c.low for c in candles[left: right + 1])


def _contact_groups(pivots: Sequence[_Pivot], candles, params: RulesV04Params) -> list[list[_Pivot]]:
    """Непрерывная серия касаний — одна реакция. Новый заход после ухода
    не меньше contact_exit_atr и возврата — следующая реакция."""
    ordered = sorted(pivots, key=lambda p: (p.formed_at, p.price))
    if not ordered:
        return []
    groups = [[ordered[0]]]
    for pivot in ordered[1:]:
        prev = groups[-1][-1]
        atr_value = prev.atr_ref or pivot.atr_ref or 0.0
        distance = params.contact_exit_atr * atr_value
        left = prev.formed_i + 1
        right = pivot.formed_i - 1
        separated = False
        if atr_value > 0 and right >= left:
            if pivot.kind == "low":
                separated = _max_high(candles, left, right) >= prev.price + distance
            else:
                separated = _min_low(candles, left, right) <= prev.price - distance
        if separated:
            groups.append([pivot])
        else:
            groups[-1].append(pivot)
    return groups


def _cluster(pivots: Sequence[_Pivot]) -> list[_Cluster]:
    ordered = sorted(pivots, key=lambda p: (p.price, p.formed_at, p.record_id))
    buckets: list[list[_Pivot]] = []
    for pivot in ordered:
        if pivot.tolerance is None or pivot.atr_ref is None:
            continue
        if buckets:
            members = buckets[-1]
            diameter = pivot.price - min(p.price for p in members)
            limit = 2.0 * min([p.tolerance for p in members] + [pivot.tolerance])
            if diameter <= limit:
                members.append(pivot)
                continue
        buckets.append([pivot])
    clusters: list[_Cluster] = []
    for members in buckets:
        kind = members[0].kind
        edge_pivot = min(members, key=lambda p: p.price) if kind == "low" else max(members, key=lambda p: p.price)
        clusters.append(_Cluster(
            kind=kind,
            pivots=list(members),
            reactions=0,
            edge=edge_pivot.price,
            confirmed_at=0,
            atr_ref=edge_pivot.atr_ref or 0.0,
            tolerance=edge_pivot.tolerance or 0.0,
            method="outer_edge",
            confirmation="",
        ))
    return clusters


def _assign_reactions(clusters: list[_Cluster], groups: Sequence[Sequence[_Pivot]], params: RulesV04Params) -> None:
    for cluster in clusters:
        ids = {p.record_id for p in cluster.pivots}
        hits = [g for g in groups if any(p.record_id in ids for p in g)]
        cluster.reactions = len(hits)
        if not hits:
            continue
        # Вторая независимая реакция подтверждает зону в момент её pivot.
        if len(hits) >= 2:
            second = hits[1]
            anchor = min(second, key=lambda p: p.confirmed_at)
            cluster.confirmed_at = anchor.confirmed_at
            cluster.confirmation = "two_reactions"
        else:
            extreme = min(cluster.pivots, key=lambda p: p.price) if cluster.kind == "low" else max(cluster.pivots, key=lambda p: p.price)
            cluster.confirmed_at = extreme.confirmed_at
            cluster.confirmation = "unconfirmed"


def _ready_clusters(clusters: Sequence[_Cluster], now_at: int) -> list[_Cluster]:
    return [
        c for c in clusters
        if c.confirmation == "two_reactions" and 0 < c.confirmed_at <= now_at and c.atr_ref > 0
    ]


def _choice_from(cluster: _Cluster, ready: Sequence[_Cluster]) -> _SideChoice:
    alternatives = []
    uncertainty = None
    for other in ready:
        if other is cluster:
            continue
        alternatives.append({
            "price": other.edge,
            "reactions": other.reactions,
            "confirmed_at": other.confirmed_at,
            "first_formed_at": other.first_formed_at,
        })
        gap = abs(other.edge - cluster.edge)
        if gap <= max(cluster.tolerance, other.tolerance) and other.reactions >= cluster.reactions:
            uncertainty = "outer_pairs_close"
    return _SideChoice(cluster=cluster, alternatives=alternatives[:5], uncertainty=uncertainty)


def _earliest_choice(clusters: Sequence[_Cluster], now_at: int) -> Optional[_SideChoice]:
    ready = _ready_clusters(clusters, now_at)
    if not ready:
        return None
    ready.sort(key=lambda c: (c.confirmed_at, c.first_formed_at, c.edge))
    return _choice_from(ready[0], ready)


def _outer_ready(
    clusters: Sequence[_Cluster],
    now_at: int,
    committed_edge: float,
    committed_tolerance: float,
    params: RulesV04Params,
) -> list[_Cluster]:
    """Подтверждённые кластеры вне уже выбранной линии.

    Низ заменяется, только если он дальше хотя бы на 1 ATR: короткий
    прокол полки остаётся шумом той же опоры. Верх заменяется, если фитиль
    вышел за допуск текущей линии. ATR свечи этого фитиля в порог не входит,
    иначе собственный всплеск прячет внешнюю реакцию.
    """
    ready = []
    for cluster in _ready_clusters(clusters, now_at):
        if cluster.kind == "low":
            moved = committed_edge - cluster.edge
            needed = params.material_atr * cluster.atr_ref
        else:
            moved = cluster.edge - committed_edge
            needed = max(committed_tolerance, cluster.tolerance)
        if moved < needed or needed <= 0:
            continue
        ready.append(cluster)
    return ready


def _outer_candidate(
    clusters: Sequence[_Cluster],
    now_at: int,
    committed_edge: float,
    committed_tolerance: float,
    params: RulesV04Params,
) -> Optional[_Cluster]:
    """Самый внешний подтверждённый кластер. Сам по себе линию ещё не двигает."""
    ready = _outer_ready(clusters, now_at, committed_edge, committed_tolerance, params)
    best: Optional[_Cluster] = None
    for cluster in ready:
        if best is None:
            best = cluster
            continue
        more_outer = cluster.edge < best.edge if cluster.kind == "low" else cluster.edge > best.edge
        if more_outer:
            best = cluster
    return best


def _pair_changed(prev: Optional[tuple[float, float]], lower: float, upper: float, tol: float) -> bool:
    if prev is None:
        return True
    return abs(prev[0] - lower) > tol * 0.25 or abs(prev[1] - upper) > tol * 0.25


def _state_for(start_i: int, end_i: int, pair_i: int, closed: bool, params: RulesV04Params) -> str:
    age = end_i - start_i + 1
    if closed:
        return "completed"
    if age > params.mature_days and (end_i - pair_i) > params.forming_days:
        return "mature"
    if age > params.forming_days:
        return "forming"
    return "building"


def _coverage(candles, start_i: int, end_i: int, lower: float, upper: float) -> dict[str, Any]:
    window = candles[start_i: end_i + 1]
    closes = [c.close for c in window]
    inside = sum(1 for value in closes if lower <= value <= upper)
    thirds = []
    span = max(1, len(window) // 3)
    for part in range(3):
        chunk = window[part * span: (part + 1) * span if part < 2 else len(window)]
        if not chunk:
            continue
        med = median([c.close for c in chunk])
        thirds.append(med)
    shift = None
    if len(thirds) >= 2 and (upper - lower) > 0:
        shift = (thirds[-1] - thirds[0]) / (upper - lower)
    return {
        "bars": len(window),
        "containment": (inside / len(closes)) if closes else 0.0,
        "efficiency": _efficiency(closes),
        "center_shift_width": shift,
        "third_medians": thirds,
    }


def _side_evidence(choice: _SideChoice, groups_n: int) -> dict[str, Any]:
    cluster = choice.cluster
    return {
        "price": cluster.edge,
        "method": cluster.method,
        "confirmation": cluster.confirmation,
        "reactions": cluster.reactions,
        "contacts": cluster.reactions,
        "contact_groups_seen": groups_n,
        "atr_ref": cluster.atr_ref,
        "tolerance": cluster.tolerance,
        "atr_status": cluster.pivots[0].atr_status if cluster.pivots else "insufficient",
        "confirmed_at": cluster.confirmed_at,
        "supports": [
            {
                "price": p.price,
                "formed_at": p.formed_at,
                "confirmed_at": p.confirmed_at,
                "atr_ref": p.atr_ref,
                "tolerance": p.tolerance,
                "atr_status": p.atr_status,
            }
            for p in sorted(cluster.pivots, key=lambda item: item.formed_at)
        ],
        "excluded": choice.excluded,
        "alternatives": choice.alternatives,
    }


def _classify_excluded(
    pivots: Sequence[_Pivot],
    chosen_ids: set[int],
    sweep_ids: set[int],
    edge: float,
    kind: str,
    first_pair_at: int,
) -> list[dict[str, Any]]:
    rows = []
    for pivot in pivots:
        if pivot.record_id in chosen_ids:
            continue
        beyond = pivot.price < edge if kind == "low" else pivot.price > edge
        if pivot.record_id in sweep_ids:
            reason = "sweep"
        elif beyond and pivot.formed_at < first_pair_at:
            reason = "transition_extreme"
        elif beyond:
            reason = "sweep" if pivot.record_id in sweep_ids else "unconfirmed_extreme"
        else:
            reason = "inner_reaction"
        if reason == "inner_reaction" and not beyond:
            # Внутренние реакции evidence не раздувают: наружу — только
            # отклонённые экстремумы и выносы.
            continue
        rows.append({
            "price": pivot.price,
            "formed_at": pivot.formed_at,
            "confirmed_at": pivot.confirmed_at,
            "reason": reason,
            "class": reason if reason != "inner_reaction" else "boundary_reaction",
        })
    return rows


def _find_next_start(candles, pos: int, params: RulesV04Params) -> Optional[int]:
    """Начало базы — кульминация направленного участка, за которым
    волатильность становится боковой. Первый pivot сам по себе начало не задаёт.
    """
    n = len(candles)
    w = params.balance_window
    i = max(pos + 2 * w, 2 * w)
    while i < n:
        closes_now = [c.close for c in candles[i - w: i]]
        closes_prev = [c.close for c in candles[i - 2 * w: i - w]]
        if (
            _efficiency(closes_now) <= params.balance_efficiency
            and _efficiency(closes_prev) >= params.trend_efficiency
        ):
            quiet_start = i - w
            begin = max(pos, quiet_start - params.trend_lookback_days)
            trend = candles[begin: quiet_start]
            if not trend:
                i += 1
                continue
            low_rel = min(range(len(trend)), key=lambda k: (trend[k].low, k))
            high_rel = max(range(len(trend)), key=lambda k: (trend[k].high, -k))
            # Кульминация — поздний экстремум длинного направленного участка.
            # Короткий отскок внутри последнего месяца тренд не разворачивает.
            start = begin + (low_rel if low_rel >= high_rel else high_rel)
            if start < pos:
                start = pos
            return start
        i += 1
    return None


def _sides_at(active: Sequence[_Pivot], sweep_ids: set[int], now_i: int, candles, params):
    visible = [p for p in active if p.record_id not in sweep_ids and p.confirm_i <= now_i]
    lows = [p for p in visible if p.kind == "low"]
    highs = [p for p in visible if p.kind == "high"]
    low_groups = _contact_groups(lows, candles, params)
    high_groups = _contact_groups(highs, candles, params)
    low_clusters = _cluster(lows)
    high_clusters = _cluster(highs)
    _assign_reactions(low_clusters, low_groups, params)
    _assign_reactions(high_clusters, high_groups, params)
    return lows, highs, low_clusters, high_clusters, low_groups, high_groups


def _opposite_break(
    candles, start: int, end: int, bound: float, side: str, params: RulesV04Params,
    atr_ref: Optional[float] = None,
) -> bool:
    """Три закрытия за противоположной границей. Возврат внутрь серии сбрасывает.

    Дистанция считается по ATR самой границы. ATR свечи пробоя сюда не входит:
    всплеск волатильности не должен прятать уже принятый выход.
    """
    run = 0
    last = min(end, len(candles) - 1)
    for i in range(max(0, start), last + 1):
        atr_value = atr_ref if atr_ref is not None and atr_ref > 0 else _atr_at_index(candles, i, params)
        if atr_value is None or atr_value <= 0:
            run = 0
            continue
        close = candles[i].close
        outside = (
            close > bound + params.move_atr * atr_value if side == "up"
            else close < bound - params.move_atr * atr_value
        )
        run = run + 1 if outside else 0
        if run >= params.exit_closes:
            return True
    return False


def _walk_segment(
    candles,
    pivots: Sequence[_Pivot],
    start_i: int,
    tick: float,
    params: RulesV04Params,
    computed_at_ms: int,
):
    """Идёт вперёд по закрытым D1. Уже выбранная пара не расширяется,
    пока новый экстремум не подтверждён возвратом в ту же базу.
    Импульс, который не вернулся, завершает базу и в опоры не входит.
    """
    n = len(candles)
    by_confirm: dict[int, list[_Pivot]] = {}
    for pivot in pivots:
        if pivot.formed_i < start_i or pivot.tolerance is None:
            continue
        by_confirm.setdefault(pivot.confirm_i, []).append(pivot)

    active: list[_Pivot] = []
    sweep_ids: set[int] = set()
    revisions: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    committed: Optional[tuple[_SideChoice, _SideChoice]] = None
    settled_low_keys: set[frozenset] = set()
    pair_i = start_i
    end_i = n - 1
    end_reason = None
    end_confirmed_at = None
    first_pair_at = None
    probe_from: Optional[int] = None
    probe_side: Optional[str] = None
    probe_bound: Optional[float] = None
    probe_run = 0
    probe_armed = False
    probe_extreme: Optional[float] = None
    low_groups: list = []
    high_groups: list = []

    def reset_probe() -> None:
        nonlocal probe_from, probe_side, probe_bound, probe_run, probe_armed, probe_extreme
        probe_from = None
        probe_side = None
        probe_bound = None
        probe_run = 0
        probe_armed = False
        probe_extreme = None

    def emit(now_i: int, low_choice: _SideChoice, high_choice: _SideChoice, reason: str, lows, highs) -> bool:
        nonlocal committed, pair_i, first_pair_at, low_groups, high_groups
        lower = low_choice.cluster.edge
        upper = high_choice.cluster.edge
        if not (upper > lower):
            return False
        if revisions:
            prev = revisions[-1]
            if abs(prev["lower"] - lower) <= tick and abs(prev["upper"] - upper) <= tick:
                committed = (low_choice, high_choice)
                return False
        now_at = close_boundary_ms(candles[now_i].open_time, ALT_TIMEFRAME)
        if first_pair_at is None:
            first_pair_at = now_at
        pair_i = now_i
        metrics = _coverage(candles, start_i, now_i, lower, upper)
        sideways = (
            metrics["efficiency"] <= params.max_base_efficiency
            and metrics["containment"] >= params.min_containment
        )
        anchor = first_pair_at or now_at
        low_choice.excluded = _classify_excluded(
            lows, {p.record_id for p in low_choice.cluster.pivots}, sweep_ids, lower, "low", anchor,
        )
        high_choice.excluded = _classify_excluded(
            highs, {p.record_id for p in high_choice.cluster.pivots}, sweep_ids, upper, "high", anchor,
        )
        state = _state_for(start_i, now_i, pair_i, False, params)
        if not sideways and state == "mature":
            state = "forming"
        available = max(low_choice.cluster.confirmed_at, high_choice.cluster.confirmed_at, now_at) if reason != "initial_pair" else max(low_choice.cluster.confirmed_at, high_choice.cluster.confirmed_at)
        revision = _revision_payload(
            candles, start_i, None, None, lower, upper, available, computed_at_ms,
            reason, state, low_choice, high_choice, metrics, params, sideways,
            len(low_groups), len(high_groups),
        )
        revisions.append(revision)
        committed = (low_choice, high_choice)
        return True

    for i in range(start_i, n):
        added = by_confirm.get(i, [])
        if added:
            active.extend(added)
        now_at = close_boundary_ms(candles[i].open_time, ALT_TIMEFRAME)
        lows, highs, low_clusters, high_clusters, low_groups, high_groups = _sides_at(
            active, sweep_ids, i, candles, params,
        )
        if committed is None:
            low_choice = _earliest_choice(low_clusters, now_at)
            high_choice = _earliest_choice(high_clusters, now_at)
            if low_choice and high_choice and high_choice.cluster.edge > low_choice.cluster.edge:
                emit(i, low_choice, high_choice, "initial_pair", lows, highs)
            # Пустой участок не забирает всю оставшуюся историю.
            if committed is None and i - start_i >= params.departure_confirm_days * 2:
                end_i = i
                break
            continue

        low_choice, high_choice = committed
        lower = low_choice.cluster.edge
        upper = high_choice.cluster.edge
        low_tol = low_choice.cluster.tolerance
        high_tol = high_choice.cluster.tolerance

        due_lows = []
        for cluster in _outer_ready(low_clusters, now_at, lower, low_tol, params):
            key = frozenset(pivot.record_id for pivot in cluster.pivots)
            if key in settled_low_keys:
                continue
            deadline = max(pivot.formed_i for pivot in cluster.pivots) + params.reclaim_window_days
            if i < deadline:
                continue
            due_lows.append((deadline, cluster.edge, cluster, key))
        # Окно каждой полки закрывается в свою дату и смотрит только свои свечи.
        # Более поздний вынос не переносит решение и не отменяет уже прожитую полку.
        due_lows.sort(key=lambda item: (item[0], item[1]))
        if due_lows:
            deadline, _edge, cluster, key = due_lows[0]
            extreme_i = min(pivot.formed_i for pivot in cluster.pivots)
            broke_up = _opposite_break(
                candles, extreme_i, deadline, upper, "up", params, high_choice.cluster.atr_ref,
            )
            back_inside = any(
                lower <= candles[j].close <= upper
                for j in range(extreme_i, deadline + 1)
            )
            settled_low_keys.add(key)
            if broke_up or not back_inside:
                for pivot in cluster.pivots:
                    sweep_ids.add(pivot.record_id)
                probes.append({
                    "side": "down",
                    "from_open_time": candles[extreme_i].open_time,
                    "to_open_time": candles[deadline].open_time,
                    "reclaimed": not broke_up,
                    "reason": "sweep" if not broke_up else "terminal_spring",
                })
            else:
                chosen = _choice_from(cluster, _ready_clusters(low_clusters, now_at))
                if emit(i, chosen, high_choice, "late_boundary_confirmed", lows, highs):
                    reset_probe()
                low_choice, high_choice = committed
                lower = low_choice.cluster.edge
                upper = high_choice.cluster.edge
                low_tol = low_choice.cluster.tolerance
                high_tol = high_choice.cluster.tolerance

        higher = _outer_candidate(high_clusters, now_at, upper, high_tol, params)
        if higher is not None:
            extreme_i = min(p.formed_i for p in higher.pivots)
            deadline = extreme_i + params.reclaim_window_days
            if i >= deadline:
                span = candles[extreme_i + 1: i + 1]
                inside = sum(1 for candle in span if candle.close <= upper)
                share = inside / len(span) if span else 0.0
                # Потолок той же базы: после экстремума цена снова проводит
                # время под прежней верхней границей. Импульс этого не делает.
                # Факт трёх закрытий выше сам по себе потолок не отменяет:
                # иначе возвращённая реакция навсегда остаётся выносом.
                if share >= 0.5:
                    chosen = _choice_from(higher, _ready_clusters(high_clusters, now_at))
                    if emit(i, low_choice, chosen, "late_boundary_confirmed", lows, highs):
                        reset_probe()
                    if committed is not None:
                        low_choice, high_choice = committed
                        lower = low_choice.cluster.edge
                        upper = high_choice.cluster.edge

        low_atr = low_choice.cluster.atr_ref or 0.0
        high_atr = high_choice.cluster.atr_ref or 0.0
        close = candles[i].close
        # За границей зоны — само закрытие. Дистанция 2 ATR проверяет,
        # что ход значимый, но не обязана держаться на каждой свече:
        # иначе спокойный ATR прячет уже принятый выход, а ATR пробоя
        # наоборот делает глубокий обвал «ещё внутри».
        outside_up = close > upper
        outside_down = close < lower
        if not probe_armed:
            side = "up" if outside_up else ("down" if outside_down else None)
            if side is None or (probe_side is not None and side != probe_side):
                reset_probe()
            else:
                if probe_from is None:
                    probe_from = i
                    probe_side = side
                    probe_bound = upper if side == "up" else lower
                    probe_run = 1
                    probe_extreme = candles[i].high if side == "up" else candles[i].low
                else:
                    probe_run += 1
                    if side == "up":
                        probe_extreme = max(probe_extreme or candles[i].high, candles[i].high)
                    else:
                        probe_extreme = min(probe_extreme or candles[i].low, candles[i].low)
                reached = False
                if probe_side == "up" and high_atr > 0 and probe_extreme is not None:
                    reached = probe_extreme >= upper + params.move_atr * high_atr
                elif probe_side == "down" and low_atr > 0 and probe_extreme is not None:
                    reached = probe_extreme <= lower - params.move_atr * low_atr
                if probe_run >= params.exit_closes and reached:
                    probe_armed = True
            continue
        if (
            probe_from is None
            or probe_bound is None
            or probe_side is None
            or i - probe_from < params.departure_confirm_days
        ):
            continue
        span = candles[probe_from: i + 1]
        if probe_side == "up":
            outside_n = sum(1 for candle in span if candle.close > probe_bound)
        else:
            outside_n = sum(1 for candle in span if candle.close < probe_bound)
        share = outside_n / len(span) if span else 0.0
        tail = span[-params.accept_bars:]
        if probe_side == "up":
            held = bool(tail) and all(candle.close > probe_bound for candle in tail)
        else:
            held = bool(tail) and all(candle.close < probe_bound for candle in tail)
        # Доля пути за границей ещё не принятие: к концу окна цена должна
        # оставаться в новой области. Возврат последних закрытий — вынос.
        if share >= params.departure_share and held:
            end_i = probe_from - 1
            if end_i < start_i:
                end_i = start_i
            end_reason = "breakout_confirmed" if probe_side == "up" else "accepted_outside"
            end_confirmed_at = close_boundary_ms(candles[i].open_time, ALT_TIMEFRAME)
            for pivot in active:
                if pivot.formed_i >= probe_from:
                    sweep_ids.add(pivot.record_id)
            break
        probes.append({
            "side": probe_side,
            "from_open_time": candles[probe_from].open_time,
            "to_open_time": candles[i].open_time,
            "reclaimed": True,
            "reason": "false_breakout_reintegrated",
            "outside_share": share,
        })
        # Нижний вынос, который не удержал цену, в опору не превращается.
        # Верхний остаётся кандидатом потолка: отказ пробы его не стирает.
        if probe_side == "down":
            for pivot in active:
                if probe_from <= pivot.formed_i <= i and pivot.kind == "low" and pivot.price < lower:
                    sweep_ids.add(pivot.record_id)
        reset_probe()

    if revisions:
        last = revisions[-1]
        lower, upper = last["lower"], last["upper"]
        metrics = _coverage(candles, start_i, end_i, lower, upper)
        sideways = (
            metrics["efficiency"] <= params.max_base_efficiency
            and metrics["containment"] >= params.min_containment
        )
        if end_reason and end_confirmed_at:
            # Завершение — отдельная ревизия. Геометрия и время её доступности
            # не сдвигаются задним числом на момент подтверждения выхода.
            done = dict(last)
            done["quality_metrics"] = metrics
            done["sideways"] = sideways
            done["base_end_open_time"] = candles[end_i].open_time
            done["base_end_confirmed_at_ms"] = end_confirmed_at
            done["end_reason"] = end_reason
            done["state"] = "completed"
            done["geometry_kind"] = "completed_base"
            done["revision_reason"] = end_reason
            done["available_at_ms"] = end_confirmed_at
            revisions.append(done)
        else:
            last["quality_metrics"] = metrics
            last["sideways"] = sideways
            last["state"] = _state_for(start_i, end_i, pair_i, False, params)
    children = _child_ranges(candles, pivots, start_i, end_i, revisions, params)
    return {
        "start_i": start_i,
        "end_i": end_i,
        "end_reason": end_reason,
        "revisions": revisions,
        "probes": probes,
        "children": children,
        "sweep_ids": sorted(sweep_ids),
    }


def _atr_at_index(candles, index: int, params: RulesV04Params) -> Optional[float]:
    if index < 1:
        return None
    sample = []
    begin = max(1, index - params.atr_window + 1)
    for i in range(begin, index + 1):
        high, low = candles[i].high, candles[i].low
        prev = candles[i - 1].close
        sample.append(max(high - low, abs(high - prev), abs(low - prev)))
    if len(sample) < params.min_atr_bars:
        return median(sample) if sample else None
    return median(sample)


def _revision_payload(
    candles, start_i, end_open, end_confirmed, lower, upper, available_at, computed_at,
    reason, state, low_choice, high_choice, metrics, params, sideways, low_groups, high_groups,
) -> dict[str, Any]:
    width = upper - lower
    mid = (upper + lower) / 2.0
    uncertainty = low_choice.uncertainty or high_choice.uncertainty
    return {
        "rules_version": RULES_VERSION,
        "geometry_kind": "structural_base" if sideways else "unresolved",
        "lower": lower,
        "upper": upper,
        "mid": mid,
        "width": width,
        "base_start_open_time": candles[start_i].open_time,
        "base_end_open_time": end_open,
        "base_end_confirmed_at_ms": end_confirmed,
        "available_at_ms": available_at,
        "computed_at_ms": computed_at,
        "revision_reason": reason,
        "state": state,
        "sideways": sideways,
        "uncertainty": uncertainty,
        "selection_reason": "outer_confirmed_pair",
        "boundary_evidence": {
            "lower": _side_evidence(low_choice, low_groups),
            "upper": _side_evidence(high_choice, high_groups),
        },
        "quality_metrics": metrics,
        "data_coverage": {},
        "targets": [
            {"tp": n, "price": upper + n * width} for n in range(1, 5)
        ],
        "cancel_price": 2 * lower - upper,
        "params": params.as_dict(),
    }


def _child_ranges(candles, pivots, start_i, end_i, revisions, params: RulesV04Params):
    if not revisions:
        return []
    outer = revisions[-1]
    lower, upper = outer["lower"], outer["upper"]
    width = upper - lower
    if width <= 0:
        return []
    visible = [
        p for p in pivots
        if start_i <= p.formed_i <= end_i and p.tolerance is not None and lower < p.price < upper
    ]
    highs = [p for p in visible if p.kind == "high" and p.price <= upper - params.move_atr * (p.atr_ref or 0)]
    groups = _contact_groups(highs, candles, params)
    clusters = _cluster(highs)
    _assign_reactions(clusters, groups, params)
    inner = [
        c for c in clusters
        if c.confirmation == "two_reactions" and upper - c.edge >= params.move_atr * max(c.atr_ref, 1e-12)
        and c.edge - lower < width * 0.75
    ]
    if not inner:
        return []
    inner.sort(key=lambda c: (c.reactions, c.edge))
    ceiling = inner[-1]
    # Первые три закрытия выше внутреннего потолка, пока внешняя зона жива.
    run = 0
    exit_i = None
    for i in range(start_i, end_i + 1):
        if candles[i].open_time < ceiling.confirmed_at - DAY_MS:
            continue
        if candles[i].close > ceiling.edge and candles[i].close < upper:
            run += 1
            if run >= params.exit_closes:
                exit_i = i
                break
        else:
            run = 0
    if exit_i is None:
        return []
    return [{
        "kind": "inner_range",
        "parent": True,
        "upper": ceiling.edge,
        "lower": lower,
        "local_exit_open_time": candles[exit_i - params.exit_closes + 1].open_time,
        "local_exit_confirmed_at_ms": close_boundary_ms(candles[exit_i].open_time, ALT_TIMEFRAME),
        "reason": "local_range_exit",
        "reactions": ceiling.reactions,
    }]


def _structure_key(start_open: int) -> str:
    raw = f"{RULES_VERSION}|{start_open}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def analyze(
    candles: Sequence[Any],
    *,
    as_of_ms: Optional[int] = None,
    computed_at_ms: int = 0,
    tick_size: Optional[float] = None,
    params: Optional[RulesV04Params] = None,
) -> dict[str, Any]:
    """Полный пересчёт по закрытым D1 до as_of включительно.

    Повторный вызов на том же префиксе даёт те же ревизии. Более поздняя
    ревизия не появляется, пока её свеча подтверждения не закрыта.
    """
    params = params or RulesV04Params()
    prepared, coverage = _prepare(candles, as_of_ms, params)
    tick = tick_size if tick_size is not None and tick_size > 0 else infer_tick(prepared or candles)
    status = "ok"
    if coverage["invalid_ohlc"] or coverage["duplicate_open_time"]:
        status = "data_pending"
    if coverage["gaps"]:
        status = "data_pending"
    need = params.pivot_left + params.pivot_right + params.min_atr_bars + 2
    if len(prepared) < need:
        return {
            "rules_version": RULES_VERSION,
            "status": "insufficient_history" if not coverage["gaps"] else "data_pending",
            "structures": [],
            "params": params.as_dict(),
            "data_coverage": {**coverage, "bars": len(prepared), "tick_size": tick},
            "computed_at_ms": computed_at_ms,
        }
    atr, med, atr_status = _atr_table(prepared, params)
    pivots = _collect_pivots(prepared, atr, med, atr_status, tick, params)
    structures = []
    pos = 0
    guard = 0
    while guard < 48:
        guard += 1
        start_i = _find_next_start(prepared, pos, params)
        if start_i is None or start_i < pos:
            break
        walked = _walk_segment(prepared, pivots, start_i, tick, params, computed_at_ms)
        end_i = walked["end_i"]
        revisions = walked["revisions"]
        if not revisions:
            pos = max(end_i, start_i) + 1
            if pos >= len(prepared) - params.balance_window:
                break
            continue
        if revisions:
            key = _structure_key(prepared[start_i].open_time)
            for rev_i, revision in enumerate(revisions, start=1):
                revision["structure_key"] = key
                revision["revision_number"] = rev_i
                revision["data_coverage"] = {
                    **coverage,
                    "bars": len(prepared),
                    "tick_size": tick,
                    "segment_bars": end_i - start_i + 1,
                }
                revision["revision_id"] = f"{key}:{rev_i}"
            final = revisions[-1]
            eligible = (
                final.get("sideways")
                and final["state"] in ELIGIBLE_STATES
                and final["upper"] > final["lower"]
                and status != "data_pending"
            )
            structures.append({
                "structure_key": key,
                "rules_version": RULES_VERSION,
                "eligible": bool(eligible),
                "state": final["state"],
                "revisions": revisions,
                "children": [
                    {**child, "parent_structure_key": key}
                    for child in walked["children"]
                ],
                "probes": walked["probes"],
                "geometry": {
                    "lower": final["lower"],
                    "upper": final["upper"],
                    "mid": final["mid"],
                    "width": final["width"],
                    "base_start_open_time": final["base_start_open_time"],
                    "base_end_open_time": final["base_end_open_time"],
                    "available_at_ms": final["available_at_ms"],
                },
            })
        pos = max(end_i, start_i) + 1
        if pos >= len(prepared) - params.balance_window:
            break
    if status == "ok" and not structures:
        status = "no_structure"
    return {
        "rules_version": RULES_VERSION,
        "status": status,
        "structures": structures,
        "params": params.as_dict(),
        "data_coverage": {**coverage, "bars": len(prepared), "tick_size": tick, "pivots": len(pivots)},
        "computed_at_ms": computed_at_ms,
    }


def result_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
