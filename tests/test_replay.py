"""§11/§13.12: replay — идемпотентность, delayed-метки, ALREADY_IN_ZONE (§8)."""
from __future__ import annotations

import pytest

from app.engine.replay import replay_from_db
from app.engine.scanner import Scanner
from app.models import EventKind, TIMEFRAME_MINUTES, ZoneType

from .conftest import load_etalon_candles, make_candle

H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
T0 = 1780272000000


def _counts(db, instrument_id):
    zones = db.get_zones(instrument_id)
    events = db.get_events()
    return len(zones), len(events)


def test_replay_idempotent(db, cfg, instrument_id):
    """Повторный replay не создаёт дублей зон и событий (§13.12 частично)."""
    db.insert_candles(load_etalon_candles(instrument_id))
    stats1 = replay_from_db(db, cfg, instrument_id)
    assert stats1["candles"] == 30
    assert stats1["zones"] > 0
    n1 = _counts(db, instrument_id)

    stats2 = replay_from_db(db, cfg, instrument_id)
    n2 = _counts(db, instrument_id)
    assert n2 == n1
    assert stats2["events_created"] == 0  # второй прогон — ни одного нового события


def test_replay_events_delayed(db, cfg, instrument_id):
    """§11: восстановленные события имеют исходное occurred_at и delayed=True."""
    db.insert_candles(load_etalon_candles(instrument_id))
    scanner = Scanner(db, cfg)
    events = scanner.replay_instrument(instrument_id)
    assert events, "ожидались исторические события"
    assert all(e.delayed for e in events)
    assert all(e.detected_at > e.occurred_at for e in events)


def _fvg_return_candles(instrument_id):
    """Бычий FVG и возврат цены в него последней свечой."""
    return [
        make_candle(T0 + 0 * H4_MS, 100.0, 101.0, 99.0, 100.5, "H4", instrument_id),
        make_candle(T0 + 1 * H4_MS, 100.5, 103.0, 100.0, 102.5, "H4", instrument_id),
        make_candle(T0 + 2 * H4_MS, 102.5, 104.0, 101.5, 103.5, "H4", instrument_id),
        make_candle(T0 + 3 * H4_MS, 103.5, 103.8, 101.2, 101.3, "H4", instrument_id),
    ]


def test_replay_touch_and_already_in_zone(db, cfg, instrument_id):
    """Историческое касание учитывается (§1); цена внутри зоны после replay →
    ALREADY_IN_ZONE (§8); повторный replay не дублирует."""
    candles = _fvg_return_candles(instrument_id)
    db.insert_candles(candles)
    scanner = Scanner(db, cfg)
    events = scanner.replay_instrument(instrument_id)

    fvg = db.get_zones(instrument_id, types=[ZoneType.FVG])[0]
    assert (fvg.lower, fvg.upper) == (101.0, 101.5)
    # диапазон последней свечи [101.2, 103.8]: глубина (101.5−101.2)/0.5 = 0.6 →
    # одно событие FVG_WEAKENED (высший достигнутый порог), порядок в свече неизвестен
    fvg_events = [e for e in events if e.zone_id == fvg.id]
    kinds = [e.kind for e in fvg_events]
    assert EventKind.FVG_WEAKENED in kinds
    assert EventKind.TOUCH not in kinds  # §13.7: один сигнал с фактической глубиной
    weakened = next(e for e in fvg_events if e.kind == EventKind.FVG_WEAKENED)
    assert weakened.evidence["intra_candle_order_unknown"] is True
    assert fvg.status.value == "weakened"

    # последняя цена (close 101.3) внутри FVG → ALREADY_IN_ZONE
    assert EventKind.ALREADY_IN_ZONE in kinds

    # идемпотентность
    events2 = Scanner(db, cfg).replay_instrument(instrument_id)
    assert events2 == []
    # визит остался открытым (цена внутри) и не задвоился
    visit = db.open_visit_for(fvg.id, 1)
    assert visit is not None and visit.observed is False
    assert visit.max_depth == pytest.approx(0.6)
