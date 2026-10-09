"""Визуальное объединение пересекающихся зон (§10).

Только отображение и текст уведомлений: исходные зоны, их границы, середины,
жизненные циклы и правила уведомлений НЕ пересчитываются и не меняются.
Общий модуль, чтобы веб-API и доставка уведомлений группировали одинаково.
"""
from __future__ import annotations

from ..models import Zone


def union_find_groups(zones: list[Zone]) -> list[list[Zone]]:
    """Union-find по пересечению диапазонов [lower, upper] (§10: визуальное
    объединение). Касание границ считается пересечением.

    Объединяются только зоны одного типа и одного таймфрейма. OB с FVG
    не сливаются, и D1 не сливается с W1: на графике это разные полосы.
    Разнотипные зоны одного ТФ обрезают друг друга (см. drawZones)."""
    parent = {z.id: z.id for z in zones}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ordered = sorted(zones, key=lambda z: z.lower)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.lower > a.upper:
                break  # отсортировано: дальше пересечений с a не будет
            if a.type != b.type or a.timeframe != b.timeframe:
                continue  # разные типы и разные ТФ не сливаются
            union(a.id, b.id)
    groups: dict[int, list[Zone]] = {}
    for z in zones:
        groups.setdefault(find(z.id), []).append(z)
    return list(groups.values())


def group_members_by_zone(zones: list[Zone]) -> dict[int, list[Zone]]:
    """zone_id -> прочие зоны той же визуальной группы.

    Возвращает только зоны из групп из 2+ участников; одиночные зоны
    в словарь не попадают.
    """
    members: dict[int, list[Zone]] = {}
    for group in union_find_groups(zones):
        if len(group) < 2:
            continue
        for z in group:
            members[z.id] = [m for m in group if m.id != z.id]
    return members
