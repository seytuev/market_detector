"""§4: Orderblock — восстановление консолидации (базы) перед подтверждённым FVG.

Рабочая интерпретация критерия консолидации (§4 «Рабочая интерпретация для
проектирования», открытое решение §14.1 — пороги НЕ калиброваны):

- идём назад от первой свечи FVG и пропускаем свечи импульса — серию
  одноцветных (в направлении движения) свечей непосредственно перед FVG;
- база начинается с первой свечи противоположного импульсу цвета; одиночная
  противоположная свеча перед импульсом — допустимый OB (§4);
- база расширяется назад, пока тело очередной свечи (open..close) целиком
  внутри текущего диапазона базы: свеча, закрывшаяся за границей базы, —
  свеча выхода и в базу не включается (для медвежьего OB — ниже L,
  для бычьего — выше U); цвет свечи значения не имеет (§4: смешанный цвет
  сам по себе не разрывает группу). Вопрос итеративного расползания
  диапазона (кейс №550) — открытая калибровка с владельцем (ТЗ 06.10.2026
  §8: точную новую формулу из отклонений не вывести, hardcode запрещён);
- свечи базы, входящие в самостоятельные подтверждённые FVG любого
  направления, помечаются флагом independent_fvg_member в журнале решений
  (ТЗ 06.10.2026 §8: проверка «не включены ли в одну консолидацию
  самостоятельные движения с собственным FVG»); исключение по этому
  признаку — открытая калибровка с владельцем: жёсткий обрыв ломает
  согласованную эталонную геометрию §13.19;
- ограничение длины базы — cfg.uncalibrated_consolidation_max_candles
  (некалиброванная настройка; новых порогов по отклонённым примерам
  №550/549/131 НЕ вводится, ТЗ 06.10.2026 §8/T15);
- решение include/exclude и основание по каждой свече — в
  evidence.candle_decisions (ТЗ 06.10.2026 §8);
- L = min(Low), U = max(High) включённых свечей.

Подтверждение OB: FVG должен быть ЗА пределами базы в направлении импульса
(§4): для бычьего OB — полностью выше U, для медвежьего — полностью ниже L.
Внутренний FVG не подтверждает (приёмка §13.2, эталон §13.19).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import DetectorConfig
from ..models import Candle, Direction, close_boundary_ms
from .fvg import FvgRecord


@dataclass
class BaseRecord:
    """Найденная консолидация перед FVG (ещё не зона в БД)."""

    direction: Direction            # направление OB = направлению импульса/FVG
    lower: float
    upper: float
    formed_at: int                  # open_time первой (старой) свечи базы
    source_candles: list[int]       # open_time всех свечей базы, от старых к новым
    start_idx: int                  # индекс первой свечи базы в отсортированном списке
    end_idx: int                    # индекс последней свечи базы
    gap_candles: int                # свечей от конца базы до первой свечи FVG (отложенный FVG, §14.2)
    evidence: dict = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2


def _is_impulse_color(candle: Candle, direction: Direction) -> bool:
    """Свеча окрашена в направление импульса (для медвежьего OB — медвежья)."""
    return candle.is_bull if direction == Direction.BULL else not candle.is_bull


def find_base(
    candles: list[Candle], fvg: FvgRecord, cfg: DetectorConfig
) -> Optional[BaseRecord]:
    """Восстанавливает консолидацию перед FVG. None — база не найдена."""
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    idx_by_ot = {c.open_time: i for i, c in enumerate(closed)}
    i1 = idx_by_ot.get(fvg.candle_open_times[0])
    if i1 is None or i1 == 0:
        return None
    direction = fvg.direction

    # ТЗ 06.10.2026 §8: свечи, входящие в самостоятельные подтверждённые FVG
    # (любого направления), помечаются в журнале решений — проверка «не
    # включены ли в одну консолидацию самостоятельные движения с собственным
    # FVG». Исключение по этому признаку — открытая калибровка с владельцем
    # (hard stop ломает согласованную эталонную геометрию §13.19, T24);
    # флаг выводится в диагностику спорных примеров (№550/549/131, T15)
    fvg_member_times: set[int] = set()
    for j in range(2, len(closed)):
        c1, c3 = closed[j - 2], closed[j]
        if c1.high < c3.low or c1.low > c3.high:
            fvg_member_times.update(
                (closed[j - 2].open_time, closed[j - 1].open_time, c3.open_time)
            )

    # 1) пропуск свечей импульса/выхода непосредственно перед FVG
    i = i1 - 1
    skipped: list[int] = []
    while i >= 0 and _is_impulse_color(closed[i], direction):
        skipped.append(closed[i].open_time)
        i -= 1
    if i < 0:
        return None

    # 2) затравка — первая свеча противоположного цвета (одиночный OB допустим)
    end_idx = i
    members = [closed[i]]
    lo, hi = closed[i].low, closed[i].high
    decisions: list[dict] = [{
        "open_time": closed[i].open_time, "decision": "include",
        "reason": "затравка: первая свеча противоположного импульсу цвета",
    }]
    stop_note: Optional[dict] = None

    # 3) расширение назад: тело свечи должно оставаться внутри диапазона базы
    i -= 1
    while i >= 0 and len(members) < cfg.uncalibrated_consolidation_max_candles:
        c = closed[i]
        body_lo, body_hi = min(c.open, c.close), max(c.open, c.close)
        if body_lo < lo or body_hi > hi:
            decisions.append({
                "open_time": c.open_time, "decision": "exclude",
                "reason": "тело за пределами текущего диапазона базы — свеча выхода, не включена",
            })
            stop_note = {
                "open_time": c.open_time,
                "reason": "тело за пределами текущего диапазона базы — свеча выхода, не включена",
            }
            break
        decisions.append({
            "open_time": c.open_time, "decision": "include",
            "reason": "тело внутри текущего диапазона базы",
            # ТЗ 06.10.2026 §8: флаг самостоятельного движения — диагностика,
            # не основание исключения (открытая калибровка, см. docstring)
            **({"independent_fvg_member": True}
               if c.open_time in fvg_member_times else {}),
        })
        members.append(c)
        lo = min(lo, c.low)
        hi = max(hi, c.high)
        i -= 1
    else:
        if len(members) >= cfg.uncalibrated_consolidation_max_candles:
            stop_note = {"reason": f"лимит длины базы {cfg.uncalibrated_consolidation_max_candles} свечей (§14.1, не калибровано)"}

    members.reverse()
    source = [c.open_time for c in members]
    evidence = {
        "rule": "§4: консолидация перед FVG, рабочая интерпретация (§14.1, пороги не калиброваны)",
        "included_open_times": source,
        "skipped_impulse_candles": skipped,
        # ТЗ 06.10.2026 §8: решение include/exclude и основание по каждой свече
        "candle_decisions": decisions,
        "contains_independent_fvg_members": any(
            d.get("independent_fvg_member") for d in decisions
        ),
        "stop": stop_note,
        # ТЗ 07.10.2026 §4.2 (T04): технический лимит сканирования — НЕ
        # рыночная граница базы. Поиск ограничен, более ранний контекст не
        # проверен — зона помечается, а не объявляется окончательной
        **({"base_search_limited": True,
            "base_search_limited_reason": (
                f"достигнут технический лимит сканирования "
                f"{cfg.uncalibrated_consolidation_max_candles} свечей — "
                f"более ранний контекст не проверен (ТЗ 07.10.2026 §4.2)"
            )} if stop_note and "лимит длины базы" in stop_note.get("reason", "")
           else {}),
        # ТЗ 06.10.2026 §7 (T08): первая проверенная тройка (FVG, породивший
        # поиск базы) хранится отдельно от фактического подтверждающего FVG
        # (evidence.actual_confirming_fvg + relation.confirming_fvg_id)
        "tested_fvg_triples": [list(fvg.candle_open_times)],
        "fvg_candle_open_times": list(fvg.candle_open_times),  # legacy-ключ
    }

    # §9.3 (приоритетное уточнение): тень следующей за базой импульсной свечи,
    # расширяющая экстремум основания, входит в OB — только соответствующий
    # конец (для бычьего OB — Low, для медвежьего — High), не вся свеча.
    # Кейс-образец: 164407 (нижний якорь 57800.19).
    # ТЗ 07.10.2026 §6 (T09): anchor — только ПРИЧИННАЯ свеча первоначального
    # движения (окрашена в направление импульса), не любой поздний ретест;
    # OHLC и момент доступности новой границы сохраняются
    next_candle = closed[end_idx + 1] if end_idx + 1 < len(closed) else None
    if next_candle is not None and not _is_impulse_color(next_candle, direction):
        next_candle = None
    if next_candle is not None:
        anchor_common = {
            "open_time": next_candle.open_time,
            "ohlc": [next_candle.open, next_candle.high,
                     next_candle.low, next_candle.close],
            "direction": direction.value,
            "available_at": close_boundary_ms(next_candle.open_time,
                                              next_candle.timeframe),
        }
        if direction == Direction.BULL and next_candle.low < lo:
            evidence["boundary_anchor"] = {
                **anchor_common, "side": "low",
                "original": lo, "extended": next_candle.low,
                "rule": "§9.3: нижняя тень импульсной свечи расширяет основание",
            }
            lo = next_candle.low
        elif direction == Direction.BEAR and next_candle.high > hi:
            evidence["boundary_anchor"] = {
                **anchor_common, "side": "high",
                "original": hi, "extended": next_candle.high,
                "rule": "§9.3: верхняя тень импульсной свечи расширяет основание",
            }
            hi = next_candle.high

    return BaseRecord(
        direction=direction,
        lower=lo,
        upper=hi,
        formed_at=members[0].open_time,
        source_candles=source,
        start_idx=end_idx - len(members) + 1,
        end_idx=end_idx,
        gap_candles=i1 - end_idx,
        evidence=evidence,
    )


def is_external(base: BaseRecord, fvg: FvgRecord) -> bool:
    """§4: подтверждающий FVG — за пределами базы в направлении импульса.

    Бычий OB: FVG полностью выше U базы; медвежий — полностью ниже L.
    FVG внутри диапазона консолидации не подтверждает OB.
    ТЗ 07.10.2026 §5: условие bull L_fvg >= U_ob / bear U_fvg <= L_ob —
    равенство касающихся краёв допустимо как неперекрытие (FVG имеет
    положительную ширину по построению: строгие High1<Low3 / Low1>High3).
    """
    if base.direction == Direction.BULL:
        return fvg.lower >= base.upper
    return fvg.upper <= base.lower
