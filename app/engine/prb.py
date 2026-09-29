"""§5: PRB — Potential Reaction Block на H1 внутри движения HTF-OB.

Механизм обнаружения базы и собственного подтверждающего FVG — тот же,
что у OB (orderblock.find_base + is_external); собственный FVG обязателен.
Здесь — только рабочая схема привязки PRB к родительскому HTF-OB (§14.2,
схема сопоставления не формализована окончательно):
родитель — ближайший по formed_at HTF-OB того же направления,
сформированный раньше PRB, за чьей дальней границей лежит PRB
(то есть PRB находится по пути движения от родительского OB).
PRB никогда не становится Breaker (§6) — это обрабатывается в scanner.
"""
from __future__ import annotations

from typing import Optional

from ..models import TIMEFRAME_MINUTES, Direction, Zone
from .orderblock import BaseRecord


def find_parent_ob(
    base: BaseRecord, prb_timeframe: str, ob_zones: list[Zone]
) -> Optional[Zone]:
    """Рабочая схема (§5): ближайший подтверждённый HTF-OB того же направления,
    formed_at не позже базы PRB, причём база PRB — за дальней границей родителя
    в направлении движения (бычий PRB выше U родителя, медвежий — ниже L).
    """
    prb_minutes = TIMEFRAME_MINUTES[prb_timeframe]
    candidates = []
    for z in ob_zones:
        if z.direction != base.direction:
            continue
        if TIMEFRAME_MINUTES[z.timeframe] <= prb_minutes:
            continue  # родитель — старший таймфрейм
        if z.confirmed_at is None or z.formed_at > base.formed_at:
            continue
        if base.direction == Direction.BULL and base.lower > z.upper:
            candidates.append(z)
        elif base.direction == Direction.BEAR and base.upper < z.lower:
            candidates.append(z)
    if not candidates:
        return None
    return max(candidates, key=lambda z: z.formed_at)
