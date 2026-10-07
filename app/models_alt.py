"""Модель данных модуля «Altcoins D1 accumulation» (изолирован, таблицы alt_*).

Независимые от HTF/LTF сущности: вселенная альткоинов (CoinMarketCap),
источники свечей D1, кандидаты и замороженные диапазоны накопления, сетапы,
структурные события (BOS/SMS/SSL), эпизоды манипуляции, точки входа, прогоны
джобы и outbox событий. Время — миллисекунды UTC (int), цены — float,
как в models.py. Состояния и типы событий — строки AltState/AltEventType.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

ALT_RULE_VERSION = "alt-0.1"

ALT_CANCEL_MODES = ("wick_on_closed_d1", "close_on_closed_d1")


class AltState(str, Enum):
    DATA_PENDING = "data_pending"            # история ещё не загружена
    SEARCHING = "searching"                  # поиск кандидата диапазона
    FORMING = "forming"                      # диапазон формируется (> forming_min_days)
    MATURE = "mature"                        # диапазон зрелый (> mature_min_days)
    ACTIVE_CONFIRMED = "active_confirmed"    # пробой подтверждён, ждём ретест/цели
    CANCELLED = "cancelled"                  # отмена по K (режим — cancel_mode)
    EXPIRED_NO_RETEST = "expired_no_retest"  # ретест не случился за окно
    TARGETS_COMPLETED = "targets_completed"  # все цели TP1..TP4 достигнуты
    REVIEW_REQUIRED = "review_required"      # неоднозначность — нужен разбор


class AltEventType(str, Enum):
    FORMING_STARTED = "forming_started"
    MATURE_FROZEN = "mature_frozen"
    MANIPULATION_STARTED = "manipulation_started"
    MANIPULATION_ENDED = "manipulation_ended"
    SSL_TAKEN = "ssl_taken"
    BOS_CONFIRMED = "bos_confirmed"
    SMS_CONFIRMED = "sms_confirmed"
    BREAKOUT = "breakout"
    RETEST = "retest"
    TARGET_HIT = "target_hit"
    CANCELLED = "cancelled"
    EXPIRED_NO_RETEST = "expired_no_retest"
    TARGETS_COMPLETED = "targets_completed"
    ENTRY_A = "entry_a"
    ENTRY_B = "entry_b"
    REVIEW_REQUIRED = "review_required"
    DATA_STALE = "data_stale"


@dataclass
class AltAsset:
    """Актив вселенной альткоинов (cmc_id — ключ CoinMarketCap)."""
    id: Optional[int]
    cmc_id: int
    symbol: str
    name: str = ""
    canonical_asset_id: Optional[str] = None   # связь с каноническим asset проекта
    cmc_rank: int = 0
    exclusion_category: Optional[str] = None   # причина исключения из вселенной
    mapping_status: str = "pending"            # pending | mapped | mapping_pending | manual
    mapping_reason: str = ""
    enabled: bool = True
    created_ms: int = 0
    updated_ms: int = 0


@dataclass
class AltInstrumentSource:
    """Источник свечей D1 актива (venue/symbol/quote + границы истории)."""
    id: Optional[int]
    asset_id: int
    venue: str                    # bybit | ...
    symbol: str                   # BTCUSDT
    quote: str = "USDT"
    earliest_available_ms: int = 0
    last_closed_ms: int = 0
    history_scope: str = "full"   # full | partial
    source_version: int = 1


@dataclass
class AltCandle:
    """Свеча D1 из alt-источника. Таймфрейм всегда D1 (модуль только D1)."""
    source_id: int
    open_time: int                # ms UTC, открытие
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timeframe: str = "D1"

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open


@dataclass
class AltRangeCandidate:
    """Кандидат диапазона накопления (живой, пересчитывается до заморозки).

    L/U/W/M — границы/ширина/середина. origin_key — стабильный ключ пары
    якорей (start/rebound) для дедупликации версий.
    """
    id: Optional[int]
    asset_id: int
    origin_key: str
    start_anchor_open_time: int   # ms: open_time свечи якоря-старта
    rebound_anchor_open_time: int # ms: open_time свечи якоря-отскока
    lower: float
    upper: float
    width: float
    mid: float
    n_days: int
    version: int = 1
    state: str = AltState.SEARCHING.value
    metrics_json: str = "{}"
    first_seen_ms: int = 0
    updated_ms: int = 0


@dataclass
class AltFrozenRange:
    """Замороженная зрелая версия диапазона (геометрия фиксируется навсегда)."""
    id: Optional[int]
    range_id: int                 # alt_range_candidate.id, из которого заморожен
    lower: float
    upper: float
    width: float
    mid: float
    start_anchor_open_time: int
    rebound_anchor_open_time: int
    included_candles: int = 0
    mature_at_ms: int = 0
    classifier_version: str = "v1"
    range_version: int = 1


@dataclass
class AltSetup:
    """Сетап накопления по замороженному диапазону (UNIQUE(asset_id, range_id)).

    K — цена отмены (cancel_price); режим проверки — cancel_mode
    (проектный выбор, см. AltConfig). targets_json — цели TP1..TP4.
    """
    id: Optional[int]
    asset_id: int
    source_id: int
    range_id: int
    state: str = AltState.SEARCHING.value
    flags_json: str = "{}"
    confirmation_event_id: Optional[int] = None
    targets_json: str = "[]"      # [{tp: 1, price: ...}, ...]
    cancel_price: Optional[float] = None   # K
    cancel_mode: str = "wick_on_closed_d1" # ALT_CANCEL_MODES
    cancel_reachable: bool = True
    breakout_close: Optional[float] = None
    breakout_closed_at: Optional[int] = None
    retest_deadline_ms: Optional[int] = None
    entry_a_id: Optional[int] = None
    entry_b_id: Optional[int] = None
    universe_eligible: bool = True
    created_ms: int = 0
    updated_ms: int = 0
    terminated_ms: Optional[int] = None


@dataclass
class AltStructureEvent:
    """Структурное событие D1 внутри сетапа: слом BOS/SMS или снятие SSL."""
    id: Optional[int]
    setup_id: int
    kind: str                     # BOS | SMS | SSL
    level_price: float
    close_price: float
    candle_open_time: int         # ms: свеча закрытия строго за уровнем
    anchors_json: str = "[]"      # якоря-pivots уровня


@dataclass
class AltManipulationEpisode:
    """Эпизод манипуляции: уход цены под L диапазона и возврат."""
    id: Optional[int]
    setup_id: int
    started_candle_open_time: int
    min_price: float
    ended_candle_open_time: Optional[int] = None
    days_below: int = 0


@dataclass
class AltEntryOpportunity:
    """Точка входа по правилам движка (app/alt/engine.py §10–§11).

    A — закрытие подтверждающей D1. B — зона ретеста [M, U] первого
    принятого ретеста. Это зафиксированные факты, не текущая заявка.
    """
    id: Optional[int]
    setup_id: int
    kind: str                     # 'A' | 'B'
    event_time_ms: int
    price: Optional[float] = None
    zone_json: str = "{}"         # {lower, upper} если вход — зона
    bases_json: str = "[]"        # основания (свечи/pivots) точки входа


@dataclass
class AltRun:
    """Прогон джобы (раз в сутки по расписанию МСК): статистика и итог."""
    id: Optional[int]
    started_ms: int
    as_of_ms: int                 # ms: граница данных прогона (последняя закрытая D1)
    status: str = "running"       # running | ok | error
    finished_ms: Optional[int] = None
    processed: int = 0
    errors: int = 0
    universe_snapshot_id: Optional[int] = None
    summary_json: str = "{}"


@dataclass
class AltEvent:
    """Outbox события модуля. Дедупликация — UNIQUE(setup_id, event_type,
    source_event_id): повторная детекция того же рыночного факта не создаёт
    вторую строку."""
    id: Optional[int]
    setup_id: int
    event_type: str               # AltEventType.value
    source_event_id: str          # стабильный ключ источника (свеча/структура)
    payload_json: str = "{}"
    event_time_ms: int = 0        # ms: время рыночного события
    detected_at_ms: int = 0       # ms: когда алгоритм его увидел
    run_id: Optional[int] = None
    delivered: bool = False
    created_ms: int = 0
