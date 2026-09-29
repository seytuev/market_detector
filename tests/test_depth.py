"""§2/§6/§8/§9: глубина, середина, первый заход до 90%, скачок насквозь (§13.7, §13.11)."""
from __future__ import annotations

import pytest

from app.engine.depth import depth_level, distance_to_zone, zone_depth
from app.engine.scanner import Scanner
from app.models import Direction, EventKind, Zone, ZoneStatus, ZoneType

T0 = 1780272000000


def _zone(ztype=ZoneType.OB, direction=Direction.BULL, lower=100.0, upper=110.0):
    return Zone(
        id=None, instrument_id=1, type=ztype, direction=direction,
        timeframe="H4", lower=lower, upper=upper, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )


def test_depth_math_bull():
    z = _zone(direction=Direction.BULL)
    assert zone_depth(z, 110.0) == pytest.approx(0.0)   # касание
    assert zone_depth(z, 105.0) == pytest.approx(0.5)   # середина
    assert zone_depth(z, 101.0) == pytest.approx(0.9)
    assert zone_depth(z, 99.0) == pytest.approx(1.1)    # за дальней границей
    assert zone_depth(z, 111.0) == pytest.approx(-0.1)  # не дошла
    assert depth_level(z, 0.0) == pytest.approx(110.0)
    assert depth_level(z, 0.5) == pytest.approx(105.0)  # U − d·W
    assert depth_level(z, 0.9) == pytest.approx(101.0)


def test_depth_math_bear():
    z = _zone(direction=Direction.BEAR)
    assert zone_depth(z, 100.0) == pytest.approx(0.0)
    assert zone_depth(z, 105.0) == pytest.approx(0.5)
    assert zone_depth(z, 109.0) == pytest.approx(0.9)
    assert depth_level(z, 0.5) == pytest.approx(105.0)  # L + d·W
    assert depth_level(z, 0.9) == pytest.approx(109.0)


def test_distance_to_zone():
    z = _zone()
    assert distance_to_zone(z, 105.0) == 0.0            # внутри — ноль (§9)
    assert distance_to_zone(z, 112.0) == pytest.approx(2.0 / 112.0)
    assert distance_to_zone(z, 95.0) == pytest.approx(5.0 / 95.0)


def test_first_entry_straight_to_90_single_event(db, cfg, instrument_id):
    """Первый заход сразу до 90% → одно DEPTH_90 с фактической глубиной,
    без отдельных TOUCH/DEPTH_50 (§13.7). ТЗ «Единый движок» §3: для OB это
    НЕ завершение — зона остаётся актуальной, но недоступной для нового входа."""
    z = _zone()
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    events = scanner.on_price(instrument_id, 100.5, T0 + 1)  # глубина 0.95
    assert [e.kind for e in events] == [EventKind.DEPTH_90]
    assert events[0].depth == pytest.approx(0.95)
    after = db.get_zone(zid)
    assert after.status == ZoneStatus.ACTIVE           # ТЗ §3: OB не worked по 90%
    assert after.market_validity == "active"
    assert after.max_test_depth == pytest.approx(0.95)
    assert after.entry_eligible is False               # 95% ≥ 90% — вход запрещён
    # повторные тики без новых порогов событий не дают
    assert scanner.on_price(instrument_id, 102.0, T0 + 2) == []
    all_events = db.get_events(zid)
    assert len(all_events) == 1


def test_prb_90_still_worked(db, cfg, instrument_id):
    """Прежнее правило 90% → WORKED сохраняется для PRB (ТЗ §6: ограничение
    OB на PRB не переносится)."""
    z = _zone(ztype=ZoneType.PRB)
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    events = scanner.on_price(instrument_id, 100.5, T0 + 1)
    assert [e.kind for e in events] == [EventKind.DEPTH_90]
    assert db.get_zone(zid).status == ZoneStatus.WORKED
    assert db.open_visit_for(zid, 1) is None  # заход завершён отработкой


def test_gradual_depth_thresholds(db, cfg, instrument_id):
    """TOUCH → DEPTH_50 → DEPTH_90 по мере углубления; пороги не повторяются.
    ТЗ §3: OB на 90% остаётся ACTIVE, но entry_eligible=False (ровно 90%)."""
    z = _zone()
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    assert scanner.on_price(instrument_id, 111.5, T0 + 1)[0].kind == EventKind.APPROACH
    assert scanner.on_price(instrument_id, 109.0, T0 + 2)[0].kind == EventKind.TOUCH
    assert scanner.on_price(instrument_id, 108.0, T0 + 3) == []  # тот же порог — молчим
    assert scanner.on_price(instrument_id, 104.0, T0 + 4)[0].kind == EventKind.DEPTH_50
    assert scanner.on_price(instrument_id, 101.0, T0 + 5)[0].kind == EventKind.DEPTH_90
    after = db.get_zone(zid)
    assert after.status == ZoneStatus.ACTIVE           # ТЗ §3: не WORKED
    assert after.entry_eligible is False               # ровно 90% — недопустимо
    assert after.max_test_depth == pytest.approx(0.9)
    kinds = [e.kind for e in db.get_events(zid)]
    assert sorted(kinds, key=str) == sorted(
        [EventKind.APPROACH, EventKind.TOUCH, EventKind.DEPTH_50, EventKind.DEPTH_90],
        key=str,
    )


def test_jump_through_no_touch(db, cfg, instrument_id):
    """Скачок через зону насквозь → JUMP_THROUGH без обычного TOUCH (§6, §13.11)."""
    z = _zone()
    zid = db.insert_zone(z)
    scanner = Scanner(db, cfg)
    assert scanner.on_price(instrument_id, 115.0, T0 + 1) == []  # выше зоны, вне полосы 2%
    events = scanner.on_price(instrument_id, 95.0, T0 + 2)       # перескочили насквозь
    assert [e.kind for e in events] == [EventKind.JUMP_THROUGH]
    assert db.get_events(zid)[0].evidence["prev_price"] == 115.0
    # TOUCH не порождён
    assert all(e.kind != EventKind.TOUCH for e in db.get_events(zid))
