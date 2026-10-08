"""Движок «Altcoins D1 accumulation» v2 — поиск и качество (этап 4 ТЗ от
07.10.2026, docs/Altcoins_Range_Engine_Spec_RU_2026_10_07.md).

Отличия от v1 (app/alt/engine.py, не изменяется; R-09: версии не смешиваются):

- R-01: поиск кандидатов продолжается при замороженных/зрелых эпизодах;
  глубокая просадка от ATH — контекст допуска со сроком действия
  cfg.v2_admission_window_days (перевзводится глубокой просадкой НОВОГО
  ATH-эпизода), новый ATH перед каждым эпизодом не требуется;
- R-02: первый минимум — гипотеза, а не постоянная опора: до
  cfg.v2_max_alternatives альтернативных якорей на зону; самостоятельная
  поздняя консолидация (отделена импульсом или разрывом >
  cfg.v2_candidate_split_days без реакций у старых границ) — НОВЫЙ кандидат
  со своими якорями и своим возрастом, сегмент старого закрывается;
- R-03: границы — кластеры ПОВТОРНЫХ подтверждённых реакций (pivots 3+3,
  та же доступность без будущего, что в v1). Допуск кластера
  tol = max(cfg.v2_cluster_atr_mult * ATR(cfg.v2_atr_period),
            cfg.v2_cluster_pct * медианная_цена).
  Один затяжной контакт — ОДНА реакция: независимые реакции разделены
  ≥ cfg.v2_min_reaction_gap_days или возвратом за медианную цену сегмента.
  Линия L/U из кластера — МЕДИАНА подтверждённых pivot-цен (предложение v2,
  подлежит калибровке). Экстремальные тени — отдельно (wick_low/wick_high),
  сами по себе L/U не образуют. Абсолютный High/Low истории и «самый узкий
  интервал» не используются;
- R-04: classify_sideways_v2 — качество независимо от ширины: реакции у
  границ и переходы между ними, устойчивость центра и границ по третям
  (нормировка на ATR, НЕ на собственную ширину), efficiency, ширина в ATR
  (дегенеративно узкие отсекаются; большая ширина и давность реакции
  остаются диагностикой), доля ширины от одиночных теней. Ширина сама
  оценку не улучшает и сжатие внутри базы не отменяет внешнюю рамку;
- зрелость/freeze: пороги v1 (СТРОГО forming_min_days / mature_min_days,
  §5 ТЗ «сначала сохранить текущие 51/101»). На freeze персистится
  AltRangeEpisode (rules_version alt-0.2, state mature, замороженные
  L/U/M/W, тени, quality_json с метриками);
- R-05 (этап 5): уход под замороженный L — отдельный эпизод выноса
  (alt_sweep_episode: начало, минимум, длительность, возврат). Статусы:
  open → return_pending («выход ниже, возврат не подтверждён») → returned
  (возврат подтверждён ЗАКРЫТИЕМ D1 выше L; равенство L не возврат —
  конвенция v1) | accepted_below (≥ cfg.v2_sweep_max_days дней ниже L без
  возврата → распад базы: state decayed, base_end_reason "decay",
  заливка завершается у свечи ухода, а подтверждение — у свечи принятия).
  Вынос НИКОГДА не расширяет замороженную геометрию вниз; принятие ниже L —
  основание для нового кандидата (общий поиск, R-01). Отмена по K = 2L − U
  (формула v1, но по СОБСТВЕННОЙ замороженной геометрии эпизода v2, R-09)
  приоритетна: нарушение K нельзя скрыть переименованием в манипуляцию —
  state terminal, base_end_reason "decay"; K ≤ 0 — отмена недостижима
  (конвенция v1). Режим проверки K — cfg.cancel_mode (как v1);
- R-06 (этап 5): выход — ЗАКРЫТИЕ D1 > U·(1 + cfg.v2_breakout_tol); тень
  выше U — лишь флаг экскурсии (lifecycle upper_excursion), не выход.
  Ретест бывшей верхней зоны [M, U] после выхода — факт того же эпизода;
  он не расширяет U до вершины импульса и не продлевает накопление.
  Структурные события (BOS/SMS/SSL v1 StructureTracker) имеют собственные
  опоры и подтверждение границ базы НЕ заменяют — в v2-расчёт границ и
  выхода они не входят;
- R-07 (этап 5): конец заливки отделён от сопровождения. При подтверждённом
  выходе: base_end_open_time = свеча выхода, base_end_reason
  "breakout_confirmed", base_end_confirmed_at_ms — граница закрытия свечи
  подтверждения (без заднего числа), state → accompaniment. При распаде —
  base_end_reason "decay", state decayed/terminal. accompaniment_end_open_time
  ставится при завершении сопровождения (все цели TP1..TP4 достигнуты,
  отмена по K, принятие ниже L). Повторный вход после завершения базы старую
  заливку НЕ открывает: новая консолидация — новый кандидат (R-01);
- R-09 (этап 5): цели TP_n = U + n·W и K = 2L − U — те же формулы, что у
  v1 (совместимость зафиксирована), но рассчитываются по СОБСТВЕННОЙ
  замороженной геометрии эпизода v2 (W = U − L, M = (L + U)/2). Факты
  жизненного цикла (вынос/возврат/выход/ретест/цели/отмена) хранятся
  структурированно в quality_json.episode.lifecycle и в таблице выносов;
  уведомления этапом 5 НЕ эмитируются (включение — этап 7);
- R-10: только закрытые свечи до as_of; полный replay детерминирован;
  origin_key стабилен (f"v2:{asset_id}:{source_id}:{anchor_start_open_time}")
  — повторный прогон не дублирует эпизоды (INSERT OR IGNORE + апдейты только
  формирующихся строк; замороженная геометрия не перезаписывается, R-09);
  строки выносов дедуплицируются по (episode_id, start_open_time).

Проектные решения этапа (не зафиксированы ТЗ, калибровка — этап 6):
- ATR — SMA последних v2_atr_period истинных диапазонов по закрытым D1;
- внутренние пороги классификатора v2 (константы модуля): дегенеративно
  узкая ширина < 0.5 ATR, сдвиг центра/границ по третям > 4 ATR,
  efficiency > 0.5, концентрация ширины в одной тени > 0.5;
- «импульсный выход» для разделения кандидатов: Close за пределами текущих
  границ ± tol; «давность» — от последней реакции у любой границы/якоря;
- выбор пары L/U среди допустимых — по сбалансированности реакций, их
  числу, переходам и длительности; равные — по времени первых опор
  (устойчиво, без предпочтения узости);
- отклонённый формирующийся кандидат с уже сохранённой строкой переводится
  в state=decayed (распад консолидации до зрелости, без подтверждённого
  выхода); этап 5 владеет полным жизненным циклом post-freeze (см. выше);
  срок релевантности сопровождения хранится в read model (alt_overview),
  экспликитного терминального состояния по его истечении этап 5 не ставит.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from ..config import AltConfig
from ..db import Database
from ..engine.liquidity import PivotRecord, find_pivots
from ..models import ZoneType, close_boundary_ms
from ..models_alt import (
    ALT_RULE_VERSION_V2,
    AltEpisodeState,
    AltRangeEpisode,
    AltState,
    AltSweepEpisode,
    AltSweepState,
)
from .engine import (
    ALT_TIMEFRAME,
    DAY_MS,
    AthTracker,
    _to_engine_candle,
    median,
    range_age_days,
    validate_ohlc,
)

# Внутренние проектные пороги классификатора v2 (калибровка — этап 6, §5 ТЗ).
# Нормировка на ATR, а не на ширину диапазона (R-04).
V2_MIN_WIDTH_ATR_MULT = 0.5      # дегенеративно узкая рамка (< половины ATR)
V2_CENTER_SHIFT_MAX_ATR = 4.0    # сдвиг центра между третями, в ATR
V2_BOUND_SHIFT_MAX_ATR = 4.0     # сдвиг границ между третями, в ATR
V2_MAX_EFFICIENCY = 0.5          # направленность движения внутри сегмента
V2_MAX_WICK_CONCENTRATION = 0.5  # доля ширины от одиночной тени
V2_PAIR_MIN_CONTAINMENT = 0.8    # конверт: мин. доля Close внутри [L,U] (R-03)

FORMING_EPISODE_STATES = (AltEpisodeState.FORMING.value,)


# ---------------------------------------------------------------------------
# Чистые функции: ATR, допуск кластера, кластеры реакций (R-03)
# ---------------------------------------------------------------------------


def compute_atr(candles: Sequence[Any], period: int) -> Optional[float]:
    """ATR по закрытым свечам: SMA последних `period` истинных диапазонов.

    TR_i = max(H−L, |H−C_{i−1}|, |L−C_{i−1}|). Чистая функция; None, если
    свечей меньше period+1 (первый TR требует предыдущего Close).
    """
    if period < 1 or len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    return sum(window) / len(window)


def cluster_tolerance(atr_value: float, median_price: float, cfg: AltConfig) -> float:
    """Допуск кластера/касания (R-03): нормировка на волатильность и шаг цены."""
    return max(cfg.v2_cluster_atr_mult * atr_value,
               cfg.v2_cluster_pct * median_price)


@dataclass
class PivotCluster:
    """Зона повторных реакций одного типа ('low'|'high').

    Линия уровня — МЕДИАНА подтверждённых pivot-цен кластера (R-03,
    предложение v2), а не экстремум: одиночный выброс цену зоны не двигает.
    """

    kind: str                      # 'low' | 'high'
    pivots: list[PivotRecord] = field(default_factory=list)

    @property
    def price(self) -> float:
        return median([p.price for p in self.pivots])

    @property
    def first_formed_at(self) -> int:
        return min(p.formed_at for p in self.pivots)

    @property
    def last_formed_at(self) -> int:
        return max(p.formed_at for p in self.pivots)


def cluster_pivots(pivots: Sequence[PivotRecord], tol: float) -> list[PivotCluster]:
    """Жадная кластеризация pivots одного типа по цене с допуском tol.

    Сортировка по цене; соседние pivots в пределах tol сливаются в одну зону.
    Детерминировано: равные разрывы разрешаются порядком цены, затем formed_at.
    """
    ordered = sorted(pivots, key=lambda p: (p.price, p.formed_at))
    out: list[PivotCluster] = []
    for p in ordered:
        kind = "low" if p.kind == ZoneType.SSL else "high"
        if out and p.price - out[-1].pivots[-1].price <= tol:
            out[-1].pivots.append(p)
        else:
            out.append(PivotCluster(kind=kind, pivots=[p]))
    return out


def count_independent_reactions(
    pivots: Sequence[PivotRecord],
    gap_ms: int,
    candles: Optional[Sequence[Any]] = None,
    midline: Optional[float] = None,
    side: Optional[str] = None,
) -> int:
    """Число НЕЗАВИСИМЫХ реакций кластера (R-03): один затяжной контакт —
    одна реакция. Следующая реакция засчитывается, если после предыдущей
    прошло ≥ gap_ms ИЛИ цена между контактами ушла за midline (возврат через
    середину: для зоны 'low' — Close выше, для 'high' — ниже)."""
    ordered = sorted(pivots, key=lambda p: p.formed_at)
    count = 0
    last: Optional[PivotRecord] = None
    for p in ordered:
        if last is None:
            count, last = 1, p
            continue
        if p.formed_at - last.formed_at >= gap_ms:
            count, last = count + 1, p
            continue
        if candles is not None and midline is not None and side is not None:
            crossed = any(
                (c.close > midline) if side == "low" else (c.close < midline)
                for c in candles
                if last.formed_at < c.open_time < p.formed_at
            )
            if crossed:
                count, last = count + 1, p
    return count


# ---------------------------------------------------------------------------
# R-03: выбор пары границ L/U
# ---------------------------------------------------------------------------


@dataclass
class BoundaryChoice:
    """Пара зон L/U, объясняющая консолидацию: кластеры, линии (медианы),
    числа независимых реакций и переходов Close через середину."""

    lower_cluster: PivotCluster
    upper_cluster: PivotCluster
    lower: float
    upper: float
    reactions_lower: int
    reactions_upper: int
    crossings: int
    span_days: int
    efficiency: float
    containment: float = 0.0


def _count_mid_crossings(closes: Sequence[float], mid: float) -> int:
    """Переходы Close через середину диапазона (возвраты между зонами, R-03)."""
    crossings = 0
    prev_side: Optional[int] = None
    for c in closes:
        if c > mid:
            side = 1
        elif c < mid:
            side = -1
        else:
            continue
        if prev_side is not None and side != prev_side:
            crossings += 1
        prev_side = side
    return crossings


def _efficiency(closes: Sequence[float]) -> float:
    """Направленность: |net| / path; 0 при нулевом знаменателе."""
    if len(closes) < 2:
        return 0.0
    denom = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / denom if denom > 0 else 0.0


def _containment(closes: Sequence[float], lower: float, upper: float) -> float:
    """Доля Close внутри [lower, upper]: насколько пара границ объясняет
    сегмент (конверт консолидации). Границы — из кластеров реакций, не
    min/max того же набора (тавтология R-04 не применима)."""
    if not closes:
        return 0.0
    return sum(1 for c in closes if lower <= c <= upper) / len(closes)


def choose_lu_pair(
    low_clusters: Sequence[PivotCluster],
    high_clusters: Sequence[PivotCluster],
    candles: Sequence[Any],
    cfg: AltConfig,
    atr_value: float,
) -> Optional[BoundaryChoice]:
    """Пара кластеров L/U, лучше всего объясняющая устойчивую консолидацию
    (R-03). Чистая функция.

    Требования к паре: ≥ cfg.v2_min_reactions независимых реакций у ОБЕИХ
    границ; возвраты между зонами (≥1 переход через середину); без
    выраженного направленного движения внутри (efficiency ≤ V2_MAX_EFFICIENCY).
    Абсолютный High/Low истории и «самый узкий интервал» не выбираются:
    ранжирование — по сбалансированности и числу реакций, переходам и
    длительности; равные — по времени первых опор (детерминированно).
    """
    if atr_value <= 0 or not candles:
        return None
    closes = [c.close for c in candles]
    median_price = median(closes)
    gap_ms = cfg.v2_min_reaction_gap_days * DAY_MS

    def reactions(cluster: PivotCluster) -> int:
        return count_independent_reactions(
            cluster.pivots, gap_ms, candles=candles,
            midline=median_price, side=cluster.kind,
        )

    lows = [c for c in low_clusters
            if reactions(c) >= cfg.v2_min_reactions]
    highs = [c for c in high_clusters
             if reactions(c) >= cfg.v2_min_reactions]

    best: Optional[BoundaryChoice] = None
    best_key: Optional[tuple] = None
    for lc in lows:
        for uc in highs:
            if lc.price >= uc.price:
                continue
            width = uc.price - lc.price
            mid = (lc.price + uc.price) / 2
            start_ot = min(lc.first_formed_at, uc.first_formed_at)
            span = [c for c in candles if c.open_time >= start_ot]
            span_closes = [c.close for c in span]
            crossings = _count_mid_crossings(span_closes, mid)
            if crossings < 1:
                continue
            eff = _efficiency(span_closes)
            if eff > V2_MAX_EFFICIENCY:
                continue
            # Конверт: границы по опорам должны ОБЪЯСНЯТЬ консолидацию —
            # поддиапазоны, отсекающие реальные Close базы (узкая рамка
            # ради узкой), проигрывают паре-конверту (R-03). Доля Close
            # внутри [L,U] по границам из кластеров реакций — не min/max
            # тех же свечей, тавтологии R-04 здесь нет.
            containment = _containment(closes, lc.price, uc.price)
            if containment < V2_PAIR_MIN_CONTAINMENT:
                continue
            rl, ru = reactions(lc), reactions(uc)
            span_days = max(1, int(
                (candles[-1].open_time - start_ot) // DAY_MS
            ))
            choice = BoundaryChoice(
                lower_cluster=lc, upper_cluster=uc,
                lower=lc.price, upper=uc.price,
                reactions_lower=rl, reactions_upper=ru,
                crossings=crossings, span_days=span_days, efficiency=eff,
                containment=containment,
            )
            # Сначала полнота объяснения (конверт). При равном охвате
            # закрытий побеждает более широкая пара зон, а не самый
            # густой внутренний кластер: узость не преимущество (R-03/R-04).
            # Число реакций — только тай-брейк после конверта. Равные пары
            # устойчиво различаются временем первых опор.
            key = (
                round(containment, 2),
                width,
                min(rl, ru), rl + ru, crossings, span_days,
                -lc.first_formed_at, -uc.first_formed_at,
            )
            if best_key is None or key > best_key:
                best, best_key = choice, key
    return best


# ---------------------------------------------------------------------------
# R-04: классификатор боковика v2 (качество независимо от ширины)
# ---------------------------------------------------------------------------


@dataclass
class ClassifierResultV2:
    """Результат classify_sideways_v2. Метрики считаются всегда, когда
    хватает данных; вердикт sideways — только при ready. Ширина сама по себе
    не улучшает оценку (R-04)."""

    ready: bool
    sideways: bool
    reason: str                       # ok | not_enough_data | zero_width | zero_atr
    failed_conditions: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    classifier_version: str = "v2"


def _band_reaction_times(
    candles: Sequence[Any], bound: float, side: str, tol: float
) -> list[int]:
    """Open_time свечей касания зоны границы: 'low' — Low в [bound−tol,
    bound+tol], 'high' — High в [bound−tol, bound+tol]. Тень глубоко под
    границей (вынос) — не касание зоны."""
    out: list[int] = []
    for c in candles:
        price = c.low if side == "low" else c.high
        if bound - tol <= price <= bound + tol:
            out.append(c.open_time)
    return out


def _count_reactions_from_times(
    times: Sequence[int],
    gap_ms: int,
    candles: Sequence[Any],
    mid: float,
    side: str,
) -> int:
    """Независимые реакции по временам касаний: разрыв ≥ gap_ms или возврат
    Close за середину между касаниями (один затяжной контакт — одна реакция)."""
    count = 0
    last: Optional[int] = None
    for ts in times:
        if last is None:
            count, last = 1, ts
            continue
        if ts - last >= gap_ms:
            count, last = count + 1, ts
            continue
        crossed = any(
            (c.close > mid) if side == "low" else (c.close < mid)
            for c in candles
            if last < c.open_time < ts
        )
        if crossed:
            count, last = count + 1, ts
    return count


def classify_sideways_v2(
    candles: Sequence[Any],
    lower: float,
    upper: float,
    cfg: AltConfig,
) -> ClassifierResultV2:
    """Классификатор боковика v2 (R-04). Чистая функция.

    candles — свечи участка (порядок = время), lower/upper — границы,
    полученные по кластерам реакций (НЕ по min/max того же набора свечей).

    Проверки (все нормировки на ATR или на абсолютные величины, не на
    собственную ширину): независимые реакции у каждой границы и переходы
    между ними; устойчивость центра и границ по третям (в ATR);
    efficiency; ширина в ATR (дегенеративно узкие отсекаются, большая
    ширина остаётся диагностикой); доля ширины от одиночной тени; давность
    последней реакции как диагностический признак.
    """
    n = len(candles)
    metrics: dict[str, Any] = {}
    if n < cfg.v2_atr_period + 1:
        return ClassifierResultV2(False, False, "not_enough_data",
                                  metrics={"n_candles": n})
    width = upper - lower
    if width <= 0 or not math.isfinite(width):
        return ClassifierResultV2(False, False, "zero_width",
                                  metrics={"n_candles": n})
    atr_value = compute_atr(candles, cfg.v2_atr_period)
    if atr_value is None or atr_value <= 0:
        return ClassifierResultV2(False, False, "zero_atr",
                                  metrics={"n_candles": n})

    closes = [c.close for c in candles]
    mid = (lower + upper) / 2
    median_price = median(closes)
    tol = cluster_tolerance(atr_value, median_price, cfg)
    gap_ms = cfg.v2_min_reaction_gap_days * DAY_MS

    # Реакции у границ и переходы между ними
    times_l = _band_reaction_times(candles, lower, "low", tol)
    times_u = _band_reaction_times(candles, upper, "high", tol)
    reactions_lower = _count_reactions_from_times(times_l, gap_ms, candles, mid, "low")
    reactions_upper = _count_reactions_from_times(times_u, gap_ms, candles, mid, "high")
    crossings = _count_mid_crossings(closes, mid)

    # Устойчивость центра и границ по третям (нормировка на ATR, R-04)
    k = n // 3
    thirds = [candles[:k], candles[k:2 * k], candles[2 * k:]] if k >= 1 else []
    centers = [median([c.close for c in t]) for t in thirds if t]
    center_shift_atr = (
        (max(centers) - min(centers)) / atr_value if len(centers) >= 2 else 0.0
    )
    bound_shift_atr = 0.0
    if thirds and all(thirds):
        # тени, усечённые границами: вынос за [L,U] — отдельный факт,
        # а не нестабильность границы (R-03)
        low_shifts = [
            abs(min(max(c.low, lower) for c in t) - lower) for t in thirds
        ]
        high_shifts = [
            abs(max(min(c.high, upper) for c in t) - upper) for t in thirds
        ]
        bound_shift_atr = max(low_shifts + high_shifts) / atr_value

    # Направленность, ширина, тени, давность реакций
    eff = _efficiency(closes)
    width_atr_mult = width / atr_value
    width_pct = width / median_price if median_price > 0 else math.inf
    # Доля ширины от одиночной тени — только ВНУТРИ границ: тень за
    # пределами [L,U] — отдельный факт (R-03, wick_low/wick_high), на
    # качество основной зоны не влияет; ширину, созданную одиночным
    # выбросом внутри границ, считаем по теням, усечённым границами
    clipped_lows = sorted({max(c.low, lower) for c in candles})
    clipped_highs = sorted({min(c.high, upper) for c in candles}, reverse=True)
    seg_l, seg_u = clipped_lows[0], clipped_highs[0]
    second_low = clipped_lows[1] if len(clipped_lows) > 1 else seg_l
    second_high = clipped_highs[1] if len(clipped_highs) > 1 else seg_u
    wick_concentration = max(
        second_low - seg_l, seg_u - second_high, 0.0
    ) / width
    last_touch = max(times_l[-1] if times_l else 0,
                     times_u[-1] if times_u else 0)
    days_since_last_reaction = int(
        (candles[-1].open_time - last_touch) // DAY_MS
    ) if last_touch else n

    metrics.update({
        "n_candles": n,
        "atr": atr_value,
        "tolerance": tol,
        "median_price": median_price,
        "reactions_lower": reactions_lower,
        "reactions_upper": reactions_upper,
        "crossings": crossings,
        "center_shift_atr": center_shift_atr,
        "bound_shift_atr": bound_shift_atr,
        "efficiency": eff,
        "width_atr_mult": width_atr_mult,
        "width_pct": width_pct,
        "wick_concentration": wick_concentration,
        "days_since_last_reaction": days_since_last_reaction,
        # Зрелая внешняя рамка может оставаться структурно значимой после
        # сжатия цены у L: ширина и давность теперь диагностика, не veto.
        "width_warning": width_atr_mult > cfg.v2_max_width_atr_mult,
        "stale_reactions_warning": (
            days_since_last_reaction > cfg.v2_max_days_since_reaction
        ),
    })

    failed: list[str] = []
    if reactions_lower < cfg.v2_min_reactions:
        failed.append("reactions_lower")
    if reactions_upper < cfg.v2_min_reactions:
        failed.append("reactions_upper")
    if crossings < 1:
        failed.append("crossings")
    if center_shift_atr > V2_CENTER_SHIFT_MAX_ATR:
        failed.append("center_shift")
    if bound_shift_atr > V2_BOUND_SHIFT_MAX_ATR:
        failed.append("bound_shift")
    if eff > V2_MAX_EFFICIENCY:
        failed.append("efficiency")
    # Широкая рамка допустима только когда её внешние стороны действительно
    # подтверждены. Пустой огромный конверт по-прежнему отбрасывается.
    if (width_atr_mult > cfg.v2_max_width_atr_mult
            and (reactions_lower < cfg.v2_min_reactions
                 or reactions_upper < cfg.v2_min_reactions)):
        failed.append("width_too_wide")
    if width_atr_mult < V2_MIN_WIDTH_ATR_MULT:
        failed.append("width_too_narrow")
    if wick_concentration > V2_MAX_WICK_CONCENTRATION:
        failed.append("wick_concentration")

    return ClassifierResultV2(
        ready=True,
        sideways=not failed,
        reason="ok",
        failed_conditions=tuple(failed),
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Оркестратор v2
# ---------------------------------------------------------------------------


@dataclass
class _LifecycleState:
    """Пост-freeze состояние эпизода v2 внутри одного replay (R-05..R-07).

    Пересчитывается из свечей при каждом прогоне (как _MatureState v1);
    в БД пишется идемпотентно. Цели TP_n = U + n·W и K = 2L − U — формулы
    v1 по собственной замороженной геометрии эпизода (R-09).
    """

    k_price: Optional[float] = None        # K = 2L − U (отмена, R-05)
    cancel_reachable: bool = True          # K > 0 (конвенция v1)
    breakout_open_time: Optional[int] = None
    breakout_close: Optional[float] = None
    targets: list[float] = field(default_factory=list)  # снимок при выходе
    targets_hit: set[int] = field(default_factory=set)
    retest_done: bool = False
    upper_excursion: bool = False
    # открытый вынос вниз (R-05)
    sweep_id: Optional[int] = None
    sweep_start_ot: Optional[int] = None
    sweep_min: float = math.inf
    sweep_min_ot: Optional[int] = None
    sweep_days: int = 0
    terminal: bool = False
    terminal_reason: Optional[str] = None  # cancelled | accepted_below | targets_completed


@dataclass
class _CandidateV2:
    """Независимое состояние кандидата v2 внутри одного replay (R-01/R-02).

    Якорь-старт — подтверждённый pivot low при действующем допуске;
    возраст и границы свои, возраст ранней базы на позднюю не переносится.
    """

    cid: int
    origin_key: str
    start: PivotRecord
    start_idx: int
    state: str = "searching"          # searching | forming | mature | accompaniment | terminal | decayed | rejected
    pivots: list[PivotRecord] = field(default_factory=list)
    alternatives: list[int] = field(default_factory=list)  # formed_at опор (R-02)
    segment_end_idx: Optional[int] = None  # сегмент закрыт новым кандидатом
    lower: Optional[float] = None
    upper: Optional[float] = None
    pair: Optional[BoundaryChoice] = None
    pair_changed_idx: Optional[int] = None  # свеча последней СУЩЕСТВЕННОЙ
                                            # смены пары L/U (стабилизация freeze)
    classifier: Optional[ClassifierResultV2] = None
    n_days: int = 0
    wick_low: float = math.inf
    wick_high: float = 0.0
    last_reaction_ms: Optional[int] = None
    frozen: bool = False
    rejected_reason: Optional[str] = None
    episode_id: Optional[int] = None
    ath_price: Optional[float] = None
    deep_drop_drawdown: Optional[float] = None
    # пост-freeze жизненный цикл (R-05..R-07)
    lstate: Optional[_LifecycleState] = None
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    episode_terminal: bool = False


class AltEngineV2:
    """Оркестратор v2 (этапы 4–5): replay закрытых D1 одного актива с
    мультикандидатным поиском, персистенцией эпизодов v2 и пост-freeze
    жизненным циклом (выносы/выход/ретест/цели/отмена, R-05..R-07).

    Дисциплина v1 сохранена: сортировка/валидация/дедуп свечей, только
    закрытые свечи до as_of, pivot доступен после закрытия правых
    подтверждающих (formed_at — время опоры, confirmed_at — доступности),
    полный replay детерминирован, записи идемпотентны по origin_key (R-10).

    Пост-freeze шаг (_lifecycle_step) идёт ПАРАЛЛЕЛЬНО с продолжающимся
    поиском новых кандидатов (R-01): зрелый/сопровождаемый эпизод не
    блокирует следующий цикл; терминальный/распавшийся эпизод не воскресает
    и не блокирует новые консолидации у своих цен. Уведомления не
    эмитируются (этап 7); факты — в quality_json.episode.lifecycle и
    alt_sweep_episode.
    """

    def __init__(self, db: Database, cfg: AltConfig):
        self.db = db
        self.cfg = cfg
        self._pivot_cfg = SimpleNamespace(
            pivot_left=cfg.pivot_left, pivot_right=cfg.pivot_right
        )

    # ----- публичный API -----

    def process_asset_history(
        self,
        asset_id: int,
        source_id: int,
        candles: Sequence[Any],
        detected_at_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Replay закрытых D1 (любой порядок на входе — сортируем, дедуп).

        Возвращает summary: агрегированное состояние, ATH-эпизод, кандидаты
        (с причинами отклонения, R-02), персистенные эпизоды v2 с флагом
        created (идемпотентность, R-10), ошибки данных.
        """
        ordered = sorted(candles, key=lambda c: c.open_time)
        valid: list[Any] = []
        data_errors: list[dict[str, Any]] = []
        seen_open_times: set[int] = set()
        for c in ordered:
            reason = validate_ohlc(c.open, c.high, c.low, c.close)
            if reason is None and c.open_time in seen_open_times:
                reason = "duplicate_open_time"
            if reason is not None:
                data_errors.append({"open_time": c.open_time, "reason": reason})
                continue
            seen_open_times.add(c.open_time)
            valid.append(c)

        min_candles = self.cfg.pivot_left + self.cfg.pivot_right + 1
        scope = "full"
        src = self.db.get_alt_instrument_source(asset_id)
        if src is not None and src.id == source_id:
            scope = src.history_scope
        if not valid or len(valid) < min_candles or scope == "partial":
            summary = {
                "asset_id": asset_id, "source_id": source_id,
                "rules_version": ALT_RULE_VERSION_V2,
                "state": AltState.DATA_PENDING.value,
                "data_pending": True, "history_scope": scope,
                "candles_total": len(ordered), "candles_valid": len(valid),
                "data_errors": data_errors,
            }
            self._save_progress(asset_id, source_id, valid, summary)
            return summary

        detected_at = (
            detected_at_ms
            if detected_at_ms is not None
            else close_boundary_ms(valid[-1].open_time, ALT_TIMEFRAME)
        )

        tracker = AthTracker(self.cfg)
        candidates: list[_CandidateV2] = []
        known_pivots: set[tuple[str, int]] = set()
        episodes_log: list[dict[str, Any]] = []
        next_cid = 1

        for t, c in enumerate(valid):
            ep = tracker.update(c.open_time, c.high, c.low)
            atr_now = compute_atr(valid[max(0, t - self.cfg.v2_atr_period): t + 1],
                                  self.cfg.v2_atr_period)
            med_now = median([x.close for x in
                              valid[max(0, t - self.cfg.v2_atr_period): t + 1]])
            tol_now = (
                cluster_tolerance(atr_now, med_now, self.cfg)
                if atr_now is not None else None
            )

            # 1. живые и зрелые кандидаты: касания границ (давность реакций)
            for cand in candidates:
                if cand.rejected_reason is not None:
                    continue
                self._track_reaction(cand, c, tol_now)

            # 2. свежеподтверждённые pivots: в сегменты живых кандидатов;
            #    pivot low — альтернативная опора или новый кандидат (R-01/R-02)
            new_pivots = self._newly_confirmed_pivots(valid, t, known_pivots)
            for p in new_pivots:
                for cand in candidates:
                    if (
                        not cand.frozen
                        and cand.rejected_reason is None
                        and p.formed_at >= cand.start.formed_at
                        and self._segment_open(cand, t)
                    ):
                        cand.pivots.append(p)
                if p.kind == ZoneType.SSL:
                    spawned = self._handle_pivot_low(
                        p, valid, tracker, candidates, tol_now,
                        asset_id, source_id, next_cid,
                    )
                    if spawned is not None:
                        candidates.append(spawned)
                        next_cid += 1
                        if ep.ath_price is not None:
                            spawned.ath_price = ep.ath_price
                            spawned.deep_drop_drawdown = ep.drawdown

            # 3. живые кандидаты: формирование/зрелость; зрелые — пост-freeze
            #    жизненный цикл ПАРАЛЛЕЛЬНО продолжающемуся поиску (R-01)
            for cand in candidates:
                if cand.rejected_reason is not None:
                    continue
                if cand.frozen:
                    self._lifecycle_step(cand, valid, t)
                    continue
                self._candidate_step(
                    cand, candidates, valid, t, asset_id, source_id, detected_at,
                    episodes_log,
                )

        state = self._aggregate_state(candidates)
        summary = {
            "asset_id": asset_id,
            "source_id": source_id,
            "rules_version": ALT_RULE_VERSION_V2,
            "state": state,
            "data_pending": False,
            "history_scope": scope,
            "candles_total": len(ordered),
            "candles_valid": len(valid),
            "data_errors": data_errors,
            "ath": self._ath_summary(tracker),
            "candidates": [self._candidate_summary(cd) for cd in candidates],
            "episodes": episodes_log,
        }
        self._save_progress(asset_id, source_id, valid, summary)
        return summary

    # ----- pivots (та же доступность без будущего, что в v1) -----

    def _newly_confirmed_pivots(
        self, candles: Sequence[Any], t: int, known: set[tuple[str, int]]
    ) -> list[PivotRecord]:
        """Pivots, чьё подтверждение стало доступно ровно на свече t
        (окно left+1+right, без будущих свечей; formed_at — время опоры,
        confirmed_at — время доступности, R-10)."""
        left, right = self.cfg.pivot_left, self.cfg.pivot_right
        if t < left + right:
            return []
        window = [
            _to_engine_candle(c) for c in candles[t - left - right: t + 1]
        ]
        out: list[PivotRecord] = []
        for p in find_pivots(window, ALT_TIMEFRAME, self._pivot_cfg):
            key = (p.kind.value, p.formed_at)
            if key not in known:
                known.add(key)
                out.append(p)
        return out

    # ----- R-01/R-02: допуск и мультикандидатный поиск -----

    def _admission_valid(self, tracker: AthTracker, formed_at: int) -> bool:
        """Допуск (R-01): pivot low ПОСЛЕ глубокой просадки какого-либо
        ATH-эпизода и в пределах cfg.v2_admission_window_days от неё.
        Новый ATH-эпизод с его просадкой перевзводит допуск."""
        window_ms = self.cfg.v2_admission_window_days * DAY_MS
        for ep in [*tracker.history, tracker.current]:
            if ep is None or ep.deep_drop_open_time is None:
                continue
            d = ep.deep_drop_open_time
            if d < formed_at and formed_at - d <= window_ms:
                return True
        return False

    def _segment_open(self, cand: _CandidateV2, t: int) -> bool:
        return cand.segment_end_idx is None or t < cand.segment_end_idx

    def _separated(
        self,
        cand: _CandidateV2,
        pivot: PivotRecord,
        pivot_idx: int,
        candles: Sequence[Any],
        tol: Optional[float],
    ) -> bool:
        """Самостоятельность поздней консолидации (R-02), оцениваемая на
        момент САМОГО pivot (formed_at): разрыв >
        cfg.v2_candidate_split_days без реакций у границ старого кандидата
        ИЛИ импульсный выход — ≥3 Close за пределами текущих границ ± tol
        (одиночный ложный вынос консолидацию не разделяет)."""
        last = cand.last_reaction_ms or cand.start.formed_at
        if pivot.formed_at - last > self.cfg.v2_candidate_split_days * DAY_MS:
            return True
        if tol is not None and cand.lower is not None and cand.upper is not None:
            seg = candles[cand.start_idx: pivot_idx + 1]
            beyond_up = sum(1 for x in seg if x.close > cand.upper + tol)
            beyond_dn = sum(1 for x in seg if x.close < cand.lower - tol)
            if max(beyond_up, beyond_dn) >= 3:
                return True
        return False

    def _handle_pivot_low(
        self,
        p: PivotRecord,
        candles: Sequence[Any],
        tracker: AthTracker,
        candidates: list[_CandidateV2],
        tol_now: Optional[float],
        asset_id: int,
        source_id: int,
        cid: int,
    ) -> Optional[_CandidateV2]:
        """Pivot low при действующем допуске — альтернативная опора живого
        кандидата (R-02, до cfg.v2_max_alternatives) или НОВЫЙ кандидат.

        Не порождает дубликат внутри/вблизи базы существующего кандидата,
        если новая опора не отделена импульсом или временем (R-02). При
        порождении отделённого кандидата сегмент старого закрывается и
        оценивается финально — возраст ранней базы на позднюю не переносится.
        """
        covering: list[tuple[_CandidateV2, bool]] = []  # (кандидат, separated)
        p_idx = next(
            i for i, c in enumerate(candles) if c.open_time == p.formed_at
        )
        for cand in candidates:
            if (
                cand.rejected_reason is not None
                or cand.episode_terminal      # завершённый эпизод не блокирует
                or p.formed_at <= cand.start.formed_at  # новые консолидации (R-01)
            ):
                continue
            lo = cand.lower if cand.lower is not None else cand.start.price
            hi = cand.upper if cand.upper is not None else lo
            tol = tol_now or 0.0
            near_lower = abs(p.price - lo) <= tol
            in_band = (lo - tol) <= p.price <= (hi + tol)
            if not (near_lower or in_band):
                continue
            sep = self._separated(cand, p, p_idx, candles, tol_now)
            covering.append((cand, sep))
            if not sep and not cand.frozen and near_lower:
                if (
                    len(cand.alternatives) < self.cfg.v2_max_alternatives
                    and p.formed_at not in cand.alternatives
                ):
                    cand.alternatives.append(p.formed_at)

        # Отделённая новая консолидация закрывает сегменты старых кандидатов:
        # у импульсного разделения — у первой свечи принятия за границей,
        # иначе у якоря нового кандидата (R-02: возраст не переносится)
        if covering and all(sep for _c, sep in covering):
            for cand, _sep in covering:
                if not cand.frozen and cand.segment_end_idx is None:
                    end = p_idx
                    if (
                        cand.lower is not None and cand.upper is not None
                        and tol_now is not None
                    ):
                        for j in range(cand.start_idx + 1, p_idx + 1):
                            x = candles[j]
                            if (x.close > cand.upper + tol_now
                                    or x.close < cand.lower - tol_now):
                                end = j
                                break
                    if end > cand.start_idx:
                        cand.segment_end_idx = end
        elif covering:
            return None  # опора внутри/вблизи живой базы — дубликат

        if not self._admission_valid(tracker, p.formed_at):
            return None
        live = sum(1 for c in candidates if c.rejected_reason is None)
        if live >= self.cfg.v2_max_candidates:
            return None
        anchor = candles[p_idx]
        return _CandidateV2(
            cid=cid,
            origin_key=f"v2:{asset_id}:{source_id}:{p.formed_at}",
            start=p,
            start_idx=p_idx,
            wick_low=anchor.low,
            wick_high=anchor.high,
            last_reaction_ms=p.formed_at,
        )

    # ----- шаг кандидата: границы по реакциям, качество, зрелость -----

    def _candidate_step(
        self,
        cand: _CandidateV2,
        candidates: list[_CandidateV2],
        candles: Sequence[Any],
        t: int,
        asset_id: int,
        source_id: int,
        detected_at: int,
        episodes_log: list[dict[str, Any]],
    ) -> None:
        """Возраст, кластеры реакций, пара L/U, классификатор v2, freeze.

        При закрытом сегменте (новый отделённый кандидат) — одна финальная
        оценка на свече закрытия; зрелость без заднего числа: freeze на
        текущей закрытой свече."""
        end_idx = (
            min(cand.segment_end_idx - 1, t)
            if cand.segment_end_idx is not None else t
        )
        if end_idx <= cand.start_idx:
            if cand.segment_end_idx is not None:
                self._reject(cand, "segment_too_short", detected_at)
            return
        last = candles[end_idx]
        cand.n_days = range_age_days(cand.start.formed_at, last.open_time)
        if cand.n_days <= self.cfg.forming_min_days:
            if cand.segment_end_idx is not None:
                self._reject(cand, "segment_closed_premature", detected_at)
            return

        seg = candles[cand.start_idx: end_idx + 1]
        seg_end_ot = last.open_time
        cand.wick_low = min(x.low for x in seg)
        cand.wick_high = max(x.high for x in seg)
        atr_seg = compute_atr(seg, self.cfg.v2_atr_period)
        if atr_seg is None or atr_seg <= 0:
            if cand.segment_end_idx is not None:
                self._reject(cand, "not_enough_data", detected_at)
            return
        med_price = median([x.close for x in seg])
        tol = cluster_tolerance(atr_seg, med_price, self.cfg)
        seg_pivots = [p for p in cand.pivots if p.formed_at <= seg_end_ot]
        lows = cluster_pivots(
            [p for p in seg_pivots if p.kind == ZoneType.SSL], tol
        )
        highs = cluster_pivots(
            [p for p in seg_pivots if p.kind == ZoneType.BSL], tol
        )
        pair = choose_lu_pair(lows, highs, seg, self.cfg, atr_seg)
        if pair is None:
            if cand.segment_end_idx is not None:
                self._reject(cand, "no_reaction_pair", detected_at)
            elif self._stale(cand, candles[t].open_time):
                self._reject(cand, "stale_reactions", detected_at)
            return
        # Стабилизация freeze (§5): первая признанная пара не запускает
        # ожидание — зрелость наступает на первом дне строго после
        # mature_min_days (101 при пороге 100). Ожидание
        # cfg.v2_freeze_stable_days включается только при РАСШИРЕНИИ уже
        # выбранного конверта дальше допуска кластера: поздняя реакция,
        # которая реально раздвигает L/U, не обрезается freeze в тот же
        # день. Сужение, сдвиг внутри допуска и мелкий дрейф медианы часы
        # не сбрасывают — иначе одиночная тень перетягивает границу.
        if cand.lower is not None and (
            pair.lower < cand.lower - tol
            or pair.upper > cand.upper + tol
        ):
            cand.pair_changed_idx = t
        cand.pair = pair
        cand.lower, cand.upper = pair.lower, pair.upper

        res = classify_sideways_v2(seg, pair.lower, pair.upper, self.cfg)
        cand.classifier = res
        if not (res.ready and res.sideways):
            if cand.segment_end_idx is not None:
                self._reject(
                    cand,
                    "quality:" + ",".join(res.failed_conditions or (res.reason,)),
                    detected_at,
                )
            elif "stale_reactions" in res.failed_conditions:
                self._reject(cand, "stale_reactions", detected_at)
            return

        dup = self._duplicate_of(cand, candidates, candles, t, tol)
        if dup is not None:
            self._reject(cand, f"duplicate_of:{dup}", detected_at)
            return

        if cand.n_days <= self.cfg.mature_min_days:
            # формирующийся эпизод: строка появляется с распознаванием
            # боковика > forming_min_days (state forming, геометрия живая)
            self._persist_forming(cand, seg, asset_id, source_id, detected_at,
                                  episodes_log)
            if cand.segment_end_idx is not None:
                self._reject(cand, "segment_closed_premature", detected_at)
            return

        if cand.segment_end_idx is None and (
            cand.pair_changed_idx is not None
            and t - cand.pair_changed_idx <= self.cfg.v2_freeze_stable_days
        ):
            # Пара ещё расширяется: геометрию формирующейся строки обновляем,
            # freeze — после окна стабильности, без заднего числа.
            self._persist_forming(
                cand, seg, asset_id, source_id, detected_at, episodes_log
            )
            return

        recognized_at = close_boundary_ms(candles[t].open_time, ALT_TIMEFRAME)
        self._freeze(cand, seg, asset_id, source_id, recognized_at,
                     detected_at, episodes_log)

    def _stale(self, cand: _CandidateV2, now_open_time: int) -> bool:
        last = cand.last_reaction_ms or cand.start.formed_at
        return (
            now_open_time - last
            > self.cfg.v2_max_days_since_reaction * DAY_MS
        )

    def _duplicate_of(
        self,
        cand: _CandidateV2,
        candidates: list[_CandidateV2],
        candles: Sequence[Any],
        t: int,
        tol: float,
    ) -> Optional[str]:
        """Дубликат уже сохранённого эпизода (R-02/R-10): совпадение границ
        в пределах допуска и пересечение сегментов по времени. Побеждает
        ранее сохранённый (меньший cid — устойчивый порядок); повторное
        использование старых цен в ДРУГУЮ эпоху (сегменты не пересекаются)
        дубликатом не является (R-01)."""
        assert cand.lower is not None and cand.upper is not None
        a_start = cand.start.formed_at
        a_end = (
            candles[min(cand.segment_end_idx, t)].open_time
            if cand.segment_end_idx is not None else None
        )
        for other in candidates:
            if (
                other is cand or other.rejected_reason is not None
                or other.episode_terminal  # завершённая эпоха — не дубликат (R-01)
                or other.episode_id is None
                or other.lower is None or other.upper is None
            ):
                continue
            if (abs(other.lower - cand.lower) > tol
                    or abs(other.upper - cand.upper) > tol):
                continue
            b_start = other.start.formed_at
            b_end = (
                candles[other.segment_end_idx].open_time
                if other.segment_end_idx is not None else None
            )
            if a_end is not None and b_start > a_end:
                continue
            if b_end is not None and a_start > b_end:
                continue
            return other.origin_key
        return None

    # ----- персистенция (идемпотентно, R-09/R-10) -----

    def _episode_row(
        self,
        cand: _CandidateV2,
        state: str,
        asset_id: int,
        source_id: int,
        detected_at: int,
    ) -> AltRangeEpisode:
        assert cand.lower is not None and cand.upper is not None
        return AltRangeEpisode(
            id=None,
            asset_id=asset_id,
            source_id=source_id,
            origin_key=cand.origin_key,
            rules_version=ALT_RULE_VERSION_V2,
            state=state,
            anchor_start_open_time=cand.start.formed_at,
            base_start_open_time=cand.start.formed_at,
            lower=cand.lower,
            upper=cand.upper,
            width=cand.upper - cand.lower,
            mid=(cand.lower + cand.upper) / 2,
            wick_low=cand.wick_low if math.isfinite(cand.wick_low) else None,
            wick_high=cand.wick_high if cand.wick_high > 0 else None,
            quality_json=json.dumps(self._quality(cand), ensure_ascii=False),
            selection_rank_reason=(
                "mature_quality_ok"
                if state == AltEpisodeState.MATURE.value
                else "forming_provisional"
            ),
            detected_at_ms=detected_at,
            created_ms=detected_at,
            updated_ms=detected_at,
        )

    def _quality(self, cand: _CandidateV2) -> dict[str, Any]:
        """Метрики качества эпизода (R-04) + объяснение выбора границ и
        отклонения альтернатив (R-02/R-03) — для read model и калибровки."""
        q: dict[str, Any] = {
            "n_days": cand.n_days,
            "alternatives": list(cand.alternatives),
            "ath_price": cand.ath_price,
            "deep_drop_drawdown": cand.deep_drop_drawdown,
        }
        if cand.pair is not None:
            p = cand.pair
            q["pair"] = {
                "reactions_lower": p.reactions_lower,
                "reactions_upper": p.reactions_upper,
                "crossings": p.crossings,
                "span_days": p.span_days,
                "efficiency": p.efficiency,
                "containment": p.containment,
                "lower_cluster_pivots": [
                    {"formed_at": x.formed_at, "price": x.price}
                    for x in p.lower_cluster.pivots
                ],
                "upper_cluster_pivots": [
                    {"formed_at": x.formed_at, "price": x.price}
                    for x in p.upper_cluster.pivots
                ],
            }
        if cand.classifier is not None:
            r = cand.classifier
            q["classifier"] = {
                "ready": r.ready, "sideways": r.sideways, "reason": r.reason,
                "failed_conditions": list(r.failed_conditions),
                "metrics": r.metrics,
                "classifier_version": r.classifier_version,
            }
        if cand.rejected_reason is not None:
            q["rejected_reason"] = cand.rejected_reason
        if cand.lstate is not None:
            ls = cand.lstate
            q["lifecycle_state"] = {
                "k_price": ls.k_price,               # K = 2L − U (формула v1)
                "cancel_reachable": ls.cancel_reachable,
                "breakout_open_time": ls.breakout_open_time,
                "breakout_close": ls.breakout_close,
                "targets": [
                    {"tp": n, "price": tp, "hit": n in ls.targets_hit}
                    for n, tp in enumerate(ls.targets, 1)
                ],
                "retest_done": ls.retest_done,
                "upper_excursion": ls.upper_excursion,
                "terminal_reason": ls.terminal_reason,
            }
            q["lifecycle"] = list(cand.lifecycle)
        return q

    def _persist_forming(
        self,
        cand: _CandidateV2,
        seg: Sequence[Any],
        asset_id: int,
        source_id: int,
        detected_at: int,
        episodes_log: list[dict[str, Any]],
    ) -> None:
        """Формирующийся эпизод: INSERT OR IGNORE по origin_key; геометрия
        обновляется, только пока строка в state=forming (замороженная и
        распавшаяся история не перезаписывается, R-09/R-10)."""
        cand.state = "forming"
        row = self._episode_row(
            cand, AltEpisodeState.FORMING.value, asset_id, source_id, detected_at
        )
        row, created = self.db.insert_alt_range_episode(row)
        cand.episode_id = row.id
        if created:
            episodes_log.append({
                "origin_key": cand.origin_key, "episode_id": row.id,
                "state": row.state, "created": True,
            })
        elif row.state in FORMING_EPISODE_STATES:
            self.db.update_alt_range_episode(
                row.id,
                lower=cand.lower, upper=cand.upper,
                width=cand.upper - cand.lower,
                mid=(cand.lower + cand.upper) / 2,
                wick_low=cand.wick_low if math.isfinite(cand.wick_low) else None,
                wick_high=cand.wick_high if cand.wick_high > 0 else None,
                quality_json=json.dumps(self._quality(cand), ensure_ascii=False),
                updated_ms=detected_at,
            )

    def _freeze(
        self,
        cand: _CandidateV2,
        seg: Sequence[Any],
        asset_id: int,
        source_id: int,
        recognized_at: int,
        detected_at: int,
        episodes_log: list[dict[str, Any]],
    ) -> None:
        """Freeze зрелого эпизода: L/U/M/W и тени фиксируются навсегда
        (R-09). Повторный прогон не перезаписывает замороженную геометрию.
        Инициализирует пост-freeze жизненный цикл: K = 2L − U по СОБСТВЕННОЙ
        геометрии эпизода (формула v1, R-09), цели — снимком при выходе."""
        assert cand.lower is not None and cand.upper is not None
        cand.frozen = True
        cand.state = "mature"
        k_price = 2 * cand.lower - cand.upper   # K = 2L − U (как v1, R-09)
        cand.lstate = _LifecycleState(
            k_price=k_price, cancel_reachable=k_price > 0
        )
        row = self._episode_row(
            cand, AltEpisodeState.MATURE.value, asset_id, source_id, detected_at
        )
        row, created = self.db.insert_alt_range_episode(row)
        cand.episode_id = row.id
        episodes_log.append({
            "origin_key": cand.origin_key, "episode_id": row.id,
            "state": row.state, "created": created,
        })
        if not created and row.state in FORMING_EPISODE_STATES:
            # переход forming → mature: финальная запись геометрии и качества
            self.db.update_alt_range_episode(
                row.id,
                state=AltEpisodeState.MATURE.value,
                lower=cand.lower, upper=cand.upper,
                width=cand.upper - cand.lower,
                mid=(cand.lower + cand.upper) / 2,
                wick_low=cand.wick_low if math.isfinite(cand.wick_low) else None,
                wick_high=cand.wick_high if cand.wick_high > 0 else None,
                quality_json=json.dumps(self._quality(cand), ensure_ascii=False),
                selection_rank_reason="mature_quality_ok",
                updated_ms=detected_at,
            )

    def _reject(self, cand: _CandidateV2, reason: str, detected_at: int) -> None:
        """Отклонение кандидата с явной причиной (R-02: участок тренда,
        протухшие реакции, закрытый сегмент). Сохранённая формирующаяся
        строка переводится в decayed — распад консолидации до зрелости."""
        cand.rejected_reason = reason
        cand.state = "rejected"
        if cand.episode_id is None:
            return
        row = self.db.get_alt_range_episode(cand.episode_id)
        if row is not None and row.state in FORMING_EPISODE_STATES:
            self.db.update_alt_range_episode(
                row.id,
                state=AltEpisodeState.DECAYED.value,
                quality_json=json.dumps(self._quality(cand), ensure_ascii=False),
                updated_ms=detected_at,
            )

    # ----- пост-freeze жизненный цикл (R-05/R-06/R-07) -----

    def _lifecycle_event(
        self,
        cand: _CandidateV2,
        kind: str,
        candle_open_time: int,
        boundary: int,
        payload: dict[str, Any],
    ) -> None:
        """Структурированный факт жизненного цикла эпизода (в quality_json;
        уведомления этапом 5 не эмитируются — включение на этапе 7)."""
        entry = {
            "kind": kind,
            "candle_open_time": candle_open_time,
            "confirmed_at_ms": boundary,
        }
        entry.update(payload)
        cand.lifecycle.append(entry)

    def _write_episode(
        self, cand: _CandidateV2, updated_ms: int, **fields: Any
    ) -> None:
        """Запись хода эпизода: quality_json пересобирается из replay
        (детерминированно); геометрия сюда никогда не передаётся (R-09)."""
        fields["quality_json"] = json.dumps(self._quality(cand), ensure_ascii=False)
        fields["updated_ms"] = updated_ms
        self.db.update_alt_range_episode(cand.episode_id, **fields)

    def _find_sweep_row(
        self, episode_id: int, start_open_time: int
    ) -> Optional[AltSweepEpisode]:
        """Дедуп выносов при replay: один уход под L = одна строка (R-10)."""
        for s in self.db.list_alt_sweep_episodes(episode_id):
            if s.start_open_time == start_open_time:
                return s
        return None

    def _lifecycle_step(
        self, cand: _CandidateV2, candles: Sequence[Any], t: int
    ) -> None:
        """Шаг пост-freeze обработки одной закрытой D1 (R-05/R-06/R-07).

        Порядок внутри свечи (приоритет как в v1): отмена по K → вынос
        вниз/возврат → выход/экскурсия → сопровождение (ретест, цели).
        Замороженная геометрия не изменяется никогда (R-09).
        """
        ls = cand.lstate
        if ls is None or ls.terminal or cand.episode_id is None:
            return
        c = candles[t]
        boundary = close_boundary_ms(c.open_time, ALT_TIMEFRAME)
        L, U = cand.lower, cand.upper
        W = U - L
        M = (L + U) / 2

        # --- R-05: отмена по K = 2L − U приоритетна (как §12 v1); нарушение
        # K нельзя скрыть переименованием в манипуляцию. K ≤ 0 — недостижима
        k = ls.k_price
        if ls.cancel_reachable and k is not None and (
            (self.cfg.cancel_mode == "wick_on_closed_d1" and c.low <= k)
            or (self.cfg.cancel_mode == "close_on_closed_d1" and c.close <= k)
        ):
            self._lifecycle_event(cand, "cancelled", c.open_time, boundary, {
                "cancel_price": k, "cancel_mode": self.cfg.cancel_mode,
                "cancel_mode_note": "same_formula_as_v1",
            })
            if ls.sweep_id is not None:
                # открытый вынос закрывается отменой — принятием ниже L,
                # а не подтверждённым возвратом
                self.db.update_alt_sweep_episode(
                    ls.sweep_id, min_price=ls.sweep_min,
                    min_open_time=ls.sweep_min_ot, end_open_time=c.open_time,
                    state=AltSweepState.ACCEPTED_BELOW.value,
                    updated_ms=boundary,
                )
            ls.terminal = True
            ls.terminal_reason = "cancelled"
            cand.episode_terminal = True
            cand.state = "terminal"
            self._write_episode(
                cand, boundary,
                state=AltEpisodeState.TERMINAL.value,
                base_end_open_time=c.open_time,
                base_end_reason="decay",
                base_end_confirmed_at_ms=boundary,
                accompaniment_end_open_time=c.open_time,
            )
            return

        # --- R-05: вынос вниз под L (отдельный эпизод; L не расширяется)
        if c.low < L:
            if ls.sweep_id is None:
                row = self._find_sweep_row(cand.episode_id, c.open_time)
                if row is None:
                    row = self.db.insert_alt_sweep_episode(AltSweepEpisode(
                        id=None, episode_id=cand.episode_id,
                        start_open_time=c.open_time, min_price=c.low,
                        min_open_time=c.open_time,
                        state=AltSweepState.OPEN.value,
                        created_ms=boundary, updated_ms=boundary,
                    ))
                ls.sweep_id = row.id
                ls.sweep_start_ot = c.open_time
                ls.sweep_min = c.low
                ls.sweep_min_ot = c.open_time
                ls.sweep_days = 1
                self._lifecycle_event(cand, "sweep_started", c.open_time,
                                      boundary, {"lower": L, "low": c.low})
            else:
                ls.sweep_days += 1
                if c.low < ls.sweep_min:
                    ls.sweep_min = c.low
                    ls.sweep_min_ot = c.open_time
                if ls.sweep_days >= self.cfg.v2_sweep_max_days:
                    # Затяжное принятие цены ниже L — распад базы (R-05/R-07):
                    # заливка завершается у свечи ухода, подтверждение — у
                    # свечи принятия; основание для НОВОГО кандидата (R-01)
                    self._lifecycle_event(
                        cand, "sweep_accepted_below", c.open_time, boundary,
                        {"started_candle_open_time": ls.sweep_start_ot,
                         "min_price": ls.sweep_min,
                         "days_below": ls.sweep_days},
                    )
                    self.db.update_alt_sweep_episode(
                        ls.sweep_id, min_price=ls.sweep_min,
                        min_open_time=ls.sweep_min_ot,
                        end_open_time=c.open_time,
                        state=AltSweepState.ACCEPTED_BELOW.value,
                        updated_ms=boundary,
                    )
                    ls.terminal = True
                    ls.terminal_reason = "accepted_below"
                    cand.episode_terminal = True
                    cand.state = "decayed"
                    self._write_episode(
                        cand, boundary,
                        state=AltEpisodeState.DECAYED.value,
                        base_end_open_time=ls.sweep_start_ot,
                        base_end_reason="decay",
                        base_end_confirmed_at_ms=boundary,
                        accompaniment_end_open_time=c.open_time,
                    )
                    return
                # до подтверждённого возврата статус — «выход ниже, возврат
                # не подтверждён», а не завершённая манипуляция (R-05)
                self.db.update_alt_sweep_episode(
                    ls.sweep_id, min_price=ls.sweep_min,
                    min_open_time=ls.sweep_min_ot,
                    state=AltSweepState.RETURN_PENDING.value,
                    updated_ms=boundary,
                )
        if ls.sweep_id is not None and c.close > L:
            # Возврат подтверждается ЗАКРЫТИЕМ D1 обратно выше L; равенство
            # L не возврат (конвенция v1)
            self.db.update_alt_sweep_episode(
                ls.sweep_id, min_price=ls.sweep_min,
                min_open_time=ls.sweep_min_ot, end_open_time=c.open_time,
                return_confirmed=True, return_confirmed_at_ms=boundary,
                state=AltSweepState.RETURNED.value, updated_ms=boundary,
            )
            self._lifecycle_event(
                cand, "sweep_returned", c.open_time, boundary,
                {"started_candle_open_time": ls.sweep_start_ot,
                 "min_price": ls.sweep_min, "days_below": ls.sweep_days},
            )
            ls.sweep_id = None
            ls.sweep_start_ot = None
            ls.sweep_min = math.inf
            ls.sweep_min_ot = None
            ls.sweep_days = 0
            self._write_episode(cand, boundary)

        # --- R-06: выход — ЗАКРЫТИЕ D1 за U·(1+tol); тень — лишь экскурсия
        if ls.breakout_open_time is None:
            if c.close > U * (1 + self.cfg.v2_breakout_tol):
                ls.breakout_open_time = c.open_time
                ls.breakout_close = c.close
                # Цели — снимок от СОБСТВЕННОЙ замороженной геометрии (R-09)
                ls.targets = [
                    U + n * W for n in range(1, self.cfg.target_count + 1)
                ]
                passed = []
                for n, tp in enumerate(ls.targets, 1):
                    if c.close >= tp:  # «пройдена к моменту подтверждения» (v1)
                        ls.targets_hit.add(n)
                        passed.append(n)
                self._lifecycle_event(cand, "breakout", c.open_time, boundary, {
                    "close": c.close, "upper": U, "targets": list(ls.targets),
                    "passed_at_confirmation": passed,
                })
                cand.state = "accompaniment"
                # R-07: конец заливки — у свечи выхода, подтверждён её
                # закрытием (без заднего числа); сопровождение — отдельно
                self._write_episode(
                    cand, boundary,
                    state=AltEpisodeState.ACCOMPANIMENT.value,
                    base_end_open_time=c.open_time,
                    base_end_reason="breakout_confirmed",
                    base_end_confirmed_at_ms=boundary,
                )
            elif c.high > U and not ls.upper_excursion:
                ls.upper_excursion = True
                self._lifecycle_event(cand, "upper_excursion", c.open_time,
                                      boundary, {"high": c.high, "upper": U})
                self._write_episode(cand, boundary)
        elif c.open_time > ls.breakout_open_time:
            # Сопровождение: ретест бывшей верхней зоны [M, U] — факт этого
            # же эпизода, не расширяет U и не продлевает накопление (R-06);
            # цели TP_n = U + n·W по High закрытых D1 после выхода (как v1)
            if not ls.retest_done and c.low <= U and c.high >= M:
                ls.retest_done = True
                payload: dict[str, Any] = {"zone": {"lower": M, "upper": U}}
                if c.low < M:
                    payload["depth_below_mid"] = M - c.low
                self._lifecycle_event(cand, "retest", c.open_time, boundary,
                                      payload)
                self._write_episode(cand, boundary)
            hits = [
                n for n, tp in enumerate(ls.targets, 1)
                if n not in ls.targets_hit and c.high >= tp
            ]
            if hits:
                ls.targets_hit.update(hits)
                self._lifecycle_event(cand, "target_hit", c.open_time, boundary,
                                      {"levels": hits,
                                       "prices": {str(n): ls.targets[n - 1]
                                                  for n in hits}})
                self._write_episode(cand, boundary)

        # --- R-07: завершение сопровождения при достижении всех целей
        if (
            ls.breakout_open_time is not None
            and not ls.terminal
            and len(ls.targets_hit) >= self.cfg.target_count
        ):
            self._lifecycle_event(cand, "targets_completed", c.open_time,
                                  boundary, {"levels": sorted(ls.targets_hit)})
            ls.terminal = True
            ls.terminal_reason = "targets_completed"
            cand.episode_terminal = True
            cand.state = "terminal"
            self._write_episode(
                cand, boundary,
                state=AltEpisodeState.TERMINAL.value,
                accompaniment_end_open_time=c.open_time,
            )

    # ----- трекинг касаний (давность реакций, R-02/R-04) -----

    def _track_reaction(
        self, cand: _CandidateV2, c: Any, tol: Optional[float]
    ) -> None:
        """Касание границ/якоря свечой — давность реакций (R-02/R-04).

        Работает и для замороженных эпизодов (геометрия не меняется) —
        давность реакций для разделения кандидатов (R-02). Терминальный
        эпизод не трекается: его цикл завершён (R-07).
        """
        if tol is None or cand.episode_terminal:
            return
        lo = cand.lower if cand.lower is not None else cand.start.price
        touched = lo - tol <= c.low <= lo + tol
        if cand.upper is not None:
            touched = touched or (cand.upper - tol <= c.high <= cand.upper + tol)
        if touched:
            cand.last_reaction_ms = c.open_time

    # ----- summary helpers -----

    def _aggregate_state(self, candidates: list[_CandidateV2]) -> str:
        if any(c.frozen for c in candidates):
            return AltState.MATURE.value
        if any(
            c.state == "forming" and c.rejected_reason is None for c in candidates
        ):
            return AltState.FORMING.value
        return AltState.SEARCHING.value

    @staticmethod
    def _candidate_summary(cand: _CandidateV2) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cid": cand.cid,
            "origin_key": cand.origin_key,
            "state": cand.state,
            "anchor_start_open_time": cand.start.formed_at,
            "lower": cand.lower,
            "upper": cand.upper,
            "wick_low": cand.wick_low if math.isfinite(cand.wick_low) else None,
            "wick_high": cand.wick_high if cand.wick_high > 0 else None,
            "n_days": cand.n_days,
            "alternatives": list(cand.alternatives),
            "rejected_reason": cand.rejected_reason,
            "episode_id": cand.episode_id,
        }
        if cand.classifier is not None:
            r = cand.classifier
            out["classifier"] = {
                "ready": r.ready, "sideways": r.sideways,
                "reason": r.reason,
                "failed_conditions": list(r.failed_conditions),
                "classifier_version": r.classifier_version,
            }
        if cand.lstate is not None:
            ls = cand.lstate
            out["lifecycle"] = {
                "k_price": ls.k_price,
                "cancel_reachable": ls.cancel_reachable,
                "breakout_open_time": ls.breakout_open_time,
                "targets_hit": sorted(ls.targets_hit),
                "retest_done": ls.retest_done,
                "sweep_open": ls.sweep_id is not None,
                "terminal_reason": ls.terminal_reason,
                "events": len(cand.lifecycle),
            }
        return out

    @staticmethod
    def _ath_summary(tracker: AthTracker) -> dict[str, Any]:
        ep = tracker.current
        if ep is None:
            return {}
        return {
            "ath_price": ep.ath_price,
            "ath_open_time": ep.ath_open_time,
            "p_min": ep.p_min,
            "drawdown": ep.drawdown,
            "deep_drop_achieved": ep.deep_drop_achieved,
            "deep_drop_open_time": ep.deep_drop_open_time,
            "episodes_total": len(tracker.history) + 1,
        }

    def _save_progress(
        self,
        asset_id: int,
        source_id: int,
        valid: Sequence[Any],
        summary: dict[str, Any],
    ) -> None:
        """Прогресс движка v2 (meta KV, отдельный от v1 ключ); replay от
        него не зависит (полный пересчёт, R-10)."""
        self.db.set_meta(
            f"alt:engine_v2:{asset_id}:{source_id}",
            json.dumps({
                "last_processed_open_time": (
                    valid[-1].open_time if valid else None
                ),
                "candles_valid": len(valid),
                "state": summary.get("state"),
            }),
        )
