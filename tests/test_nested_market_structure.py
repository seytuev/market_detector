"""User-approved BOS/SMS contract, 14 October 2026."""
from dataclasses import replace
from types import SimpleNamespace
import csv
from pathlib import Path

import pytest

from app.engine.ltf.breaks import BreakCursor, detect_breaks, expected_structure_conditions, scan_instrument_structure
from app.engine.ltf.market_structure import StructureMachine, replay_structure
from app.engine.ltf.pivots import PivotCandidate
from app.models import Direction
from app.models_ltf import ScanTrace
from tests.conftest import H1_MS, make_candle

T0 = 1_780_000_000_000


def pivot(index, price, kind, confirmed=None):
    at = T0 + index * H1_MS
    return PivotCandidate(1, price, kind, at, at,
                          T0 + (index if confirmed is None else confirmed) * H1_MS + H1_MS - 1,
                          0, 0)


def candle(index, close):
    return make_candle(T0 + index * H1_MS, close, close + 1, close - 1, close, timeframe="H1")


def example():
    pivots = [pivot(0, 83_147.95, "high"), pivot(1, 81_033.66, "low"),
              pivot(2, 84_168, "high"), pivot(3, 83_122.95, "low", 4),
              pivot(5, 83_850, "high"), pivot(7, 82_972.74, "low", 8),
              pivot(9, 83_958, "high"), pivot(10, 83_136.8, "low"),
              pivot(11, 84_146.72, "high")]
    closes = [83_000, 82_000, 83_100, 83_500, 83_500, 83_500, 83_500,
              83_095.88, 83_300, 83_700, 83_169, 83_432, 82_743.75]
    return pivots, [candle(i, close) for i, close in enumerate(closes)]


def mirror(pivots, candles):
    ceiling = 100_000
    return ([replace(p, price=ceiling - p.price, kind="low" if p.kind == "high" else "high") for p in pivots],
            [replace(c, open=ceiling-c.open, close=ceiling-c.close, high=ceiling-c.low, low=ceiling-c.high) for c in candles])


@pytest.mark.parametrize("mirrored", [False, True])
def test_sms_continuation_does_not_become_bos_inside_parent(mirrored):
    pivots, bars = example()
    if mirrored:
        pivots, bars = mirror(pivots, bars)
    machine, events = replay_structure(pivots, bars, bars[-1].close_time)
    assert [(e.kind, e.stage) for e in events] == [("SMS", "primary"), ("SMS", "secondary")]
    assert [e.break_level for e in events] == pytest.approx([16_877.05, 17_027.26] if mirrored else [83_122.95, 82_972.74])
    direction = Direction.BULL if mirrored else Direction.BEAR
    assert all(e.direction == direction for e in events)
    assert events[0].occurred_at == bars[7].close_time
    assert events[1].occurred_at == bars[12].close_time
    # The child can continue within its retained parent's range.
    assert len(machine.frames) == 2
    assert machine.frames[0].extreme.price == (15_832 if mirrored else 84_168)
    assert events[-1].evidence["parent_direction"] == ("bear" if mirrored else "bull")


def test_sms_needs_no_confirmed_lower_high():
    pivots, bars = example()
    machine, events = replay_structure(pivots[:4], bars[:8], bars[7].close_time)
    assert [(e.kind, e.break_level) for e in events] == [("SMS", 83_122.95)]
    assert machine.direction == Direction.BEAR


@pytest.mark.parametrize("close,closed", [(83_122.95, True), (83_500, True), (83_095, False)])
def test_sms_is_strict_closed_and_does_not_use_wicks(close, closed):
    pivots, bars = example()
    bars = bars[:7] + [replace(bars[7], close=close, low=82_000, closed=closed)]
    assert replay_structure(pivots[:4], bars, bars[-1].close_time)[1] == []


