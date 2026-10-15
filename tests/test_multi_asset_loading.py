import asyncio
import threading

from app.engine.ltf.engine import LtfEngine, LtfTickResult
from app.models import Instrument
from tests.test_ltf_worker import _make_worker, FakeAdapter, _parent_zone


async def test_engine_batches_are_isolated_and_slow_asset_does_not_block_another(db, monkeypatch):
    worker, engine = _make_worker(db, FakeAdapter())
    await worker.seed_instruments()
    instruments = db.get_instruments(enabled_only=True)[:2]
    entered, release = threading.Event(), threading.Event()

    def process_h1_close(self, iid):
        if iid == instruments[0].id:
            entered.set()
            assert release.wait(5)
        return id(self)

    monkeypatch.setattr(LtfEngine, 'process_h1_close', process_h1_close)
    first = asyncio.create_task(worker._calculate(engine.process_h1_close, instruments[0].id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = await asyncio.wait_for(worker._calculate(engine.process_h1_close, instruments[1].id), 2)
        assert not first.done()
    finally:
        release.set()
        original = await first
        await worker.data_sync.shutdown()
    assert second != original


async def test_cancelled_calculation_retains_its_instrument_without_blocking_other_assets(db):
    worker, _ = _make_worker(db, FakeAdapter())
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(5)
    first = asyncio.create_task(worker.data_sync.calculate(blocked, instrument_id=1))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        next_same = asyncio.create_task(worker.data_sync.calculate(lambda: 'same', instrument_id=1))
        assert await asyncio.wait_for(worker.data_sync.calculate(lambda: 'other', instrument_id=2), 2) == 'other'
        assert not first.done() and not next_same.done()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await next_same == 'same'
        await worker.data_sync.shutdown()


async def test_worker_exposes_queue_and_heartbeat_while_restore_is_blocked(db, monkeypatch):
    worker, _ = _make_worker(db, FakeAdapter())
    worker.settings.quote_poll_seconds = 0
    entered = asyncio.Event()
    async def restore():
        entered.set()
        await worker._stop.wait()
    async def idle():
        await worker._stop.wait()
    monkeypatch.setattr(worker, '_ltf_restore_all', restore)
    for name in ('_backfill_all', '_main_loop', 'ltf_loop', 'events_loop'):
        monkeypatch.setattr(worker, name, idle)
    monkeypatch.setattr(worker.data_sync, 'boundary_loop', idle)
    running = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(100):
            if db.get_meta('runtime:calc_heartbeat'):
                break
            await asyncio.sleep(.01)
        assert worker.data_sync.ready
        assert worker.calc_queue._started
        assert db.get_meta('runtime:calc_heartbeat')
        assert not running.done()
    finally:
        worker.stop()
        await asyncio.wait_for(running, 5)
        await worker.calc_queue.stop()
        await worker.data_sync.shutdown()


async def test_marked_parents_are_seeded_in_one_replay_after_all_observations_exist(db, monkeypatch):
    from dataclasses import replace
    worker, engine = _make_worker(db, FakeAdapter())
    await worker.seed_instruments()
    ins = db.get_instruments(enabled_only=True)[0]
    base = _parent_zone(db, ins.id)
    for i in (1, 2):
        db.insert_zone(replace(base, id=None, formed_at=base.formed_at+i, lower=base.lower-i))
    calls = []
    async def calculate(fn, *args, **kwargs):
        if getattr(fn, '__name__', '') == 'replay_observation':
            calls.append((args[0], len(db.list_ltf_observations(instrument_id=ins.id))))
            return LtfTickResult()
        return fn(*args, **kwargs)
    async def load(*args):
        pass
    monkeypatch.setattr(worker, '_calculate', calculate)
    monkeypatch.setattr(worker, '_ltf_load_h1_history', load)
    try:
        await worker._ltf_open_marked_zones(ins)
        assert len(calls) == 1 and calls[0][1] == 3
        observations = db.list_ltf_observations(instrument_id=ins.id)
        assert all(db.get_meta(f'ltf:h1:seeded:{o.id}') == '1' for o in observations)
        await worker._ltf_open_marked_zones(ins)
        assert len(calls) == 1
    finally:
        await worker.data_sync.shutdown()


def test_parallel_restore_and_backfill_keep_replaying_until_both_finish(db):
    worker, _ = _make_worker(db, FakeAdapter())
    worker._set_replaying(1, True)
    worker._set_replaying(1, True)
    worker._set_replaying(1, False)
    assert db.get_meta('replaying:1') == '1'
    worker._set_replaying(1, False)
    assert db.get_meta('replaying:1') == '0'


async def test_htf_background_replay_does_not_hold_other_instrument_calculations(db, monkeypatch):
    worker, _ = _make_worker(db, FakeAdapter())
    entered, release = threading.Event(), threading.Event()
    def replay_instrument(iid):
        entered.set()
        assert release.wait(5)
        return []
    # Bind a normally named scanner method so the worker assigns its key.
    def slow(self, iid):
        return replay_instrument(iid)
    slow.__name__ = 'replay_instrument'
    monkeypatch.setattr(worker.scanner, 'replay_instrument', slow.__get__(worker.scanner))
    pending = asyncio.create_task(worker._calculate(worker.scanner.replay_instrument, 1))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert await asyncio.wait_for(worker._calculate(lambda: 'quote', calculation_instrument=2), 2) == 'quote'
        assert not pending.done()
    finally:
        release.set()
        await pending
        await worker.data_sync.shutdown()


async def test_calculating_new_asset_preserves_other_assets_projection_versions(db):
    from app.config import DetectorConfig
    from app.services.projection_cache import structural_version
    worker, _ = _make_worker(db, FakeAdapter())
    before = [structural_version(db, iid, DetectorConfig()) for iid in (1, 2)]
    def update():
        with db.batch_writes():
            db.bump_domain('seq:ltf')
            db.bump_domain('seq:zones')
    try:
        await worker._calculate(update, calculation_instrument=2)
        after = [structural_version(db, iid, DetectorConfig()) for iid in (1, 2)]
        assert after[0] == before[0]
        assert after[1] != before[1]
        # An API edit without a calculation scope must invalidate both assets.
        update()
        assert all(structural_version(db, iid, DetectorConfig()) != after[iid-1] for iid in (1, 2))
    finally:
        await worker.data_sync.shutdown()
