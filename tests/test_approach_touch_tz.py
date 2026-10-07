"""ТЗ 07.10.2026 §4.1–§4.2: приближение/касание без противоречий
(приёмка T07–T10)."""
from __future__ import annotations

import pytest

from app.engine.depth import approach_distance
from app.engine.scanner import Scanner
from app.models import Direction, EventKind, Zone, ZoneStatus, ZoneType

T0 = 1780272000000


def _fvg_bull(lower: float, upper: float) -> Zone:
    return Zone(
        id=None, instrument_id=1, type=ZoneType.FVG, direction=Direction.BULL,
        timeframe="W1", lower=lower, upper=upper, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )


def test_t07_approach_distance_example_b():
    """FVG W1 [81 951; 82 563], P = 83 827,72: приближение ≈1,51% (порог 2%),
    а не «цена пришла в зону» (§4.1, единая формула 100·|P−B|/P)."""
    z = _fvg_bull(81_951.0, 82_563.0)
    dist = approach_distance(z, 83_827.72, 83_827.72)
    assert dist == pytest.approx(0.0151, abs=1e-4)
    # внутри зоны APPROACH не существует
    assert approach_distance(z, 82_257.0, 82_257.0) is None


def test_t08_boundary_reached_is_touch_not_approach(db, cfg, instrument_id):
    """Первое достижение границы — событие касания, не приближения (T08)."""
    z = _fvg_bull(100.0, 110.0)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    events = scanner.on_price(instrument_id, 110.0, T0 + 1)
    assert [e.kind for e in events] == [EventKind.TOUCH]
    assert EventKind.APPROACH not in {e.kind for e in db.get_events(zid)}


def test_t08_price_inside_zone_no_approach(db, cfg, instrument_id):
    """Цена внутри диапазона: APPROACH не создаётся (T08, §4.1)."""
    z = _fvg_bull(100.0, 110.0)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 113.5, T0 + 1)     # вне полосы 2%
    events = scanner.on_price(instrument_id, 105.0, T0 + 2)  # внутри, до середины
    assert [e.kind for e in events] == [EventKind.FVG_WEAKENED]
    assert EventKind.APPROACH not in {e.kind for e in db.get_events(zid)}


def test_t09_single_update_touch_has_priority(db, cfg, instrument_id):
    """Одним обновлением достигнуты и порог приближения, и зона: только
    касание, без дублирующего APPROACH (T09, §4.2)."""
    z = _fvg_bull(100.0, 110.0)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 113.5, T0 + 1)      # вне зоны, вне полосы 2%
    events = scanner.on_price(instrument_id, 109.0, T0 + 2)  # сразу в зону
    assert [e.kind for e in events] == [EventKind.TOUCH]
    assert EventKind.APPROACH not in {e.kind for e in db.get_events(zid)}


def test_t09_no_approach_after_touch_and_exit(db, cfg, instrument_id):
    """После касания и выхода повторное сближение не порождает APPROACH —
    зона уже достигалась в этом цикле (§4.1/§4.2, устранение противоречия)."""
    z = _fvg_bull(100.0, 110.0)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    assert scanner.on_price(instrument_id, 111.5, T0 + 1)[0].kind == EventKind.APPROACH
    assert scanner.on_price(instrument_id, 112.5, T0 + 2) == []  # вышли из полосы
    assert scanner.on_price(instrument_id, 109.0, T0 + 3)[0].kind == EventKind.TOUCH
    scanner.on_price(instrument_id, 112.5, T0 + 4)               # выход из зоны
    events = scanner.on_price(instrument_id, 111.5, T0 + 5)      # снова в полосе 2%
    assert events == []
    kinds = [e.kind for e in db.get_events(zid)]
    assert kinds.count(EventKind.APPROACH) == 1
    assert EventKind.TOUCH in kinds


def test_t10_touch_recorded_with_event_time(db, cfg, instrument_id):
    """Внутрисвечное касание фиксируется событием со своей ценой/временем,
    даже если к моменту доставки цена уже ушла (T10, §4.2)."""
    z = _fvg_bull(100.0, 110.0)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 112.0, T0 + 1)
    touch = scanner.on_price(instrument_id, 108.0, T0 + 2)[0]
    scanner.on_price(instrument_id, 112.0, T0 + 3)  # цена ушла из зоны
    assert touch.kind == EventKind.TOUCH
    assert touch.occurred_at == T0 + 2
    assert touch.price == pytest.approx(108.0)
    stored = [e for e in db.get_events(zid) if e.kind == EventKind.TOUCH]
    assert len(stored) == 1 and stored[0].occurred_at == T0 + 2
