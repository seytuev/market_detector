"""Offline reproductions for the SOL incident; only in-memory databases."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DetectorConfig
from app.db import Database
from app.models import Direction, Event, EventKind, Instrument, Zone, ZoneStatus, ZoneType, now_ms
from tests.test_ltf_worker import FakeAdapter, _make_worker, _recent_h1


async def main() -> None:
    db = Database(":memory:")
    iid = db.upsert_instrument(Instrument(None, "SOL", "binance", "spot", "SOLUSDT", "USDT"))
    ins = db.get_instrument(iid)
    cfg = DetectorConfig()
    worker, engine = _make_worker(db, FakeAdapter(), cfg)
    zid = db.insert_zone(Zone(
        None, iid, ZoneType.OB, Direction.BULL, "D1", 110, 116,
        formed_at=now_ms() - 10_000, confirmed_at=now_ms() - 5_000,
        status=ZoneStatus.CANDIDATE,
    ))
    zone = db.get_zone(zid)
    opened = []

    async def record_open(instrument, parent, occurred):
        opened.append(parent.id)

    worker._ltf_open_observation = record_open
    await worker._ltf_open_marked_zones(ins)
    marked = len(opened)
    event = Event(None, zid, 1, EventKind.TOUCH, now_ms(), now_ms(), 115)
    await worker._ltf_on_poll(ins, [event])
    print(json.dumps({
        "case": "confirmed_candidate_ob",
        "canonical_relevant": zone.is_currently_relevant(),
        "opened_via_analyze": marked,
        "opened_via_touch": len(opened) - marked,
    }))

    fvg = Zone(None, iid, ZoneType.FVG, Direction.BULL, "W1", 114.32, 116.32,
               formed_at=1000, confirmed_at=2000, status=ZoneStatus.ACTIVE)
    print(json.dumps({"case": "fvg_policy", "htf_context_types": cfg.htf_context_types,
                      "accepted_as_context": worker._ltf_is_context_zone(fvg)}))

    empty = Database(":memory:")
    eiid = empty.upsert_instrument(Instrument(None, "SOL", "binance", "spot", "SOLUSDT", "USDT"))
    adapter = FakeAdapter()
    adapter.candles = _recent_h1([(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)], instrument_id=eiid)
    ew, _ = _make_worker(empty, adapter)
    await ew.ltf_poll_once()
    print(json.dumps({
        "case": "h1_without_observation",
        "ltf_analyze": empty.get_instrument(eiid).ltf_analyze,
        "h1_candles": len(empty.get_candles(eiid, "H1")),
        "pivots": len(empty.list_ltf_pivots(eiid)),
        "cursor": empty.get_meta(f"ltf:h1:last_close:{eiid}"),
    }))


if __name__ == "__main__":
    asyncio.run(main())
