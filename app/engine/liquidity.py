"""§7: SSL/BSL — локальные экстремумы по теням закрытых D1/W1-свечей (pivot 3+3).

BSL: High центра строго выше High трёх свечей слева и трёх справа.
SSL: Low центра строго ниже Low трёх слева и трёх справа.
Уровень активен только после закрытия третьей правой свечи (confirmed_at).

Кластеризация (§7, формула — предложение, §14.3): допуск
(p_max − p_min) / p_min ≤ cfg.cluster_tolerance_pct для всей группы,
без цепочного расширения — каждый кандидат сравнивается с диапазоном
всей группы, а не с соседом. Уровень группы BSL = max High, SSL = min Low.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import DetectorConfig
from ..models import TIMEFRAME_MINUTES, Candle, Zone, ZoneType, open_times_follow


@dataclass
class PivotRecord:
    kind: ZoneType                  # SSL | BSL
    price: float                    # уровень экстремума (одна цена)
    formed_at: int                  # open_time центральной свечи
    confirmed_at: int               # граница после закрытия 3-й правой свечи
    timeframe: str
    candle_open_times: tuple[int, ...]  # окно 3+1+3


def find_pivots(
    candles: list[Candle], timeframe: str, cfg: DetectorConfig
) -> list[PivotRecord]:
    """Строгий pivot 3+3 по закрытым свечам.

    TODO(§14.3): плато с равными пиками строгий pivot пропускает —
    нужен отдельный согласованный пример; флаг
    cfg.uncalibrated_plateau_equal_peaks пока не задействован.
    """
    left, right = cfg.pivot_left, cfg.pivot_right
    tf_ms = TIMEFRAME_MINUTES[timeframe] * 60_000
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    out: list[PivotRecord] = []
    for i in range(left, len(closed) - right):
        c = closed[i]
        window = closed[i - left : i + right + 1]
        if any(
            not open_times_follow(
                window[j].open_time, window[j + 1].open_time, timeframe,
            )
            for j in range(len(window) - 1)
        ):
            continue
        others = [w for w in window if w.open_time != c.open_time]
        confirmed_at = closed[i + right].open_time + tf_ms
        ots = tuple(w.open_time for w in window)
        if all(c.high > w.high for w in others):
            out.append(PivotRecord(ZoneType.BSL, c.high, c.open_time,
                                   confirmed_at, timeframe, ots))
        if all(c.low < w.low for w in others):
            out.append(PivotRecord(ZoneType.SSL, c.low, c.open_time,
                                   confirmed_at, timeframe, ots))
    return out


def cluster_prices(prices: list[float], tolerance_pct: float) -> list[list[int]]:
    """Группировка близких уровней. Возвращает группы индексов исходного списка.

    Без цепочного расширения: кандидат добавляется в группу, только если
    (p_max − p_min)/p_min ≤ допуска для ВСЕЙ группы с учётом кандидата.
    """
    order = sorted(range(len(prices)), key=lambda i: prices[i])
    groups: list[list[int]] = []
    for idx in order:
        placed = False
        for g in groups:
            members = [prices[i] for i in g] + [prices[idx]]
            p_min, p_max = min(members), max(members)
            if p_min > 0 and (p_max - p_min) / p_min <= tolerance_pct:
                g.append(idx)
                placed = True
                break
        if not placed:
            groups.append([idx])
    return groups


def level_crossed(zone: Zone, lo: float, hi: float) -> bool:
    """Пересечение уровня наблюдаемым диапазоном [lo,hi].

    BSL — рост выше уровня, SSL — падение ниже. При исторической обработке
    вызывается с тенями закрытой свечи (§7).
    TODO(§14.4): равенство уровню vs строгое пересечение — принят строгий
    вариант (>), подлежит закреплению в приёмочных примерах.
    """
    if zone.type == ZoneType.BSL:
        return hi > zone.lower
    return lo < zone.lower
