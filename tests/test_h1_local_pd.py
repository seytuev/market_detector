"""Локальный PD текущего движения H1. Арифметика и правила ноги — раздел 11 ТЗ v2.

Числа 80 400 / 83 400 / 83 600 — расчётный пример, не котировка со скрина.
Скриншотовые 82 776,01 и 109–112 тыс. проверяются как запрет подмены, а не как OHLC.
"""
from __future__ import annotations

from app.engine.ltf.breaks import StructureEventDraft
from app.engine.ltf.eligibility import ELIGIBILITY_REASONS
from app.engine.ltf.engine import LtfEngine
from app.engine.ltf.pivots import PivotCandidate
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone, LtfEvent, LtfObservation, LtfRange, LtfScenario,
)
from app.notify.formatting import fmt_price_ru
from app.services.h1_setup import (
    _persist_leg,
    apply_candle,
    causal_origin,
    entry_block_reason,
    pd_bounds,
    project_setup,
    replay_events,
    setup_card_lines,
    touch_is_retrospective,
    zone_against_leg,
)
from app.services.overview import instrument_structure
from tests.conftest import H1_MS, make_candle, make_h1_candles
from tests.test_ltf_breaks import SERIES_D_CLOSES, SERIES_D_HL, _series

T0 = 1_780_000_000_000


def _close(i: int) -> int:
    return T0 + i * H1_MS + H1_MS - 1


def _open(i: int) -> int:
    return T0 + i * H1_MS


def _pivot(kind: str, price: float, at_i: int, confirmed_i: int, role: str = "none"):
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind,
        pivot_at=_open(at_i), candle_open_time=_open(at_i),
        confirmed_at=_close(confirmed_i), left=3, right=3,
        state="confirmed", role=role,
    )


def _bos(direction: Direction, at_i: int, level: float, key: str, stage: str = "primary"):
    return StructureEventDraft(
        kind="BOS", stage=stage, direction=direction, break_level=level,
        break_candle_open_time=_open(at_i), occurred_at=_close(at_i),
        detected_at=_close(at_i), level_key=key,
        evidence={"broken_pivot_id": at_i, "role_at_event": "LH" if direction == Direction.BULL else "HL"},
    )


def _bars():
    """Нога вверх: основание 80 400, high 83 400, затем 83 600. Промежуточные high ниже."""
    raw = [
        (80600, 80700, 80400, 80650),  # 0 основание
        (80650, 81000, 80600, 80900),  # 1
        (80900, 81600, 81100, 81500),  # 2 FVG от свечи 0
        (81500, 82200, 81400, 82100),  # 3
        (82100, 82800, 82000, 82700),  # 4 FVG, пересекает будущие 50%
        (82700, 83400, 82600, 83300),  # 5 BOS, наблюдаемый high
        (83300, 83350, 82800, 82900),  # 6 локальный HL, high не обновляет
        (82900, 83200, 82700, 83000),  # 7
        (83000, 83100, 82600, 82800),  # 8 подтверждение pivot high без нового экстремума
        (82800, 83600, 82500, 83500),  # 9 новый фактический high
    ]
    return make_h1_candles(raw, T0, 1)


def _pivots():
    return [
        _pivot("low", 80400, 0, 3, "LL"),
        _pivot("high", 83400, 5, 8, "HH"),
        _pivot("low", 82800, 6, 9, "HL"),
    ]


def _bull(at_i: int = 5):
    return _bos(Direction.BULL, at_i, 82000, f"bos:primary:bull:{at_i}")


def _drive(db, instrument_id, candles, pivots, events):
    grouped: dict[int, list] = {}
    for event in events:
        grouped.setdefault(int(event.occurred_at), []).append(event)
    state = {"current": None, "epoch": 0, "quality": "ok"}
    seen = []
    for candle in candles:
        if not candle.closed:
            continue
        seen.append(candle)
        known = [
            p for p in pivots
            if p.state == "confirmed" and int(p.confirmed_at) <= int(candle.close_time)
        ]
        for leg in apply_candle(state, candle, grouped.get(int(candle.close_time), []), known, seen):
            _persist_leg(db, instrument_id, leg)
    db._commit()
    return state


def _leg_rows(db, instrument_id):
    return [
        dict(row) for row in db.conn.execute(
            "SELECT * FROM h1_local_leg WHERE instrument_id=? ORDER BY bos_at, id",
            (instrument_id,),
        ).fetchall()
    ]


