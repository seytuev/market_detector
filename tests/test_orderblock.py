"""§4 + приёмка §13.19: эталон медвежьего OB BTCUSDT Binance H4 (2–3 июня 2026)."""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.engine.fvg import scan_fvgs
from app.engine.orderblock import find_base, is_external
from app.engine.scanner import Scanner
from app.models import Direction, ZoneType

from .conftest import (
    ETALON_BASE_OPEN_TIMES,
    ETALON_CONFIRMED_AT,
    ETALON_FVG_EXTERNAL,
    ETALON_FVG_INTERNAL_1,
    ETALON_FVG_INTERNAL_2,
    ETALON_L,
    ETALON_M,
    ETALON_U,
    load_etalon_candles,
)


def _fvg_by_formed_at(candles, formed_at):
    for f in scan_fvgs(candles, "H4"):
        if f.formed_at == formed_at:
            return f
    raise AssertionError(f"FVG с formed_at={formed_at} не найден")


def test_etalon_base_exact_bounds():
    """База — 4 свечи смешанного цвета; L=65426.34, U=68146.30, M=66786.32."""
    candles = load_etalon_candles()
    fvg = _fvg_by_formed_at(candles, ETALON_FVG_EXTERNAL[2])
    base = find_base(candles, fvg, DetectorConfig())
    assert base is not None
    assert base.direction == Direction.BEAR
    assert base.source_candles == ETALON_BASE_OPEN_TIMES
    assert base.lower == pytest.approx(ETALON_L)
    assert base.upper == pytest.approx(ETALON_U)
    assert base.mid == pytest.approx(ETALON_M)
    assert is_external(base, fvg)


def test_mixed_consolidation_includes_all_four():
    """§4: смешанный цвет не разрывает группу — в базе и бычьи, и медвежьи свечи."""
    candles = load_etalon_candles()
    fvg = _fvg_by_formed_at(candles, ETALON_FVG_EXTERNAL[2])
    base = find_base(candles, fvg, DetectorConfig())
    assert len(base.source_candles) == 4
    by_ot = {c.open_time: c for c in candles}
    colors = ["bull" if by_ot[ot].is_bull else "bear" for ot in base.source_candles]
    assert colors == ["bull", "bear", "bear", "bull"]


def test_internal_fvgs_do_not_confirm():
    """Два внутренних FVG лежат внутри диапазона базы и не подтверждают OB."""
    cfg = DetectorConfig()
    candles = load_etalon_candles()
    for formed_at in (ETALON_FVG_INTERNAL_1[2], ETALON_FVG_INTERNAL_2[2]):
        fvg = _fvg_by_formed_at(candles, formed_at)
        base = find_base(candles, fvg, cfg)
        assert base is not None
        assert base.source_candles == ETALON_BASE_OPEN_TIMES
        assert not is_external(base, fvg)


def test_ob_not_visible_before_confirmed(db, cfg, instrument_id):
    """При replay OB недоступен алгоритму раньше confirmed_at (§4, §13.19):
    до закрытия 3-й свечи внешнего FVG — только неподтверждённый кандидат."""
    candles = load_etalon_candles(instrument_id)
    scanner = Scanner(db, cfg)

    def etalon_ob():
        for z in db.get_zones(instrument_id, types=[ZoneType.OB]):
            if round(z.lower, 2) == ETALON_L and round(z.upper, 2) == ETALON_U:
                return z
        return None

    # свечи до закрытия 3-й свечи внешнего FVG (open 2026-06-04 00:00) —
    # OB существует только как кандидат без confirmed_at
    for c in candles:
        if c.open_time < ETALON_FVG_EXTERNAL[2]:
            scanner.on_closed_candle(c)
    ob = etalon_ob()
    assert ob is not None
    assert ob.confirmed_at is None
    assert ob.source_candles == ETALON_BASE_OPEN_TIMES

    # закрытие 3-й свечи внешнего FVG подтверждает OB ровно на границе 04:00 UTC
    third = next(c for c in candles if c.open_time == ETALON_FVG_EXTERNAL[2])
    scanner.on_closed_candle(third)
    ob = etalon_ob()
    assert ob.confirmed_at == ETALON_CONFIRMED_AT
    rel = db.get_relation(ob.id)
    assert rel is not None and rel.confirming_fvg_id is not None

    # остаток истории: ни одного события по OB раньше confirmed_at
    for c in candles:
        if c.open_time > ETALON_FVG_EXTERNAL[2]:
            scanner.on_closed_candle(c)
    events = db.get_events(ob.id)
    assert all(e.occurred_at >= ETALON_CONFIRMED_AT for e in events)


def test_ob_candidate_status_and_evidence(db, cfg, instrument_id):
    """Авто-OB — CANDIDATE (§4/§10); evidence объясняет выбор свечей и FVG."""
    candles = load_etalon_candles(instrument_id)
    scanner = Scanner(db, cfg)
    for c in candles:
        scanner.on_closed_candle(c)
    ob = next(z for z in db.get_zones(instrument_id, types=[ZoneType.OB])
              if round(z.lower, 2) == ETALON_L)
    assert ob.status.value == "candidate"
    assert ob.evidence["included_open_times"] == ETALON_BASE_OPEN_TIMES
    assert ob.evidence["external_fvg"] is True
