"""§8/§9: «закрепление внутри» (закрытие D1 в зоне) — событие перехода.
Ежедневные напоминания отменены: пока цена закрывается внутри зоны
несколько дней подряд, событие эмитится один раз; повторное закрепление
после выхода — новое событие."""
from __future__ import annotations

from app.engine.scanner import Scanner
from app.models import Direction, EventKind, TIMEFRAME_MINUTES, Zone, ZoneStatus, ZoneType

from .conftest import make_candle

D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000
T0 = 1780272000000


def _d1_zone(db, instrument_id):
    z = Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )
    return db.get_zone(db.insert_zone(z))


def _day_candles(closes: list[float]):
    """Дневные свечи с перекрывающимися телами: close предыдущего дня =
    open текущего, чтобы последовательность не порождала лишних FVG."""
    candles = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c - 1.0
        candles.append(make_candle(
            T0 + (i + 1) * D1_MS, o, max(o, c) + 0.5, min(o, c) - 0.5, c,
        ))
    return candles


def test_d1_close_inside_only_on_transition(db, cfg, instrument_id):
    z = _d1_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    # день 1: снаружи; 2–3: внутри; 4: снаружи; 5–6: внутри
    candles = _day_candles([95.0, 105.0, 107.0, 115.0, 108.0, 109.0])
    per_day = [scanner.on_closed_candle(c) for c in candles]
    kinds = [
        [e.kind for e in evs if e.zone_id == z.id and e.kind == EventKind.D1_CLOSE_INSIDE]
        for evs in per_day
    ]
    # событие — только в дни перехода внутрь (2-й и 5-й), без ежедневных повторов
    assert kinds == [[], [EventKind.D1_CLOSE_INSIDE], [], [], [EventKind.D1_CLOSE_INSIDE], []]


def test_d1_close_inside_no_history_no_event_outside(db, cfg, instrument_id):
    """Закрытие вне зоны события не создаёт вовсе."""
    z = _d1_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    for c in _day_candles([90.0, 95.0, 99.0]):
        evs = [e for e in scanner.on_closed_candle(c) if e.zone_id == z.id]
        assert evs == []