def _rev_rows(db, leg_id):
    return [
        dict(row) for row in db.conn.execute(
            "SELECT * FROM h1_local_leg_revision WHERE leg_id=? ORDER BY revision",
            (leg_id,),
        ).fetchall()
    ]


def test_arithmetic_bounds_and_unreached_extreme():
    assert pd_bounds("bull", 80400, 83400) == (80400, 83400, 81900)
    assert pd_bounds("bull", 80400, 83600) == (80400, 83600, 82000)
    assert pd_bounds("bear", 83400, 80400) == (80400, 83400, 81900)
    assert pd_bounds("bull", 83400, 80400) is None

    candles = _bars()
    pivots = _pivots()
    event = _bull()
    early = replay_events([event], pivots, candles[:6], _close(5))
    leg = early["current"]
    assert leg["origin_price"] == 80400
    assert leg["endpoint_price"] == 83400
    assert leg["eq"] == 81900
    assert leg["range_status"] == "provisional"
    assert leg["endpoint_status"] == "provisional"
    assert all(c.high != 83600 for c in candles[:6])

    mid = replay_events([event], pivots, candles[:9], _close(8))
    confirmed = mid["current"]
    assert confirmed["endpoint_price"] == 83400
    assert confirmed["eq"] == 81900
    assert confirmed["range_status"] == "confirmed"
    assert confirmed["endpoint_status"] == "confirmed"
    assert len(confirmed["facts"]) == 1
    assert confirmed["origin_price"] == 80400

    late = replay_events([event], pivots, candles, _close(9))
    extended = late["current"]
    assert extended["endpoint_price"] == 83600
    assert extended["eq"] == 82000
    assert extended["range_status"] == "provisional"
    assert extended["origin_price"] == 80400
    assert extended["trigger_bos_key"] == event.level_key + f"@{event.occurred_at}"
    assert len(extended["facts"]) == 1


def test_stale_extreme_is_not_the_observed_high():
    """Наблюдаемый high выше старой отметки 82 776,01 берётся из свечи, не из картинки."""
    raw = [
        (81000, 81200, 80393.56, 81100),
        (81100, 82000, 81000, 81900),
        (81900, 82776.01, 81800, 82600),
        (82600, 83418.0, 82500, 83300),
    ]
    candles = make_h1_candles(raw, T0, 1)
    origin = _pivot("low", 80393.56, 0, 3)
    event = _bull(3)
    state = replay_events([event], [origin], candles, _close(3))
    leg = state["current"]
    assert leg["origin_price"] == 80393.56
    assert leg["endpoint_price"] == 83418.0
    assert leg["endpoint_price"] != 82776.01
    assert 83600 not in {c.high for c in candles}
    low, high, eq = pd_bounds("bull", leg["origin_price"], leg["endpoint_price"])
    assert high == 83418.0
    assert eq == (80393.56 + 83418.0) / 2


def test_open_candle_does_not_move_pd():
    candles = _bars()
    live = make_candle(
        _open(10), 83500, 90000, 83000, 89000,
        timeframe="H1", instrument_id=1, closed=False,
    )
    state = replay_events([_bull()], _pivots(), candles + [live], _close(9))
    assert state["current"]["endpoint_price"] == 83600
    assert state["current"]["eq"] == 82000


def test_local_hl_does_not_move_origin_and_impulse_breaks_stay_one_leg():
    candles = _bars()
    pivots = _pivots()
    first = _bull(5)
    second = _bos(Direction.BULL, 7, 83000, "bos:primary:bull:7")
    state = replay_events([first, second], pivots, candles, _close(9))
    leg = state["current"]
    assert leg["origin_price"] == 80400
    assert leg["origin_at"] == _open(0)
    assert len(leg["facts"]) == 2
    assert leg["state"] != "superseded"
    assert leg["eq"] == 82000


