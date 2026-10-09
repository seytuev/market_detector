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
from .entries import classify_entry, entry_reusable, fvg_filled
from .ranges import RangeDraft
from .relevance import relevance_reason

# Стабильные reason-коды (ТЗ §10/§12: фильтры истории по причине исключения)
REASON_OK = "ok"                        # подходит: все условия выполнены
REASON_OUTSIDE_PD = "outside_pd"        # вне нужной половины диапазона
REASON_TESTED_TOO_DEEP = "tested_too_deep"  # тест >= entry_reuse_max_depth (90%)
REASON_TYPE_DISABLED = "type_disabled"  # тип отключён настройкой ltf_entry_types
REASON_INVALID = "invalid"              # зона рыночно невалидна
REASON_SWEPT_LEVEL = "swept_level"      # BSL/SSL с подтверждённым снятием
REASON_LEVEL_BROKEN = "level_broken"    # уровень пройден закрытием без возврата (§9)
REASON_FVG_FILLED = "fvg_filled"        # FVG перекрыт полностью (§10, Этап 6)
REASON_ORIGIN_UNRESOLVED = "origin_unresolved"  # принадлежность движению не доказана
REASON_RANGE_PENDING = "range_pending"  # диапазон ещё не подтверждён — кандидат
# Предварительный допуск отката, пока верхняя опора ноги не подтверждена.
# Это не подтверждённый pivot и не вход по рынку.
REASON_PROVISIONAL = "eligible_provisional"