def test_higher_local_high_and_wick_above_main_do_not_move_it():
    pivots, bars = example()
    pivots = pivots[:3] + [pivot(3, 83_500, "high"), pivot(4, 84_000, "high"), pivot(5, 84_300, "high")]
    bars = bars[:3] + [candle(i, 83_500) for i in range(3, 7)]
    machine, events = replay_structure(pivots, bars, bars[-1].close_time)
    assert not events
    assert machine.frames[0].extreme.price == 84_168
    assert machine.roles[("high", pivots[4].pivot_at)] == "LH"


def test_parent_continuation_ends_nested_correction():
    pivots, bars = example()
    bars.append(candle(13, 84_200))
    machine, events = replay_structure(pivots, bars, bars[-1].close_time)
    assert (events[-1].kind, events[-1].direction, events[-1].break_level) == ("BOS", Direction.BULL, 84_168)
    assert len(machine.frames) == 1
    assert machine.direction == Direction.BULL


def test_invalid_parent_cannot_resume_and_no_double_event():
    pivots, bars = example()
    bars = bars[:8] + [candle(8, 80_000), candle(9, 85_000)]
    machine, events = replay_structure(pivots[:4], bars, bars[-1].close_time)
    assert len(machine.frames) == 1
    assert [e.kind for e in events] == ["SMS", "BOS", "BOS"]
    assert events[1].break_level == 81_033.66


def test_no_lookahead_and_no_role_input_dependency():
    pivots, bars = example()
    cutoff = bars[6].close_time
    assert scan_instrument_structure(pivots, bars, cutoff) == []
    events = scan_instrument_structure(pivots, bars, bars[-1].close_time)
    wrong_roles = [replace(p, role="HH" if p.kind == "high" else "HL") for p in pivots]
    assert scan_instrument_structure(wrong_roles, bars, bars[-1].close_time) == events
    # A pivot confirmed later cannot backdate the event to a past break.
    late = [replace(p, confirmed_at=bars[8].close_time) if p.pivot_at == pivots[3].pivot_at else p for p in pivots[:4]]
    assert replay_structure(late, bars[:8], bars[7].close_time)[1] == []


def test_incremental_trace_since_and_reverse_cancellation_agree():
    pivots, bars = example()
    now = bars[-1].close_time
    full = detect_breaks(pivots, bars, Direction.BEAR, now, stop_on_cancellation=False)
    traced = detect_breaks(pivots, bars, Direction.BEAR, now, trace=ScanTrace(), stop_on_cancellation=False)
    assert full.events == traced.events
    cursor = BreakCursor(Direction.BEAR, stop_on_cancellation=False)
    for i, bar in enumerate(bars):
        known = sorted([p for p in pivots if p.confirmed_at <= bar.close_time], key=lambda p: (p.confirmed_at, p.pivot_at))
        incremental = cursor.scan(known, bars[:i+1], [c.open_time for c in bars[:i+1]], now)
    assert incremental.events == full.events
    assert [(e.kind, e.stage) for e in detect_breaks(pivots, bars, Direction.BEAR, now, since_ms=bars[8].open_time).events] == [("SMS", "secondary")]
    cancelled = detect_breaks(pivots, bars, Direction.BULL, now, cancel_not_before_ms=bars[6].close_time)
    assert cancelled.cancellation.pattern == "reverse_sms"
    assert cancelled.cancellation.event.break_level == 83_122.95


def test_expected_sms_is_ready_without_pullback_and_old_level_not_reissued():
    pivots, bars = example()
    condition = expected_structure_conditions(pivots[:4], bars[:5], Direction.BEAR, bars[4].close_time)["sms"]
    assert (condition["status"], condition["level"], condition["opens_scenario"]) == ("ready", 83_122.95, True)
    events = scan_instrument_structure(pivots[:4], bars[:8] + [candle(8, 83_000), candle(9, 82_900)], bars[9].close_time)
    assert len(events) == 1


def reference_bars():
    with (Path(__file__).parent / "fixtures" / "btc_h1_structure_2026_10_14.csv").open(encoding="utf-8") as source:
        return [make_candle(int(row["open_time"]), *(float(row[k]) for k in ("open", "high", "low", "close")),
                            timeframe="H1") for row in csv.DictReader(source)]


