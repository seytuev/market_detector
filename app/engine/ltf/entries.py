"""§8/§9: Entry Zones H1 — причинное движение, обнаружение OB/FVG/BSL/SSL,
свежесть (first_test_at) и пространственный фильтр Premium/Discount.

Происхождение (§8.1): зона должна быть сформирована движением, приведшим к
конкретному BOS/SMS. Нога — от последнего противоположного pivot до свечи
слома; OB-основание у начальной опоры и подтверждающий импульс — одна
цепочка, поэтому окно обнаружения расширено назад на глубину базы (точная
нижняя граница многосвечной базы — открытый вопрос §16.3; исходные свечи
сохраняются в evidence). Зоны последующих движений (formed_at после слома)
отклоняются с явной причиной.

Свежесть (§9): first_test_at ищется по всей доступной истории после рыночного
подтверждения зоны. Формирующие свечи FVG/OB тестом не считаются (п.11).
Гэп через всю зону без цен внутри касанием не является — у обеих свечей гэпа
touch_bar == False (§8.5). Для уровней равное касание считается тестом
(консервативный режим, §9).

Повторное использование (ТЗ «Единый движок» §3/§4): протестированная зона
может быть выбрана как Entry Zone повторно, если максимальная глубина
прежних тестов СТРОГО меньше cfg.entry_reuse_max_depth (ровно порог — уже
недопустимо; точное сравнение по сохранённому экстремуму, без epsilon).
Глубина накапливается за ВСЮ историю тестов: мелкий поздний тест не стирает
более глубокий прежний. validity при этом не меняется — «tested» остаётся
фактом истории, допуск к выбору считается отдельно (entry_reusable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from ...config import DetectorConfig
from ...models import Candle, Direction, TIMEFRAME_MINUTES
from ...models_ltf import LtfEntryZone
from ..depth import ZoneGeometry, exact_depth, reaches_depth
from ..fvg import scan_fvgs
from ..orderblock import find_base, is_external
from .breaks import StructureEventDraft
from .pivots import PivotCandidate
from .ranges import RangeDraft, eligible_overlap, level_in_half, target_half

H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000


def _ref(p: PivotCandidate) -> int:
    return p.pivot_id if p.pivot_id is not None else p.pivot_at


def touch_bar(lower: float, upper: float, candle: Candle) -> bool:
    """§8.5: касание по ПОЛНОМУ диапазону зоны: High >= L AND Low <= U."""
    return candle.high >= lower and candle.low <= upper


@dataclass
class MovementDraft:
    """Причинное движение, приведшее к слому (§8.1), до записи в БД."""

    scenario_id: int
    direction: Direction
    start_pivot_ref: int           # pivot_id (или pivot_at до материализации)
    end_pivot_ref: int
    start_at: int                  # ms: pivot начала ноги
    end_at: int                    # ms: open_time свечи слома
    window_start: int              # ms: start_at минус глубина базы (§16.3)
    break_event_key: str
    source_candle_ids: list[int] = field(default_factory=list)
    provenance_status: str = "ok"  # ok | ambiguous


@dataclass
class EntryZoneDraft:
    """Entry Zone до записи в БД (слой БД создаёт LtfEntryZone)."""

    type: str                      # OB | FVG | BSL | SSL
    direction: Direction
    lower: float
    upper: float                   # для уровня равна lower
    formed_at: int
    confirmed_at: int
    first_test_at: Optional[int]   # §9: первый допустимый тест по всей истории
    validity: str                  # fresh | tested
    max_test_depth: float = 0.0    # ТЗ §3: максимум глубины тестов за историю
    test_extreme: Optional[float] = None  # глубочайший экстремум тестов
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_level(self) -> bool:
        return self.type in ("BSL", "SSL") or self.lower == self.upper


@dataclass
class EntryDetection:
    zones: list[EntryZoneDraft]
    rejected: list[dict[str, Any]]  # исключённые с объяснением (приёмка п.9)


def build_movement(
    scenario_id: int,
    pivots: list[PivotCandidate],
    candles: list[Candle],
    event: StructureEventDraft,
    direction: Direction,
    lookback_ms: int,
    start_pivot: Optional[PivotCandidate] = None,
) -> Optional[MovementDraft]:
    """Нога, приведшая к слому: для bear — от последнего high-pivot до свечи
    слома (LL/свеча слома как конец, §8.1), для bull — зеркально.

    None — нет стартового pivot (структура без опоры; зоны не ищем).
    """
    break_t = event.break_candle_open_time
    kind_start = "high" if direction == Direction.BEAR else "low"
    kind_end = "low" if direction == Direction.BEAR else "high"
    starts = [p for p in pivots if p.kind == kind_start and p.pivot_at <= break_t]
    if start_pivot is not None:
        # Разворотная нога: исходный LL/HH, а не последний локальный HL/LH.
        # Обычный вызов без start_pivot сохраняет прежний выбор.
        start = start_pivot
    else:
        if not starts:
            return None
        start = max(starts, key=lambda p: p.pivot_at)
    ends = [p for p in pivots if p.kind == kind_end and p.pivot_at <= break_t]
    end = max(ends, key=lambda p: p.pivot_at) if ends else start
    source = [
        c.open_time for c in candles
        if c.closed and start.pivot_at <= c.open_time <= break_t
    ]
    return MovementDraft(
        scenario_id=scenario_id,
        direction=direction,
        start_pivot_ref=_ref(start),
        end_pivot_ref=_ref(end),
        start_at=start.pivot_at,
        end_at=break_t,
        window_start=start.pivot_at - lookback_ms,
        break_event_key=event.level_key,
        source_candle_ids=source,
    )


def merge_test_extreme(
    direction: Direction, prev: Optional[float], cur: float
) -> float:
    """Глубочайший экстремум теста: bull — min Low, bear — max High
    (ТЗ §3: мелкий поздний тест не стирает более глубокий прежний)."""
    if prev is None:
        return cur
    return min(prev, cur) if direction == Direction.BULL else max(prev, cur)


def test_depth_of(zone: ZoneGeometry, extreme: Optional[float]) -> float:
    """Глубина теста по экстремуму в точной арифметике (ТЗ §4), кламп
    [0..1]; уровень (W=0) глубины не имеет — 0."""
    if extreme is None:
        return 0.0
    d = exact_depth(zone, extreme)
    return float(max(Decimal(0), min(Decimal(1), d)))


def entry_reusable(zone: LtfEntryZone, cfg: DetectorConfig) -> bool:
    """ТЗ §3: протестированная Entry Zone допустима к повторному выбору,
    если максимальная глубина прежних тестов СТРОГО меньше
    entry_reuse_max_depth (ровно порог — уже недопустимо; точное сравнение
    по сохранённому экстремуму, без epsilon).

    Допуск не меняет validity: зона остаётся рыночно актуальной («tested»
    — факт истории), ограничен только новый вход."""
    if zone.test_extreme is not None:
        return not reaches_depth(zone, zone.test_extreme, cfg.entry_reuse_max_depth)
    return zone.max_test_depth < cfg.entry_reuse_max_depth


def fvg_filled(zone: LtfEntryZone) -> bool:
    """§10 (Этап 6): FVG перекрыт полностью — цена дошла до дальней границы
    (глубина 100% в точном сравнении по сохранённому экстремуму, как ТЗ §4).

    Терминальное состояние самого FVG (как fresh-void / новая Entry Zone он
    больше не используется) и только его: связанный OB — самостоятельный
    объект и оценивается своим правилом 90% (entry_reusable)."""
    if zone.type != "FVG" or zone.is_level:
        return False
    if zone.test_extreme is not None:
        return reaches_depth(zone, zone.test_extreme, 1.0)
    return zone.max_test_depth >= 1.0


def fvg_fill_status(zone: LtfEntryZone) -> Optional[str]:
    """open | partially_filled | filled; None — не FVG (производный статус,
    отдельной колонки не требует)."""
    if zone.type != "FVG":
        return None
    if fvg_filled(zone):
        return "filled"
    if zone.first_test_at is not None or zone.max_test_depth > 0:
        return "partially_filled"
    return "open"


def _range_test_stats(
    candles: list[Candle], lower: float, upper: float, direction: Direction,
    after_open: int,
) -> tuple[Optional[int], Optional[float]]:
    """§9 + ТЗ §3: первое касание [L;U] ПОСЛЕ подтверждающей свечи и
    глубочайший экстремум всех таких касаний (формирующие свечи и
    первоначальный проход импульса тестами не являются — приёмка п.11).
    Возвращает (first_test_at, test_extreme)."""
    first: Optional[int] = None
    extreme: Optional[float] = None
    for c in candles:
        if c.open_time <= after_open:
            continue
        if not touch_bar(lower, upper, c):
            continue
        if first is None:
            first = c.open_time
        cur = c.low if direction == Direction.BULL else c.high
        extreme = merge_test_extreme(direction, extreme, cur)
    return first, extreme


def _first_level_test(
    candles: list[Candle], level: float, after_open: int
) -> Optional[int]:
    """§9: первое достижение уровня после рождения экстремума; равное
    касание считается тестом (консервативный режим свежести)."""
    for c in candles:
        if c.open_time <= after_open:
            continue
        if c.high >= level and c.low <= level:
            return c.open_time
    return None


def detect_entry_zones(
    candles: list[Candle],
    movement: MovementDraft,
    pivots: list[PivotCandidate],
    direction: Direction,
    cfg: DetectorConfig,
) -> EntryDetection:
    """Все свежие/протестированные зоны причинного движения, без выбора
    «лучшей» и без ранжирования (§8, приёмка п.9).

    candles — вся доступная история до текущего момента (свежесть считается
    по ней, §9); обнаружение — только окно движения (§8.1).
    """
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    zones: list[EntryZoneDraft] = []
    rejected: list[dict[str, Any]] = []

    # --- FVG H1 (§8.2) и OB H1 (§8.3) ---
    for f in scan_fvgs(closed, "H1"):
        if f.candle_open_times[0] < movement.window_start:
            continue  # древняя история вне окна базы — не кандидат
        if f.direction != direction:
            rejected.append({
                "type": "FVG", "formed_at": f.formed_at,
                "range": [f.lower, f.upper],
                "reason": "направление не совпадает со сценарием (§8.3)",
            })
            continue
        if f.candle_open_times[0] < movement.start_at or f.formed_at > movement.end_at:
            rejected.append({
                "type": "FVG", "formed_at": f.formed_at,
                "range": [f.lower, f.upper],
                "reason": "вне причинного движения слома (§8.1)",
            })
            continue
        first_test, extreme = _range_test_stats(
            closed, f.lower, f.upper, direction, after_open=f.candle_open_times[2]
        )
        fz = EntryZoneDraft(
            type="FVG", direction=direction, lower=f.lower, upper=f.upper,
            formed_at=f.formed_at, confirmed_at=f.confirmed_at,
            first_test_at=first_test,
            validity="tested" if first_test is not None else "fresh",
            test_extreme=extreme,
            evidence={"fvg_candles": list(f.candle_open_times)},
        )
        fz.max_test_depth = test_depth_of(fz, extreme)
        zones.append(fz)
        # OB: база перед FVG; без внешнего подтверждающего FVG — кандидат,
        # не готовая зона (§8.3)
        base = find_base(
            [c for c in closed if c.open_time <= f.candle_open_times[2]], f, cfg
        )
        if base is None or not is_external(base, f):
            rejected.append({
                "type": "OB", "formed_at": f.formed_at,
                "reason": "нет подтверждающего внешнего FVG — кандидат (§8.3)",
            })
            continue
        ob_first_test, ob_extreme = _range_test_stats(
            closed, base.lower, base.upper, direction,
            after_open=f.candle_open_times[2],
        )
        oz = EntryZoneDraft(
            type="OB", direction=direction, lower=base.lower, upper=base.upper,
            formed_at=base.formed_at, confirmed_at=f.confirmed_at,
            first_test_at=ob_first_test,
            validity="tested" if ob_first_test is not None else "fresh",
            test_extreme=ob_extreme,
            evidence={
                "base_candles": base.source_candles,
                "fvg_candles": list(f.candle_open_times),
                "base_detail": base.evidence,
            },
        )
        oz.max_test_depth = test_depth_of(oz, ob_extreme)
        zones.append(oz)

    # --- BSL/SSL H1 (§8.4): экстремумы по тени, относящиеся к движению ---
    kind = "high" if direction == Direction.BEAR else "low"
    type_ = "BSL" if direction == Direction.BEAR else "SSL"
    for p in pivots:
        if p.kind != kind or p.state != "confirmed":
            continue
        if not (movement.start_at <= p.pivot_at <= movement.end_at):
            continue
        first_test = _first_level_test(closed, p.price, after_open=p.pivot_at)
        zones.append(EntryZoneDraft(
            type=type_, direction=direction, lower=p.price, upper=p.price,
            formed_at=p.pivot_at, confirmed_at=p.confirmed_at,
            first_test_at=first_test,
            validity="tested" if first_test is not None else "fresh",
            evidence={"pivot_ref": _ref(p), "pivot_at": p.pivot_at},
        ))

    return EntryDetection(zones=zones, rejected=rejected)


def classify_entry(
    lower: float, upper: float, is_level: bool,
    rng: Optional[RangeDraft], direction: Direction,
) -> tuple[bool, str]:
    """Пространственный фильтр Premium/Discount (§8.5): (eligible, overlap).

    range_pending (rng is None): фильтр не применяется — зоны копятся
    кандидатами сценария, eligible определится при появлении диапазона.
    Границы зоны НЕ обрезаются ни при каком исходе.
    """
    if rng is None:
        return True, "pending"
    half_low, half_high = target_half(rng, direction)
    if is_level:
        return (True, "full") if level_in_half(lower, half_low, half_high) else (False, "none")
    if not eligible_overlap(lower, upper, half_low, half_high):
        return False, "none"
    full = half_low <= lower and upper <= half_high
    return True, "full" if full else "partial"