def test_correction_opens_new_leg_and_opposite_bos_switches():
    candles = _bars() + make_h1_candles(
        [(83500, 83700, 82900, 83000), (83000, 83200, 82000, 82100), (82100, 82200, 80000, 80100)],
        _open(10), 1,
    )
    pivots = _pivots() + [_pivot("low", 82000, 11, 12, "HL")]
    first = _bull(5)
    continuation = _bos(Direction.BULL, 12, 83000, "bos:primary:bull:12")
    state = replay_events([first, continuation], pivots, candles, _close(12))
    assert state["current"]["origin_price"] == 82000
    assert state["current"]["origin_at"] == _open(11)
    assert state["current"]["direction"] == "bull"

    bear = _bos(Direction.BEAR, 9, 83000, "bos:primary:bear:9")
    switched = replay_events([first, bear], _pivots(), _bars(), _close(9))
    current = switched["current"]
    assert current["direction"] == "bear"
    assert current["origin_price"] == 83400
    assert current["state"] != "superseded"
    assert current["lower"] != 109_000


def test_unproven_origin_does_not_reuse_old_pd():
    candles = _bars()
    bull = _bull()
    state = replay_events([bull], _pivots(), candles, _close(5))
    old_eq = state["current"]["eq"]
    assert old_eq == 81900
    bear = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BEAR, break_level=80000,
        break_candle_open_time=T0 - H1_MS, occurred_at=_close(6),
        detected_at=_close(6), level_key="bos:primary:bear:unproven",
        evidence={},
    )
    # без подтверждённого high и без свечи до break_open происхождение пустое
    origin = causal_origin(Direction.BEAR, bear, [p for p in _pivots() if p.kind == "low"], candles)
    assert origin is None
    switched = replay_events([bull, bear], [p for p in _pivots() if p.kind == "low"], candles, _close(6))
    current = switched["current"]
    assert current["reason"] == "origin_unresolved"
    assert current["range_status"] == "range_pending"
    assert current["lower"] is None
    assert current["eq"] is None
    assert current["eq"] != old_eq


def test_zones_of_this_leg_and_old_geometry(db, cfg, instrument_id):
    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    db.insert_candles(candles)
    event = _bull()
    _drive(db, instrument_id, candles, _pivots(), [event])
    snap = project_setup(db, cfg, instrument_id, _close(5), mode="event", candles=candles, pivots=_pivots())
    assert snap["lower"] == 80400 and snap["upper"] == 83400 and snap["eq"] == 81900
    assert snap["range_status"] == "provisional"
    assert snap["history"] is True
    assert snap["setup_status"] == "analytics"
    assert "context_missing" in snap["exclusion_reasons"]
    assert snap["eligible_regions"] == []
    base = next(z for z in snap["candidates"] if z["lower"] == 80700 and z["upper"] == 81100)
    assert base["status"] == "confirmed"
    assert base["reason"] == "eligible_provisional"
    crossed = next(z for z in snap["candidates"] if z["lower"] == 81600 and z["upper"] == 82000)
    assert crossed["admitted"] == [81600, 81900]
    assert crossed["reason"] == "eligible_provisional"
    above = [z for z in snap["candidates"] if z.get("lower") is not None and z["lower"] > 81900]
    assert above and all(z["reason"] == "outside_pd" for z in above)
    assert all(z["status"] != "forming" or z not in snap["eligible_regions"] for z in snap["candidates"])

    forming = zone_against_leg(candles[2].open_time, False, 80700, 81100, snap)
    assert forming == "forming"
    foreign = zone_against_leg(_open(0) - 100 * H1_MS, True, 81000, 81200, snap)
    assert foreign == "other_movement"

    obs = _observation(db, instrument_id, _close(5))
    db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    traded = project_setup(db, cfg, instrument_id, _close(5), mode="current", candles=candles, pivots=_pivots())
    assert traded["setup_status"] == "trade"
    assert any(z["lower"] == 80700 for z in traded["eligible_regions"])
    assert "context_missing" not in traded["exclusion_reasons"]
    lines = setup_card_lines(traded)
    assert any("80 400,00" in line and "83 400,00" in line for line in lines)
    assert any("81 900,00" in line and "Предварительный PD" in line for line in lines)
    assert all("109 000" not in line for line in lines)


def test_old_leg_zone_inside_new_discount_is_not_current(db, cfg, instrument_id):
    raw = [
        (80500, 80600, 80450, 80550),
        (80550, 80800, 80500, 80750),
        (80750, 80900, 80700, 80850),  # старый FVG 80 600–80 700
    ]
    for _ in range(3, 30):
        raw.append((80800, 80900, 80750, 80850))
    raw.append((81000, 81200, 80400, 81100))  # 30 основание новой ноги
    raw.append((81100, 82000, 81000, 81900))
    raw.append((81900, 85000, 82100, 84900))  # 32 BOS
    candles = make_h1_candles(raw, T0, instrument_id)
    origin = _pivot("low", 80400, 30, 32)
    event = _bos(Direction.BULL, 32, 83000, "bos:primary:new")
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, [origin], [event])
    snap = project_setup(
        db, cfg, instrument_id, _close(32), mode="current", candles=candles, pivots=[origin],
    )
    assert snap["origin_anchor"]["price"] == 80400
    formed = {z.get("formed_at") for z in snap["candidates"]}
    assert candles[2].open_time not in formed
    assert candles[32].open_time in formed
    assert snap["lower"] == 80400
    assert snap["eq"] == 82700


