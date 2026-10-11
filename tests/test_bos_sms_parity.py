"""Этап 5: равноправие BOS/SMS (правила v1 §6.1).

R11: SMS от HTF — полноценный сценарий и PD от собственных опор, ждать BOS
не нужно. Детерминированный выбор триггера при BOS+SMS на одной свече (по
доступности уровня, без приоритета типа). Локальная нога H1 открывается
и от SMS.
"""
from __future__ import annotations

from app.engine.ltf.breaks import StructureEventDraft
from app.engine.ltf.pivots import PivotCandidate
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.services.h1_setup import apply_candle
from app.services.htf_context import (
    _primary_break,
    context_state_v2,
    record_interaction,
)
from tests.conftest import H1_MS, make_h1_candles

T = 1_780_012_800_000
H = 3_600_000


def _zone(db, iid, direction=Direction.BULL, serial=0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def _pivot(pid, kind, price, pivot_at, confirmed_at):
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=pivot_at,
        candle_open_time=pivot_at, confirmed_at=confirmed_at, left=3, right=3,
        state="confirmed", pivot_id=pid,
    )


def _draft(kind, direction, level, open_t, close_t, pivot_confirmed_at, key):
    return StructureEventDraft(
        kind=kind, stage="primary", direction=direction, break_level=level,
        break_candle_open_time=open_t, occurred_at=close_t,
        detected_at=close_t, level_key=key,
        evidence={"pivot_confirmed_at": pivot_confirmed_at},
    )


def test_primary_break_sms_equals_bos():
    # SMS и BOS на одной свече: выбор по доступности уровня, не по типу
    candle = make_h1_candles([(10.0, 11.0, 9.0, 10.5)], T)[0]
    sms = _draft("SMS", Direction.BULL, 10.5, candle.open_time,
                 candle.close_time, T - 10 * H, "sms-early")
    bos = _draft("BOS", Direction.BULL, 10.6, candle.open_time,
                 candle.close_time, T - 2 * H, "bos-late")

    class _Scan:
        events = [bos, sms]

    import app.services.htf_context as hc
    original = hc.detect_breaks
    hc.detect_breaks = lambda *a, **kw: _Scan()
    try:
        chosen = hc._primary_break([], [], Direction.BULL, candle, 0, 0)
    finally:
        hc.detect_breaks = original
    # уровень SMS подтверждён раньше — он и якорь, приоритета BOS нет
    assert chosen is sms


def test_primary_break_returns_none_without_events():
    candle = make_h1_candles([(10.0, 11.0, 9.0, 10.5)], T)[0]

    class _Scan:
        events = []

    import app.services.htf_context as hc
    original = hc.detect_breaks
    hc.detect_breaks = lambda *a, **kw: _Scan()
    try:
        assert hc._primary_break([], [], Direction.BULL, candle, 0, 0) is None
    finally:
        hc.detect_breaks = original


def test_sms_confirms_episode_without_bos(db, cfg, instrument_id):
    # R11: причинный SMS подтверждает эпизод наравне (тип сохраняется)
    zone = _zone(db, instrument_id, Direction.BULL)
    record_interaction(db, cfg, instrument_id, zone, T)
    from app.services.htf_context import plan_context_bar
    candle = make_h1_candles([(10.0, 11.0, 9.0, 10.5)], T + H)[0]
    sms = _draft("SMS", Direction.BULL, 10.5, candle.open_time,
                 candle.close_time, T - H, "sms-1")

    class _Scan:
        events = [sms]

    class _Empty:
        events = []

    import app.services.htf_context as hc
    original = hc.detect_breaks
    hc.detect_breaks = lambda avail, candles, direction, now, **kw: (
        _Scan() if direction == Direction.BULL else _Empty()
    )
    pivot = _pivot(1, "low", 9.0, T, T + H)
    try:
        plan = plan_context_bar(db, cfg, instrument_id, candle, [pivot],
                                [candle], candle.close_time)
    finally:
        hc.detect_breaks = original
    assert plan is not None and plan["confirm"] is True
    assert plan["event"].kind == "SMS"


def test_h1_leg_opens_from_sms():
    # локальная нога H1 открывается и от SMS (те же опоры, на равных)
    candle = make_h1_candles([(10.0, 11.0, 9.0, 10.5)], T)[0]
    sms = _draft("SMS", Direction.BULL, 10.5, candle.open_time,
                 candle.close_time, T - H, "sms-leg")
    pivot = _pivot(1, "low", 9.0, T, candle.close_time)
    state = {"epoch": 0, "current": None, "quality": "ok"}
    dirty = apply_candle(state, candle, [sms], [pivot], [candle])
    assert dirty
    leg = state["current"]
    assert leg is not None and leg["direction"] == "bull"
    assert leg["bos_at"] == candle.close_time


def test_h1_leg_bos_and_sms_same_bar_no_type_priority():
    candle = make_h1_candles([(10.0, 11.0, 9.0, 10.5)], T)[0]
    sms = _draft("SMS", Direction.BULL, 10.5, candle.open_time,
                 candle.close_time, T - 10 * H, "sms-early")
    bos = _draft("BOS", Direction.BULL, 10.6, candle.open_time,
                 candle.close_time, T - 2 * H, "bos-late")
    pivot = _pivot(1, "low", 9.0, T, candle.close_time)
    state = {"epoch": 0, "current": None, "quality": "ok"}
    apply_candle(state, candle, [bos, sms], [pivot], [candle])
    leg = state["current"]
    # одна нога направления на бар; факты обоих событий сохранены
    assert leg["direction"] == "bull"
    keys = {f["key"] for f in leg["facts"]}
    assert "sms-early" in keys
