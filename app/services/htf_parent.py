"""Единый допуск HTF-родителя (ТЗ 07.10.2026, инцидент SOL, F01–F05).

Один предикат для открытия наблюдения, восстановления, выбора текущего
контекста, списка, графика и доставки. Предварительный фильтр статусов
не должен сужать этот результат: неподтверждённый candidate, rejected,
invalidated и завершённый объект не проходят здесь, а не «до» предиката.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models import ZoneStatus

PARENT_TIMEFRAMES = frozenset({"D1", "W1"})
PARENT_TYPES = frozenset({"OB", "FVG"})
# Статусы, которые предикат вообще может принять. Выборка воркера может
# ограничиться ими: уже, чем этот набор, она отрезала бы подтверждённый
# candidate (F01). Шире — не нужно, остальные статусы здесь не допускаются.
PARENT_QUERY_STATUSES = (
    ZoneStatus.ACTIVE, ZoneStatus.WEAKENED, ZoneStatus.CANDIDATE,
)


@dataclass(frozen=True)
class ParentDecision:
    eligible: bool
    reason: str


def policy_types(policy) -> set[str]:
    """Профиль контекста: DetectorConfig либо набор имён типов."""
    if hasattr(policy, "htf_context_type_set"):
        return set(policy.htf_context_type_set())
    return {str(t).strip().upper() for t in (policy or ())} & set(PARENT_TYPES)


def parent_decision(zone, policy, as_of: Optional[int] = None) -> ParentDecision:
    """Допуск зоны как HTF-родителя на момент as_of.

    as_of=None — «сейчас»: подтверждение уже записано в зоне.
    Учитываются тип, D1/W1, профиль, доступность подтверждения к as_of
    (в том числе явное ручное) и canonical relevance: рыночная валидность,
    незавершённый рисунок (display_until) и статус. Уровень ревью статус
    candidate не подменяет. Цикл зоны завершён, когда рисунок закрыт
    (display_until): возраст и расстояние до цены сами по себе не причина.
    """
    if zone is None:
        return ParentDecision(False, "not_relevant")
    if zone.timeframe not in PARENT_TIMEFRAMES:
        return ParentDecision(False, "wrong_timeframe")
    typ = zone.type.value if hasattr(zone.type, "value") else str(zone.type)
    typ = str(typ).upper()
    if typ not in PARENT_TYPES:
        return ParentDecision(False, "not_context_type")
    if typ not in policy_types(policy):
        return ParentDecision(False, "type_disabled")

    manual = bool(
        getattr(zone, "manual_confirmation_only", False)
        or getattr(zone, "source", None) == "manual"
    )
    confirmed_at = zone.confirmed_at
    if confirmed_at is None and not manual:
        return ParentDecision(False, "unconfirmed")
    if as_of is not None:
        if confirmed_at is not None and confirmed_at > as_of and not manual:
            return ParentDecision(False, "confirmation_not_yet")
        born = confirmed_at if confirmed_at is not None else (zone.formed_at or 0)
        if manual and confirmed_at is None and born > as_of:
            return ParentDecision(False, "confirmation_not_yet")

    status = zone.status
    if status not in PARENT_QUERY_STATUSES:
        return ParentDecision(False, "not_relevant")
    if getattr(zone, "market_validity", "active") != "active":
        return ParentDecision(False, "not_relevant")
    until = zone.display_until
    if until is not None and (as_of is None or until <= as_of):
        return ParentDecision(False, "finished")
    return ParentDecision(True, "ok")


def eligible_htf_parent(zone, policy, as_of: Optional[int] = None) -> bool:
    return parent_decision(zone, policy, as_of).eligible
