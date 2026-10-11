"""Additional audit probes: in-memory DB only, no network or Telegram.

Run: python -m tools.repro_htf_ltf_followup_2026_10_10
Assertions document current defects, not desired engine behavior.
"""
import json
from dataclasses import replace

from app.config import DetectorConfig
from app.engine.ltf.engine import LtfEngine
from app.engine.ltf.pivots import find_h1_pivots
from app.engine.ltf.structure import StructureBatch
from app.models import Candle, Direction, Zone, ZoneStatus, ZoneType
from app.services.htf_context import (
    record_interaction, drop_source, open_episode, episode_sources,
    context_supports_direction,
)
from tools.repro_htf_ltf_audit_2026_10_10 import database, scenario, T, H


def bars(iid):
    return [Candle(instrument_id=iid, timeframe="H1", open_time=T+i*H,
                   close_time=T+(i+1)*H-1, open=5, high=hi, low=1,
                   close=5, closed=True)
            for i, hi in enumerate([10, 10, 10, 12, 10, 10, 10])]


def source(db, iid, direction):
    serial = len(db.get_zones(iid))
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T-H+serial,
        confirmed_at=T-1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def run():
    results = {}
    db, iid = database()
    try:
        db.set_quote(iid, 110, T+H)
        db.set_quote(iid, 90, T)
        actual = db.get_quote(iid)
        assert actual == (90, T)
        results["P1_quote_regresses"] = {"latest_written": [110, T+H], "after_old_quote": actual}
    finally:
        db.close()

    db, iid = database()
    try:
        c = bars(iid)[0]
        db.insert_candles([c])
        db.insert_candles([replace(c, closed=False, high=9)])
        assert db.last_candle(iid, "H1") is None
        results["P2_closed_bar_downgraded"] = {"closed_count": len(db.get_candles(iid, "H1")), "stored_closed": db.last_candle(iid, "H1", closed_only=False).closed}
    finally:
        db.close()

    db, iid = database()
    try:
        cfg = DetectorConfig()
        original = bars(iid)
        corrected = list(original)
        corrected[3] = replace(original[3], high=9)
        db.insert_candles(original)
        engine = LtfEngine(db, cfg)
        first = engine.process_h1_close(iid, now_ms=original[-1].close_time)
        db.insert_candles([corrected[3]])
        second = engine.process_h1_close(iid, now_ms=original[-1].close_time+1)
        assert first.processed == 7 and second.processed == 0
        stored = [(p.kind, p.price) for p in db.list_ltf_pivots(iid)]
        fresh = [(p.kind, p.price) for p in find_h1_pivots(corrected, 3, 3)]
        assert stored == [("high", 12)] and fresh == []
        batch = StructureBatch()
        batch.pivots_for(original, 3, 3)
        cached = [(p.kind, p.price) for p in batch.pivots_for(corrected, 3, 3)]
        assert cached == [("high", 12)]
        results["P3_correction_not_reprocessed"] = {"processed_after_correction": second.processed, "stored_pivots": stored, "fresh_pivots": fresh, "batch_pivots": cached}
    finally:
        db.close()

    db, iid = database()
    try:
        cfg = DetectorConfig()
        bear, bull = source(db, iid, Direction.BEAR), source(db, iid, Direction.BULL)
        record_interaction(db, cfg, iid, bear, T)
        record_interaction(db, cfg, iid, bull, T+H)
        drop_source(db, iid, bull.id, T+2*H, "market_invalid")
        ep = open_episode(db, iid)
        sources = episode_sources(db, ep["id"])
        accepts_bull = context_supports_direction(db, ep, sources, "bull")
        assert ep["candidate_direction"] == "neutral" and accepts_bull
        results["P4_removed_source_still_supports"] = {"aggregate": ep["candidate_direction"], "active_sources": [json.loads(s["evidence"])["direction"] for s in sources if s["active"]], "supports_bull": accepts_bull}
    finally:
        db.close()

    directions = []
    for order in [(Direction.BEAR, Direction.BULL, Direction.BULL), (Direction.BULL, Direction.BULL, Direction.BEAR)]:
        db, iid = database()
        try:
            for d in order:
                record_interaction(db, DetectorConfig(), iid, source(db, iid, d), T)
            directions.append(open_episode(db, iid)["candidate_direction"])
        finally:
            db.close()
    assert directions == ["bull", "neutral"]
    results["P5_equal_time_order_changes_aggregate"] = {"bear_bull_bull": directions[0], "bull_bull_bear": directions[1]}

    db, iid = database()
    try:
        obs, sc = scenario(db, iid)
        z = db.get_zone(obs.zone_id)
        db.update_zone(z.id, market_validity="invalid", evidence={**z.evidence, "invalidated_at": T+H}, end_reason="ob_invalidated")
        assert LtfEngine(db, DetectorConfig()).check_parent_validity(db.get_zone(z.id))
        event = next(e for e in db.list_ltf_events(observation_id=obs.id) if e.kind == "cancellation")
        assert event.processing_mode == "live" and event.occurred_at > T+H and not event.delayed
        results["P6_historical_parent_failure_becomes_live"] = {"parent_invalidated_at": T+H, "event_occurred_at": event.occurred_at, "mode": event.processing_mode, "delayed": event.delayed}
    finally:
        db.close()
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    run()
