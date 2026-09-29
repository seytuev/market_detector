"""§5.2: структурные роли pivots — чистая классификация HH/HL/LH/LL.

Локальный pivot и его структурная роль — разные сущности. Роль сравнивается
с предыдущим pivot того же типа: high выше предыдущего high → HH, иначе LH;
low ниже предыдущего low → LL, иначе HL. Первый pivot каждого типа — «none»
(контекста ещё нет).

Дополнительные правила спеки:
- опорный HL — минимум отката, после которого сформировался следующий HH
  (при появлении HH безымянный откат перед ним получает HL); зеркально
  опорный LH перед LL;
- если в восходящей структуре вместо HH сформирован более низкий максимум,
  внутренний минимум между пиком и этим максимумом участвует в SMS —
  получает роль internal_low (только если он выше опорного HL, §6.3);
  зеркально internal_high в нисходящей (§6.4);
- равные экстремумы произвольно не разрешаются: роль не присваивается,
  в изменениях фиксируется ambiguous-запись.

При пересмотре роли старая не стирается: каждое изменение возвращается
в списке changes, а слой БД пишет в ltf_pivot_role_log только те пересмотры,
которые меняют сохранённую в ltf_pivot роль.
"""
from __future__ import annotations

from dataclasses import dataclass

from .pivots import PivotCandidate


@dataclass
class RoleChange:
    pivot_index: int               # индекс во входном списке assign_roles
    old_role: str
    new_role: str
    ambiguous: bool = False        # равный экстремум — роль сознательно не присвоена
    note: str = ""


@dataclass
class RoleResult:
    roles: list[str]               # финальная роль каждого входного pivot
    changes: list[RoleChange]      # история присвоений/пересмотров в порядке возникновения


def assign_roles(pivots: list[PivotCandidate]) -> RoleResult:
    """Классифицирует pivots (подтверждённые, отсортированные по pivot_at)."""
    roles = ["none"] * len(pivots)
    changes: list[RoleChange] = []

    def set_role(i: int, new: str, note: str = "") -> None:
        old = roles[i]
        if old == new:
            return
        roles[i] = new
        changes.append(RoleChange(i, old, new, note=note))

    prev_high: int | None = None   # индекс предыдущего high-pivot
    prev_low: int | None = None
    last_hh: int | None = None     # последний HH (пик восходящей структуры)
    last_ll: int | None = None     # последний LL (дно нисходящей)
    ref_of_hh: dict[int, int | None] = {}  # HH → опорный HL перед ним
    ref_of_ll: dict[int, int | None] = {}  # LL → опорный LH перед ним
    trend: str | None = None       # up после HH, down после LL

    for i, p in enumerate(pivots):
        if p.kind == "high":
            if prev_high is not None:
                prev = pivots[prev_high]
                if p.price > prev.price:
                    set_role(i, "HH")
                    trend = "up"
                    # откат, после которого сформировался HH, — опорный HL
                    if prev_low is not None and roles[prev_low] == "none":
                        set_role(prev_low, "HL", note="опорный HL перед HH")
                    ref_of_hh[i] = prev_low
                    last_hh = i
                elif p.price < prev.price:
                    set_role(i, "LH")
                    # §6.3: в восходящей структуре более низкий максимум делает
                    # внутренний минимум перед ним участником SMS
                    if (
                        trend == "up" and last_hh is not None
                        and prev_low is not None
                        and pivots[prev_low].pivot_at > pivots[last_hh].pivot_at
                    ):
                        ref = ref_of_hh.get(last_hh)
                        if ref is not None and pivots[prev_low].price > pivots[ref].price:
                            set_role(prev_low, "internal_low",
                                     note="внутренний минимум для SMS (§6.3)")
                else:
                    # равные максимумы произвольно не разрешаем (§5.2)
                    changes.append(RoleChange(
                        i, "none", "none", ambiguous=True,
                        note="равный предыдущему high — роль не присвоена",
                    ))
            prev_high = i
        else:  # low — зеркально
            if prev_low is not None:
                prev = pivots[prev_low]
                if p.price < prev.price:
                    set_role(i, "LL")
                    trend = "down"
                    if prev_high is not None and roles[prev_high] == "none":
                        set_role(prev_high, "LH", note="опорный LH перед LL")
                    ref_of_ll[i] = prev_high
                    last_ll = i
                elif p.price > prev.price:
                    set_role(i, "HL")
                    # §6.4: в нисходящей структуре более высокий минимум делает
                    # внутренний максимум перед ним участником bull SMS
                    if (
                        trend == "down" and last_ll is not None
                        and prev_high is not None
                        and pivots[prev_high].pivot_at > pivots[last_ll].pivot_at
                    ):
                        ref = ref_of_ll.get(last_ll)
                        if ref is not None and pivots[prev_high].price < pivots[ref].price:
                            set_role(prev_high, "internal_high",
                                     note="внутренний максимум для bull SMS (§6.4)")
                else:
                    changes.append(RoleChange(
                        i, "none", "none", ambiguous=True,
                        note="равный предыдущему low — роль не присвоена",
                    ))
            prev_low = i

    return RoleResult(roles=roles, changes=changes)