def test_user_reference_all_internal_breaks_remain_sms():
    from app.engine.ltf.pivots import find_h1_pivots
    bars = reference_bars()
    pivots = find_h1_pivots(bars, 3, 3)
    machine, events = replay_structure(pivots, bars, bars[-1].close_time)
    main = machine.main_structure()
    assert (main["direction"], main["origin"], main["extreme"]) == ("bull", 80_579.43, 87_395.67)
    main_peak_at = main["extreme_at"]
    assert all(e.kind == "SMS" for e in events if e.break_candle_open_time > main_peak_at)
    assert [(e.kind, e.stage, e.break_level) for e in events[-2:]] == [
        ("SMS", "primary", 83_122.95), ("SMS", "secondary", 82_972.74)]
    conditions = expected_structure_conditions(pivots, bars, Direction.BEAR, bars[-1].close_time)
    assert conditions["bos"]["level"] == 80_579.43
    # Neither the old local low nor another internal high changes main BOS.
    next_bar = make_candle(bars[-1].open_time + H1_MS, 82_000, 84_500, 80_900, 81_000, timeframe="H1")
    extended, next_events = replay_structure(pivots, bars + [next_bar], next_bar.close_time)
    assert extended.main_structure()["origin"] == 80_579.43
    assert not [e for e in next_events if e.kind == "BOS" and e.occurred_at == next_bar.close_time]
    break_bar = replace(next_bar, open_time=next_bar.open_time + H1_MS,
                        close_time=next_bar.close_time + H1_MS, low=80_000, close=80_500)
    _, broken = replay_structure(pivots, bars + [next_bar, break_bar], break_bar.close_time)
    assert (broken[-1].kind, broken[-1].direction, broken[-1].break_level) == ("BOS", Direction.BEAR, 80_579.43)


def test_main_continuation_reanchors_hl_only_after_new_main_hh():
    from app.engine.ltf.market_structure import Frame
    machine = StructureMachine()
    low, high = pivot(0, 80, "low"), pivot(1, 120, "high")
    machine.frames = [Frame(Direction.BULL, low, high, high.pivot_at)]
    machine.last = {"low": low, "high": high}
    correction = pivot(2, 90, "low")
    machine.absorb(correction)
    assert machine.on_candle(candle(2, 100), candle(2, 100).close_time) == []
    # An internal wick above HH does not confirm main BOS.
    assert machine.on_candle(replace(candle(3, 110), high=125), candle(3, 110).close_time) == []
    bos = machine.on_candle(candle(4, 122), candle(4, 122).close_time)
    assert bos[0].kind == "BOS" and bos[0].break_level == 120
    new_hl = pivot(5, 95, "low")
    machine.absorb(new_hl)
    machine.on_candle(candle(5, 123), candle(5, 123).close_time)
    new_hh = pivot(6, 130, "high")
    machine.absorb(new_hh)
    machine.on_candle(candle(6, 125), candle(6, 125).close_time)
    assert machine.main_structure()["origin"] == 95
    reversal = machine.on_candle(candle(7, 94), candle(7, 94).close_time)
    assert (reversal[0].kind, reversal[0].direction, reversal[0].break_level) == ("BOS", Direction.BEAR, 95)


@pytest.mark.parametrize("timeframe,multiplier", [("D1", 24), ("W1", 168)])
def test_same_rules_on_daily_and_weekly(timeframe, multiplier):
    from tests.test_d1_structure import daily_bars
    from app.services.d1_structure import timeframe_structure
    original = daily_bars()
    bars = [replace(c, timeframe=timeframe, open_time=T0 + i * multiplier * H1_MS,
                    close_time=T0 + (i+1) * multiplier * H1_MS - 1) for i, c in enumerate(original)]
    before = timeframe_structure(bars, bars[16].close_time, timeframe=timeframe)
    shifted = timeframe_structure(bars, bars[17].close_time, timeframe=timeframe)
    broken = timeframe_structure(bars, bars[18].close_time, timeframe=timeframe)
    assert before["direction"] == "bull"
    assert shifted["direction"] == "bear"
    assert shifted["last_event"]["kind"] == "SMS"
    assert broken["last_event"]["kind"] == "BOS"
    assert broken["last_event"]["level"] == 5
    assert all(condition["timeframe"] == timeframe for side in shifted["expected"].values() for condition in side.values())


