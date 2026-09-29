"""§6 + §15.6/§9.5–6: Breaker и состояния блоков.

Переход OB → Breaker (уточнения с приоритетом над прежним правилом):
- закрытие свечи таймфрейма OB за дальней границей (тень не считается);
- НОВЫЙ FVG пробойного движения (старый подтверждающий FVG исходного OB
  не заменяет его); до обоих подтверждений активного Breaker нет;
- предыдущий ОТДЕЛЬНЫЙ тест с глубиной >50% навсегда исключает Breaker
  для данного OB; ровно 50% допустимо; глубины самого пробойного движения
  не считаются предыдущими тестами.

Рабочая интерпретация «пробойного движения» (открыто для формализации §15.6):
предыдущий тест — закрытый заход (Visit) с выходом обратно (exit_kind=return)
или заход, завершённый отработкой (worked), после которого был новый вход.
Заход, ушедший за дальнюю границу (beyond), — само пробойное движение.

Пробитый Breaker по закрытию за дальней границей → ARCHIVED, обратного
превращения нет (§13.16). PRB при таком закрытии архивируется без Breaker
(§5, §13.10).
"""
from __future__ import annotations

from typing import Optional

from ..config import DetectorConfig
from ..models import Candle, Direction, TIMEFRAME_MINUTES, Visit, Zone, ZoneStatus, ZoneType
from . import depth as geom
from .fvg import FvgRecord, scan_fvgs


def far_boundary_broken(zone: Zone, candle: Candle) -> bool:
    """Закрытие свечи за дальней границей зоны (тень не считается).

    Бычья зона: Close < L; медвежья: Close > U.
    """
    if zone.direction == Direction.BULL:
        return candle.close < zone.lower
    return candle.close > zone.upper


def converts_to_breaker(ob: Zone, candle: Candle) -> bool:
    """Первое из двух условий §15.6: закрытие за дальней границей."""
    return ob.type == ZoneType.OB and far_boundary_broken(ob, candle)


def breaker_forbidden(
    ob: Zone, visits: list[Visit], candles: list[Candle], cfg: DetectorConfig
) -> bool:
    """§15.6/§9.5: предыдущий отдельный тест >50% исключает Breaker навсегда.

    Ровно 50% допустимо. Глубины пробойного движения тестами не считаются.
    Порог — cfg.depth_mid (§2). ТЗ «Единый движок» §4/§6: сравнение глубины —
    точное, по экстремуму захода (без epsilon); ранние самостоятельные тесты
    до подтверждения FVG учитываются (хранятся визитами кандидата).
    Рабочая интерпретация (§15.6 — открыто для формализации):
    - заход с выходом обратно (exit_kind='return') и глубиной >depth_mid — тест;
    - заход, завершённый отработкой 90% ('worked'), — тоже отдельный тест,
      если после него была закрытая свеча обратно на стороне входа (для
      бычьего OB — Close > U, для медвежьего — Close < L); без такого выхода
      90% считается частью того же движения (§15.6, приёмка «90% в том же
      движении и отдельным предыдущим тестом»).
    """
    ordered = sorted(visits, key=lambda v: v.entered_at)
    for v in ordered:
        if v.exited_at is None:
            continue
        if v.extreme is not None:
            deep = geom.strictly_deeper(ob, v.extreme, cfg.depth_mid)
        else:
            # legacy-визиты без сохранённого экстремума — по float-глубине
            deep = v.max_depth > cfg.depth_mid
        if not deep:
            continue
        if v.exit_kind == "return":
            return True
        if v.exit_kind == "worked":
            for c in candles:
                if not c.closed or c.open_time < v.entered_at:
                    continue
                if ob.direction == Direction.BULL and c.close > ob.upper:
                    return True
                if ob.direction == Direction.BEAR and c.close < ob.lower:
                    return True
    return False


def find_breakout_fvg(
    candles: list[Candle], ob: Zone, breakout_open_ms: int, cfg: DetectorConfig
) -> Optional[FvgRecord]:
    """Новый FVG пробойного движения (§9.6).

    Направление — направление пробоя (противоположно направлению OB).
    Окно привязки (§15.6, открыто для формализации — не калибровано):
    первая свеча FVG не раньше свечи пробоя минус back-свечей; подтверждение
    не позже закрытия пробоя плюс delay-свечей (FVG может подтвердиться позже
    пробойного закрытия — приёмка §15.6). Старый подтверждающий FVG исходного
    OB исключён явно.
    """
    tf_ms = TIMEFRAME_MINUTES[ob.timeframe] * 60_000
    breakout_dir = Direction.BULL if ob.direction == Direction.BEAR else Direction.BEAR
    lo_bound = breakout_open_ms - cfg.uncalibrated_breakout_fvg_back_candles * tf_ms
    hi_bound = breakout_open_ms + tf_ms + cfg.uncalibrated_breakout_fvg_delay_candles * tf_ms
    old_fvg_formed = ob.evidence.get("confirming_fvg_formed_at")

    best: Optional[FvgRecord] = None
    for f in scan_fvgs(candles, ob.timeframe):
        if f.direction != breakout_dir:
            continue
        if f.candle_open_times[0] < lo_bound:
            continue
        if f.confirmed_at > hi_bound:
            continue
        if old_fvg_formed is not None and f.formed_at == old_fvg_formed:
            continue
        if best is None or f.confirmed_at < best.confirmed_at:
            best = f
    return best


def make_breaker(ob: Zone, activate_ms: int, now_ms: int,
                 breakout_fvg: Optional[FvgRecord] = None) -> Zone:
    """Новая зона Breaker с прежними границами и своим жизненным циклом (§6).

    activate_ms — момент выполнения обоих условий §15.6 (закрытие + новый
    FVG): max(граница пробойного закрытия, подтверждение FVG).
    """
    evidence = {
        "rule": "§6+§15.6: OB → Breaker по закрытию за дальней границей "
                "и новому FVG пробойного движения",
        "predecessor_ob_id": ob.id,
    }
    if breakout_fvg is not None:
        evidence["breakout_fvg_range"] = [breakout_fvg.lower, breakout_fvg.upper]
        evidence["breakout_fvg_formed_at"] = breakout_fvg.formed_at
        evidence["breakout_fvg_confirmed_at"] = breakout_fvg.confirmed_at
    return Zone(
        id=None,
        instrument_id=ob.instrument_id,
        type=ZoneType.BREAKER,
        direction=Direction.BULL if ob.direction == Direction.BEAR else Direction.BEAR,
        timeframe=ob.timeframe,
        lower=ob.lower,
        upper=ob.upper,
        formed_at=activate_ms,
        confirmed_at=activate_ms,
        status=ZoneStatus.ACTIVE,
        cycle_id=ob.cycle_id + 1,
        source="auto",
        rule_version=ob.rule_version,
        source_candles=list(ob.source_candles),
        evidence=evidence,
        created_at=now_ms,
    )
