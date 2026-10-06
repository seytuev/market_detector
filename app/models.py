"""Модель данных HTF Zones (§12 спеки) — контракт между движком, БД, уведомлениями и вебом.

Время — миллисекунды UTC (int). Цены — float с точностью инструмента.
Различаем наблюдаемое рыночное событие (Event) и факт доставки (Delivery) (§12).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# Таймфреймы: минуты в одной свече (календарь источника хранится отдельно, §11)
TIMEFRAME_MINUTES = {"H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


def close_boundary_ms(open_time: int, timeframe: str) -> int:
    """Единая временная конвенция движка (ТЗ 06.10.2026 §3.6/§5).

    Граница закрытия свечи = open_time + длительность ТФ. Это EXCLUSIVE
    close boundary: первая миллисекунда СЛЕДУЮЩЕГО интервала, т.е.
    close_time + 1 мс (close_time — последняя мс своего интервала).
    Событие «свеча закрылась» доступно алгоритму с этого момента; разница
    в 1 мс между boundary и close_time — следствие конвенции, а не
    использование будущих данных.
    """
    return open_time + TIMEFRAME_MINUTES[timeframe] * 60_000


class ZoneType(str, Enum):
    FVG = "fvg"
    OB = "ob"
    PRB = "prb"
    BREAKER = "breaker"
    SSL = "ssl"
    BSL = "bsl"
    MANUAL = "manual"


class Direction(str, Enum):
    BULL = "bull"
    BEAR = "bear"


class ZoneStatus(str, Enum):
    CANDIDATE = "candidate"      # §4: автоматическая разметка до ручной проверки
    ACTIVE = "active"
    WEAKENED = "weakened"        # §3: FVG после 50% (сила −80% — качественная пометка)
    WORKED = "worked"            # 90% — только PRB/ручные (§15.7: OB на 90% валиден)
    CONVERTED = "converted"      # §6: OB превратился в Breaker
    ARCHIVED = "archived"        # пробитый Breaker / пробитый PRB / неактуальное
    TAKEN = "taken"              # §7: снятый экстремум SSL/BSL
    REJECTED = "rejected"        # ручное отклонение кандидата


class EventKind(str, Enum):
    # Касания и глубина (§8, §9)
    APPROACH = "approach"            # приближение на 2%
    TOUCH = "touch"                  # первое касание границы
    DEPTH_50 = "depth_50"            # достигнуто 50%
    DEPTH_90 = "depth_90"            # достигнуто 90% (для OB — только история глубины, не завершение; PRB — отработан)
    D1_CLOSE_INSIDE = "d1_close_inside"  # §9: закрепление внутри (закрытие D1 в зоне)
    JUMP_THROUGH = "jump_through"    # §6: проход зоны скачком насквозь
    ALREADY_IN_ZONE = "already_in_zone"  # §8: цена уже в зоне при подключении
    # FVG (§3)
    FVG_WEAKENED = "fvg_weakened"    # 50% FVG — ослаблен
    FVG_FILLED = "fvg_filled"        # полное перекрытие
    # Структура
    OB_CONFIRMED = "ob_confirmed"
    OB_INVALIDATED = "ob_invalidated"  # ТЗ 06.10.2026 §6: первое закрытие за границей
    BREAKER_CREATED = "breaker_created"
    BREAKER_ARCHIVED = "breaker_archived"
    PRB_ARCHIVED = "prb_archived"
    LEVEL_TAKEN = "level_taken"      # §7: пересечение уровня SSL/BSL
    ZONE_CONFIRMED_BY_USER = "zone_confirmed_by_user"
    # Сервис
    DATA_STALE = "data_stale"        # §11: устаревшие данные
    DATA_RECOVERED = "data_recovered"


# Какие события подлежат 120-часовому подавлению повторов (§8)
SUPPRESSED_KINDS = {EventKind.APPROACH, EventKind.TOUCH, EventKind.DEPTH_50}


@dataclass
class Instrument:
    id: Optional[int]
    asset: str            # BTC
    venue: str            # binance | hyperliquid
    market_type: str      # spot
    symbol: str           # BTCUSDT
    quote_asset: str      # USDT
    precision: int = 8
    enabled: bool = True
    # «Анализировать» (настройки LTF): LTF-наблюдения на подтверждённые
    # OB D1/W1 открываются без ожидания касания
    ltf_analyze: bool = True


@dataclass
class Candle:
    instrument_id: int
    timeframe: str        # H1 | H4 | D1 | W1
    open_time: int        # ms UTC, открытие
    close_time: int       # ms UTC, закрытие (последняя ms интервала)
    open: float
    high: float
    low: float
    close: float
    closed: bool = True
    source: str = ""      # явный источник каждого значения (§11)

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open


@dataclass
class Zone:
    id: Optional[int]
    instrument_id: int
    type: ZoneType
    direction: Direction
    timeframe: str
    lower: float                      # L; для уровней SSL/BSL — сам уровень
    upper: float                      # U; для уровней равна lower
    formed_at: int                    # ms: дата свечи-основания (§4: не путать с confirmed_at)
    confirmed_at: Optional[int]       # ms: момент подтверждения (закрытие 3-й свечи FVG и т.п.)
    status: ZoneStatus
    cycle_id: int = 1                 # жизненный цикл (Breaker — новый цикл, §6)
    source: str = "auto"              # auto | manual (§10)
    rule_version: str = "0.2"
    source_candles: list[int] = field(default_factory=list)  # open_time свечей базы
    evidence: dict[str, Any] = field(default_factory=dict)   # объяснение обнаружения (§13)
    created_at: int = 0
    # Терминальные display-поля (§15.1.3) и состояние Breaker-машины (§6/§15.6) —
    # типизированные колонки БД, а не ключи evidence
    display_from: Optional[int] = None      # ms: начало рисунка (FVG — средняя свеча)
    display_until: Optional[int] = None     # ms: конец рисунка (завершённая зона)
    end_reason: Optional[str] = None        # машинная причина завершения
    breakout_close_at: Optional[int] = None  # закрытие за границей, ждём FVG пробоя
    breaker_forbidden: bool = False         # тест >depth_mid запретил Breaker навсегда
    breakout_expired: bool = False          # окно подтверждения пробоя истекло
    # §15.7: история после подтверждения неполна — статус нельзя считать
    # проверенным до догрузки свечей и replay
    needs_replay: bool = False
    # ТЗ «Единый движок» (22.09.2026): раздельные свойства актуальности и
    # пригодности для входа. status остаётся lifecycle-статусом отображения;
    # рыночная актуальность OB теряется только закрытием свечи его ТФ строго
    # за дальней границей — не глубиной теста.
    market_validity: str = "active"     # active | invalid
    max_test_depth: float = 0.0         # максимум глубины самостоятельных тестов за всю историю
    has_tests: bool = False             # были ли самостоятельные тесты (для d=0 различаем)
    entry_eligible: bool = True         # market_validity==active AND max_test_depth < 0.90
    # Глубочайший экстремум тестов (bull OB — min Low, bear OB — max High) для
    # точных Decimal-сравнений порога 90% (ТЗ §4: без epsilon)
    test_extreme: Optional[float] = None
    # Ручные зоны (ТЗ §7): выбранное пользователем начало на графике (может быть
    # историческим) — не время создания записи; zone_type — типовое правило
    # (ob/fvg/…), т.к. type=manual сам по себе правил не задаёт
    anchor_time: Optional[int] = None
    zone_type: Optional[str] = None     # для source=manual: ob | fvg | ... ; None = type

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def is_level(self) -> bool:
        return self.type in (ZoneType.SSL, ZoneType.BSL) or self.lower == self.upper

    @property
    def manual_confirmation_only(self) -> bool:
        """ТЗ 06.10.2026 §4 (T09): зона одобрена владельцем без
        подтверждающего FVG — ручное одобрение не создаёт доказательств."""
        return bool(self.evidence.get("manual_confirmation_only"))

    def is_currently_relevant(self) -> bool:
        """ТЗ 06.10.2026 §4/§13 (T21) + ТЗ 07.10.2026 §3: единый canonical
        state актуальной зоны для API, графика, Telegram, HTF-списка и
        LTF-контекстов.

        Актуальна = подтверждена (автоматический FVG, явно только вручную
        или ручная зона владельца) И рыночно валидна И не завершена.
        Статус candidate — признак рабочего процесса РЕВЬЮ (очередь ручной
        проверки), а не рыночного состояния: подтверждённый кандидат
        актуален и отображается сразу, без ожидания оценки (ТЗ 07.10.2026:
        распознавание отделено от review_state). Неподтверждённые кандидаты,
        rejected и invalidated сюда не входят — они доступны в проверке и
        истории отдельными выборками."""
        return (
            self.status in (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED,
                            ZoneStatus.CANDIDATE)
            and self.market_validity == "active"
            and self.display_until is None
            # подтверждение: FVG, явное «только вручную» (T09) или ручная
            # зона владельца (source=manual — одобрена по определению)
            and (
                self.confirmed_at is not None
                or self.manual_confirmation_only
                or self.source == "manual"
            )
        )

    def evidence_json(self) -> str:
        return json.dumps(
            {**self.evidence, "source_candles": self.source_candles}, ensure_ascii=False
        )


@dataclass
class ZoneRelation:
    zone_id: int
    parent_ob_id: Optional[int] = None       # §5: PRB → исходный OB
    confirming_fvg_id: Optional[int] = None  # §4: OB → подтверждающий FVG
    predecessor_ob_id: Optional[int] = None  # §6: Breaker → исходный OB
    visual_group_id: Optional[int] = None    # §10: только отображение


@dataclass
class InnerLevel:
    """ТЗ «Единый движок» §5: внутренний уровень ликвидности после теста OB.

    Экстремум самостоятельного теста родительского OB (минимум теста бычьего
    OB → SSL, максимум теста медвежьего → BSL). Подтверждение — общее правило
    трёх свечей слева и трёх справа на ТФ уровня; до закрытия i+3 — кандидат.
    Снятие уровня не отменяет родительский OB; снятые остаются в истории.
    """
    id: Optional[int]
    parent_ob_id: int                 # зона-родитель (OB)
    instrument_id: int
    timeframe: str                    # ТФ экстремумов (D1 и H1 внутри D1 OB — раздельно)
    kind: str                         # "bsl" | "ssl"
    price: float                      # экстремум теста
    pivot_time: int                   # ms: свеча экстремума
    confirmed_at: Optional[int] = None   # ms: закрытие i+3; None — кандидат
    source_test_id: Optional[int] = None # visit.id породившего теста
    status: str = "candidate"         # candidate | active | taken
    taken_at: Optional[int] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0


@dataclass
class Visit:
    id: Optional[int]
    zone_id: int
    cycle_id: int
    entered_at: int
    exited_at: Optional[int] = None
    max_depth: float = 0.0
    observed: bool = True   # False — восстановлено из истории (§11)
    # Тип выхода (§15.6): return — вернулась обратно (отдельный тест),
    # beyond — ушла за дальнюю границу (пробойное движение), worked/filled —
    # завершение внутри зоны по 90%/перекрытию (цена не выходила — то же движение)
    exit_kind: Optional[str] = None
    # ТЗ §4: исходная глубина и экстремум захода сохраняются для диагностики
    # (bull OB — min Low визита, bear OB — max High)
    extreme: Optional[float] = None
    d_raw: Optional[float] = None


@dataclass
class Event:
    id: Optional[int]
    zone_id: int
    cycle_id: int
    kind: EventKind
    occurred_at: int          # ms: время рыночного события
    detected_at: int          # ms: когда алгоритм его увидел
    price: float
    depth: float = 0.0
    delayed: bool = False     # §11: восстановленное событие с исходным временем
    evidence: dict[str, Any] = field(default_factory=dict)

    def idempotency_key(self, user: str = "owner") -> str:
        """§8/§9: повторная обработка не удваивает отправку."""
        return f"{user}:{self.zone_id}:{self.cycle_id}:{self.kind.value}:{self.occurred_at}"


@dataclass
class Delivery:
    id: Optional[int]
    event_ids: list[int]
    destination: str          # telegram | log
    status: str               # pending | sent | failed
    idempotency_key: str
    delivered_at: Optional[int] = None
    error: Optional[str] = None


@dataclass
class AlertState:
    """§8: ключ подавления user+объект+цикл+вид/порог; перезапуск не сбрасывает."""
    zone_id: int
    cycle_id: int
    event_kind: str
    last_delivered_at: int
    user: str = "owner"
    muted_until: Optional[int] = None   # кнопка «Отложить»/«Отключить» (§9)
    acknowledged: bool = False          # кнопка «Изучаю»


@dataclass
class Review:
    """§10: ручная проверка кандидатов и заметки."""
    id: Optional[int]
    zone_id: int
    decision: str           # confirmed | corrected | rejected | note + коды §15.3
    author: str = "owner"
    text: str = ""
    boundary_version: int = 1
    created_at: int = 0


@dataclass
class ReviewAssessment:
    """§15.1.1/§15.3 (R01, R13): раздельная оценка геометрии и актуальности.

    Решение ревью (review_decision) не равно ни вердикту о форме
    (geometry_verdict), ни состоянию жизненного цикла (lifecycle_verdict):
    «размечено верно» не воскрешает завершённый объект, «сейчас неактуально»
    не отменяет правильную геометрию. Текст комментария здесь не дублируется —
    он хранится в review (§12).
    """
    id: Optional[int]
    zone_id: int
    review_id: int                     # ссылка на review.id
    review_decision: str               # correct | now_irrelevant | fix_boundaries | ...
    geometry_verdict: str              # valid | invalid | needs_correction | unknown
    lifecycle_verdict: Optional[str] = None   # completed | converted | None
    reason_code: str = ""              # машинный код причины (R13)
    evidence_source: str = "manual_ui" # откуда взята оценка
    assessed_as_of: int = 0            # ms: состояние актуально на этот момент
    reviewed_at: int = 0               # ms: время самого действия ревью
    requires_clarification: bool = False  # True — нужен контекст/уточнение (§15.4)


@dataclass
class BoundaryCorrection:
    """§15.2 (R07/R08): версионированная правка границ с якорем-свечой.

    Хранит исходные и исправленные границы, точную свечу-якорь и причину:
    приблизительное число из комментария не должно подменять проверяемую
    поправку (R08).
    """
    id: Optional[int]
    zone_id: int
    boundary_version: int
    original_lower: float
    original_upper: float
    corrected_lower: float
    corrected_upper: float
    anchor_candle_open_time: Optional[int] = None  # ms; None — якорь не указан
    reason: str = ""
    created_at: int = 0


def now_ms() -> int:
    import time

    return int(time.time() * 1000)