def test_retrospective_touch_uses_prior_admission():
    zone_low, zone_high = 81200, 81600
    revisions = [
        {"lower": 80400, "upper": 81000, "eq": 80700, "as_of": _close(2),
         "revision": 1, "range_status": "provisional"},
        {"lower": 80400, "upper": 83400, "eq": 81900, "as_of": _close(3),
         "revision": 2, "range_status": "provisional"},
    ]
    assert touch_is_retrospective(revisions, "bull", zone_low, zone_high, _close(3)) is True
    assert touch_is_retrospective(revisions, "bull", zone_low, zone_high, _close(4)) is False


def test_live_structure_is_current_and_explicit_as_of_is_history(db, cfg, instrument_id):
    """Стол не передаёт as_of. Заполненный now не помечает развивающуюся ногу историей."""
    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, _pivots(), [_bull()])

    class _Settings:
        detector = cfg

    live = instrument_structure(db, _Settings(), instrument_id)
    assert live["setup"]["mode"] == "current"
    assert live["setup"]["history"] is False
    assert live["setup"]["state"] == "developing"
    assert live["setup"]["lower"] == 80400
    assert live["setup"]["upper"] == 83600
    assert live["setup"]["eq"] == 82000
    assert live["setup"]["label"] == "PD H1 текущего движения"
    live_text = "\n".join(setup_card_lines(live["setup"]))
    assert "Исторический снимок" not in live_text
    assert "82 000,00" in live_text

    past = instrument_structure(db, _Settings(), instrument_id, as_of=_close(5))
    assert past["setup"]["mode"] == "event"
    assert past["setup"]["history"] is True
    assert past["setup"]["eq"] == 81900
    assert past["setup"]["range_status"] == "provisional"
    assert past["setup"]["movement_id"] == live["setup"]["movement_id"]
    assert any("Исторический снимок" in line for line in setup_card_lines(past["setup"]))


def test_history_keeps_as_of_and_replay_does_not_duplicate(db, cfg, instrument_id):
    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    db.insert_candles(candles)
    event = _bull()
    _drive(db, instrument_id, candles, _pivots(), [event])
    _drive(db, instrument_id, candles, _pivots(), [event])
    legs = _leg_rows(db, instrument_id)
    assert len(legs) == 1
    revs = _rev_rows(db, legs[0]["id"])
    assert [r["endpoint_price"] for r in revs] == [83400, 83400, 83600]
    assert [r["range_status"] for r in revs] == ["provisional", "confirmed", "provisional"]
    assert [r["eq"] for r in revs] == [81900, 81900, 82000]
    past = project_setup(db, cfg, instrument_id, _close(5), mode="event", candles=candles, pivots=_pivots())
    live = project_setup(db, cfg, instrument_id, _close(9), mode="current", candles=candles, pivots=_pivots())
    assert past["eq"] == 81900 and past["history"] is True
    assert past["endpoint"]["status"] == "provisional"
    assert live["eq"] == 82000 and live["history"] is False
    assert live["movement_id"] == past["movement_id"]
    assert "PD H1 текущего движения" == live["label"]
    assert live["live_preview"] is None
    assert db.conn.execute("SELECT COUNT(*) AS n FROM ltf_event").fetchone()["n"] == 0
    assert ELIGIBILITY_REASONS[-2:] == ("range_pending", "eligible_provisional")
    assert "context_missing" not in ELIGIBILITY_REASONS
    assert "other_movement" not in ELIGIBILITY_REASONS


