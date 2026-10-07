"""Движок «Altcoins D1 accumulation» — §5–§13 ТЗ (07.10.2026).

Чистый, replay-безопасный последовательный разбор закрытых D1-свечей одного
актива: ATH/просадка (§5), стартовые якоря по pivots 3+3 (§5), формирующийся
диапазон и возраст (§6), классификатор боковика v1 (§7), зрелость и freeze (§8),
манипуляция и SSL (§9), структура D1 и вход A (§10), breakout/ретест/14 дней
(§11), отмена K (§12), подтверждение и цели TP1..TP4 (§13).

Принципы:
- чистые функции для математики (ATH, классификатор, геометрия, структура),
  тонкая оркестрация с БД поверх;
- replay никогда не использует будущие свечи: на шаге t доступны только
  закрытые свечи prefix[:t+1]; pivot доступен после закрытия 3 правых (§5);
- идемпотентность: origin_key диапазона и source_event_id событий стабильны
  (формат `{type}:{setup_id}:{candle_open_time}`, без run_id) — повторный
  replay дедуплицируется UNIQUE-ключом alt_event; строки структуры/эпизодов/
  входов дедуплицируются find-методами репозитория;
- пороги только из AltConfig (классификатор отделён от торговых порогов, §7.4);
- состояние пост-freeze логики каждый прогон пересчитывается из свечей
  (in-memory _MatureState), БД — только идемпотентная запись: терминальный
  сетап не воскресает, поздний ретест не оживляет EXPIRED (§11).

Проектные решения (ТЗ их явно не фиксирует):
- неоднозначность старта (REVIEW_REQUIRED): после выбора стартового pivot low
  любой другой подтверждённый pivot low того же участка с ТОЧНО равной ценой
  считается равноправной альтернативной опорой (двойное дно);
- если классификатор отклоняет участок как направленный, кандидат остаётся
  в поиске (пере-якорение на следующий pivot low — отдельный этап);
- готовность классификатора: минимум 3 блока, k>=1, W>0;
- события FORMING_STARTED/REVIEW_REQUIRED эмитируются при создании сетапа
  (схема alt_event требует setup_id), с исходным event_time распознавания;
- «защищённый LH медвежьей структуры» (§10) — последний подтверждённый pivot
  high с ролью LH, не пробитый закрытием; уровень сгорает при BOS;
- внутренний SSL (§9) — подтверждённый pivot low 3+3, сформированный не раньше
  start_anchor диапазона; снятие — Low закрытой D1 строго ниже уровня;
- структурные события до maturity persist'ятся как historical (§10) и никогда
  не становятся входом задним числом; обратные BOS/SMS — только строки
  alt_structure_event (аналитический факт, без уведомления и без отмены);
- REVIEW_REQUIRED блокирует входы/подтверждение (нужен разбор опор), но не
  защитную отмену и не факты breakout/ретеста;
- days_below эпизода манипуляции — число свечей эпизода с Low < L;
- несколько TP на одной D1 — ОДНО событие TARGET_HIT со списком уровней
  (§13 «объединять в сообщение»); каждый уровень — один раз за setup_id;
- при совпадении отмены/ретеста/цели/входа на одной D1 события-факты эмитируются
  с payload-флагом intra_candle_sequence_unknown, но новые возможности не
  выдаются, если отмена приоритетна (§12).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from ..config import AltConfig
from ..db import Database
from ..engine.liquidity import PivotRecord, find_pivots, level_crossed
from ..models import (
    Candle,
    Direction,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
)
from ..models_alt import (
    AltEntryOpportunity,
    AltEvent,
    AltEventType,
    AltFrozenRange,
    AltManipulationEpisode,
    AltRangeCandidate,
    AltSetup,
    AltState,
    AltStructureEvent,
)

DAY_MS = 86_400_000
ALT_TIMEFRAME = "D1"

# Стадии кандидата внутри SEARCHING (state-колонка candidate — AltState,
# substage живёт в metrics_json / summary)
SUBSTAGE_RANGE_PENDING = "range_pending"


# ---------------------------------------------------------------------------
# Чистые функции: валидация и статистика
# ---------------------------------------------------------------------------


def validate_ohlc(open_: float, high: float, low: float, close: float) -> Optional[str]:
    """Санитарная проверка OHLC (§5). Возвращает причину или None.

    Повреждённая свеча — ошибка данных, а не молча используемое значение."""
    values = (open_, high, low, close)
    if any(not math.isfinite(v) for v in values):
        return "non_finite"
    if any(v <= 0 for v in values):
        return "non_positive"
    if high < low:
        return "high_below_low"
    if high < open_ or high < close:
        return "high_below_body"
    if low > open_ or low > close:
        return "low_above_body"
    return None


def median(xs: Sequence[float]) -> float:
    """Медиана (для чётного N — среднее двух центральных)."""
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


# ---------------------------------------------------------------------------
# §7: классификатор «боковик или тренд» (v1, проектные пороги)
# ---------------------------------------------------------------------------


@dataclass
class ClassifierResult:
    """Результат classify_sideways. Метрики считаются всегда, когда хватает
    данных; вердикт sideways — только при ready."""

    ready: bool
    sideways: bool
    reason: str                       # ok | not_enough_data | zero_width | degenerate_x
    slope_b: Optional[float] = None   # знаковый наклон центров блоков
    slope_sign: str = "na"            # up | down | flat | na
    slope_normalized: Optional[float] = None
    center_shift: Optional[float] = None
    failed_conditions: tuple[str, ...] = ()
    efficiency: Optional[float] = None
    n_blocks: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    classifier_version: str = "v1"


def classify_sideways(
    closes: Sequence[float],
    width: float,
    cfg: AltConfig,
    candles: Optional[Sequence[Any]] = None,
) -> ClassifierResult:
    """Классификатор боковика v1 (§7.1–§7.3). Чистая функция.

    closes — Close свечей участка от start_anchor (порядок = время),
    width — W диапазона (U−L>0 по теням того же участка),
    candles — необязательно свечи участка для диагностики по теням
    (касания границ, концентрация ширины в одной тени).

    x_j — средний 0-based индекс календарного дня внутри блока относительно
    начала участка (проверено на синтетических векторах §7.4).
    """
    version = cfg.classifier_version
    n = len(closes)
    block = cfg.classifier_block_days
    diagnostics: dict[str, Any] = {}

    # §7.3: Efficiency по дневным Close (0 при нулевом знаменателе)
    efficiency: Optional[float] = None
    if n >= 2:
        denom = sum(abs(closes[i] - closes[i - 1]) for i in range(1, n))
        efficiency = (
            abs(closes[-1] - closes[0]) / denom if denom > 0 else 0.0
        )
        diagnostics["close_min"] = min(closes)
        diagnostics["close_max"] = max(closes)
        diagnostics["close_mean"] = sum(closes) / n
        diagnostics["close_median"] = median(closes)

    # Диагностика по теням (§7.3): касания границ и концентрация ширины
    if candles:
        lows = [c.low for c in candles]
        highs = [c.high for c in candles]
        seg_l, seg_u = min(lows), max(highs)
        touches = sum(
            1 for c in candles if c.low == seg_l or c.high == seg_u
        )
        sorted_lows = sorted(set(lows))
        sorted_highs = sorted(set(highs), reverse=True)
        second_low = sorted_lows[1] if len(sorted_lows) > 1 else seg_l
        second_high = sorted_highs[1] if len(sorted_highs) > 1 else seg_u
        w = seg_u - seg_l
        concentration = (
            max(second_low - seg_l, seg_u - second_high) / w if w > 0 else 0.0
        )
        diagnostics["touch_count"] = touches
        diagnostics["width_concentration_single_wick"] = concentration
        # NB: «доля Close внутри [L,U]» в период формирования тавтологично
        # равна 100% (рамка=max/min того же участка) и НЕ используется как
        # метрика (§7.3). Для пост-freeze наблюдений — main_time_fraction().

    # Готовность: минимум 3 блока, k>=1, W>0 (проектный минимум данных;
    # движок вызывает классификатор только при N_days > forming_min_days)
    n_blocks = (n + block - 1) // block if n else 0
    k = n // 3
    if width <= 0 or not math.isfinite(width):
        return ClassifierResult(False, False, "zero_width", n_blocks=n_blocks,
                                efficiency=efficiency, diagnostics=diagnostics,
                                classifier_version=version)
    if n_blocks < 3 or k < 1:
        return ClassifierResult(False, False, "not_enough_data", n_blocks=n_blocks,
                                efficiency=efficiency, diagnostics=diagnostics,
                                classifier_version=version)

    # §7.1: блоки по block_days закрытых D1, последний неполный — с фактическими
    xs: list[float] = []
    ys: list[float] = []
    for start in range(0, n, block):
        idxs = list(range(start, min(start + block, n)))
        xs.append(sum(idxs) / len(idxs))
        ys.append(median([closes[i] for i in idxs]))
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return ClassifierResult(False, False, "degenerate_x", n_blocks=n_blocks,
                                efficiency=efficiency, diagnostics=diagnostics,
                                classifier_version=version)
    b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    slope_normalized = abs(b) * (max(xs) - min(xs)) / width

    # §7.2: сдвиг центра первой/последней трети
    c_first = median(closes[:k])
    c_last = median(closes[n - k:])
    center_shift = abs(c_last - c_first) / width

    failed: list[str] = []
    if slope_normalized > cfg.classifier_slope_max:
        failed.append("slope")
    if center_shift > cfg.classifier_center_shift_max:
        failed.append("center_shift")

    return ClassifierResult(
        ready=True,
        sideways=not failed,
        reason="ok",
        slope_b=b,
        slope_sign="up" if b > 0 else ("down" if b < 0 else "flat"),
        slope_normalized=slope_normalized,
        center_shift=center_shift,
        failed_conditions=tuple(failed),
        efficiency=efficiency,
        n_blocks=n_blocks,
        diagnostics=diagnostics,
        classifier_version=version,
    )


def main_time_fraction(closes: Sequence[float], lower: float, upper: float) -> float:
    """§7.3: доля Close внутри ЗАФИКСИРОВАННЫХ границ на последующих свечах.

    Диагностический показатель (проектный порог cfg.main_time_fraction),
    применим только ПОСЛЕ freeze к новым свечам; в период формирования
    тавтологичен (§7.3) и здесь не вычисляется.
    """
    if not closes:
        return 0.0
    inside = sum(1 for c in closes if lower <= c <= upper)
    return inside / len(closes)


# ---------------------------------------------------------------------------
# §5: ATH / просадка (чистый трекер эпизодов)
# ---------------------------------------------------------------------------


@dataclass
class AthEpisode:
    """Эпизод ATH: выбранная вершина, минимум после неё и факт глубокого падения.

    При равенстве High текущему ATH началом эпизода становится ПОСЛЕДНЯЯ
    свеча с этой ценой; все равные вершины сохраняются в equal_top_open_times.
    """

    ath_price: float
    ath_open_time: int
    equal_top_open_times: list[int] = field(default_factory=list)
    p_min: Optional[float] = None          # min Low строго после ATH
    p_min_open_time: Optional[int] = None
    deep_drop_open_time: Optional[int] = None  # свеча, достигшая порога

    @property
    def drawdown(self) -> Optional[float]:
        """1 − P_min/A; None, если после ATH ещё нет свечей (без деления)."""
        if self.p_min is None:
            return None
        return 1 - self.p_min / self.ath_price

    @property
    def deep_drop_achieved(self) -> bool:
        return self.deep_drop_open_time is not None


class AthTracker:
    """Последовательный трекер ATH по теням (§5). Только прошлые/текущая свеча.

    Новый ATH (в т.ч. равный по цене) открывает НОВЫЙ эпизод для будущего
    поиска; достигнутый факт падения эпизода не отменяется восстановлением
    цены и не переписывает уже подтверждённую историю (эпизоды сохраняются).
    """

    def __init__(self, cfg: AltConfig):
        self.cfg = cfg
        self.current: Optional[AthEpisode] = None
        self.history: list[AthEpisode] = []

    def update(self, open_time: int, high: float, low: float) -> AthEpisode:
        ep = self.current
        if ep is None or high >= ep.ath_price:
            # Новый эпизод: строго выше — новая вершина; равенство — последняя
            # свеча с той же ценой становится началом эпизода (проектное правило)
            equal_tops = [open_time]
            if ep is not None and high == ep.ath_price:
                equal_tops = ep.equal_top_open_times + [open_time]
                self.history.append(ep)
            elif ep is not None:
                self.history.append(ep)
            ep = AthEpisode(ath_price=high, ath_open_time=open_time,
                            equal_top_open_times=equal_tops)
            self.current = ep
            return ep
        # Свеча строго после ATH: минимум по теням, порог — строгий
        if ep.p_min is None or low < ep.p_min:
            ep.p_min = low
            ep.p_min_open_time = open_time
        threshold_price = ep.ath_price * (1 - self.cfg.drawdown_threshold)
        if ep.deep_drop_open_time is None and low < threshold_price:
            # Строго: ровно 80% (low == A*0.20) НЕ подходит (§5, T05)
            ep.deep_drop_open_time = open_time
        return ep


# ---------------------------------------------------------------------------
# §6: геометрия диапазона (чистая)
# ---------------------------------------------------------------------------


@dataclass
class RangeGeometry:
    """L/U по теням участка; W=U−L>0; M=(L+U)/2. Расширение до freeze (§6)."""

    lower: float
    upper: float

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2

    def expand(self, low: float, high: float) -> bool:
        """Расширить границы новой свечой. True, если границы изменились."""
        changed = False
        if low < self.lower:
            self.lower = low
            changed = True
        if high > self.upper:
            self.upper = high
            changed = True
        return changed


def range_age_days(start_open_time: int, last_open_time: int) -> int:
    """§6: N_days = (exclusive close boundary последней D1 − start open_time)
    / 86400000. Для непрерывных D1 — число закрытых свечей участка."""
    return int(
        (close_boundary_ms(last_open_time, ALT_TIMEFRAME) - start_open_time)
        // DAY_MS
    )


def _to_engine_candle(c: Any) -> Candle:
    """AltCandle-подобный объект → Candle для общего find_pivots (все закрыты)."""
    return Candle(
        instrument_id=getattr(c, "source_id", 0),
        timeframe=ALT_TIMEFRAME,
        open_time=c.open_time,
        close_time=close_boundary_ms(c.open_time, ALT_TIMEFRAME) - 1,
        open=c.open, high=c.high, low=c.low, close=c.close,
        closed=True, source="alt",
    )


# ---------------------------------------------------------------------------
# §10: структура D1 — роли HH/HL/LH/LL, BOS/SMS (чистый трекер)
# ---------------------------------------------------------------------------


@dataclass
class StructureEvent:
    """Факт структуры на закрытии D1 (§10). kind: BOS | SMS — бычьи;
    BOS_REV | SMS_REV — обратные (только аналитика); SSL — снятие внутреннего
    структурного SSL (§9)."""

    kind: str
    level_price: float
    close_price: float
    candle_open_time: int          # свеча ЗАКРЫТИЯ строго за уровнем
    anchors: list[dict[str, Any]] = field(default_factory=list)


class StructureTracker:
    """Структура D1 по подтверждённым pivots 3+3 (по теням, §10).

    Роли HH/HL/LH/LL — структурные роли относительно предыдущего подтверждённого
    pivot того же типа, а не подписи любого локального экстремума.

    Bullish BOS: Close строго выше защищённого LH (последний подтверждённый
    pivot high с ролью LH); уровень сгорает при сломе — повторные закрытия
    за уже пробитым уровнем нового BOS не создают.
    Bullish SMS: минимум → внутренний high → higher low → Close строго выше
    внутреннего high; обе опоры и исходный минимум — в anchors.
    Обратные BOS/SMS — зеркально, как аналитический факт (kind *_REV).
    Тень и Close == level не подтверждают слом — проверяется только Close,
    строгое сравнение.
    """

    def __init__(self) -> None:
        self.last_high: Optional[PivotRecord] = None
        self.last_low: Optional[PivotRecord] = None
        self.roles: dict[int, str] = {}            # formed_at -> HH|HL|LH|LL
        self.protected_lh: Optional[PivotRecord] = None  # цель bullish BOS
        self.protected_hl: Optional[PivotRecord] = None  # цель обратного BOS
        self._bos_fired: set[int] = set()          # formed_at пробитых уровней
        self._bos_rev_fired: set[int] = set()
        # Bullish SMS-цепочка: минимум → внутренний high → higher low
        self.sms_min: Optional[PivotRecord] = None
        self.sms_high: Optional[PivotRecord] = None
        self.sms_hl: Optional[PivotRecord] = None
        self._sms_fired: set[int] = set()          # formed_at внутреннего high
        # Обратная SMS-цепочка: максимум → внутренний low → lower high
        self.rev_max: Optional[PivotRecord] = None
        self.rev_low: Optional[PivotRecord] = None
        self.rev_lh: Optional[PivotRecord] = None
        self._sms_rev_fired: set[int] = set()

    def add_pivot(self, p: PivotRecord) -> None:
        """Роль и уровни по свежеподтверждённому pivot (вызов — в момент
        подтверждения, будущие свечи недоступны)."""
        if p.kind == ZoneType.BSL:
            if self.last_high is None or p.price > self.last_high.price:
                self.roles[p.formed_at] = "HH"
            else:
                self.roles[p.formed_at] = "LH"
                self.protected_lh = p  # новый защищённый LH медвежьей структуры
            self.last_high = p
            # bullish SMS: первый внутренний high после минимума
            if (
                self.sms_min is not None
                and p.formed_at > self.sms_min.formed_at
                and self.sms_high is None
            ):
                self.sms_high = p
            # обратная SMS: новый максимум сбрасывает цепочку; иначе lower high
            if self.rev_max is None or p.price > self.rev_max.price:
                self.rev_max, self.rev_low, self.rev_lh = p, None, None
            elif self.rev_low is not None and p.formed_at > self.rev_low.formed_at:
                self.rev_lh = p
        else:  # SSL — pivot low
            if self.last_low is None or p.price > self.last_low.price:
                self.roles[p.formed_at] = "HL"
                self.protected_hl = p  # защищённый HL для обратного BOS
            else:
                self.roles[p.formed_at] = "LL"
            self.last_low = p
            # bullish SMS: новый минимум сбрасывает цепочку; иначе higher low
            if self.sms_min is None or p.price < self.sms_min.price:
                self.sms_min, self.sms_high, self.sms_hl = p, None, None
            elif self.sms_high is not None and p.formed_at > self.sms_high.formed_at:
                self.sms_hl = p
            # обратная SMS: первый внутренний low после максимума
            if (
                self.rev_max is not None
                and p.formed_at > self.rev_max.formed_at
                and self.rev_low is None
            ):
                self.rev_low = p

    def on_close(self, open_time: int, close: float) -> list[StructureEvent]:
        """События структуры на закрытии свечи (только Close, строго)."""
        out: list[StructureEvent] = []
        lh = self.protected_lh
        if (
            lh is not None
            and lh.formed_at not in self._bos_fired
            and close > lh.price
        ):
            self._bos_fired.add(lh.formed_at)
            self.protected_lh = None
            out.append(StructureEvent(
                "BOS", lh.price, close, open_time,
                anchors=[{"formed_at": lh.formed_at, "price": lh.price,
                          "role": "LH"}],
            ))
        if (
            self.sms_min is not None
            and self.sms_high is not None
            and self.sms_hl is not None
            and self.sms_high.formed_at not in self._sms_fired
            and close > self.sms_high.price
        ):
            self._sms_fired.add(self.sms_high.formed_at)
            out.append(StructureEvent(
                "SMS", self.sms_high.price, close, open_time,
                anchors=[
                    {"formed_at": self.sms_min.formed_at,
                     "price": self.sms_min.price, "role": "min"},
                    {"formed_at": self.sms_high.formed_at,
                     "price": self.sms_high.price, "role": "internal_high"},
                    {"formed_at": self.sms_hl.formed_at,
                     "price": self.sms_hl.price, "role": "higher_low"},
                ],
            ))
        hl = self.protected_hl
        if (
            hl is not None
            and hl.formed_at not in self._bos_rev_fired
            and close < hl.price
        ):
            self._bos_rev_fired.add(hl.formed_at)
            self.protected_hl = None
            out.append(StructureEvent(
                "BOS_REV", hl.price, close, open_time,
                anchors=[{"formed_at": hl.formed_at, "price": hl.price,
                          "role": "HL"}],
            ))
        if (
            self.rev_max is not None
            and self.rev_low is not None
            and self.rev_lh is not None
            and self.rev_low.formed_at not in self._sms_rev_fired
            and close < self.rev_low.price
        ):
            self._sms_rev_fired.add(self.rev_low.formed_at)
            out.append(StructureEvent(
                "SMS_REV", self.rev_low.price, close, open_time,
                anchors=[
                    {"formed_at": self.rev_max.formed_at,
                     "price": self.rev_max.price, "role": "max"},
                    {"formed_at": self.rev_low.formed_at,
                     "price": self.rev_low.price, "role": "internal_low"},
                    {"formed_at": self.rev_lh.formed_at,
                     "price": self.rev_lh.price, "role": "lower_high"},
                ],
            ))
        return out


def _ssl_zone(price: float, formed_at: int, confirmed_at: int) -> Zone:
    """Уровень SSL как Zone общего движка — для level_crossed (§9/§14 ТЗ:
    геометрия общих правил переиспользуется)."""
    return Zone(
        id=None, instrument_id=0, type=ZoneType.SSL, direction=Direction.BULL,
        timeframe=ALT_TIMEFRAME, lower=price, upper=price, formed_at=formed_at,
        confirmed_at=confirmed_at, status=ZoneStatus.ACTIVE,
    )


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------


@dataclass
class _MatureState:
    """Пост-freeze состояние сетапа внутри одного replay (§9–§13).

    Пересчитывается из свечей при каждом прогоне; в БД пишется идемпотентно.
    """

    manip_id: Optional[int] = None          # открытый эпизод манипуляции
    manip_start_ot: Optional[int] = None
    manip_min: float = math.inf
    manip_days: int = 0
    breakout_open_time: Optional[int] = None
    confirmed: bool = False
    confirmation_open_time: Optional[int] = None
    targets: list[float] = field(default_factory=list)
    targets_hit: set[int] = field(default_factory=set)
    entry_a_done: bool = False
    entry_b_done: bool = False
    retest_done: bool = False
    terminal: bool = False


@dataclass
class _CandidateState:
    """Живое состояние кандидата внутри одного replay."""

    start: PivotRecord
    rebound: Optional[PivotRecord] = None
    start_idx: int = 0
    geometry: Optional[RangeGeometry] = None
    version: int = 0
    versions: list[dict[str, Any]] = field(default_factory=list)
    review: bool = False
    alternative_anchor_open_times: list[int] = field(default_factory=list)
    forming_recognized_ms: Optional[int] = None
    forming_n_days: Optional[int] = None
    frozen: bool = False
    candidate_id: Optional[int] = None
    n_days: int = 0
    classifier: Optional[ClassifierResult] = None
    # ATH-эпизод, давший глубокое падение перед участком (§5) — для
    # текстов уведомлений (§18: «после падения X% от биржевого ATH»)
    ath_price: Optional[float] = None
    deep_drop_drawdown: Optional[float] = None
    # §9: внутренние SSL (pivot lows, formed_at >= start_anchor) и их снятия
    ssl_levels: list[PivotRecord] = field(default_factory=list)
    ssl_taken: set[int] = field(default_factory=set)
    # §10: структурные факты до maturity — только исторически
    pre_facts: list[StructureEvent] = field(default_factory=list)
    mstate: Optional[_MatureState] = None

    @property
    def origin_key(self) -> str:
        return f"{self.start.formed_at}:{self.rebound.formed_at if self.rebound else 0}"


class AltEngine:
    """Оркестратор §5–§8: replay закрытых D1 одного актива с персистенцией.

    process_asset_history полностью перематывает историю детерминированно
    (backfill и ежедневное догоняние — один код); записи в БД идемпотентны:
    кандидат по origin_key, сетап по UNIQUE(asset_id, range_id), события по
    UNIQUE(setup_id, event_type, source_event_id). Прогресс — meta KV
    alt:engine:{asset_id}:{source_id}. Завершённый (terminated) диапазон
    никогда не воскресает; новый самостоятельный диапазон требует новых
    якорей (новый origin_key → новый range_id).
    """

    def __init__(self, db: Database, cfg: AltConfig):
        self.db = db
        self.cfg = cfg
        self._pivot_cfg = SimpleNamespace(
            pivot_left=cfg.pivot_left, pivot_right=cfg.pivot_right
        )

    # ----- публичный API -----

    def process_asset_history(
        self,
        asset_id: int,
        source_id: int,
        candles: Sequence[Any],
        detected_at_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Replay закрытых D1 (любой порядок на входе — сортируем, §4).

        Возвращает summary: состояние, ATH-эпизод, кандидат/диапазон/сетап,
        классификатор, ошибки данных. detected_at_ms по умолчанию — граница
        закрытия последней свечи (детерминировано для replay).
        """
        ordered = sorted(candles, key=lambda c: c.open_time)
        valid: list[Any] = []
        data_errors: list[dict[str, Any]] = []
        seen_open_times: set[int] = set()
        for c in ordered:
            reason = validate_ohlc(c.open, c.high, c.low, c.close)
            if reason is None and c.open_time in seen_open_times:
                reason = "duplicate_open_time"
            if reason is not None:
                data_errors.append({"open_time": c.open_time, "reason": reason})
                continue
            seen_open_times.add(c.open_time)
            valid.append(c)

        min_candles = self.cfg.pivot_left + self.cfg.pivot_right + 1
        scope = "full"
        src = self.db.get_alt_instrument_source(asset_id)
        if src is not None and src.id == source_id:
            scope = src.history_scope
        if not valid or len(valid) < min_candles or scope == "partial":
            state = AltState.DATA_PENDING.value
            summary = {
                "asset_id": asset_id, "source_id": source_id, "state": state,
                "data_pending": True, "history_scope": scope,
                "candles_total": len(ordered), "candles_valid": len(valid),
                "data_errors": data_errors,
            }
            self._save_progress(asset_id, source_id, valid, summary)
            return summary

        detected_at = (
            detected_at_ms
            if detected_at_ms is not None
            else close_boundary_ms(valid[-1].open_time, ALT_TIMEFRAME)
        )

        tracker = AthTracker(self.cfg)
        structure = StructureTracker()
        cand: Optional[_CandidateState] = None
        known_pivots: set[tuple[str, int]] = set()
        frozen_row: Optional[AltFrozenRange] = None
        setup: Optional[AltSetup] = None
        events: list[tuple[str, bool]] = []
        terminal_setup = self._terminal_setup(asset_id)

        for t, c in enumerate(valid):
            ep = tracker.update(c.open_time, c.high, c.low)

            # Pivots подтверждаются на каждом шаге (уровень доступен после
            # закрытия 3 правых, §5/§10); структурный трекер видит всю историю
            new_pivots = self._newly_confirmed_pivots(valid, t, known_pivots)
            for p in new_pivots:
                structure.add_pivot(p)
                if (
                    cand is not None
                    and p.kind == ZoneType.SSL
                    and p.formed_at >= cand.start.formed_at
                ):
                    cand.ssl_levels.append(p)  # внутренний SSL участка (§9)

            # Пост-freeze: манипуляция/структура/входы/цели/отмена (§9–§13)
            if cand is not None and cand.frozen and frozen_row is not None:
                events.extend(self._mature_step(
                    cand, frozen_row, setup, structure, valid, t, detected_at
                ))
                continue

            # Факты структуры/SSL до maturity — буфер «только исторически»
            if cand is not None and t >= cand.start_idx:
                cand.pre_facts.extend(self._ssl_check(cand, c))
                cand.pre_facts.extend(structure.on_close(c.open_time, c.close))

            if terminal_setup is not None or not ep.deep_drop_achieved:
                continue

            if cand is None:
                cand = self._try_start_candidate(ep, new_pivots, valid, t,
                                                 asset_id, detected_at)
                continue

            if cand.rebound is None:
                self._try_rebound(cand, new_pivots, valid, t, asset_id,
                                  detected_at)
                continue

            # Неоднозначность стартовой опоры: равный по цене pivot low
            # того же участка — альтернативный якорь (проектное правило)
            for p in new_pivots:
                if (
                    p.kind == ZoneType.SSL
                    and cand.start.formed_at < p.formed_at
                    and p.price == cand.start.price
                    and p.formed_at not in cand.alternative_anchor_open_times
                ):
                    cand.review = True
                    cand.alternative_anchor_open_times.append(p.formed_at)

            frozen_row, setup, ev = self._range_step(
                cand, valid, t, asset_id, source_id, detected_at, structure
            )
            events.extend(ev)

        if cand is not None:
            state = self._persist_candidate(asset_id, cand, detected_at)
        elif terminal_setup is not None:
            # Завершённый сетап не воскресает и не пересчитывается (§15):
            # сводка — из сохранённого состояния, история остаётся
            state = terminal_setup.state
            if setup is None:
                setup = terminal_setup
                frozen_row = self.db.get_alt_frozen_range(setup.range_id)
        else:
            state = self._persist_candidate(asset_id, cand, detected_at)
        summary = {
            "asset_id": asset_id,
            "source_id": source_id,
            "state": state,
            "data_pending": False,
            "history_scope": scope,
            "candles_total": len(ordered),
            "candles_valid": len(valid),
            "data_errors": data_errors,
            "ath": self._ath_summary(tracker),
            "candidate_id": (
                cand.candidate_id if cand
                else (frozen_row.range_id if frozen_row else None)
            ),
            "origin_key": (
                f"{asset_id}:{source_id}:{cand.origin_key}" if cand else None
            ),
            "n_days": cand.n_days if cand else 0,
            "range_version": cand.version if cand else 0,
            "review_required": bool(cand and cand.review),
            "alternative_anchors": (
                list(cand.alternative_anchor_open_times) if cand else []
            ),
            "classifier": self._classifier_summary(cand),
            "frozen_range_id": frozen_row.id if frozen_row else None,
            "setup_id": setup.id if setup else None,
            "setup_state": setup.state if setup else None,
            "setup_flags": (
                json.loads(setup.flags_json) if setup and setup.flags_json
                else None
            ),
            "events": events,
        }
        self._save_progress(asset_id, source_id, valid, summary)
        return summary

    # ----- pivots -----

    def _newly_confirmed_pivots(
        self, candles: Sequence[Any], t: int, known: set[tuple[str, int]]
    ) -> list[PivotRecord]:
        """Pivots, чьё подтверждение стало доступно ровно на свече t.

        Центр окна — свеча t−pivot_right; find_pivots общего движка зовётся
        на окне left+1+right, что эквивалентно полному префиксу (строгое
        правило 3+3 локально), но без O(N²). Будущие свечи не используются.
        """
        left, right = self.cfg.pivot_left, self.cfg.pivot_right
        if t < left + right:
            return []
        window = [
            _to_engine_candle(c) for c in candles[t - left - right : t + 1]
        ]
        out: list[PivotRecord] = []
        for p in find_pivots(window, ALT_TIMEFRAME, self._pivot_cfg):
            key = (p.kind.value, p.formed_at)
            if key not in known:
                known.add(key)
                out.append(p)
        return out

    # ----- стартовые якоря (§5) -----

    def _try_start_candidate(
        self,
        ep: AthEpisode,
        new_pivots: list[PivotRecord],
        candles: Sequence[Any],
        t: int,
        asset_id: int,
        detected_at: int,
    ) -> Optional[_CandidateState]:
        """Первый подтверждённый pivot low ПОСЛЕ свечи достижения падения —
        кандидат старта консолидации (самая ранняя опора участка). Падение
        до него в диапазон не вклеивается."""
        lows = [
            p for p in new_pivots
            if p.kind == ZoneType.SSL and p.formed_at > ep.deep_drop_open_time
        ]
        if not lows:
            return None
        start = min(lows, key=lambda p: p.formed_at)
        start_idx = next(
            i for i, c in enumerate(candles) if c.open_time == start.formed_at
        )
        cand = _CandidateState(start=start, start_idx=start_idx)
        cand.ath_price = ep.ath_price
        cand.deep_drop_drawdown = ep.drawdown
        # Кандидат существует в памяти сразу (range_pending до rebound);
        # строка в БД появляется, когда определены обе опоры (границы)
        self._try_rebound(cand, new_pivots, candles, t, asset_id, detected_at)
        return cand

    def _try_rebound(
        self,
        cand: _CandidateState,
        new_pivots: list[PivotRecord],
        candles: Sequence[Any],
        t: int,
        asset_id: int,
        detected_at: int,
    ) -> None:
        """Первый подтверждённый pivot high после start low задаёт первичный
        верх; до его доступности кандидат — range_pending (§5)."""
        if cand.rebound is not None:
            return
        highs = [
            p for p in new_pivots
            if p.kind == ZoneType.BSL and p.formed_at > cand.start.formed_at
        ]
        if not highs:
            return
        rebound = min(highs, key=lambda p: p.formed_at)
        cand.rebound = rebound
        cand.n_days = range_age_days(cand.start.formed_at, candles[t].open_time)
        seg = candles[cand.start_idx : t + 1]
        cand.geometry = RangeGeometry(
            min(c.low for c in seg), max(c.high for c in seg)
        )
        cand.version = 1
        cand.versions = [{
            "version": 1,
            "lower": cand.geometry.lower,
            "upper": cand.geometry.upper,
            "changed_by_open_time": candles[t].open_time,
        }]
        cand.candidate_id = self._ensure_candidate_row(
            asset_id, cand, candles[t].open_time, detected_at
        )

    # ----- формирование / зрелость (§6–§8) -----

    def _range_step(
        self,
        cand: _CandidateState,
        candles: Sequence[Any],
        t: int,
        asset_id: int,
        source_id: int,
        detected_at: int,
        structure: StructureTracker,
    ) -> tuple[Optional[AltFrozenRange], Optional[AltSetup], list[tuple[str, bool]]]:
        """Шаг по активному кандидату: возраст, расширение, классификация,
        freeze при первом распознавании зрелого боковика (§8)."""
        assert cand.geometry is not None and cand.rebound is not None
        c = candles[t]
        events: list[tuple[str, bool]] = []
        cand.n_days = range_age_days(cand.start.formed_at, c.open_time)

        if cand.geometry.expand(c.low, c.high):
            cand.version += 1
            cand.versions.append({
                "version": cand.version,
                "lower": cand.geometry.lower,
                "upper": cand.geometry.upper,
                "changed_by_open_time": c.open_time,
            })

        if cand.n_days <= self.cfg.forming_min_days:
            return None, None, events  # внутренний поиск, не в стандартный вывод

        seg = candles[cand.start_idx : t + 1]
        closes = [x.close for x in seg]
        res = classify_sideways(closes, cand.geometry.width, self.cfg, candles=seg)
        cand.classifier = res
        recognized_at = close_boundary_ms(c.open_time, ALT_TIMEFRAME)
        if not (res.ready and res.sideways):
            return None, None, events  # направленный участок — не forming/mature

        if cand.n_days <= self.cfg.mature_min_days:
            if cand.forming_recognized_ms is None:
                cand.forming_recognized_ms = recognized_at
                cand.forming_n_days = cand.n_days
            return None, None, events

        # Первое распознавание зрелости: freeze без заднего числа (§8)
        # Факты ЭТОЙ свечи уже детектированы в буферной ветке цикла — они
        # живые (§10: на одном закрытии сначала доступность зрелого диапазона,
        # затем событие структуры этого закрытия), а не historical
        today_facts = [
            f for f in cand.pre_facts if f.candle_open_time == c.open_time
        ]
        cand.pre_facts = [
            f for f in cand.pre_facts if f.candle_open_time != c.open_time
        ]
        frozen_row, setup, ev = self._freeze(
            cand, seg, asset_id, source_id, recognized_at, detected_at
        )
        events.extend(ev)
        events.extend(self._mature_step(
            cand, frozen_row, setup, structure, candles, t, detected_at,
            facts=today_facts,
        ))
        return frozen_row, setup, events

    def _freeze(
        self,
        cand: _CandidateState,
        seg: Sequence[Any],
        asset_id: int,
        source_id: int,
        recognized_at: int,
        detected_at: int,
    ) -> tuple[AltFrozenRange, AltSetup, list[tuple[str, bool]]]:
        """Неизменяемая заморозка геометрии + сетап + события (§8, §15)."""
        assert cand.geometry is not None and cand.rebound is not None
        cand.frozen = True
        cand.candidate_id = self._ensure_candidate_row(
            asset_id, cand, seg[-1].open_time, detected_at
        )
        origin = f"{asset_id}:{source_id}:{cand.origin_key}"

        existing = self.db.get_alt_frozen_range_by_range(cand.candidate_id)
        if existing is not None:
            frozen_row = existing
        else:
            frozen_row = self.db.insert_alt_frozen_range(AltFrozenRange(
                id=None,
                range_id=cand.candidate_id,
                lower=cand.geometry.lower,
                upper=cand.geometry.upper,
                width=cand.geometry.width,
                mid=cand.geometry.mid,
                start_anchor_open_time=cand.start.formed_at,
                rebound_anchor_open_time=cand.rebound.formed_at,
                included_candles=len(seg),
                mature_at_ms=recognized_at,  # когда реально стало доступно
                classifier_version=self.cfg.classifier_version,
                range_version=cand.version,
            ))

        k_price = 2 * frozen_row.lower - frozen_row.upper  # K=2L−U (§12)
        setup, _created = self.db.insert_alt_setup(AltSetup(
            id=None,
            asset_id=asset_id,
            source_id=source_id,
            range_id=frozen_row.id,
            state=(
                AltState.REVIEW_REQUIRED.value
                if cand.review else AltState.MATURE.value
            ),
            flags_json=json.dumps({
                "upper_excursion": False,
                "breakout_confirmed": False,
                "lower_excursion": False,
            }),
            cancel_price=k_price,
            cancel_mode=self.cfg.cancel_mode,
            cancel_reachable=k_price > 0,
            created_ms=detected_at,
            updated_ms=detected_at,
        ))

        events: list[tuple[str, bool]] = []
        if cand.forming_recognized_ms is not None:
            forming_payload: dict[str, Any] = {
                "origin_key": origin,
                "n_days_at_recognition": cand.forming_n_days,
                "lower": frozen_row.lower, "upper": frozen_row.upper,
            }
            if cand.deep_drop_drawdown is not None:
                # §18: «после падения X% от биржевого ATH»
                forming_payload["drawdown_pct"] = round(
                    cand.deep_drop_drawdown * 100, 4
                )
                forming_payload["ath_price"] = cand.ath_price
            self._record(
                events, setup.id, AltEventType.FORMING_STARTED,
                f"forming:{origin}",
                cand.forming_recognized_ms, detected_at,
                forming_payload,
            )
        self._record(
            events, setup.id, AltEventType.MATURE_FROZEN, f"frozen:{origin}",
            recognized_at, detected_at,
            {
                "origin_key": origin,
                "lower": frozen_row.lower, "upper": frozen_row.upper,
                "width": frozen_row.width, "mid": frozen_row.mid,
                "included_candles": frozen_row.included_candles,
                "classifier_version": frozen_row.classifier_version,
                "range_version": frozen_row.range_version,
            },
        )
        if cand.review:
            self._record(
                events, setup.id, AltEventType.REVIEW_REQUIRED,
                f"review:{origin}",
                recognized_at, detected_at,
                {
                    "origin_key": origin,
                    "chosen_start_anchor": cand.start.formed_at,
                    "alternative_anchors": list(cand.alternative_anchor_open_times),
                },
            )

        # §10: структурные факты до maturity — только исторически (строки
        # alt_structure_event с historical=true, без уведомлений и без
        # заднего превращения в вход)
        cand.mstate = _MatureState()
        if cand.pre_facts:
            flags = json.loads(setup.flags_json or "{}")
            for se in cand.pre_facts:
                self._persist_structure_event(setup.id, se, historical=True)
                if se.kind == "SSL":
                    flags["ssl_event"] = True
                else:
                    flags["structure_event"] = True
            self.db.update_alt_setup(
                setup.id, flags_json=json.dumps(flags), updated_ms=detected_at
            )
            setup.flags_json = json.dumps(flags)
        return frozen_row, setup, events

    # ----- §9–§13: пост-freeze логика зрелого сетапа -----

    def _ssl_check(self, cand: _CandidateState, c: Any) -> list[StructureEvent]:
        """§9: снятие внутреннего SSL — Low закрытой D1 строго ниже уровня
        подтверждённого pivot low 3+3 (общий level_crossed). Произвольный
        микроминимум уровнем не является; каждый уровень снимается один раз.
        Снятие SSL необязательно для входа — это диагностический факт."""
        out: list[StructureEvent] = []
        for p in cand.ssl_levels:
            if p.formed_at in cand.ssl_taken:
                continue
            zone = _ssl_zone(p.price, p.formed_at, p.confirmed_at)
            if level_crossed(zone, c.low, c.high):
                cand.ssl_taken.add(p.formed_at)
                out.append(StructureEvent(
                    "SSL", p.price, c.close, c.open_time,
                    anchors=[{"formed_at": p.formed_at, "price": p.price,
                              "role": "SSL"}],
                ))
        return out

    def _persist_structure_event(
        self, setup_id: int, se: StructureEvent, historical: bool
    ) -> None:
        """Строка alt_structure_event; дедуп по (setup_id, kind, свеча)."""
        if self.db.find_alt_structure_event(
            setup_id, se.kind, se.candle_open_time
        ) is not None:
            return
        self.db.insert_alt_structure_event(AltStructureEvent(
            id=None, setup_id=setup_id, kind=se.kind,
            level_price=se.level_price, close_price=se.close_price,
            candle_open_time=se.candle_open_time,
            anchors_json=json.dumps(
                {"anchors": se.anchors, "historical": historical},
                ensure_ascii=False,
            ),
        ))

    def _manipulation_step(
        self,
        ms: _MatureState,
        setup: AltSetup,
        frozen: AltFrozenRange,
        c: Any,
        boundary: int,
        detected_at: int,
        flags: dict[str, Any],
        events: list[tuple[str, bool]],
    ) -> None:
        """§9: эпизод манипуляции — Low < L закрытой D1 зрелого диапазона.
        Границы не расширяются; Close < L допускается без TTL; завершение —
        Close > L (равенство L не возврат — конвенция v1); повторный Low < L
        после завершения — НОВЫЙ эпизод того же сетапа."""
        if c.low < frozen.lower:
            if ms.manip_id is None:
                existing = self.db.find_alt_manipulation_episode(
                    setup.id, c.open_time
                )
                if existing is not None:
                    ep_id = existing.id
                else:
                    ep_id = self.db.insert_alt_manipulation_episode(
                        AltManipulationEpisode(
                            id=None, setup_id=setup.id,
                            started_candle_open_time=c.open_time,
                            min_price=c.low, days_below=1,
                        )
                    )
                ms.manip_id = ep_id
                ms.manip_start_ot = c.open_time
                ms.manip_min = c.low
                ms.manip_days = 1
                flags["manipulation_active"] = True
                self._record(events, setup.id, AltEventType.MANIPULATION_STARTED,
                             f"manip_start:{setup.id}:{c.open_time}",
                             boundary, detected_at,
                             {"range_lower": frozen.lower, "low": c.low})
            else:
                ms.manip_days += 1
                ms.manip_min = min(ms.manip_min, c.low)
                self.db.update_alt_manipulation_episode(
                    ms.manip_id, min_price=ms.manip_min, days_below=ms.manip_days
                )
        if ms.manip_id is not None and c.close > frozen.lower:
            self.db.update_alt_manipulation_episode(
                ms.manip_id, ended_candle_open_time=c.open_time,
                min_price=ms.manip_min, days_below=ms.manip_days,
            )
            self._record(events, setup.id, AltEventType.MANIPULATION_ENDED,
                         f"manip_end:{setup.id}:{c.open_time}",
                         boundary, detected_at,
                         {"started_candle_open_time": ms.manip_start_ot,
                          "min_price": ms.manip_min,
                          "days_below": ms.manip_days})
            ms.manip_id = None
            flags["manipulation_active"] = False

    def _mature_step(
        self,
        cand: _CandidateState,
        frozen: AltFrozenRange,
        setup: Optional[AltSetup],
        structure: StructureTracker,
        candles: Sequence[Any],
        t: int,
        detected_at: int,
        facts: Optional[list[StructureEvent]] = None,
    ) -> list[tuple[str, bool]]:
        """Шаг пост-freeze обработки одной закрытой D1 (§9–§13).

        Порядок внутри свечи: факты структуры/SSL → отмена (приоритет для
        выдачи возможностей, §12) → манипуляция → breakout → подтверждение/
        вход A → ретест/истечение → цели. Совпавшие события получают флаг
        intra_candle_sequence_unknown — порядок внутри D1 не выдумывается.
        """
        events: list[tuple[str, bool]] = []
        ms = cand.mstate
        if ms is None or ms.terminal or setup is None:
            return events
        c = candles[t]
        boundary = close_boundary_ms(c.open_time, ALT_TIMEFRAME)
        L, U, W, M = frozen.lower, frozen.upper, frozen.width, frozen.mid
        flags = json.loads(setup.flags_json or "{}")
        review = setup.state == AltState.REVIEW_REQUIRED.value
        dirty = False

        # --- факты структуры/SSL (только подтверждённые уровни, без будущего);
        # на свече freeze они уже детектированы буферной веткой и переданы
        # параметром (повторный on_close недопустим — уровни сгорают)
        if facts is None:
            facts = self._ssl_check(cand, c)
            facts.extend(structure.on_close(c.open_time, c.close))
        bullish = [e for e in facts if e.kind in ("BOS", "SMS")]

        # --- пересечения этой свечи (для флага неизвестной последовательности)
        breakout_now = c.close > U and ms.breakout_open_time is None
        touch_now = (
            ms.breakout_open_time is not None
            and c.open_time != ms.breakout_open_time
            and c.low <= U and c.high >= M
        )
        k = setup.cancel_price
        cancel_hit = bool(
            k is not None and setup.cancel_reachable and (
                (setup.cancel_mode == "wick_on_closed_d1" and c.low <= k)
                or (setup.cancel_mode == "close_on_closed_d1" and c.close <= k)
            )
        )
        cancel_seq_unknown = cancel_hit and bool(facts or breakout_now or touch_now)

        # Цели этой свечи считаются заранее — для флага совпадения с ретестом
        hits_now: list[int] = []
        if (
            ms.confirmed
            and ms.confirmation_open_time is not None
            and c.open_time > ms.confirmation_open_time
        ):
            hits_now = [
                n for n, tp in enumerate(ms.targets, 1)
                if n not in ms.targets_hit and c.high >= tp
            ]

        # --- строки структуры + события (обратные BOS/SMS — только строки)
        first_basis_event_id: Optional[int] = None
        for se in facts:
            self._persist_structure_event(setup.id, se, historical=False)
            payload = {
                "level_price": se.level_price, "close": se.close_price,
                "anchors": se.anchors,
                "intra_candle_sequence_unknown": cancel_seq_unknown,
            }
            if se.kind == "SSL":
                flags["ssl_event"] = True
                dirty = True
                ev = self._record(
                    events, setup.id, AltEventType.SSL_TAKEN,
                    f"ssl:{setup.id}:{se.anchors[0]['formed_at']}:"
                    f"{se.candle_open_time}",
                    boundary, detected_at, payload,
                )
            elif se.kind in ("BOS", "SMS"):
                flags["structure_event"] = True
                dirty = True
                et = (AltEventType.BOS_CONFIRMED if se.kind == "BOS"
                      else AltEventType.SMS_CONFIRMED)
                ev = self._record(
                    events, setup.id, et,
                    f"{se.kind.lower()}:{setup.id}:{se.candle_open_time}",
                    boundary, detected_at, payload,
                )
                if first_basis_event_id is None:
                    first_basis_event_id = ev.id
            else:  # BOS_REV / SMS_REV — аналитический факт, не отмена
                flags["structure_event"] = True
                flags[f"reverse_{se.kind.lower()}_last"] = c.open_time
                dirty = True

        # --- §12: отмена приоритетна для выдачи новых возможностей
        if cancel_hit:
            self._record(events, setup.id, AltEventType.CANCELLED,
                         f"cancel:{setup.id}:{c.open_time}",
                         boundary, detected_at,
                         {"cancel_price": k, "cancel_mode": setup.cancel_mode,
                          "cancel_mode_note": "project_default_v1",
                          "candle_open_time": c.open_time,
                          "intra_candle_sequence_unknown": cancel_seq_unknown})
            setup.state = AltState.CANCELLED.value
            setup.terminated_ms = boundary
            flags["manipulation_active"] = False
            ms.terminal = True
            self._write_setup(setup, flags, detected_at)
            return events

        # --- §9: манипуляция (отмена не наступила)
        self._manipulation_step(ms, setup, frozen, c, boundary, detected_at,
                                flags, events)
        dirty = True  # flags эпизода могли измениться

        # --- §11: breakout (первый неизменен, таймер не перезапускается)
        if breakout_now:
            ms.breakout_open_time = c.open_time
            setup.breakout_close = c.close
            setup.breakout_closed_at = boundary
            setup.retest_deadline_ms = (
                boundary + self.cfg.retest_window_days * DAY_MS
            )
            flags["breakout_confirmed"] = True
            flags["upper_excursion"] = True
            ev = self._record(events, setup.id, AltEventType.BREAKOUT,
                              f"breakout:{setup.id}:{c.open_time}",
                              boundary, detected_at,
                              {"close": c.close, "upper": U,
                               "closed_at": boundary,
                               "retest_deadline_ms": setup.retest_deadline_ms})
            if first_basis_event_id is None:
                first_basis_event_id = ev.id
        elif c.high > U and not flags.get("upper_excursion"):
            # §8: High>U при Close<=U — факт выноса, НЕ breakout
            flags["upper_excursion"] = True
            flags["upper_excursion_first_open_time"] = c.open_time
        if c.low < L and not flags.get("lower_excursion"):
            flags["lower_excursion"] = True
            flags["lower_excursion_first_open_time"] = c.open_time

        # --- §13: первое подтверждение — первый bullish BOS/SMS или breakout;
        # совпадение на одном закрытии — одно подтверждение с несколькими
        # основаниями; снимок целей неизменен далее
        bases = [b.kind.lower() for b in bullish]
        if breakout_now:
            bases.append("breakout")
        if not ms.confirmed and bases and not review:
            ms.confirmed = True
            ms.confirmation_open_time = c.open_time
            ms.targets = [
                U + n * W for n in range(1, self.cfg.target_count + 1)
            ]
            targets = []
            for n, tp in enumerate(ms.targets, 1):
                passed = c.close >= tp  # «пройдена к моменту подтверждения»
                if passed:
                    ms.targets_hit.add(n)
                targets.append({"tp": n, "price": tp,
                                "passed_at_confirmation": passed})
            setup.targets_json = json.dumps(targets)
            setup.confirmation_event_id = first_basis_event_id
            setup.state = AltState.ACTIVE_CONFIRMED.value
            flags["target_snapshot"] = {
                "lower": L, "upper": U, "width": W, "mid": M,
                "bases": bases, "as_of": boundary,
            }
            flags["targets_hit"] = sorted(ms.targets_hit)
            dirty = True

        # --- §10: вход A — Close подтверждающей D1; один первый A на сетап;
        # манипуляция/SSL/HTF-контекст не требуются и могут сосуществовать
        if bullish and not ms.entry_a_done and not review and ms.confirmed:
            ms.entry_a_done = True
            flags["entry_a_confirmed"] = True
            opp = self.db.find_alt_entry_opportunity(setup.id, "A")
            if opp is None:
                opp = self.db.insert_alt_entry_opportunity(AltEntryOpportunity(
                    id=None, setup_id=setup.id, kind="A",
                    event_time_ms=boundary, price=c.close,
                    bases_json=json.dumps(
                        [b.anchors for b in bullish], ensure_ascii=False
                    ),
                ))
            setup.entry_a_id = opp.id
            self._record(events, setup.id, AltEventType.ENTRY_A,
                         f"entry_a:{setup.id}", boundary, detected_at,
                         {"price": c.close,
                          "bases": [b.kind.lower() for b in bullish],
                          "anchors": [b.anchors for b in bullish]})
            dirty = True

        # --- §11: ретест [M,U] и таймер 14 дней от первого breakout
        if (
            ms.breakout_open_time is not None
            and c.open_time != ms.breakout_open_time
        ):
            deadline = setup.retest_deadline_ms
            if not ms.retest_done:
                if touch_now and boundary <= deadline:
                    # Свеча ровно на deadline ещё проверяется — касание считается
                    ms.retest_done = True
                    flags["retest_received"] = True
                    payload: dict[str, Any] = {
                        "zone": {"lower": M, "upper": U},
                        "candle_open_time": c.open_time,
                        "intra_candle_sequence_unknown": bool(hits_now),
                    }
                    if c.low < M:
                        # Глубина ниже M — отдельный факт, не условие отмены
                        payload["depth_below_mid"] = M - c.low
                    self._record(events, setup.id, AltEventType.RETEST,
                                 f"retest:{setup.id}:{c.open_time}",
                                 boundary, detected_at, payload)
                    if not ms.entry_b_done and not review:
                        ms.entry_b_done = True
                        opp = self.db.find_alt_entry_opportunity(setup.id, "B")
                        if opp is None:
                            opp = self.db.insert_alt_entry_opportunity(
                                AltEntryOpportunity(
                                    id=None, setup_id=setup.id, kind="B",
                                    event_time_ms=boundary,
                                    zone_json=json.dumps(
                                        {"lower": M, "upper": U}
                                    ),
                                    bases_json=json.dumps(
                                        [{"candle_open_time": c.open_time}]
                                    ),
                                )
                            )
                        setup.entry_b_id = opp.id
                        self._record(events, setup.id, AltEventType.ENTRY_B,
                                     f"entry_b:{setup.id}", boundary,
                                     detected_at,
                                     {"zone": {"lower": M, "upper": U}})
                    dirty = True
                elif boundary is not None and deadline is not None \
                        and boundary > deadline:
                    # EXPIRED_NO_RETEST: завершает ВЕСЬ сетап (даже после A);
                    # event_time — исторический deadline, не время доставки
                    ms.terminal = True
                    setup.state = AltState.EXPIRED_NO_RETEST.value
                    setup.terminated_ms = deadline
                    self._record(events, setup.id,
                                 AltEventType.EXPIRED_NO_RETEST,
                                 f"expired:{setup.id}", deadline, detected_at,
                                 {"first_breakout_closed_at":
                                  setup.breakout_closed_at,
                                  "deadline": deadline})
                    self._write_setup(setup, flags, detected_at)
                    return events
            elif touch_now:
                # Дальнейшие касания — только журнал (антиспам: один первый B)
                self._record(events, setup.id, AltEventType.RETEST,
                             f"retest:{setup.id}:{c.open_time}",
                             boundary, detected_at,
                             {"zone": {"lower": M, "upper": U},
                              "journal": True,
                              "intra_candle_sequence_unknown": bool(hits_now)})

        # --- §13: цели (High закрытой D1 >= TP_n ПОСЛЕ подтверждения; High
        # свечи подтверждения достижение не доказывает). Несколько целей на
        # одной D1 — одно объединённое событие; каждый уровень — один раз
        if hits_now:
            ms.targets_hit.update(hits_now)
            flags["targets_hit"] = sorted(ms.targets_hit)
            self._record(events, setup.id, AltEventType.TARGET_HIT,
                         f"target:{setup.id}:{c.open_time}",
                         boundary, detected_at,
                         {"levels": hits_now,
                          "prices": {str(n): ms.targets[n - 1]
                                     for n in hits_now},
                          "intra_candle_sequence_unknown": touch_now})
            dirty = True
        if (
            ms.confirmed
            and len(ms.targets_hit) >= self.cfg.target_count
            and not ms.terminal
        ):
            ms.terminal = True
            setup.state = AltState.TARGETS_COMPLETED.value
            setup.terminated_ms = boundary
            self._record(events, setup.id, AltEventType.TARGETS_COMPLETED,
                         f"targets_completed:{setup.id}", boundary, detected_at,
                         {"levels": sorted(ms.targets_hit)})
            dirty = True

        if dirty:
            self._write_setup(setup, flags, detected_at)
        return events

    def _write_setup(
        self, setup: AltSetup, flags: dict[str, Any], detected_at: int
    ) -> None:
        """Единая точка записи состояния сетапа (идемпотентна при replay)."""
        setup.flags_json = json.dumps(flags)
        self.db.update_alt_setup(
            setup.id,
            state=setup.state,
            flags_json=setup.flags_json,
            confirmation_event_id=setup.confirmation_event_id,
            targets_json=setup.targets_json,
            breakout_close=setup.breakout_close,
            breakout_closed_at=setup.breakout_closed_at,
            retest_deadline_ms=setup.retest_deadline_ms,
            entry_a_id=setup.entry_a_id,
            entry_b_id=setup.entry_b_id,
            terminated_ms=setup.terminated_ms,
            updated_ms=detected_at,
        )

    # ----- персистенция -----

    def _ensure_candidate_row(
        self,
        asset_id: int,
        cand: _CandidateState,
        last_open_time: int,
        detected_at: int,
    ) -> int:
        """Строка кандидата по origin_key: одна пара якорей = один диапазон
        (новые наблюдения тех же опор — версии, не новые строки, §15)."""
        assert cand.geometry is not None and cand.rebound is not None
        origin = cand.origin_key
        existing = self.db.find_alt_range_candidate_by_origin(asset_id, origin)
        if existing is not None:
            cand.candidate_id = existing.id
            return existing.id
        row = self.db.insert_alt_range_candidate(AltRangeCandidate(
            id=None,
            asset_id=asset_id,
            origin_key=origin,
            start_anchor_open_time=cand.start.formed_at,
            rebound_anchor_open_time=cand.rebound.formed_at,
            lower=cand.geometry.lower,
            upper=cand.geometry.upper,
            width=cand.geometry.width,
            mid=cand.geometry.mid,
            n_days=cand.n_days,
            version=cand.version,
            state=AltState.SEARCHING.value,
            metrics_json="{}",
            first_seen_ms=detected_at,
            updated_ms=detected_at,
        ))
        cand.candidate_id = row.id
        return row.id

    def _persist_candidate(
        self, asset_id: int, cand: Optional[_CandidateState], detected_at: int
    ) -> str:
        """Финальная запись состояния кандидата (идемпотентно по origin_key).
        Возвращает lifecycle-состояние актива для summary."""
        if cand is None:
            return AltState.SEARCHING.value
        if cand.rebound is None or cand.geometry is None:
            return SUBSTAGE_RANGE_PENDING
        if cand.review:
            state = AltState.REVIEW_REQUIRED.value
        elif cand.frozen:
            state = AltState.MATURE.value
        elif cand.forming_recognized_ms is not None:
            state = AltState.FORMING.value
        else:
            state = AltState.SEARCHING.value

        metrics: dict[str, Any] = {
            "versions": cand.versions,
            "review": cand.review,
            "alternative_anchor_open_times": cand.alternative_anchor_open_times,
        }
        if cand.classifier is not None:
            r = cand.classifier
            metrics["classifier"] = {
                "ready": r.ready, "sideways": r.sideways, "reason": r.reason,
                "slope_b": r.slope_b, "slope_sign": r.slope_sign,
                "slope_normalized": r.slope_normalized,
                "center_shift": r.center_shift,
                "failed_conditions": list(r.failed_conditions),
                "efficiency": r.efficiency,
                "n_blocks": r.n_blocks,
                "diagnostics": r.diagnostics,
                "classifier_version": r.classifier_version,
            }
        self.db.update_alt_range_candidate(
            cand.candidate_id,
            lower=cand.geometry.lower, upper=cand.geometry.upper,
            width=cand.geometry.width, mid=cand.geometry.mid,
            n_days=cand.n_days, version=cand.version, state=state,
            metrics_json=json.dumps(metrics), updated_ms=detected_at,
        )
        return state

    def _emit(
        self,
        setup_id: int,
        event_type: AltEventType,
        source_event_id: str,
        event_time_ms: int,
        detected_at_ms: int,
        payload: dict[str, Any],
    ) -> tuple[AltEvent, bool]:
        """Событие в outbox; source_event_id стабилен (опоры+время), не от
        прогона — повторный replay дедуплицируется UNIQUE-ключом (§18)."""
        return self.db.insert_alt_event(AltEvent(
            id=None,
            setup_id=setup_id,
            event_type=event_type.value,
            source_event_id=source_event_id,
            payload_json=json.dumps(payload, ensure_ascii=False),
            event_time_ms=event_time_ms,
            detected_at_ms=detected_at_ms,
            created_ms=detected_at_ms,
        ))

    def _record(
        self,
        events: list[tuple[str, bool]],
        setup_id: int,
        event_type: AltEventType,
        source_event_id: str,
        event_time_ms: int,
        detected_at_ms: int,
        payload: dict[str, Any],
    ) -> AltEvent:
        """_emit + запись (тип, created) в журнал summary прогона."""
        ev, created = self._emit(
            setup_id, event_type, source_event_id,
            event_time_ms, detected_at_ms, payload,
        )
        events.append((ev.event_type, created))
        return ev

    def _terminal_setup(self, asset_id: int) -> Optional[AltSetup]:
        """Завершённый сетап не воскресает при пересчёте (§15). Новый поиск
        для актива возможен только как новый диапазон с новыми опорами —
        этот этап не открывает новых кандидатов поверх terminal-истории."""
        for s in self.db.list_alt_setups(asset_id):
            if s.terminated_ms is not None or s.state in (
                AltState.CANCELLED.value,
                AltState.EXPIRED_NO_RETEST.value,
                AltState.TARGETS_COMPLETED.value,
            ):
                return s
        return None

    def _save_progress(
        self,
        asset_id: int,
        source_id: int,
        valid: Sequence[Any],
        summary: dict[str, Any],
    ) -> None:
        """Прогресс движка по активу/источнику (meta KV) — точка возобновления
        ежедневного догоняния; replay от неё не зависит (полный пересчёт)."""
        self.db.set_meta(
            f"alt:engine:{asset_id}:{source_id}",
            json.dumps({
                "last_processed_open_time": (
                    valid[-1].open_time if valid else None
                ),
                "candles_valid": len(valid),
                "state": summary.get("state"),
                "candidate_id": summary.get("candidate_id"),
                "setup_id": summary.get("setup_id"),
            }),
        )

    # ----- summary helpers -----

    @staticmethod
    def _ath_summary(tracker: AthTracker) -> dict[str, Any]:
        ep = tracker.current
        if ep is None:
            return {}
        return {
            "ath_price": ep.ath_price,
            "ath_open_time": ep.ath_open_time,
            "equal_top_open_times": list(ep.equal_top_open_times),
            "p_min": ep.p_min,
            "p_min_open_time": ep.p_min_open_time,
            "drawdown": ep.drawdown,
            "deep_drop_achieved": ep.deep_drop_achieved,
            "deep_drop_open_time": ep.deep_drop_open_time,
            "episodes_total": len(tracker.history) + 1,
        }

    @staticmethod
    def _classifier_summary(cand: Optional[_CandidateState]) -> Optional[dict[str, Any]]:
        if cand is None or cand.classifier is None:
            return None
        r = cand.classifier
        return {
            "ready": r.ready, "sideways": r.sideways,
            "slope_normalized": r.slope_normalized,
            "center_shift": r.center_shift,
            "slope_sign": r.slope_sign,
            "failed_conditions": list(r.failed_conditions),
            "efficiency": r.efficiency,
            "classifier_version": r.classifier_version,
        }
