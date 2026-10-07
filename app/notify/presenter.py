"""Нормализованный тип события и единый снимок данных (ТЗ 07.10.2026 §4, §11).

Заголовок, строка «Событие», статус, кнопки и изображение строятся из одного
EventSnapshot, а не из нескольких независимых шаблонов. Внутренние названия
адаптированы к текущей модели (EventKind / ltf_event.kind), смысл и
взаимоисключение противоречивых утверждений сохранены:

- APPROACH — приближение к зоне, цена ещё вне зоны;
- TOUCH — первое касание границы/достижение зоны;
- DEPTH_50 / DEPTH_90 — достижение 50% / 90% зоны (FVG: 50% = ослабление);
- ZONE_INVALIDATED — зона перестала быть актуальной по правилам своего типа;
- LIQUIDITY_TAKEN — SSL/BSL снят; НЕ обратная зона входа;
- SWEEP_CONFIRMED — первое снятие без закрепления, подтверждённое закрытием H1;
- BOS_CONFIRMED / SMS_CONFIRMED — подтверждённый структурный слом;
- ENTRY_TOUCH — касание допустимой LTF Entry Zone;
- SCENARIO_CANCELLED — отмена сценария (причина в payload);
- INFO — остальные служебные виды (сервис, подтверждения пользователя).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..models import EventKind
from ..texts_ru import DIRECTION_RU, STATUS_RU, TYPE_RU


class NormalizedKind(str, Enum):
    APPROACH = "approach"
    TOUCH = "touch"
    DEPTH_50 = "depth_50"
    DEPTH_90 = "depth_90"
    ZONE_INVALIDATED = "zone_invalidated"
    LIQUIDITY_TAKEN = "liquidity_taken"
    SWEEP_CONFIRMED = "sweep_confirmed"
    BOS_CONFIRMED = "bos_confirmed"
    SMS_CONFIRMED = "sms_confirmed"
    ENTRY_TOUCH = "entry_touch"
    SCENARIO_CANCELLED = "scenario_cancelled"
    INFO = "info"


_HTF_MAP: dict[EventKind, NormalizedKind] = {
    EventKind.APPROACH: NormalizedKind.APPROACH,
    EventKind.TOUCH: NormalizedKind.TOUCH,
    EventKind.ALREADY_IN_ZONE: NormalizedKind.TOUCH,
    EventKind.JUMP_THROUGH: NormalizedKind.TOUCH,
    EventKind.DEPTH_50: NormalizedKind.DEPTH_50,
    EventKind.FVG_WEAKENED: NormalizedKind.DEPTH_50,
    EventKind.DEPTH_90: NormalizedKind.DEPTH_90,
    EventKind.FVG_FILLED: NormalizedKind.ZONE_INVALIDATED,
    EventKind.OB_INVALIDATED: NormalizedKind.ZONE_INVALIDATED,
    EventKind.BREAKER_ARCHIVED: NormalizedKind.ZONE_INVALIDATED,
    EventKind.PRB_ARCHIVED: NormalizedKind.ZONE_INVALIDATED,
    EventKind.LEVEL_TAKEN: NormalizedKind.LIQUIDITY_TAKEN,
}

_LTF_MAP: dict[str, NormalizedKind] = {
    "bos": NormalizedKind.BOS_CONFIRMED,
    "sms": NormalizedKind.SMS_CONFIRMED,
    "touch": NormalizedKind.ENTRY_TOUCH,
    "sweep_confirmed": NormalizedKind.SWEEP_CONFIRMED,
    # «пройден без возврата» — ликвидность снята с закреплением; уровень
    # исключён из выбора, это НЕ обратная зона входа
    "sweep_failed": NormalizedKind.LIQUIDITY_TAKEN,
    "cancellation": NormalizedKind.SCENARIO_CANCELLED,
}


@dataclass
class EventSnapshot:
    """Связь события/рендера с данными снимка (§11). Текст, статус, кнопки
    и изображение обязаны брать значения отсюда — тогда заголовок, цена,
    время и статус не могут разъехаться."""

    kind: NormalizedKind
    raw_kind: str
    event_id: Optional[int] = None
    symbol: str = "?"
    instrument_id: Optional[int] = None
    venue: Optional[str] = None          # метаданные источника — внутри
    market_type: Optional[str] = None    # системы, не в обычном тексте (§5.1)
    timeframe: Optional[str] = None
    direction: Optional[str] = None      # bull | bear
    zone_id: Optional[int] = None
    zone_type: Optional[str] = None
    is_level: bool = False               # SSL/BSL: уровень, без середины
    lower: Optional[float] = None
    upper: Optional[float] = None
    mid: Optional[float] = None
    event_price: Optional[float] = None
    distance_pct: Optional[float] = None  # для APPROACH (§4.1)
    event_at: Optional[int] = None       # ms UTC — момент события
    detected_at: Optional[int] = None
    as_of: Optional[int] = None          # снимок данных, на котором событие
    candle_close_at: Optional[int] = None  # close_time свечи (не open_time!)
    scenario_id: Optional[int] = None
    parent_zone_id: Optional[int] = None
    parent_type: Optional[str] = None
    parent_timeframe: Optional[str] = None
    parent_direction: Optional[str] = None
    movement_id: Optional[int] = None
    range_version: Optional[int] = None
    status_ru: Optional[str] = None      # реальный статус из движка
    mode: str = "event_snapshot"         # event_snapshot | current_view
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def direction_ru(self) -> str:
        return DIRECTION_RU.get(self.direction or "", self.direction or "")

    @property
    def type_ru(self) -> str:
        return TYPE_RU.get(self.zone_type or "", self.zone_type or "")


def zone_status_ru(zone: Any) -> str:
    """Фактический статус зоны из движка (§5.2: не выдумывать «актуальна»)."""
    base = STATUS_RU.get(zone.status.value, zone.status.value)
    if getattr(zone, "market_validity", "active") != "active":
        return "невалидна"
    return base


def htf_snapshot(view: Any) -> EventSnapshot:
    """Снимок HTF-события из EventView диспетчера (queue.EventView)."""
    event, zone, ins = view.event, view.zone, view.instrument
    kind = _HTF_MAP.get(event.kind, NormalizedKind.INFO)
    is_level = zone is not None and zone.type.value in ("ssl", "bsl")
    return EventSnapshot(
        kind=kind,
        raw_kind=event.kind.value,
        event_id=event.id,
        symbol=ins.symbol if ins is not None else "?",
        instrument_id=ins.id if ins is not None else None,
        venue=ins.venue if ins is not None else None,
        market_type=ins.market_type if ins is not None else None,
        timeframe=zone.timeframe if zone is not None else None,
        direction=zone.direction.value if zone is not None else None,
        zone_id=zone.id if zone is not None else None,
        zone_type=zone.type.value if zone is not None else None,
        is_level=is_level,
        lower=zone.lower if zone is not None else None,
        upper=zone.upper if zone is not None else None,
        mid=None if is_level else (zone.mid if zone is not None else None),
        event_price=event.price,
        distance_pct=event.evidence.get("distance_pct"),
        event_at=event.occurred_at,
        detected_at=event.detected_at,
        as_of=event.detected_at,
        status_ru=zone_status_ru(zone) if zone is not None else None,
        payload=dict(event.evidence or {}),
    )


def ltf_snapshot(ev: Any, ctx: Any) -> EventSnapshot:
    """Снимок LTF-события из LtfEvent + LtfContext диспетчера."""
    kind = _LTF_MAP.get(ev.kind, NormalizedKind.INFO)
    ins, parent, sc = ctx.instrument, ctx.zone, ctx.scenario
    p = ev.payload or {}
    entry_type = p.get("type")
    is_level = entry_type in ("BSL", "SSL")
    candle_open = p.get("candle_open_time")
    return EventSnapshot(
        kind=kind,
        raw_kind=ev.kind,
        event_id=ev.id,
        symbol=ins.symbol if ins is not None else "?",
        instrument_id=ins.id if ins is not None else None,
        venue=ins.venue if ins is not None else None,
        market_type=ins.market_type if ins is not None else None,
        timeframe="H1",
        direction=sc.direction.value if sc is not None else None,
        zone_id=p.get("entry_zone_id"),
        zone_type=entry_type.lower() if entry_type else None,
        is_level=is_level,
        lower=p.get("lower"),
        upper=p.get("upper"),
        mid=None if is_level else p.get("mid"),
        event_price=p.get("price"),
        event_at=ev.occurred_at,
        detected_at=ev.detected_at,
        as_of=ev.detected_at,
        # occurred_at событий движка — close_time свечи (engine._emit)
        candle_close_at=ev.occurred_at if candle_open is not None else None,
        scenario_id=ev.scenario_id,
        parent_zone_id=parent.id if parent is not None else None,
        parent_type=parent.type.value if parent is not None else None,
        parent_timeframe=parent.timeframe if parent is not None else None,
        parent_direction=(
            parent.direction.value if parent is not None else None
        ),
        range_version=p.get("range_version"),
        status_ru=None,
        payload=dict(p),
    )