def test_parents_share_one_pd_and_old_ranges_do_not_alert(db, cfg, instrument_id):
    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, _pivots(), [_bull()])
    obs_a = _observation(db, instrument_id, _close(1), cycle=1)
    obs_b = _observation(db, instrument_id, _close(1), cycle=2)
    sc_a = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs_a.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="range_pending",
    ))
    sc_b = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs_b.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc_a.id, version=1, lower=109_000, upper=112_000,
        mid=110_500, available_at=_close(1),
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc_b.id, version=1, lower=88_000, upper=91_000,
        mid=89_500, available_at=_close(1),
    ))
    left = project_setup(db, cfg, instrument_id, _close(9), mode="current")
    right = project_setup(db, cfg, instrument_id, _close(9), mode="current")
    assert left["movement_id"] == right["movement_id"]
    assert left["eq"] == right["eq"] == 82000
    assert left["lower"] == 80400 and left["upper"] == 83600
    assert left["snapshot_id"] == right["snapshot_id"]
    assert "109" not in str(left["lower"]) 
    text = "\n".join(setup_card_lines(left))
    assert "82 000,00" in text
    assert "109 000,00" not in text and "88 000,00" not in text
    assert db.conn.execute("SELECT COUNT(*) AS n FROM ltf_event").fetchone()["n"] == 0

    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=81000, upper=81200, formed_at=_open(2), confirmed_at=_close(2),
    ))
    touch = LtfEvent(
        id=None, observation_id=obs_a.id, kind="touch", occurred_at=_close(9),
        detected_at=_close(9), dedupe_key="touch:test", scenario_id=sc_a.id,
        payload={"entry_zone_id": zone.id},
    )
    stored, _ = db.insert_ltf_event(touch)
    assert entry_block_reason(db, stored, _close(9)) is None
    bos_event = LtfEvent(
        id=None, observation_id=obs_a.id, kind="bos", occurred_at=_close(5),
        detected_at=_close(5), dedupe_key="bos:test", payload={},
    )
    assert entry_block_reason(db, bos_event, _close(9)) is None
    ready = LtfEvent(
        id=None, observation_id=obs_a.id, kind="range_ready", occurred_at=_close(8),
        detected_at=_close(8), dedupe_key="range:test", payload={},
    )
    assert entry_block_reason(db, ready, _close(9)) is None


def test_opposite_bos_suppresses_old_entry_and_pending_origin(db, cfg, instrument_id):
    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    bear = _bos(Direction.BEAR, 9, 83000, "bos:primary:bear:9")
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, _pivots(), [_bull(), bear])
    legs = _leg_rows(db, instrument_id)
    assert len(legs) == 2
    assert legs[0]["state"] == "superseded"
    assert legs[1]["direction"] == "bear"
    assert legs[1]["origin_price"] == 83400
    assert _rev_rows(db, legs[1]["id"])[-1]["lower"] is not None
    snap = project_setup(db, cfg, instrument_id, _close(9), mode="current", candles=candles, pivots=_pivots())
    assert snap["direction"] == "bear"
    assert snap["movement_id"] == legs[1]["id"]
    assert snap["lower"] != 109_000

    obs = _observation(db, instrument_id, _close(1))
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=81000, upper=81200, formed_at=_open(2), confirmed_at=_close(2),
    ))
    old_touch = LtfEvent(
        id=None, observation_id=obs.id, kind="touch", occurred_at=_close(5),
        detected_at=_close(5), dedupe_key="touch:old-leg",
        payload={"entry_zone_id": zone.id},
    )
    stored, _ = db.insert_ltf_event(old_touch)
    assert entry_block_reason(db, stored, _close(9)) == "superseded"

    foreign = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB", direction=Direction.BEAR,
        lower=110_000, upper=111_000, formed_at=_open(0) - 80 * H1_MS,
        confirmed_at=_close(0),
    ))
    foreign_touch = LtfEvent(
        id=None, observation_id=obs.id, kind="entries_ready", occurred_at=_close(9),
        detected_at=_close(9), dedupe_key="entries:foreign",
        payload={"entry_zone_id": foreign.id},
    )
    stored_foreign, _ = db.insert_ltf_event(foreign_touch)
    assert entry_block_reason(db, stored_foreign, _close(9)) == "other_movement"