ELIGIBILITY_REASONS = (
    REASON_OK,
    REASON_OUTSIDE_PD,
    REASON_TESTED_TOO_DEEP,
    REASON_TYPE_DISABLED,
    REASON_INVALID,
    REASON_SWEPT_LEVEL,
    REASON_LEVEL_BROKEN,
    REASON_FVG_FILLED,
    REASON_ORIGIN_UNRESOLVED,
    REASON_RANGE_PENDING,
    REASON_PROVISIONAL,
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
    pd_status: Optional[str] = None,
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
    ban = relevance_reason(zone, cfg, liquidity_tests or ())
    if ban:
        return EligibilityResult(ban, "invalid" if ban == REASON_INVALID else "tested", eligible, overlap)
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
    # 3c) §10 (Этап 6): полностью перекрытый FVG терминально недопустим как
    # новая Entry Zone — по собственному правилу, независимо от связанного OB
    if fvg_filled(zone):
        return EligibilityResult(REASON_FVG_FILLED, "tested", eligible, overlap)
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
    if pd_status == "provisional":
        return EligibilityResult(REASON_PROVISIONAL, "fresh", True, overlap)
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


def _tests_of(zone: LtfEntryZone, liquidity_tests) -> list:
    return [
        t for t in (liquidity_tests or ())
        if t.entry_zone_id == zone.id
    ]


def _terminal_reason(zone: LtfEntryZone, liquidity_tests) -> Optional[str]:
    """Обязательный запрет по актуальному lifecycle, а не по старой строке.

    SSL/BSL с исходом confirmed или failed, полностью перекрытый FVG и
    invalid-зона исключаются на любой версии диапазона. Повторно допустимый
    OB этим запретом не затрагивается — у него своё правило глубины.
    """
    if zone.validity == "invalid":
        return REASON_INVALID
    if zone.type in ("BSL", "SSL"):
        tests = _tests_of(zone, liquidity_tests)
        if any(t.state == "confirmed" for t in tests):
            return REASON_SWEPT_LEVEL
        if any(t.state == "failed" for t in tests):
            return REASON_LEVEL_BROKEN
    if fvg_filled(zone):
        return REASON_FVG_FILLED
    return None


def _resolved_reason(
    entry: LtfScenarioEntry, zone: LtfEntryZone, cfg: DetectorConfig,
) -> str:
    """reason строки. Пустой reason у tested больше не считается ok
    без типа и проверки глубины (F31)."""
    if entry.reason:
        return entry.reason
    if entry.state != "tested":
        return entry_reason(entry)
    if zone.type in ("BSL", "SSL"):
        return REASON_LEVEL_BROKEN
    if zone.type == "OB" and not entry_reusable(zone, cfg):
        return REASON_TESTED_TOO_DEEP
    if zone.type == "FVG" and fvg_filled(zone):
        return REASON_FVG_FILLED
    if zone.type == "OB" and entry_reusable(zone, cfg):
        return REASON_OK
    if zone.type == "FVG":
        return REASON_OK
    return REASON_LEVEL_BROKEN if zone.is_level else REASON_OK


def evaluate_final(
    entry: LtfScenarioEntry,
    zone: LtfEntryZone,
    *,
    allow_outside: bool,
    liquidity_tests: Optional[list[LtfLiquidityTest]] = None,
    cfg: Optional[DetectorConfig] = None,
) -> FinalEligibility:
    """Окончательный допуск (L01 + F29–F31).

    Сохранённые eligible/reason не перебивают обязательные запреты
    актуальной зоны и её тестов. Контекстное исключение §18 по-прежнему
    единственный путь допуска FVG вне половины диапазона и не оживляет
    терминальный объект.
    """
    cfg = cfg or DetectorConfig()
    ban = relevance_reason(zone, cfg, liquidity_tests or ())
    if ban is None and zone.type in ("BSL", "SSL") and entry.state == "tested":
        if not _tests_of(zone, liquidity_tests):
            # Явный swept_level без строки теста остаётся снятием.
            # Пустой reason, ok и прочие tested без доказательства
            # снятия — пробой уровня (F30), не новый вход.
            ban = (
                REASON_SWEPT_LEVEL
                if entry.reason == REASON_SWEPT_LEVEL
                else REASON_LEVEL_BROKEN
            )
    if ban is None and zone.type == "OB" and not entry_reusable(zone, cfg):
        if zone.validity == "tested" or entry.state == "tested":
            if zone.max_test_depth >= cfg.entry_reuse_max_depth or (
                zone.test_extreme is not None
                and not entry_reusable(zone, cfg)
            ):
                ban = REASON_TESTED_TOO_DEEP
    reason = ban or _resolved_reason(entry, zone, cfg)
    base = dict(
        primary_reason=reason,
        state="invalid" if reason == REASON_INVALID else entry.state,
        spatial_overlap=entry.overlap,
        range_version=entry.range_version,
        rule_version=zone.rule_version,
    )
    if ban is not None:
        return FinalEligibility(
            eligible_now=False, blocking_reasons=(ban,),
            admission_basis=None, **base,
        )
    if (entry.eligible and reason == REASON_OK
            and entry.state in ("fresh", "tested")):
        return FinalEligibility(
            eligible_now=True, blocking_reasons=(),
            admission_basis=ADMISSION_RULE, **base,
        )
    if (entry.eligible and reason == REASON_PROVISIONAL
            and entry.state in ("fresh", "tested")):
        return FinalEligibility(
            eligible_now=True, blocking_reasons=(),
            admission_basis="provisional_pd", **base,
        )
    if allow_outside and reason == REASON_OUTSIDE_PD and zone.type == "FVG":
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
    tests = db.list_ltf_liquidity_tests(scenario_id=scenario_id)
    out: list[tuple[LtfScenarioEntry, LtfEntryZone, FinalEligibility]] = []
    for e in rows:
        if e.range_version != ver:
            continue
        zone = db.get_ltf_entry_zone(e.entry_zone_id)
        if zone is None:
            continue
        fe = evaluate_final(
            e, zone, allow_outside=allow_outside, liquidity_tests=tests,
        )
        if fe.eligible_now:
            out.append((e, zone, fe))
    return out


# Строки до миграции (reason == ''): fresh/out_of_range/invalid выводятся
# из state. tested → ok снят (F31): без типа и lifecycle это не допуск.
_STATE_FALLBACK_REASON = {
    "fresh": REASON_OK,
    "out_of_range": REASON_OUTSIDE_PD,
    "invalid": REASON_INVALID,
}


def entry_reason(entry: LtfScenarioEntry) -> str:
    """reason привязки. Пустой reason у tested не становится ok."""
    if entry.reason:
        return entry.reason
    if entry.state == "tested":
        return ""
    return _STATE_FALLBACK_REASON.get(entry.state, REASON_OK)
