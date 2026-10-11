"""Read-only audit reproductions. Only in-memory SQLite; no network/senders.

Run from repository root: python -m tools.repro_htf_ltf_audit_2026_10_10
Reports observed behavior, not assertions of desired trading rules.
"""
import json
from dataclasses import replace

from app.config import DetectorConfig
from app.db import Database
from app.engine.scanner import Scanner
from app.engine.ltf.engine import LtfEngine, LtfTickResult
from app.engine.ltf.eligibility import admitted_scenario_entries, evaluate_final
from app.models import Candle, Direction, Event, EventKind, Instrument, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfEntryZone, LtfObservation, LtfRange, LtfScenario, LtfScenarioEntry
from app.services.h1_setup import _trade_context
from app.services.htf_context import record_interaction
from app.services.htf_parent import parent_decision

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000


def database():
    db = Database(":memory:")
    iid = db.upsert_instrument(Instrument(
        id=None, asset="TEST", venue="binance", market_type="spot",
        symbol="TESTUSDT", quote_asset="USDT",
    ))
    return db, iid


def scenario(db, iid, at=T, direction=Direction.BEAR):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T-D,
        confirmed_at=T-1, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zid, zone_version=1,
        cycle_id=1, direction=direction, state="active", activated_at=at,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction, trigger="BOS",
        stage="primary", state="monitoring_entries", created_at=at, updated_at=at,
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90, upper=110, mid=100,
        anchor_low_pivot_id=None, anchor_high_pivot_id=None, available_at=at,
    ))
    return obs, sc


def entry(db, iid, sc, validity="fresh", depth=0):
    z = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=iid, type="OB", direction=Direction.BEAR,
        lower=100, upper=104, formed_at=T-2*H, confirmed_at=T-H,
        validity=validity, max_test_depth=depth,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=1,
        eligible=True, overlap="full", state="fresh", reason="ok",
        added_at=T, updated_at=T,
    ))
    return z, db.list_ltf_scenario_entries(sc.id)[0]


def run():
    out = {}
    db, iid = database()
    try:
        _, sc = scenario(db, iid)
        z, e = entry(db, iid, sc, validity="tested", depth=.95)
        cfg = DetectorConfig(entry_reuse_max_depth=.98)
        out["R1_configured_vs_default_eligibility"] = {
            "configured_threshold": cfg.entry_reuse_max_depth, "depth": .95,
            "explicit_cfg_admits": evaluate_final(e, z, allow_outside=False, cfg=cfg).eligible_now,
            "shared_helper_admits": bool(admitted_scenario_entries(db, sc.id)),
        }
    finally:
        db.close()

    db, iid = database()
    try:
        _, sc = scenario(db, iid, at=T+10*H)
        past = T+H
        before = _trade_context(db, iid, Direction.BEAR.value, past)[0]
        db.update_ltf_scenario(sc.id, state="cancelled", cancelled_at=T+12*H)
        after = _trade_context(db, iid, Direction.BEAR.value, past)[0]
        out["R2_historical_context_depends_on_future_scenario"] = {
            "as_of": past, "scenario_created_at": sc.created_at,
            "context_before_future_scenario_cancel": before,
            "same_past_context_after_cancel": after,
        }
    finally:
        db.close()

    db, iid = database()
    try:
        obs, sc = scenario(db, iid, direction=Direction.BULL)
        zid = db.insert_zone(Zone(
            id=None, instrument_id=iid, type=ZoneType.FVG, direction=Direction.BEAR,
            timeframe="D1", lower=90, upper=100, formed_at=T+H,
            confirmed_at=T+2*H, status=ZoneStatus.ACTIVE,
        ))
        db.insert_event(Event(
            id=None, zone_id=zid, cycle_id=1, kind=EventKind.DEPTH_50,
            occurred_at=T+3*H, detected_at=T+3*H, price=95,
        ))
        c = Candle(instrument_id=iid, timeframe="H1", open_time=T,
                   close_time=T+H-1, open=101, high=105, low=100, close=102, closed=True)
        eng = LtfEngine(db, DetectorConfig())
        result = LtfTickResult()
        eng._update_scenario_context(obs, sc, [], [c], T+4*H, "catchup", 3*H, result)
        updates = [e for e in result.events if e.kind == "context_update"]
        out["R3_future_htf_fact_backdated_to_old_h1"] = {
            "h1_close": c.close_time, "fvg_confirmed": T+2*H,
            "htf_test_at": T+3*H,
            "ltf_emitted": [{"occurred_at": e.occurred_at, "payload": e.payload} for e in updates],
        }
    finally:
        db.close()

    db, iid = database()
    try:
        values = [(100, 102, 99, 101), (101, 109, 100, 108), (110, 112, 109, 111)]
        candles = [Candle(
            instrument_id=iid, timeframe="D1", open_time=T+i*D,
            close_time=T+(i+1)*D-1, open=o, high=h, low=l, close=c, closed=True,
        ) for i, (o,h,l,c) in enumerate(values)]
        db.insert_candles(candles)
        Scanner(db, DetectorConfig()).on_closed_candle(candles[0])
        out["R4_htf_scanner_reads_future_database_candles"] = {
            "processing_close": candles[0].close_time,
            "future_zones": [{"type": z.type.value, "confirmed_at": z.confirmed_at}
                             for z in db.get_zones(iid) if z.confirmed_at and z.confirmed_at > candles[0].close_time+1],
        }
    finally:
        db.close()

    db, iid = database()
    try:
        obs, sc = scenario(db, iid)
        z, e = entry(db, iid, sc)
        c = Candle(instrument_id=iid, timeframe="H1", open_time=T,
                   close_time=T+H-1, open=101, high=103.8, low=100.5, close=101, closed=True)
        LtfEngine(db, DetectorConfig())._on_entry_touched(
            obs, sc, e, z, c, [], c.close_time, "live", 0, LtfTickResult())
        stored = db.list_ltf_scenario_entries(sc.id)[0]
        actual = db.get_ltf_entry_zone(z.id)
        out["R5_first_deep_touch_persists_wrong_reason"] = {
            "stored_reason": stored.reason, "stored_state": stored.state,
            "zone_validity": actual.validity, "depth": actual.max_test_depth,
            "final_reason": evaluate_final(stored, actual, allow_outside=False).primary_reason,
        }
    finally:
        db.close()

    z = Zone(id=1, instrument_id=1, type=ZoneType.OB, direction=Direction.BEAR,
             timeframe="D1", lower=100, upper=104, formed_at=T, confirmed_at=T+H,
             status=ZoneStatus.ACTIVE)
    cfg = DetectorConfig()
    out["R6_parent_as_of_uses_present_lifecycle"] = {
        "as_of": T+2*H,
        "before_later_invalidation": parent_decision(z, cfg, T+2*H).eligible,
        "after_later_invalidation": parent_decision(
            replace(z, status=ZoneStatus.ARCHIVED, market_validity="invalid", display_until=T+5*H),
            cfg, T+2*H).eligible,
    }

    db, iid = database()
    try:
        z = replace(z, id=None, instrument_id=iid)
        zid = db.insert_zone(z)
        z = db.get_zone(zid)
        newer = record_interaction(db, cfg, iid, z, T+10*H)
        older = record_interaction(db, cfg, iid, z, T+2*H)
        out["R7_out_of_order_interaction_moves_expiry_back"] = {
            "expiry_before": newer["expires_at"], "expiry_after": older["expires_at"],
            "last_interaction_before": newer["last_distinct_interaction_at"],
            "last_interaction_after": older["last_distinct_interaction_at"],
        }
    finally:
        db.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