def test_first_admission_close_is_not_a_touch(db, cfg, instrument_id):
    raw = [
        (80500, 80600, 80400, 80550),
        (80550, 80800, 80500, 80700),
        (80700, 81000, 80650, 80950),  # BOS, H=81000, EQ=80700, зона ещё вне discount
        (80950, 83400, 82000, 83300),  # допуск впервые на этом закрытии
    ]
    candles = make_h1_candles(raw, T0, instrument_id)
    origin = _pivot("low", 80400, 0, 2)
    event = _bull(2)
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, [origin], [event])
    legs = _leg_rows(db, instrument_id)
    revs = _rev_rows(db, legs[0]["id"])
    assert touch_is_retrospective(revs, "bull", 81200, 81600, _close(3)) is True
    obs = _observation(db, instrument_id, _close(1))
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=81200, upper=81600, formed_at=_open(1), confirmed_at=_close(1),
    ))
    touch = LtfEvent(
        id=None, observation_id=obs.id, kind="touch", occurred_at=_close(3),
        detected_at=_close(3), dedupe_key="touch:retro",
        payload={"entry_zone_id": zone.id},
    )
    stored, _ = db.insert_ltf_event(touch)
    assert entry_block_reason(db, stored, _close(3)) == "zone_became_available"
    later = LtfEvent(
        id=None, observation_id=obs.id, kind="touch", occurred_at=_close(3) + H1_MS,
        detected_at=_close(3) + H1_MS, dedupe_key="touch:later",
        payload={"entry_zone_id": zone.id},
    )
    assert entry_block_reason(db, later, _close(3) + H1_MS) is None


def test_engine_replay_is_stable_and_silent(db, cfg, instrument_id):
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    db.insert_candles(candles)
    now = candles[-1].close_time + 1
    LtfEngine(db, cfg).process_h1_close(instrument_id, now)
    legs = db.conn.execute(
        "SELECT COUNT(*) AS n FROM h1_local_leg WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()["n"]
    revs = db.conn.execute("SELECT COUNT(*) AS n FROM h1_local_leg_revision").fetchone()["n"]
    events = db.conn.execute("SELECT COUNT(*) AS n FROM ltf_event").fetchone()["n"]
    assert legs >= 1 and revs >= 1 and events == 0
    current = db.conn.execute(
        """SELECT direction, state FROM h1_local_leg
           WHERE instrument_id=? AND state!='superseded'""",
        (instrument_id,),
    ).fetchall()
    assert len(current) == 1 and current[0]["direction"] == "bull"
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", "0")
    LtfEngine(db, cfg).process_h1_close(instrument_id, now)
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM h1_local_leg WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()["n"] == legs
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM h1_local_leg_revision",
    ).fetchone()["n"] == revs
    assert db.conn.execute("SELECT COUNT(*) AS n FROM ltf_event").fetchone()["n"] == 0


def test_old_ranges_do_not_stretch_png(db, cfg, instrument_id, tmp_path, monkeypatch):
    import matplotlib.pyplot as plt
    from app.bot.charts import render_ltf_chart
    from app.config import Settings

    candles = _bars()
    for candle in candles:
        candle.instrument_id = instrument_id
    db.insert_candles(candles)
    _drive(db, instrument_id, candles, _pivots(), [_bull()])
    obs = _observation(db, instrument_id, _close(1), lower=9000, upper=9100)
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="cancelled",
        cancellation_reason="reverse_bos", cancelled_at=_close(2),
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=109_000, upper=112_000,
        mid=110_500, available_at=_close(1),
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=2, lower=88_000, upper=91_000,
        mid=89_500, available_at=_close(2),
    ))
    settings = Settings()
    settings.db_path = str(tmp_path / "unused.db")
    figs = []
    monkeypatch.setattr(plt, "close", lambda fig=None: figs.append(fig))
    path = render_ltf_chart(
        db, obs.id, tmp_path / "local.png", tf="H1", period_days=3,
        settings=settings, now=_close(9) + 1,
    )
    assert path
    ax = figs[0].axes[0]
    y0, y1 = ax.get_ylim()
    assert 70_000 < y0 < y1 < 100_000
    note = " ".join(t.get_text() for t in figs[0].texts)
    assert "вне окна" in note and "9 000,00" in note
    assert "109 000,00" not in note and "88 000,00" not in note
    assert "PD H1 текущего движения" in note
    assert "предварительный PD" in note or "PD подтверждён" in note
    assert fmt_price_ru(80400) == "80 400,00"


def _observation(db, instrument_id, when, *, cycle=1, lower=80_000, upper=81_000):
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=lower, upper=upper,
        formed_at=cycle, confirmed_at=2, status=ZoneStatus.ACTIVE,
    ))
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=cycle, direction=Direction.BULL, state="active", activated_at=when,
    ))
