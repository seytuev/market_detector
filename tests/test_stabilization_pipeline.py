import asyncio
from types import SimpleNamespace
import pytest
from app.adapters.base import QuoteTick
from app.config import DetectorConfig
from app.engine.scanner import Scanner
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.services.data_sync import DataSync
from app.services.market_queue import MarketQueue


def worker_for(db):
    worker = SimpleNamespace(db=db, scan_tfs=("D1",), _stop=asyncio.Event())
    worker.scanner = Scanner(db, DetectorConfig())
    worker.data_sync = DataSync(worker)
    worker._calculate = worker.data_sync.calculate
    worker.deliveries = []
    async def dispatch(events):
        worker.deliveries.extend(e.id for e in events)
    async def ltf(*args):
        pass
    worker._dispatch_events = dispatch
    worker._ltf_on_poll = ltf
    return worker


async def test_failed_tick_is_retained_then_recovered(db, instrument_id):
    worker = worker_for(db)
    instrument = db.get_instrument(instrument_id)
    def fail(*args):
        raise RuntimeError("fault injection")
    worker.scanner.on_price = fail
    queue = MarketQueue(worker)
    tick = QuoteTick(100,1780272000001,1780272000002,"trade","receipt-1")
    queue.enqueue(instrument,tick,"ws")
    for _ in range(100):
        if db.load_spool(instrument_id)[0]["status"] == "retry": break
        await asyncio.sleep(.01)
    row = db.load_spool(instrument_id)[0]
    assert row["status"] == "retry" and row["last_error"]
    assert not await queue.wait_idle(instrument_id,.02)
    worker.scanner.on_price = lambda *args: []
    db.retry_quote(row["id"])
    queue._ensure_consumer(queue._state(instrument_id))
    assert await queue.wait_idle(instrument_id,3)
    assert db.load_spool(instrument_id) == []
    await queue.stop()


async def test_committed_event_is_published_after_dispatch_failure(db, instrument_id):
    worker = worker_for(db)
    db.insert_zone(Zone(id=None,instrument_id=instrument_id,type=ZoneType.OB,direction=Direction.BULL,
        timeframe="D1",lower=100,upper=110,formed_at=1780272000000,confirmed_at=1780272000000,status=ZoneStatus.ACTIVE))
    calls = []
    async def fail(events):
        calls.extend(e.id for e in events)
        raise ConnectionError("publication process interrupted")
    worker._dispatch_events = fail
    queue = MarketQueue(worker)
    queue.enqueue(db.get_instrument(instrument_id),QuoteTick(105,1780272000001,1780272000002,"trade","2"),"ws")
    for _ in range(100):
        if calls: break
        await asyncio.sleep(.01)
    assert calls and db.pending_publications(instrument_id)
    assert db.load_spool(instrument_id) == []
    await queue.stop()
    recovered = worker_for(db)
    restarted = MarketQueue(recovered)
    restarted.start()
    assert await restarted.wait_idle(instrument_id,3)
    assert recovered.deliveries == calls
    assert db.pending_publications(instrument_id) == []
    await restarted.stop()


async def test_durable_intake_restarts_without_in_memory_queue(db, instrument_id):
    for i in range(8):
        db.spool_quote(instrument_id,source="ws",event_at=1780272000000+i,
            received_at=1780272000001+i,price=120+i,price_kind="trade",source_seq=str(i))
    worker = worker_for(db)
    queue = MarketQueue(worker,spool_batch=2)
    queue.start()
    assert await queue.wait_idle(instrument_id,3)
    assert db.spool_counts() == {}
    await queue.stop()
