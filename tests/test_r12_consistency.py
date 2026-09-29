"""Регрессии §15.3/R12 (целостность связи OB↔FVG) и §9.3 (тень импульсной
свечи в границе OB)."""
from __future__ import annotations

from app.engine import replay_from_db
from app.engine.scanner import Scanner
from app.models import Direction, EventKind, TIMEFRAME_MINUTES, Zone, ZoneRelation, ZoneStatus, ZoneType

from .conftest import load_etalon_candles, make_candle

H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
T0 = 1780272000000


def test_r12_confirming_fvg_consistency_on_etalon(db, cfg, instrument_id):
    """§15.3/R12: у каждого OB один проверяемый источник подтверждения —
    relation, evidence и confirmed_at описывают один и тот же FVG.

    Регрессия по кейсу 164398: повторное нахождение той же базы поздним FVG
    не должно перезаписывать связь первого подтверждения.
    """
    scanner = Scanner(db, cfg)
    for c in load_etalon_candles(instrument_id):
        scanner.on_closed_candle(c)
    # повторный прогон — поздние FVG находят те же базы; связи не меняются
    for c in load_etalon_candles(instrument_id):
        scanner.on_closed_candle(c)

    obs = db.get_zones(instrument_id, types=[ZoneType.OB])
    assert obs, "эталон должен дать OB"
    for ob in obs:
        rel = db.get_relation(ob.id)
        if rel is None or rel.confirming_fvg_id is None:
            continue
        fvg = db.get_zone(rel.confirming_fvg_id)
        assert fvg is not None
        # OB не может быть подтверждён раньше закрытия 3-й свечи именно этого FVG
        assert ob.confirmed_at == fvg.confirmed_at
        # evidence ссылается на тот же FVG, что и relation
        rng = ob.evidence.get("confirming_fvg_range")
        if rng is not None:
            assert [fvg.lower, fvg.upper] == [pytest_approx(rng[0]), pytest_approx(rng[1])]


def pytest_approx(x: float) -> float:
    return x  # сравнение точное: одна и та же запись об одном FVG


def test_relation_first_write_wins(db, cfg, instrument_id):
    """Первый подтверждающий FVG сохраняется; поздний не перезаписывает."""
    zone = Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=100.0, upper=110.0, formed_at=T0, confirmed_at=T0,
        status=ZoneStatus.ACTIVE,
    )
    zid = db.insert_zone(zone)
    db.set_relation(ZoneRelation(zone_id=zid, confirming_fvg_id=111))
    db.set_relation(ZoneRelation(zone_id=zid, confirming_fvg_id=222))
    assert db.get_relation(zid).confirming_fvg_id == 111


def test_impulse_shadow_extends_ob_boundary(db, cfg, instrument_id):
    """§9.3: тень следующей за базой импульсной свечи, расширяющая экстремум
    основания, входит в границу OB (бычий OB — нижняя тень). Якорь — в evidence."""
    scanner = Scanner(db, cfg)
    # база: две смешанные свечи [100..110]
    scanner.on_closed_candle(make_candle(T0, 105, 110, 100, 108, "H4", instrument_id))
    scanner.on_closed_candle(make_candle(T0 + H4_MS, 108, 109, 102, 103, "H4", instrument_id))
    # импульсная свеча: уходит вверх, но её Low 99 расширяет основание вниз
    scanner.on_closed_candle(make_candle(T0 + 2 * H4_MS, 103, 118, 99, 117, "H4", instrument_id))
    # свечи FVG: бычий FVG [118.5, 119.5] выше базы — внешний
    scanner.on_closed_candle(make_candle(T0 + 3 * H4_MS, 117, 118.5, 112, 113, "H4", instrument_id))
    scanner.on_closed_candle(make_candle(T0 + 4 * H4_MS, 113, 120, 112.5, 119, "H4", instrument_id))
    scanner.on_closed_candle(make_candle(T0 + 5 * H4_MS, 119, 121, 119.5, 120.5, "H4", instrument_id))

    obs = [z for z in db.get_zones(instrument_id, types=[ZoneType.OB])
           if z.direction == Direction.BULL]
    assert len(obs) == 1
    ob = obs[0]
    assert ob.lower == 99.0   # тень импульсной свечи вошла в границу
    assert ob.upper == 110.0  # верх без изменений (противоположный конец не включаем)
    anchor = ob.evidence.get("boundary_anchor")
    assert anchor and anchor["side"] == "low" and anchor["original"] == 100.0
    assert anchor["open_time"] == T0 + 2 * H4_MS
    # свеча-якорь не включается как член базы
    assert (T0 + 2 * H4_MS) not in ob.source_candles


def test_etalon_not_extended_by_shadow_rule(db, cfg, instrument_id):
    """Эталон §13.19 НЕ меняется правилом §9.3: High свечи 08:00 (67476.69)
    ниже U базы (68146.30) — расширения нет."""
    scanner = Scanner(db, cfg)
    for c in load_etalon_candles(instrument_id):
        scanner.on_closed_candle(c)
    obs = [z for z in db.get_zones(instrument_id, types=[ZoneType.OB])
           if z.lower == 65426.34]
    assert len(obs) == 1
    assert obs[0].upper == 68146.30
    assert "boundary_anchor" not in obs[0].evidence
