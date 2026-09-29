"""Модель данных модуля LTF Confirmations (LTF_Confirmations_Window_Spec_v0.2.md §12).

Независимые от HTF сущности: наблюдение за HTF-зоной, направленный сценарий,
pivots структуры H1, BOS/SMS, движения, диапазоны Premium/Discount, Entry Zones
и журнал событий окна. Время — миллисекунды UTC (int), как в models.py.
Состояния хранятся строками (значения перечислены в комментариях к полям).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Direction

RULE_VERSION_LTF = "ltf-0.2"


@dataclass
class LtfObservation:
    """Наблюдение структуры H1 после достижения HTF-зоны (§4).

    UNIQUE(zone_id, cycle_id): повторное HTF-событие не создаёт второе
    наблюдение. data_quality (§12) живёт независимо от торгового статуса.
    """
    id: Optional[int]
    instrument_id: int
    zone_id: int
    zone_version: int
    cycle_id: int
    direction: Direction
    activated_at: int                  # ms: фактическое достижение HTF-зоны
    state: str = "waiting_structure"   # waiting_structure | active | paused_data |
                                       # closed_by_parent | closed_by_user | closed_stale
    data_quality: str = "live"         # live | stale | gap | replaying | ambiguous
    created_at: int = 0
    updated_at: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instrument_id": self.instrument_id,
            "zone_id": self.zone_id,
            "zone_version": self.zone_version,
            "cycle_id": self.cycle_id,
            "direction": self.direction.value,
            "state": self.state,
            "activated_at": self.activated_at,
            "data_quality": self.data_quality,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": self.evidence,
        }


@dataclass
class LtfScenario:
    """Направленный LTF-сценарий внутри наблюдения (§6)."""
    id: Optional[int]
    observation_id: int
    direction: Direction
    trigger: str                       # BOS | SMS
    stage: str                         # primary | secondary
    state: str = "range_pending"       # range_pending | monitoring_entries |
                                       # cancelled | closed
    trigger_event_id: Optional[int] = None
    cancellation_reason: Optional[str] = None
    cancelled_at: Optional[int] = None
    created_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation_id": self.observation_id,
            "direction": self.direction.value,
            "trigger": self.trigger,
            "stage": self.stage,
            "state": self.state,
            "trigger_event_id": self.trigger_event_id,
            "cancellation_reason": self.cancellation_reason,
            "cancelled_at": self.cancelled_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class LtfPivot:
    """Локальный экстремум H1 и его структурная роль (§5.1, §5.2).

    pivot_at (время свечи экстремума) и confirmed_at (когда pivot стал
    известен — закрытие i+r) хранятся раздельно: неподтверждённый pivot —
    кандидат и в сигналах не участвует.
    """
    id: Optional[int]
    instrument_id: int
    price: float
    kind: str                          # high | low
    pivot_at: int                      # ms: open_time свечи экстремума
    candle_open_time: int              # ms: та же свеча-якорь (для ссылок на OHLC)
    confirmed_at: Optional[int] = None
    role: str = "none"                 # HH | HL | LH | LL | none
    role_assigned_at: Optional[int] = None
    left: int = 3                      # настройка l свечей слева (§5.1, default 3+3)
    right: int = 3                     # настройка r свечей справа
    state: str = "candidate"           # candidate | confirmed | ambiguous

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instrument_id": self.instrument_id,
            "price": self.price,
            "kind": self.kind,
            "pivot_at": self.pivot_at,
            "confirmed_at": self.confirmed_at,
            "role": self.role,
            "role_assigned_at": self.role_assigned_at,
            "left": self.left,
            "right": self.right,
            "candle_open_time": self.candle_open_time,
            "state": self.state,
        }


@dataclass
class LtfStructureEvent:
    """Слом структуры BOS/SMS (§6). Один слом на уровень/этап в сценарии:
    UNIQUE(scenario_id, level_key, stage)."""
    id: Optional[int]
    scenario_id: int
    kind: str                          # BOS | SMS
    stage: str                         # primary | secondary
    direction: Direction
    break_level: float                 # пробитый структурный уровень
    break_candle_open_time: int        # ms: свеча закрытия строго за уровнем
    occurred_at: int                   # ms: время рыночного события
    detected_at: int                   # ms: когда алгоритм его увидел
    level_key: str                     # стабильный ключ уровня для дедупликации
    ref_pivot_ids: list[int] = field(default_factory=list)
    accompanying: bool = False         # §6.5: SMS сопутствующий при BOS на той же свече
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "stage": self.stage,
            "direction": self.direction.value,
            "break_level": self.break_level,
            "break_candle_open_time": self.break_candle_open_time,
            "occurred_at": self.occurred_at,
            "detected_at": self.detected_at,
            "ref_pivot_ids": self.ref_pivot_ids,
            "accompanying": self.accompanying,
            "level_key": self.level_key,
            "evidence": self.evidence,
        }


@dataclass
class LtfMovement:
    """Причинное движение, приведшее к конкретному BOS/SMS (§8.1)."""
    id: Optional[int]
    scenario_id: int
    start_pivot_id: int
    end_pivot_id: int
    start_at: int
    end_at: int
    break_event_id: Optional[int] = None
    confirmed_at: Optional[int] = None
    source_candle_ids: list[int] = field(default_factory=list)
    provenance_status: str = "ok"      # ok | ambiguous

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "start_pivot_id": self.start_pivot_id,
            "end_pivot_id": self.end_pivot_id,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "break_event_id": self.break_event_id,
            "confirmed_at": self.confirmed_at,
            "source_candle_ids": self.source_candle_ids,
            "provenance_status": self.provenance_status,
        }


@dataclass
class LtfRange:
    """Версия диапазона Premium/Discount (§7). Старые версии не
    переписываются: события сохраняют геометрию своего range_version."""
    id: Optional[int]
    scenario_id: int
    version: int
    lower: float
    upper: float
    mid: float
    available_at: int                  # ms: когда пара подтверждена (3 правые свечи)
    anchor_low_pivot_id: Optional[int] = None
    anchor_high_pivot_id: Optional[int] = None
    prev_version_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "version": self.version,
            "lower": self.lower,
            "upper": self.upper,
            "mid": self.mid,
            "anchor_low_pivot_id": self.anchor_low_pivot_id,
            "anchor_high_pivot_id": self.anchor_high_pivot_id,
            "available_at": self.available_at,
            "prev_version_id": self.prev_version_id,
        }


@dataclass
class LtfEntryZone:
    """Entry Zone H1: OB/FVG/BSL/SSL выбранного движения (§8).

    Геометрия наследует правила HTF, но жизненный цикл свой: касание
    потребляет текущий выбор зоны (first_test_at, §9), однако по ТЗ
    «Единый движок» §3 протестированная зона может быть выбрана повторно,
    пока max_test_depth СТРОГО меньше порога entry_reuse_max_depth.
    validity при этом не меняется — «tested» остаётся фактом истории, а
    допуск к выбору считается отдельно по max_test_depth/test_extreme.
    Для уровня (BSL/SSL) lower == upper. movement_id=0 — без привязки к
    движению (NULL в UNIQUE-дедупе не работает, поэтому NOT NULL DEFAULT 0).
    """
    id: Optional[int]
    instrument_id: int
    type: str                          # OB | FVG | BSL | SSL
    direction: Direction
    lower: float
    upper: float
    formed_at: int                     # ms: свеча-основание
    confirmed_at: Optional[int] = None
    movement_id: int = 0
    first_test_at: Optional[int] = None
    validity: str = "fresh"            # fresh | tested | invalid
    # ТЗ §3/§4: максимум глубины тестов за всю историю и глубочайший
    # экстремум (bull — min Low, bear — max High) для точных сравнений
    # порога 90% без epsilon; уровни (W=0) глубины не имеют
    max_test_depth: float = 0.0
    test_extreme: Optional[float] = None
    source: str = "auto"
    rule_version: str = RULE_VERSION_LTF
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2

    @property
    def is_level(self) -> bool:
        return self.type in ("BSL", "SSL") or self.lower == self.upper

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, ensure_ascii=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instrument_id": self.instrument_id,
            "type": self.type,
            "direction": self.direction.value,
            "lower": self.lower,
            "upper": self.upper,
            "mid": self.mid,
            "is_level": self.is_level,
            "formed_at": self.formed_at,
            "confirmed_at": self.confirmed_at,
            "movement_id": self.movement_id,
            "first_test_at": self.first_test_at,
            "validity": self.validity,
            "max_test_depth": self.max_test_depth,
            "test_extreme": self.test_extreme,
            "source": self.source,
            "rule_version": self.rule_version,
            "evidence": self.evidence,
        }


@dataclass
class LtfScenarioEntry:
    """Привязка Entry Zone к сценарию с версией диапазона (§7, §8.5).

    eligible/overlap — результат пространственного фильтра на момент
    range_version; пересчёт диапазона создаёт новую строку, а не
    переписывает старую. Ключ касания range_version не включает (§11.5).
    """
    id: Optional[int]
    scenario_id: int
    entry_zone_id: int
    range_version: int
    eligible: bool = True
    overlap: str = "none"              # full | partial | none
    state: str = "fresh"               # fresh | out_of_range | tested | invalid
    reason: str = ""                   # код пригодности (eligibility.ELIGIBILITY_REASONS);
                                       # пустой — строка до миграции (fallback из state)
    added_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "entry_zone_id": self.entry_zone_id,
            "range_version": self.range_version,
            "eligible": self.eligible,
            "overlap": self.overlap,
            "state": self.state,
            "reason": self.reason,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
        }


@dataclass
class LtfLiquidityTest:
    """Двухэтапное событие BSL/SSL: снятие и закрытие той же H1-свечи (§10).

    Равное закрытие уровню (Close=K) — неподтверждённый исход: equal_close.
    """
    id: Optional[int]
    entry_zone_id: int
    scenario_id: int
    level: float
    touch_at: int                      # ms: первое достижение уровня
    candle_open_time: int              # ms: свеча, исход которой ждём
    state: str = "awaiting_close"      # awaiting_close | confirmed | failed | equal_close
    close_price: Optional[float] = None
    sweep_at: Optional[int] = None     # ms: подтверждённое снятие с возвратом
    resolved_at: Optional[int] = None  # ms: закрытие свечи-исхода

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_zone_id": self.entry_zone_id,
            "scenario_id": self.scenario_id,
            "level": self.level,
            "touch_at": self.touch_at,
            "candle_open_time": self.candle_open_time,
            "state": self.state,
            "close_price": self.close_price,
            "sweep_at": self.sweep_at,
            "resolved_at": self.resolved_at,
        }


# Машиночитаемые причины reject в трассировке detect_breaks (§6).
# Строки стабильны — на них можно опираться в анализе и тестах.
TRACE_REJECT_REASONS = (
    "anchor_not_set",          # нет якоря (последний HH/LL ещё не поглощён)
    "ref_not_set",             # якорь есть, опорный HL/LH перед ним не найден
    "ref_already_broken",      # опорный уровень уже пробит ранее
    "internal_not_set",        # внутренний экстремум не зафиксирован
    "pullback_missing",        # откат не сформирован (§6.1/§6.3)
    "first_not_set",           # вторичный: экстремум за ref не зафиксирован
    "primary_not_fired",       # вторичный: первичный слом ещё не случился
    "secondary_done",          # вторичный этап уже зафиксирован
    "close_not_beyond_level",  # закрытие не строго за уровнем (§6)
    "duplicate_level_key",     # уровень/этап уже пробит (дедуп по level_key)
)

# Причины skip в трассировке (свеча не оценивалась машиной).
TRACE_SKIP_REASONS = (
    "candle_not_closed",       # незакрытая свеча в сломе не участвует (§6)
    "after_cancellation",      # свеча после обратного слома (§13/§6.5)
    "reverse_before_trigger",  # обратный слом раньше открытия сценария (§6.5)
)


@dataclass
class PivotAbsorb:
    """Факт поглощения pivot машиной стороны в момент его подтверждения.

    Фиксирует роль, известную на тот момент — позволяет увидеть, почему
    экстремум классифицирован как HH/HL/internal_* и когда он стал доступен.
    """
    candle_close_time: int             # ms: закрытие свечи, на которой pivot поглощён
    direction: str                     # bear | bull — сторона машины
    pivot_ref: int                     # id в ltf_pivot, до материализации — pivot_at
    kind: str                          # high | low
    role: str                          # роль на момент поглощения (HH | HL | internal_* | ...)
    price: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "candle_close_time": self.candle_close_time,
            "direction": self.direction,
            "pivot_ref": self.pivot_ref,
            "kind": self.kind,
            "role": self.role,
            "price": self.price,
        }


@dataclass
class TraceEntry:
    """Одна запись трассировки сканирования структуры (диагностика §6).

    На каждую закрытую свечу окна — по записи на каждую проверку уровня
    (primary_bos | sms | secondary_bos) каждой из двух сторон. decision:
    accept (уровень пробит, событие создано), reject (причина из
    TRACE_REJECT_REASONS), context_only (свеча раньше since_ms — только
    контекст, событий не даёт), skip (TRACE_SKIP_REASONS). Снапшот состояния
    машины — на момент ДО проверок этой свечи; *_pivot_id — id в БД или
    pivot_at до материализации. pending_pivots — подтверждённые pivots, ещё
    не доступные на этой свече (confirmed_at позже её закрытия).
    """
    candle_open_time: int
    candle_close_time: int
    direction: str                     # bear | bull — сторона машины
    check: str                         # primary_bos | sms | secondary_bos | scan
    decision: str                      # accept | reject | context_only | skip
    reason: Optional[str] = None       # TRACE_REJECT_REASONS / TRACE_SKIP_REASONS
    level_kind: Optional[str] = None   # BOS | SMS — вид проверяемого уровня
    level_stage: Optional[str] = None  # primary | secondary
    level_price: Optional[float] = None
    ref_pivot_ids: list[int] = field(default_factory=list)   # pivots уровня при accept
    pending_pivots: list[int] = field(default_factory=list)  # подтверждены, но недоступны
    anchor_price: Optional[float] = None
    anchor_pivot_id: Optional[int] = None
    ref_price: Optional[float] = None
    ref_pivot_id: Optional[int] = None
    internal_price: Optional[float] = None
    internal_pivot_id: Optional[int] = None
    pullback_price: Optional[float] = None
    pullback_pivot_id: Optional[int] = None
    first_price: Optional[float] = None
    first_pivot_id: Optional[int] = None
    pullback2_price: Optional[float] = None
    pullback2_pivot_id: Optional[int] = None
    ref_broken: bool = False
    primary_at: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candle_open_time": self.candle_open_time,
            "candle_close_time": self.candle_close_time,
            "direction": self.direction,
            "check": self.check,
            "decision": self.decision,
            "reason": self.reason,
            "level_kind": self.level_kind,
            "level_stage": self.level_stage,
            "level_price": self.level_price,
            "ref_pivot_ids": self.ref_pivot_ids,
            "pending_pivots": self.pending_pivots,
            "anchor_price": self.anchor_price,
            "anchor_pivot_id": self.anchor_pivot_id,
            "ref_price": self.ref_price,
            "ref_pivot_id": self.ref_pivot_id,
            "internal_price": self.internal_price,
            "internal_pivot_id": self.internal_pivot_id,
            "pullback_price": self.pullback_price,
            "pullback_pivot_id": self.pullback_pivot_id,
            "first_price": self.first_price,
            "first_pivot_id": self.first_pivot_id,
            "pullback2_price": self.pullback2_price,
            "pullback2_pivot_id": self.pullback2_pivot_id,
            "ref_broken": self.ref_broken,
            "primary_at": self.primary_at,
        }


@dataclass
class ScanTrace:
    """Трасса одного прогона detect_breaks: записи по свечам + поглощения.

    Диагностика, off by default: detect_breaks(trace=None) ничего не
    записывает и не меняет ни поведение, ни результат сканирования.
    """
    entries: list[TraceEntry] = field(default_factory=list)
    absorptions: list[PivotAbsorb] = field(default_factory=list)

    def entries_for(self, candle_open_time: int) -> list[TraceEntry]:
        return [e for e in self.entries if e.candle_open_time == candle_open_time]

    def accepts(self) -> list[TraceEntry]:
        return [e for e in self.entries if e.decision == "accept"]

    def rejects(self) -> list[TraceEntry]:
        return [e for e in self.entries if e.decision == "reject"]


# Решения ревью Entry Zone: базовые вердикты как на HTF (§15.3 HTF-спеки),
# minus already_breaker — машины Breaker на LTF нет. Оценка только
# фиксируется: validity зоны, привязки и движок она не меняет.
LTF_REVIEW_DECISIONS = (
    "correct",           # размечено верно
    "now_irrelevant",    # верная форма, но сейчас неактуально
    "fix_boundaries",    # поправить границы (записываются, зона не меняется)
    "wrong_base",        # другое основание
    "wrong_type",        # неверный тип/форма
    "no_context",        # недостаточно контекста для оценки
    "wrong",             # legacy rejected (reason_code обязателен, иначе unknown)
)

# Обратная совместимость старых решений (как на HTF-ревью, R13)
LTF_LEGACY_DECISIONS = {
    "confirmed": "correct",
    "rejected": "wrong",
    "corrected": "fix_boundaries",
}

# LTF-специфичные машинные коды причины (reason_code)
LTF_REVIEW_REASON_CODES = (
    "wrong_movement",    # зона построена не по тому движению (movement_id)
    "wrong_eligible",    # зона не должна была попасть в сценарий (eligible/overlap)
    "wrong_level",       # неверный уровень BSL/SSL (не тот экстремум)
    "wrong_fvg_base",    # неверная свеча-основание FVG
    "wrong_ob_base",     # неверная свеча-основание OB
    "late_zone",         # зона появилась/подтверждена слишком поздно
    "duplicate_zone",    # дублирует уже существующую зону
    "no_context",        # не могу оценить
)


@dataclass
class LtfReview:
    """Ручная оценка Entry Zone (аналог review HTF-зоны, §15.3 HTF-спеки).

    decision сохраняется как нажато (legacy-коды не переписываются);
    scenario_id — контекст: из какого сценария оценивали (сценарий может
    быть удалён, поэтому без FK).
    """
    id: Optional[int]
    entry_zone_id: int
    scenario_id: Optional[int]
    decision: str                      # LTF_REVIEW_DECISIONS + legacy-коды
    author: str = "owner"
    text: str = ""
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_zone_id": self.entry_zone_id,
            "scenario_id": self.scenario_id,
            "decision": self.decision,
            "author": self.author,
            "text": self.text,
            "created_at": self.created_at,
        }


@dataclass
class LtfReviewAssessment:
    """Раздельная оценка геометрии и актуальности Entry Zone (аналог
    ReviewAssessment). Текст комментария не дублируется — он в LtfReview.

    corrected_lower/upper — исправленные границы при fix_boundaries: они
    только записываются в датасет, сама зона не меняется.
    """
    id: Optional[int]
    entry_zone_id: int
    review_id: int
    review_decision: str               # уже нормализованное решение
    geometry_verdict: str              # valid | invalid | needs_correction | unknown
    lifecycle_verdict: Optional[str] = None   # tested | None
    reason_code: str = ""              # LTF_REVIEW_REASON_CODES / решение / unknown
    evidence_source: str = "manual_ui"
    assessed_as_of: int = 0            # ms: open_time последней закрытой H1-свечи
    reviewed_at: int = 0
    requires_clarification: bool = False
    corrected_lower: Optional[float] = None
    corrected_upper: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entry_zone_id": self.entry_zone_id,
            "review_id": self.review_id,
            "review_decision": self.review_decision,
            "geometry_verdict": self.geometry_verdict,
            "lifecycle_verdict": self.lifecycle_verdict,
            "reason_code": self.reason_code,
            "evidence_source": self.evidence_source,
            "assessed_as_of": self.assessed_as_of,
            "reviewed_at": self.reviewed_at,
            "requires_clarification": self.requires_clarification,
            "corrected_lower": self.corrected_lower,
            "corrected_upper": self.corrected_upper,
        }


@dataclass
class LtfEvent:
    """Событие окна LTF и его журнал (§11). Подавление дублей — §11.5:
    UNIQUE(dedupe_key); ключ касания не включает range_version."""
    id: Optional[int]
    observation_id: int
    kind: str                          # bos | sms | range_ready | entries_ready |
                                       # touch | sweep_confirmed | sweep_failed |
                                       # cancellation | note | context_update (§18)
    occurred_at: int
    detected_at: int
    dedupe_key: str
    scenario_id: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)
    delivered: bool = False
    delayed: bool = False              # восстановленное событие с исходным временем (§13)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "observation_id": self.observation_id,
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "detected_at": self.detected_at,
            "dedupe_key": self.dedupe_key,
            "delivered": self.delivered,
            "delayed": self.delayed,
        }