def test_sms_continuation_builds_new_local_pd_without_changing_main(db, cfg, instrument_id):
    from app.services.h1_setup import H1LegCursor, local_leg_at, revision_at
    pivots, bars = example()
    for p in pivots:
        p.instrument_id = instrument_id
    bars = [replace(c, instrument_id=instrument_id) for c in bars]
    db.insert_candles(bars)
    cursor = H1LegCursor(instrument_id)
    cursor.advance(db, pivots, bars, bars[-1].close_time, cfg)
    leg = local_leg_at(db, instrument_id, bars[-1].close_time)
    assert leg["trigger_bos_key"].startswith("sms:secondary:")
    assert leg["origin_price"] == 84_146.72
    assert revision_at(db, leg["id"], bars[-1].close_time)["upper"] == 84_146.72
    first_count = db.conn.execute("select count(*) from h1_local_leg").fetchone()[0]
    cursor.advance(db, pivots, bars, bars[-1].close_time, cfg)
    assert db.conn.execute("select count(*) from h1_local_leg").fetchone()[0] == first_count


def test_upgrade_keeps_old_rows_and_rebuilds_sms_card_without_old_alerts(db, cfg, instrument_id):
    import json
    from app.engine.ltf import LtfEngine
    from app.engine.ltf.pivots import find_h1_pivots
    from app.models_ltf import LtfPivot
    from app.services.h1_setup import H1LegCursor, local_leg_at
    bars = [replace(c, instrument_id=instrument_id) for c in reference_bars()]
    db.insert_candles(bars)
    version, _ = db.get_or_create_calc_version("ltf_structure", {"left": 3, "right": 3}, "ltf-0.3", bars[-1].close_time)
    pivots = find_h1_pivots(bars, 3, 3)
    for p in pivots:
        p.pivot_id = db.insert_ltf_pivot(LtfPivot(
            id=None, instrument_id=instrument_id, price=p.price, kind=p.kind,
            pivot_at=p.pivot_at, confirmed_at=p.confirmed_at, role="none", left=3, right=3,
            candle_open_time=p.candle_open_time, calc_version_id=version))
    H1LegCursor(instrument_id).advance(db, pivots, bars, bars[-1].close_time, cfg)
    old = local_leg_at(db, instrument_id, bars[-1].close_time)
    db.conn.execute("update h1_local_leg set trigger_bos_key=? where id=?",
                    ("bos:primary:HL:999:83136.8@" + str(bars[-1].close_time), old["id"]))
    engine = LtfEngine(db, cfg)
    result = engine.process_h1_close(instrument_id, bars[-1].close_time)
    assert not result.events
    new = local_leg_at(db, instrument_id, bars[-1].close_time)
    assert new["trigger_bos_key"].startswith("sms:secondary:")
    assert new["origin_price"] == 84_146.72
    assert db.conn.execute("select state from h1_local_leg where id=?", (old["id"],)).fetchone()[0] == "superseded"
    main = json.loads(db.get_meta(f"h1:main_structure:{instrument_id}"))
    assert (main["origin"], main["extreme"]) == (80_579.43, 87_395.67)
    assert not db.pending_ltf_events()
    count = db.conn.execute("select count(*) from h1_local_leg").fetchone()[0]
    engine.process_h1_close(instrument_id, bars[-1].close_time)
    assert db.conn.execute("select count(*) from h1_local_leg").fetchone()[0] == count
