"""ТЗ «LTF Current Setup» §10: модель пригодности Entry Zone (eligible_now).

Чистая функция evaluate_entry — конъюнкция независимых признаков пары
scenario_id + zone_id с первым сработавшим стабильным reason-кодом:

- рыночная актуальность зоны != invalid (невалидная не возвращается в fresh);
- тип включён в cfg.ltf_entry_types (настройка применяется к расчёту
  пригодности, API, счётчикам и новым уведомлениям; история не удаляется);
- снятый BSL/SSL (подтверждённый sweep в ltf_liquidity_test) не подходит
  и не воскресает от новой версии диапазона (приёмка п.17);
- принадлежность разрешённому движению: movement_id зоны обязан
  разрешаться в движения сценария с однозначным происхождением
  (неоднозначные — origin_unresolved, молча не добавляются);
- для tested-зон — повторное использование по глубине СТРОГО меньше
  entry_reuse_max_depth (0.9), отдельно от рыночной актуальности;
- пересечение нужной половины диапазона: частичного пересечения достаточно,
  midpoint принадлежит обеим половинам (явный convention §10) — сама
  геометрия считается переиспользованным classify_entry.

eligible/overlap результата — пространственный фильтр classify_entry на
момент range_version (как и раньше); reason/state — итог конъюнкции.
Пространственная метрика считается всегда, даже при более раннем отказе:
строка сохраняет overlap своей версии диапазона.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...config import DetectorConfig
from ...models import Direction
from ...models_ltf import LtfEntryZone, LtfLiquidityTest, LtfMovement, LtfScenarioEntry
from .entries import classify_entry, entry_reusable
from .ranges import RangeDraft

# Стабильные reason-коды (ТЗ §10/§12: фильтры истории по причине исключения)
REASON_OK = "ok"                        # подходит: все условия выполнены
REASON_OUTSIDE_PD = "outside_pd"        # вне нужной половины диапазона
REASON_TESTED_TOO_DEEP = "tested_too_deep"  # тест >= entry_reuse_max_depth (90%)
REASON_TYPE_DISABLED = "type_disabled"  # тип отключён настройкой ltf_entry_types
REASON_INVALID = "invalid"              # зона рыночно невалидна
REASON_SWEPT_LEVEL = "swept_level"      # BSL/SSL с подтверждённым снятием
REASON_LEVEL_BROKEN = "level_broken"    # уровень пройден закрытием без возврата (§9)
REASON_ORIGIN_UNRESOLVED = "origin_unresolved"  # принадлежность движению не доказана
REASON_RANGE_PENDING = "range_pending"  # диапазон ещё не подтверждён — кандидат

ELIGIBILITY_REASONS = (
    REASON_OK,
    REASON_OUTSIDE_PD,
    REASON_TESTED_TOO_DEEP,
    REASON_TYPE_DISABLED,
    REASON_INVALID,
    REASON_SWEPT_LEVEL,
    REASON_LEVEL_BROKEN,
    REASON_ORIGIN_UNRESOLVED,
    REASON_RANGE_PENDING,
)


@dataclass(frozen=True)
class EligibilityResult:
    """Итог проверки пары (сценарий, зона) на версии диапазона."""

    reason: str                  # ELIGIBILITY_REASONS
    state: str                   # fresh | out_of_range | tested | invalid
    eligible: bool               # пространственный фильтр (classify_entry)
    overlap: str                 # full | partial | none | pending


def evaluate_entry(
    zone: LtfEntryZone,
    direction: Direction,
    cfg: DetectorConfig,
    rng: Optional[RangeDraft],
    *,
    movements: Optional[list[LtfMovement]] = None,
    liquidity_tests: Optional[list[LtfLiquidityTest]] = None,
) -> EligibilityResult:
    """eligible_now зоны для сценария: конъюнкция условий §10.

    Проверки идут от рыночных фактов к конфигурационным и пространственным;
    первый сработавший отказ — стабильный reason. rng=None — диапазон ещё
    не подтверждён: пространственный фильтр не применяется, зона копится
    кандидатом (state fresh, reason range_pending).
    """
    eligible, overlap = classify_entry(
        zone.lower, zone.upper, zone.is_level, rng, direction
    )
    # 1) рыночная актуальность: invalid не переводится обратно в fresh
    if zone.validity == "invalid":
        return EligibilityResult(REASON_INVALID, "invalid", eligible, overlap)
    # 2) тип отключён настройкой (история и сама зона сохраняются)
    if zone.type not in cfg.ltf_entry_type_set():
        return EligibilityResult(REASON_TYPE_DISABLED, "out_of_range",
                                 eligible, overlap)
    # 3) снятый BSL/SSL не подходит и не воскресает от новой версии (п.17)
    if zone.type in ("BSL", "SSL") and any(
        t.entry_zone_id == zone.id and t.state == "confirmed"
        for t in liquidity_tests or ()
    ):
        return EligibilityResult(REASON_SWEPT_LEVEL, "tested", eligible, overlap)
    # 3b) §9 (Этап 5): уровень, пройденный закрытием H1 без возврата
    # (исход failed — строгое закрытие за уровнем), — терминальное состояние:
    # не «свежая нетронутая ликвидность», возврат за уровень позже исход
    # пробойной свечи не меняет и от новой версии диапазона не воскресает
    if zone.type in ("BSL", "SSL") and any(
        t.entry_zone_id == zone.id and t.state == "failed"
        for t in liquidity_tests or ()
    ):
        return EligibilityResult(REASON_LEVEL_BROKEN, "tested", eligible, overlap)
    # 4) принадлежность движению сценария: movement_id обязан разрешаться;
    # movement_id=0 (уровень якоря диапазона) — вне этой проверки
    if zone.movement_id and movements is not None:
        mv = next((m for m in movements if m.id == zone.movement_id), None)
        if mv is None or mv.provenance_status == "ambiguous":
            return EligibilityResult(REASON_ORIGIN_UNRESOLVED, "out_of_range",
                                     eligible, overlap)
    # 5) допустимость по истории тестов: глубина СТРОГО < порога (ТЗ §3)
    if zone.validity == "tested" and not entry_reusable(zone, cfg):
        return EligibilityResult(REASON_TESTED_TOO_DEEP, "tested",
                                 eligible, overlap)
    # 6) диапазона ещё нет — кандидат до появления подтверждённой пары
    if rng is None:
        return EligibilityResult(REASON_RANGE_PENDING, "fresh", True, "pending")
    # 7) пересечение нужной половины диапазона (частичного достаточно)
    if not eligible:
        return EligibilityResult(REASON_OUTSIDE_PD, "out_of_range", False, overlap)
    return EligibilityResult(REASON_OK, "fresh", True, overlap)


# Основание допуска (ТЗ переработки L01): обычное правило или контекстное
# исключение §18 (FVG вне половины диапазона при полном контексте)
ADMISSION_RULE = "rule"
ADMISSION_CONTEXT = "context_exception"


@dataclass(frozen=True)
class FinalEligibility:
    """Окончательное решение о допустимости привязки (L01).

    eligible учитывает только пространственный фильтр своей версии диапазона
    и НЕ описывает итоговую допустимость; итог — eligible_now с явным
    основанием admission_basis. Решение производное (контекст может
    дополниться без смены строки), поэтому вычисляется проекцией, а не
    сохраняется в строке. Пространственное пересечение — отдельное поле
    spatial_overlap, в решении не участвует (outside_pd решает reason).
    """

    eligible_now: bool                # итоговая допустимость для нового входа
    primary_reason: str               # ELIGIBILITY_REASONS
    blocking_reasons: tuple[str, ...] # причины отказа (пусто при допуске)
    admission_basis: Optional[str]    # rule | context_exception | None
    state: str                        # fresh | out_of_range | tested | invalid
    spatial_overlap: str              # full | partial | none | pending
    range_version: int
    rule_version: str


def evaluate_final(
    entry: LtfScenarioEntry,
    zone: LtfEntryZone,
    *,
    allow_outside: bool,
) -> FinalEligibility:
    """Единая функция окончательного решения (L01): конъюнкция evaluate_entry
    (сохранённая в строке как state+reason) плюс контекстный допуск §18.

    Допуск по обычному правилу: строка пространственно подходит,
    reason == ok и состояние fresh (или tested у строк до миграции с
    пустым reason — прежний fallback tested → ok, eligibility.entry_reason;
    мигрированные tested-строки всегда несут явный reason отказа).
    Контекстное исключение: при полном контексте §18 FVG с
    reason == outside_pd допускается с основанием context_exception.
    """
    reason = entry_reason(entry)
    base = dict(
        primary_reason=reason,
        state=entry.state,
        spatial_overlap=entry.overlap,
        range_version=entry.range_version,
        rule_version=zone.rule_version,
    )
    if (entry.eligible and reason == REASON_OK
            and entry.state in ("fresh", "tested")):
        return FinalEligibility(
            eligible_now=True, blocking_reasons=(),
            admission_basis=ADMISSION_RULE, **base,
        )
    if allow_outside and reason == REASON_OUTSIDE_PD and zone.type == "FVG":
        # §18: контекстный допуск FVG вне Premium при полном контексте
        return FinalEligibility(
            eligible_now=True, blocking_reasons=(),
            admission_basis=ADMISSION_CONTEXT, **base,
        )
    return FinalEligibility(
        eligible_now=False,
        blocking_reasons=(() if reason == REASON_OK else (reason,)),
        admission_basis=None, **base,
    )


def admitted_scenario_entries(
    db, scenario_id: int
) -> list[tuple[LtfScenarioEntry, LtfEntryZone, FinalEligibility]]:
    """Единый отбор допущенных зон сценария на текущей версии диапазона.

    Единственный источник правила допуска для движка (уведомления), API
    (карточка, график) и счётчиков — замена дублирующихся отборов
    engine._entry_candidates и ltf_api._fresh_entries (L01). ver=0 до
    появления первой пары диапазона.
    """
    from .context import context_complete, context_flags

    cur = db.get_current_ltf_range(scenario_id)
    ver = cur.version if cur is not None else 0
    allow_outside = context_complete(
        context_flags(db.list_ltf_events(scenario_id=scenario_id, limit=1000))
    )
    rows = db.list_ltf_scenario_entries(scenario_id, state="fresh")
    if allow_outside:
        rows += db.list_ltf_scenario_entries(scenario_id, state="out_of_range")
    out: list[tuple[LtfScenarioEntry, LtfEntryZone, FinalEligibility]] = []
    for e in rows:
        if e.range_version != ver:
            continue
        zone = db.get_ltf_entry_zone(e.entry_zone_id)
        if zone is None:
            continue
        fe = evaluate_final(e, zone, allow_outside=allow_outside)
        if fe.eligible_now:
            out.append((e, zone, fe))
    return out


# Строки до миграции (reason == ''): пригодность выводится из прежнего state
_STATE_FALLBACK_REASON = {
    "fresh": REASON_OK,
    "tested": REASON_OK,
    "out_of_range": REASON_OUTSIDE_PD,
    "invalid": REASON_INVALID,
}


def entry_reason(entry: LtfScenarioEntry) -> str:
    """reason привязки; для строк без миграции (пустой reason) — из state."""
    return entry.reason or _STATE_FALLBACK_REASON.get(entry.state, REASON_OK)
