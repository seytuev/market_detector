"""ТЗ «Единый движок HTF/LTF» §2/§6: прежняя ветка §9.8 («первое касание H1
завершает зону») и запрет первой протестированной H1-зоны удалены — H1-зоны
живут по общему lifecycle. PRB сохраняет своё правило 90% → WORKED; FVG —
50% → WEAKENED, 100% → FILLED; JUMP_THROUGH не порождает обычный TOUCH."""
from __future__ import annotations

import asyncio

import pytest

from app.engine.scanner import Scanner
from app.models import now_ms, Direction, EventKind, TIMEFRAME_MINUTES, Zone, ZoneStatus, ZoneType
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender

H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000
T0 = 1780272000000


def _h1_zone(db, instrument_id, ztype=ZoneType.PRB, direction=Direction.BULL):
    z = Zone(
        id=None, instrument_id=instrument_id, type=ztype, direction=direction,
        timeframe="H1", lower=100.0, upper=110.0, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )
    return db.get_zone(db.insert_zone(z))


def test_h1_first_touch_keeps_zone_active(db, cfg, instrument_id):
    """Первое касание H1 — обычное событие TOUCH общего lifecycle; зона НЕ
    уходит в историю (ветка §9.8 удалена, ТЗ §6)."""
    z = _h1_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    # подход издалека — без касания событий глубины нет
    scanner.on_price(instrument_id, 120.0, T0 + H1_MS)
    # первое касание (бычья зона, возврат сверху)
    events = scanner.on_price(instrument_id, 110.0, now_ms())
    kinds = [e.kind for e in events]
    assert EventKind.TOUCH in kinds
    after = db.get_zone(z.id)
    assert after.status == ZoneStatus.ACTIVE
    assert after.display_until is None
    assert after.end_reason is None

    # углубление даёт обычные пороги; 90% завершает PRB (его правило, ТЗ §6)
    events2 = scanner.on_price(instrument_id, 105.0, T0 + 3 * H1_MS)
    assert [e.kind for e in events2] == [EventKind.DEPTH_50]
    events3 = scanner.on_price(instrument_id, 101.0, T0 + 4 * H1_MS)
    assert [e.kind for e in events3] == [EventKind.DEPTH_90]
    assert db.get_zone(z.id).status == ZoneStatus.WORKED
    # по отработанной PRB — тишина
    assert scanner.on_price(instrument_id, 100.5, T0 + 5 * H1_MS) == []


def test_h1_fvg_first_touch_is_not_fill(db, cfg, instrument_id):
    """Первое касание H1-FVG ≠ полное заполнение: FVG_FILLED не создаётся,
    зона остаётся активной (общие правила FVG, ТЗ §6)."""
    z = _h1_zone(db, instrument_id, ztype=ZoneType.FVG)
    scanner = Scanner(db, cfg)
    events = scanner.on_price(instrument_id, 109.5, T0 + 2 * H1_MS)
    kinds = [e.kind for e in events]
    assert EventKind.TOUCH in kinds
    assert EventKind.FVG_FILLED not in kinds
    assert db.get_zone(z.id).status == ZoneStatus.ACTIVE


def test_h1_jump_through_without_touch(db, cfg, instrument_id):
    """Проход H1-зоны насквозь — JUMP_THROUGH без сигнала касания (§6);
    автоархивации по §9.8 больше нет."""
    z = _h1_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 120.0, T0 + H1_MS)
    events = scanner.on_price(instrument_id, 90.0, T0 + 2 * H1_MS)  # скачок сквозь
    kinds = [e.kind for e in events]
    assert EventKind.TOUCH not in kinds
    assert EventKind.JUMP_THROUGH in kinds
    assert db.get_zone(z.id).status == ZoneStatus.ACTIVE


def test_h1_no_repeat_deliveries(db, cfg, instrument_id):
    """Без новых порогов — тишина: повторное нахождение в зоне (в т.ч. через
    120+ часов) не порождает ни событий, ни доставок."""
    z = _h1_zone(db, instrument_id)
    scanner = Scanner(db, cfg)
    sender = LogSender()
    dispatcher = EventDispatcher(db, cfg, sender)

    events = scanner.on_price(instrument_id, 110.0, now_ms())
    asyncio.run(dispatcher.dispatch(events))
    assert len(sender.cards) == 1
    # через 120+ часов всё ещё у той же границы — ни событий, ни доставок
    events2 = scanner.on_price(instrument_id, 109.5, T0 + 2 * H1_MS + 121 * 3600_000)
    asyncio.run(dispatcher.dispatch(events2))
    assert events2 == []
    assert len(sender.cards) == 1


def test_d1_zone_same_lifecycle_as_h1(db, cfg, instrument_id):
    """ТЗ §2/приёмка A: одинаковая ситуация даёт одинаковые события на H1 и D1
    — скрытых веток по таймфрейму нет."""
    zones = {}
    for tf in ("H1", "D1"):
        z = Zone(
            id=None, instrument_id=instrument_id, type=ZoneType.PRB,
            direction=Direction.BULL, timeframe=tf, lower=100.0, upper=110.0,
            formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE,
        )
        zones[tf] = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    events = scanner.on_price(instrument_id, 110.0, now_ms())
    for tf, zid in zones.items():
        kinds = [e.kind for e in events if e.zone_id == zid]
        assert kinds == [EventKind.TOUCH], tf
        assert db.get_zone(zid).status == ZoneStatus.ACTIVE
