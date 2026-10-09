"""Приёмка HTF-контекста, разворота H1, полноты зон и одной карточки слома.

22 случая из docs/GROK_SPEC_HTF_CONTEXT_H1_REVERSAL_2026-10-09.md §9.
83600 не константа: PD считается от фактически наблюдаемого high.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.bot.charts import bot_reversal_view
from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.eligibility import evaluate_entry
from app.engine.ltf.engine import LtfTickResult
from app.engine.ltf.entries import build_movement, detect_entry_zones
from app.engine.ltf.breaks import StructureEventDraft
from app.engine.ltf.pivots import PivotCandidate
from app.models import Direction, Instrument, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone, LtfEvent, LtfObservation, LtfScenario, LtfScenarioEntry,
)
from app.notify.ltf_queue import LtfDispatcher
from app.notify.ltf_templates import LtfContext
from app.notify.outbox import Card, Outbox
from app.notify.telegram import LogSender
from app.services.h1_chart import _admission, assemble_h1_layers
from app.services.htf_context import (
    close_episode,
    discount_relation,
    entry_segment,
    explain_missing_zone,
    leg_at,
    load_episode,
    note_source_outcome,
    notification_key,
    observed_extreme,
    open_episode,
    plan_context_bar,
    record_interaction,
    reversal_projection,
    save_leg,
    transition_card_text,
    transition_key,
)
from tests.conftest import H1_MS, make_candle, make_h1_candles
from tests.test_ltf_breaks import SERIES_D_CLOSES, SERIES_D_HL, _series
from tests.test_ltf_engine import SERIES_H_CLOSES, SERIES_H_HL

T0 = 1_780_000_000_000


def _feed(db, engine, instrument_id, candles, upto=None):
    seq = candles if upto is None else candles[: upto + 1]
    for candle in seq:
        db.insert_candles([candle])
        engine.process_h1_close(instrument_id, now_ms=candle.close_time)


def _zone(db, instrument_id, *, kind=ZoneType.OB, direction=Direction.BEAR,
          timeframe="D1", lower=9.0, upper=10.0, formed_at=T0, source="auto",
          evidence=None, status=ZoneStatus.ACTIVE):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=kind, direction=direction,
        timeframe=timeframe, lower=lower, upper=upper, formed_at=formed_at,
        confirmed_at=formed_at, status=status, source=source,
        evidence=evidence or {},
    ))
    return db.get_zone(zid)


def _events(db, instrument_id):
    return db.list_ltf_events_for_instrument(instrument_id, limit=5000)


def _packets(db):
    return db.conn.execute(
        "SELECT id, destination, semantic_key, status FROM notification_packet ORDER BY id"
    ).fetchall()


class Boom(LogSender):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    async def send_card(self, *args, **kwargs):
        raise self.exc


class TimeoutAfterAccept(LogSender):
    async def send_card(self, *args, **kwargs):
        await super().send_card(*args, **kwargs)
        raise TimeoutError("accepted but response lost")


async def test_01_one_bos_ten_shorts_one_card(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    for i in range(10):
        zone = _zone(
            db, instrument_id, lower=9.0 + i, upper=10.0 + i,
            formed_at=T0 - 10_000_000 - i,
        )
        engine.on_htf_zone_touched(instrument_id, zone, occurred_at=T0)
    _feed(db, engine, instrument_id, candles)
    cancels = [e for e in _events(db, instrument_id) if e.kind == "cancellation"]
    assert len(cancels) == 10
    keys = {e.payload.get("market_transition_key") for e in cancels}
    assert len(keys) == 1 and None not in keys
    anchors = {e.payload.get("market_break_key") for e in cancels}
    assert all(k and k.startswith(next(iter(keys)) + "|pivot:") for k in anchors)
    sender = LogSender()
    disp = LtfDispatcher(db, cfg, sender)
    assert await disp.deliver(cancels) == 10
    assert len(sender.cards) == 1
    assert len(_packets(db)) == 1
    assert "один рыночный слом" in sender.cards[0][0].text or "Отменено сценариев: 10" in sender.cards[0][0].text


async def test_02_replay_poll_and_concurrent_delivery_do_not_duplicate(db, cfg, instrument_id, tmp_path):
    path = str(tmp_path / "box.sqlite")
    stored = Database(path)
    iid = stored.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    zone = _zone(stored, iid)
    obs = stored.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zone.id, zone_version=1, cycle_id=1,
        direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = stored.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    key = transition_key(stored.get_instrument(iid), T0, "bull")
    payload = {"market_transition_key": key, "reason": "reverse_bos", "break_direction": "bull"}

    def make_event(database, n):
        ev, _ = database.insert_ltf_event(LtfEvent(
            id=None, observation_id=obs.id, scenario_id=sc.id, kind="cancellation",
            payload=payload, occurred_at=T0 + H1_MS, detected_at=T0 + H1_MS,
            dedupe_key=f"cancellation:{n}:bar",
        ))
        return ev

    first = make_event(stored, 1)
    sender = LogSender()
    sender.chat_id = "owner-a"
    disp = LtfDispatcher(stored, cfg, sender)
    await disp.deliver([first])
    await disp.deliver([first])
    assert len(sender.cards) == 1

    other = LogSender()
    other.chat_id = "owner-a"
    again = LtfDispatcher(stored, cfg, other)
    await again.deliver([first])
    assert other.cards == []

    class Slow(LogSender):
        def __init__(self):
            super().__init__()
            self.chat_id = "race"

        async def send_card(self, *args, **kwargs):
            await asyncio.sleep(0.02)
            return await super().send_card(*args, **kwargs)

    slow = Slow()
    second = make_event(stored, 2)
    left = LtfDispatcher(stored, cfg, slow)
    right = LtfDispatcher(stored, cfg, slow)
    await asyncio.gather(left.deliver([second]), right.deliver([second]))
    assert len(slow.cards) == 1
    stored.close()

    reopened = Database(path)
    quiet = LogSender()
    quiet.chat_id = "owner-a"
    box = Outbox(reopened, quiet, cfg)
    box.put("ltf", _packets_key(reopened, "owner-a"), Card("повтор"), [first.id])
    await box.flush()
    assert quiet.cards == []
    reopened.close()


def _packets_key(db, destination):
    row = db.conn.execute(
        "SELECT semantic_key FROM notification_packet WHERE destination=? AND channel='ltf' ORDER BY id LIMIT 1",
        (destination,),
    ).fetchone()
    return row["semantic_key"]


async def test_03_sent_failed_and_unknown_timeout_stay_distinct(db):
    ok_box, ok_sender = _box(db, LogSender())
    ok_id = ok_box.put("test", "sent", Card("ok"), [1])
    await ok_box.flush()
    assert ok_box.get(ok_id)["status"] == "sent"
    assert len(ok_sender.cards) == 1

    fail_box, fail_sender = _box(db, Boom(RuntimeError("до отправки")))
    fail_id = fail_box.put("test", "fail", Card("нет"), [2])
    await fail_box.flush()
    assert fail_box.get(fail_id)["status"] == "failed"
    assert fail_sender.cards == []

    unknown_box, unknown_sender = _box(db, TimeoutAfterAccept())
    unknown_id = unknown_box.put("test", "timeout", Card("может быть принято"), [3])
    await unknown_box.flush()
    db.conn.execute("UPDATE notification_packet SET due_at=0 WHERE id=?", (unknown_id,))
    db.conn.commit()
    await unknown_box.flush()
    assert unknown_box.get(unknown_id)["status"] == "uncertain"
    assert len(unknown_sender.cards) == 1


def _box(db, sender):
    box = Outbox(db, sender, DetectorConfig())
    box.register("test", lambda r, m: None, lambda i, s: None)
    return box, sender


def test_04_worked_context_confirms_long_after_60h(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    bos_open = candles[12].open_time
    touched = bos_open - 60 * H1_MS
    manual = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        lower=80.0, upper=81.5, formed_at=touched, source="manual",
        evidence={"direction_unset": True},
    )
    fvg = _zone(
        db, instrument_id, kind=ZoneType.FVG, direction=Direction.BULL,
        timeframe="W1", lower=78.0, upper=82.0, formed_at=touched + 1, source="auto",
    )
    engine.on_htf_zone_touched(instrument_id, manual, occurred_at=touched)
    engine.on_htf_zone_touched(instrument_id, fvg, occurred_at=touched + 1)
    for zone in (manual, fvg):
        db.update_zone(zone.id, status=ZoneStatus.WORKED, max_test_depth=1.0, has_tests=True)
        note_source_outcome(db, db.get_zone(zone.id), touched + 2)
    for obs in db.list_ltf_observations(instrument_id=instrument_id):
        db.update_ltf_observation(obs.id, state="closed_by_parent", updated_at=touched)
    before = len(db.conn.execute("SELECT id FROM htf_context_source").fetchall())
    _feed(db, engine, instrument_id, candles, 12)
    episode = open_episode(db, instrument_id) or _latest(db, instrument_id)
    assert episode["state"] == "confirmed"
    assert episode["confirmed_scenario_id"]
    scenario = db.get_ltf_scenario(episode["confirmed_scenario_id"])
    assert scenario.direction == Direction.BULL
    assert len(db.conn.execute("SELECT id FROM htf_context_source").fetchall()) == before
    assert int(episode["last_distinct_interaction_at"]) <= touched + 1


async def test_05_same_bos_cancels_short_and_confirms_long(db, cfg, instrument_id):
    cfg.ltf_range_anchor_policy = "continuation_only"
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    bear = _zone(db, instrument_id)
    manual = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        lower=80.0, upper=81.5, formed_at=T0 - 5_000, source="manual",
        evidence={"direction_unset": True},
    )
    engine.on_htf_zone_touched(instrument_id, bear, occurred_at=T0)
    engine.on_htf_zone_touched(instrument_id, manual, occurred_at=T0 + 1)
    db.update_ltf_observation(
        db.get_ltf_observation_by_zone(manual.id, manual.cycle_id).id,
        state="closed_by_parent", updated_at=T0,
    )
    _feed(db, engine, instrument_id, candles)
    episode = _latest(db, instrument_id)
    assert episode["state"] == "confirmed"
    cancels = [e for e in _events(db, instrument_id) if e.kind == "cancellation" and e.payload.get("reason") == "reverse_bos"]
    longs = [e for e in _events(db, instrument_id) if e.kind == "bos" and e.payload.get("reversal_confirmed")]
    assert cancels and longs
    assert cancels[0].payload["market_transition_key"] == longs[0].payload["market_transition_key"]
    sender = LogSender()
    disp = LtfDispatcher(db, cfg, sender)
    await disp.deliver(cancels + longs)
    assert len(sender.cards) == 1
    text = sender.cards[0][0].text
    assert "отмен" in text.lower()
    assert "бычий сценарий подтверждён" in text.lower()


def test_06_context_without_bos_does_not_confirm(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    zone = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        source="manual", evidence={"direction_unset": True}, formed_at=T0 - H1_MS,
    )
    engine.on_htf_zone_touched(instrument_id, zone, occurred_at=T0 - H1_MS)
    _feed(db, engine, instrument_id, candles, 8)
    episode = open_episode(db, instrument_id)
    assert episode["state"] == "awaiting_h1"
    assert db.list_ltf_scenarios_for_instrument(instrument_id) == []


def test_07_bsl_cross_is_not_a_long(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    bsl = _zone(
        db, instrument_id, kind=ZoneType.BSL, direction=Direction.BEAR,
        lower=15.0, upper=15.0, formed_at=T0 - H1_MS,
    )
    engine.on_htf_zone_touched(instrument_id, bsl, occurred_at=T0 - H1_MS)
    db.update_ltf_observation(
        db.get_ltf_observation_by_zone(bsl.id, bsl.cycle_id).id,
        state="closed_by_parent", updated_at=T0,
    )
    _feed(db, engine, instrument_id, candles, 12)
    episode = open_episode(db, instrument_id)
    assert episode is not None and episode["state"] == "awaiting_h1"
    sources = db.conn.execute(
        "SELECT interaction FROM htf_context_source WHERE episode_id=?",
        (episode["id"],),
    ).fetchall()
    assert [r["interaction"] for r in sources] == ["level_cross"]
    assert not any(
        s.direction == Direction.BULL
        for s in db.list_ltf_scenarios_for_instrument(instrument_id)
    )


def test_08_unknown_intrabar_order_does_not_invent_cause(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    inside = candles[12].open_time + 1_000
    zone = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        source="manual", evidence={"direction_unset": True}, formed_at=inside,
    )
    engine.on_htf_zone_touched(instrument_id, zone, occurred_at=inside)
    db.update_ltf_observation(
        db.get_ltf_observation_by_zone(zone.id, zone.cycle_id).id,
        state="closed_by_parent", updated_at=inside,
    )
    _feed(db, engine, instrument_id, candles, 12)
    episode = open_episode(db, instrument_id)
    assert episode["intrabar_order_unknown"] == 1
    assert episode["state"] == "awaiting_h1"
    assert db.list_ltf_scenarios_for_instrument(instrument_id) == []


def test_09_lower_and_upper_zones_survive_a_later_hl(cfg):
    bars = [
        (9.2, 10.0, 8.0, 9.5),
        (11.0, 12.0, 10.5, 11.5),
        (13.2, 14.0, 13.0, 13.5),
        (14.0, 14.5, 13.6, 14.2),
        (13.8, 14.2, 12.5, 13.0),
        (15.0, 16.0, 14.8, 15.5),
        (17.0, 18.0, 16.5, 17.5),
        (19.2, 20.0, 19.0, 19.5),
        (20.5, 22.0, 20.0, 21.5),
    ]
    candles = make_h1_candles(bars, T0)
    ll = _pivot("low", candles[0], 1)
    hl = _pivot("low", candles[4], 2)
    hh = _pivot("high", candles[8], 3)
    event = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BULL,
        break_level=21.5, break_candle_open_time=candles[8].open_time,
        occurred_at=candles[8].close_time, detected_at=candles[8].close_time,
        level_key="bos-test",
    )
    full = build_movement(1, [ll, hl, hh], candles, event, Direction.BULL, 20 * H1_MS, start_pivot=ll)
    late = build_movement(1, [ll, hl, hh], candles, event, Direction.BULL, 20 * H1_MS)
    assert full.start_at == candles[0].open_time
    assert late.start_at == candles[4].open_time
    found = {(z.type, z.lower, z.upper) for z in detect_entry_zones(candles, full, [ll, hl, hh], Direction.BULL, cfg).zones}
    cut = {(z.type, z.lower, z.upper) for z in detect_entry_zones(candles, late, [ll, hl, hh], Direction.BULL, cfg).zones}
    assert ("FVG", 10.0, 13.0) in found
    assert ("FVG", 16.0, 19.0) in found
    assert ("FVG", 10.0, 13.0) not in cut
    assert ("FVG", 16.0, 19.0) in cut


def test_10_drawing_without_detector_does_not_create_a_zone(db, instrument_id):
    candles = make_h1_candles([(10, 10.2, 9.8, 10.0)] * 6, T0)
    text = explain_missing_zone(candles, Direction.BULL, 9.5, 10.5)
    assert "не нашёл" in text
    assert db.list_ltf_entry_zones(instrument_id=instrument_id) == []


def test_11_filled_fvg_stays_filled_after_bos(cfg):
    zone = LtfEntryZone(
        id=1, instrument_id=1, type="FVG", direction=Direction.BULL,
        lower=10.0, upper=13.0, formed_at=T0, confirmed_at=T0 + H1_MS,
        movement_id=0, validity="tested", max_test_depth=1.0, test_extreme=10.0,
    )
    from app.engine.ltf.ranges import RangeDraft
    rng = RangeDraft(
        direction=Direction.BULL, lower=8.0, upper=22.0, mid=15.0,
        anchor_low_ref=None, anchor_high_ref=None, available_at=T0 + 8 * H1_MS,
        kind="reversal_leg",
    )
    decision = evaluate_entry(zone, Direction.BULL, cfg, rng, pd_status="provisional")
    assert decision.reason == "fvg_filled"
    assert decision.reason != "eligible_provisional"


def test_12_discount_of_80400_83600():
    low, high = 80400.0, 83600.0
    eq = 82000.0
    assert (low + high) / 2 == eq
    assert discount_relation(80600, 81800, low, eq) == "full"
    assert discount_relation(81000, 83000, low, eq) == "partial"
    assert entry_segment(81000, 83000, low, eq) == (81000.0, 82000.0)
    assert discount_relation(82100, 83000, low, eq) == "none"
    assert entry_segment(82100, 83000, low, eq) is None


def test_13_future_high_is_not_used(db, cfg, instrument_id):
    zone = _zone(db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
                 source="manual", evidence={"direction_unset": True})
    episode = record_interaction(db, cfg, instrument_id, zone, T0)
    early = make_candle(T0, 81000, 82000, 80400, 81900, timeframe="H1", instrument_id=instrument_id)
    later = make_candle(T0 + H1_MS, 82000, 83600, 81800, 83000, timeframe="H1", instrument_id=instrument_id)
    preview = make_candle(T0 + 2 * H1_MS, 83000, 84000, 82900, 83900, timeframe="H1",
                          instrument_id=instrument_id, closed=False)
    seen = observed_extreme([early, preview], early.open_time, early.close_time, side="high")
    assert seen[0] == 82000
    save_leg(db, episode["id"], 80400, seen[0], 0, "provisional", early.close_time, early.open_time, False, 0)
    assert leg_at(db, episode["id"], early.close_time)["high"] == 82000
    save_leg(db, episode["id"], 80400, later.high, 0, "provisional", later.close_time, later.open_time, False, 0)
    assert leg_at(db, episode["id"], early.close_time)["high"] == 82000
    assert leg_at(db, episode["id"], later.close_time)["high"] == 83600
    assert 83600 not in (leg_at(db, episode["id"], early.close_time)["high"],)


def test_14_provisional_pd_is_labeled(db, cfg, instrument_id):
    view, zone = _provisional_view(db, cfg, instrument_id)
    assert view["pd"]["status"] == "provisional"
    assert view["pd"]["label"] == "предварительная"
    assert view["zones"][0]["reason"] == "eligible_provisional"
    assert view["zones"][0]["allowed"] is True
    obs = db.list_ltf_observations(instrument_id=instrument_id)[0]
    admitted = _admission(db, instrument_id, obs.id, [{
        "id": zone.id, "type": zone.type, "direction": zone.direction.value,
        "formed_at": zone.formed_at, "lower": zone.lower, "upper": zone.upper,
    }], [zone])
    assert admitted[0]["eligibility"] == "eligible_provisional"


def test_15_touch_above_eq_is_not_a_buy(db, cfg, instrument_id):
    engine, obs, sc, _zone_row = _visit_case(
        db, cfg, instrument_id, lower=140.0, upper=180.0, low=100.0, high=200.0, eq=150.0,
    )
    candle = make_candle(T0 + 5 * H1_MS, 165, 170, 160, 168, timeframe="H1", instrument_id=instrument_id)
    engine._scan_reversal_visits(obs, sc, candle, candle.close_time, "live", 0, LtfTickResult())
    assert [e for e in db.list_ltf_events(observation_id=obs.id) if e.kind == "touch"] == []


def test_16_upper_test_does_not_mark_lower(db, cfg, instrument_id):
    engine, obs, sc, upper = _visit_case(
        db, cfg, instrument_id, lower=130.0, upper=145.0, low=100.0, high=200.0, eq=150.0,
    )
    lower = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=100.0, upper=120.0, formed_at=T0, confirmed_at=T0, movement_id=0,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=lower.id, range_version=0,
        eligible=True, overlap="full", state="fresh", reason="ok",
    ))
    candle = make_candle(T0 + 5 * H1_MS, 134, 140, 132, 136, timeframe="H1", instrument_id=instrument_id)
    engine._scan_reversal_visits(obs, sc, candle, candle.close_time, "live", 0, LtfTickResult())
    assert db.get_ltf_entry_zone(lower.id).validity == "fresh"
    assert db.get_ltf_entry_zone(upper.id).validity == "tested"


def test_17_one_alert_per_visit_and_reuse(db, cfg, instrument_id):
    engine, obs, sc, zone = _visit_case(
        db, cfg, instrument_id, lower=100.0, upper=120.0, low=100.0, high=200.0, eq=150.0,
    )
    inside = [
        make_candle(T0 + (5 + i) * H1_MS, 108, 112, 106, 110, timeframe="H1", instrument_id=instrument_id)
        for i in range(3)
    ]
    outside = make_candle(T0 + 9 * H1_MS, 130, 134, 128, 132, timeframe="H1", instrument_id=instrument_id)
    back = make_candle(T0 + 10 * H1_MS, 108, 112, 106, 110, timeframe="H1", instrument_id=instrument_id)
    result = LtfTickResult()
    for candle in inside + [outside, back]:
        engine._scan_reversal_visits(obs, sc, candle, candle.close_time, "live", 0, result)
    touches = sorted(
        (e for e in db.list_ltf_events(observation_id=obs.id) if e.kind == "touch"),
        key=lambda e: e.payload["visit"],
    )
    assert [e.payload["visit"] for e in touches] == [1, 2]
    assert db.get_ltf_entry_zone(zone.id).validity == "tested"


def test_18_close_below_protected_ll_cancels_once(db, cfg, instrument_id):
    engine, obs, sc = _protected_long(db, cfg, instrument_id, price=100.0)
    wick = make_candle(T0, 101, 102, 99, 100.5, timeframe="H1", instrument_id=instrument_id)
    assert engine._cancel_on_protected_level(obs, sc, wick, wick.close_time, "live", 0, LtfTickResult()) is False
    assert db.get_ltf_scenario(sc.id).state != "cancelled"
    broke = make_candle(T0 + H1_MS, 100, 101, 98, 99, timeframe="H1", instrument_id=instrument_id)
    assert engine._cancel_on_protected_level(obs, sc, broke, broke.close_time, "live", 0, LtfTickResult()) is True
    again = make_candle(T0 + 2 * H1_MS, 99, 100, 97, 98, timeframe="H1", instrument_id=instrument_id)
    engine._cancel_on_protected_level(
        obs, db.get_ltf_scenario(sc.id), again, again.close_time, "live", 0, LtfTickResult(),
    )
    cancels = [e for e in db.list_ltf_events(observation_id=obs.id) if e.kind == "cancellation"]
    assert len(cancels) == 1
    assert cancels[0].payload["reason"] == "protected_ll"
    assert "market_transition_key" not in cancels[0].payload
    closed = db.get_ltf_scenario(sc.id)
    engine._build_entries(
        obs, closed, StructureEventDraft(
            kind="BOS", stage="primary", direction=Direction.BULL, break_level=99,
            break_candle_open_time=again.open_time, occurred_at=again.close_time,
            detected_at=again.close_time, level_key="nope",
        ), 1, [], [], again.close_time, LtfTickResult(),
    )
    assert db.list_ltf_entry_zones(instrument_id=instrument_id) == []


def test_19_expired_or_manual_close_needs_a_new_interaction(db, cfg, instrument_id):
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    zone = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        source="manual", evidence={"direction_unset": True},
    )
    episode = record_interaction(db, cfg, instrument_id, zone, T0)
    db.conn.execute(
        "UPDATE htf_context_episode SET expires_at=? WHERE id=?",
        (candles[12].close_time - 1, episode["id"]),
    )
    db._commit()
    engine = LtfEngine(db, cfg)
    _feed(db, engine, instrument_id, candles, 12)
    expired = load_episode(db, episode["id"])
    assert expired["state"] == "expired"
    assert expired["confirmed_scenario_id"] is None

    fresh = record_interaction(db, cfg, instrument_id, zone, candles[12].close_time + H1_MS)
    assert fresh["id"] != episode["id"]
    close_episode(db, fresh["id"], candles[12].close_time + H1_MS)
    plan = plan_context_bar(
        db, cfg, instrument_id, candles[12], [], candles[:13], candles[12].close_time,
    )
    assert plan is None
    assert load_episode(db, fresh["id"])["state"] == "closed_by_user"


async def test_20_replay_matches_live_and_does_not_notify(db, cfg, instrument_id, tmp_path):
    live = _drive_reversal(db, cfg, instrument_id, "live")
    other = Database(str(tmp_path / "replay.sqlite"))
    iid = other.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    replay = _drive_reversal(other, cfg, iid, "replay")
    assert live["state"] == replay["state"] == "confirmed"
    assert live["keys"] == replay["keys"]
    assert replay["delayed"] and not live["delayed"]
    sender = LogSender()
    disp = LtfDispatcher(other, cfg, sender)
    await disp.deliver(replay["events"])
    assert sender.cards == []
    other.close()


async def test_21_venues_and_recipients_stay_separate(db, cfg):
    binance = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    bybit = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="bybit", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    left = transition_key(db.get_instrument(binance), T0, "bull")
    right = transition_key(db.get_instrument(bybit), T0, "bull")
    assert left != right
    events = []
    for iid, key in ((binance, left), (bybit, right)):
        zone = _zone(db, iid, formed_at=T0 + iid)
        obs = db.insert_ltf_observation(LtfObservation(
            id=None, instrument_id=iid, zone_id=zone.id, zone_version=1, cycle_id=1,
            direction=Direction.BULL, state="active", activated_at=T0,
        ))
        sc = db.insert_ltf_scenario(LtfScenario(
            id=None, observation_id=obs.id, direction=Direction.BULL,
            trigger="BOS", stage="primary", state="monitoring_entries",
        ))
        ev, _ = db.insert_ltf_event(LtfEvent(
            id=None, observation_id=obs.id, scenario_id=sc.id, kind="bos",
            payload={"market_transition_key": key, "reversal_confirmed": True, "break_direction": "bull"},
            occurred_at=T0 + H1_MS, detected_at=T0 + H1_MS, dedupe_key=f"bos:{iid}",
        ))
        events.append(ev)
    a, b = LogSender(), LogSender()
    a.chat_id, b.chat_id = "alice", "bob"
    await LtfDispatcher(db, cfg, a).deliver([events[0]])
    await LtfDispatcher(db, cfg, b).deliver([events[0]])
    await LtfDispatcher(db, cfg, a).deliver([events[1]])
    assert len(a.cards) == 2
    assert len(b.cards) == 1
    assert notification_key("alice", "ltf", left, "market_transition") != notification_key(
        "bob", "ltf", left, "market_transition",
    )


def test_22_chart_api_card_and_bot_share_one_projection(db, cfg, instrument_id):
    view, _zone_row = _provisional_view(db, cfg, instrument_id)
    pack = assemble_h1_layers(
        db, SimpleNamespace(detector=cfg), instrument_id, as_of=view["as_of"],
    )
    bot = bot_reversal_view(db, instrument_id, view["as_of"], cfg)
    ctx = LtfContext(
        instrument=db.get_instrument(instrument_id), zone=None, observation=None, scenario=None,
    )
    event = SimpleNamespace(kind="bos", payload={"reversal_confirmed": True, "break_direction": "bull"})
    text = transition_card_text([event], ctx, view)
    assert pack["reversal"]["pd"] == view["pd"] == bot["pd"]
    assert [z["id"] for z in pack["reversal"]["zones"]] == [z["id"] for z in view["zones"]] == [z["id"] for z in bot["zones"]]
    assert str(int(view["pd"]["L"])) in text
    assert str(int(view["pd"]["H"])) in text
    assert str(int(view["pd"]["EQ"])) in text
    assert "предварительный PD" in text


def _latest(db, instrument_id):
    row = db.conn.execute(
        "SELECT * FROM htf_context_episode WHERE instrument_id=? ORDER BY id DESC LIMIT 1",
        (instrument_id,),
    ).fetchone()
    return dict(row) if row else None


def _pivot(kind, candle, pid):
    price = candle.low if kind == "low" else candle.high
    return PivotCandidate(
        instrument_id=candle.instrument_id, price=price, kind=kind,
        pivot_at=candle.open_time, candle_open_time=candle.open_time,
        confirmed_at=candle.close_time, left=3, right=3, state="confirmed",
        pivot_id=pid,
    )


def _provisional_view(db, cfg, instrument_id):
    parent = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        source="manual", evidence={"direction_unset": True},
    )
    episode = record_interaction(db, cfg, instrument_id, parent, T0)
    save_leg(db, episode["id"], 80400, 82000, 0, "provisional", T0 + H1_MS, T0, False, 0)
    db.conn.execute(
        """UPDATE htf_context_episode
           SET state='confirmed', confirmed_at=?, protected_price=?, protected_kind=?
           WHERE id=?""",
        (T0 + H1_MS, 80400, "ll", episode["id"]),
    )
    db._commit()
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent.id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, state="active", activated_at=T0,
        evidence={"reversal_episode_id": episode["id"]},
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL, trigger="BOS",
        stage="primary", state="monitoring_entries",
    ))
    db.conn.execute(
        "UPDATE htf_context_episode SET confirmed_scenario_id=? WHERE id=?",
        (sc.id, episode["id"]),
    )
    db._commit()
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=80600, upper=81800, formed_at=T0, confirmed_at=T0, movement_id=0,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=zone.id, range_version=0,
        eligible=True, overlap="full", state="fresh", reason="eligible_provisional",
    ))
    return reversal_projection(db, instrument_id, T0 + H1_MS, cfg), zone


def _visit_case(db, cfg, instrument_id, *, lower, upper, low, high, eq):
    parent = _zone(db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
                   source="manual", evidence={"direction_unset": True})
    episode = record_interaction(db, cfg, instrument_id, parent, T0)
    save_leg(db, episode["id"], low, high, eq, "provisional", T0 + H1_MS, T0, False, 0)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent.id, zone_version=1,
        cycle_id=7, direction=Direction.BULL, state="active", activated_at=T0,
        evidence={"reversal_episode_id": episode["id"]},
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL, trigger="BOS",
        stage="primary", state="monitoring_entries",
    ))
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB", direction=Direction.BULL,
        lower=lower, upper=upper, formed_at=T0, confirmed_at=T0, movement_id=0,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=zone.id, range_version=0,
        eligible=True, overlap="partial", state="fresh", reason="ok",
    ))
    return LtfEngine(db, cfg), obs, sc, zone


def _protected_long(db, cfg, instrument_id, price):
    parent = _zone(db, instrument_id)
    episode = record_interaction(db, cfg, instrument_id, parent, T0)
    db.conn.execute(
        """UPDATE htf_context_episode
           SET state='confirmed', protected_price=?, protected_kind=?, protected_candle_open=?
           WHERE id=?""",
        (price, "ll", T0, episode["id"]),
    )
    db._commit()
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent.id, zone_version=1,
        cycle_id=8, direction=Direction.BULL, state="active", activated_at=T0,
        evidence={"reversal_episode_id": episode["id"], "protected_kind": "ll"},
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL, trigger="BOS",
        stage="primary", state="monitoring_entries",
    ))
    return LtfEngine(db, cfg), obs, sc


def _drive_reversal(db, cfg, instrument_id, mode):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    touched = candles[12].open_time - 60 * H1_MS
    zone = _zone(
        db, instrument_id, kind=ZoneType.MANUAL, direction=Direction.BULL,
        source="manual", evidence={"direction_unset": True}, formed_at=touched,
    )
    obs = engine.on_htf_zone_touched(instrument_id, zone, occurred_at=touched)
    db.update_ltf_observation(obs.id, state="closed_by_parent", updated_at=touched)
    if mode == "live":
        _feed(db, engine, instrument_id, candles, 12)
    else:
        db.insert_candles(candles[:13])
        engine.replay_observation(obs.id)
    episode = _latest(db, instrument_id)
    events = _events(db, instrument_id)
    keys = sorted(
        e.payload.get("market_transition_key")
        for e in events if e.payload.get("market_transition_key")
    )
    return {
        "state": episode["state"] if episode else None,
        "keys": keys,
        "delayed": all(e.delayed for e in events) if events else False,
        "events": events,
    }
