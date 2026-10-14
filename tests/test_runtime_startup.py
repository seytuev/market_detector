"""A persisted lease must delay a role, not shut down the web service."""
import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.main import run_bot_polling
from app.services import runtime
from .test_worker import FakeAdapter, _make_worker


async def test_seed_waits_for_sqlite_writer_without_stopping_event_loop(db):
    import threading
    from app.models import Instrument

    legacy = db.upsert_instrument(Instrument(None, "HYPE", "binance", "spot", "HYPEUSDT", "USDT"))
    worker, _ = _make_worker(db, FakeAdapter())
    held = threading.Event()
    release = threading.Event()

    def writer():
        with db.batch_writes():
            db.set_meta("test:startup_writer", "held")
            held.set()
            assert release.wait(3)

    writing = asyncio.create_task(asyncio.to_thread(writer))
    await asyncio.wait_for(asyncio.to_thread(held.wait), 2)
    seeding = asyncio.create_task(worker.seed_instruments())
    try:
        await asyncio.sleep(0.03)
        assert not seeding.done(), "seed must wait for the writer, rather than fail with SQLITE_BUSY"
        release.set()
        await asyncio.wait_for(seeding, 2)
        assert not db.get_instrument(legacy).enabled
    finally:
        release.set()
        await writing
        await asyncio.gather(seeding, return_exceptions=True)


@pytest.mark.parametrize("handoff", ["expiry", "release"])
async def test_wait_for_owner_preserves_active_lease_then_takes_over(db, monkeypatch, handoff):
    stamp = 1_000_000
    monkeypatch.setattr(runtime, "now_ms", lambda: stamp)
    old = runtime.claim_owner(db, runtime.CALC_OWNER, {"instance_id": "old", "pid": 1})["owner"]
    attempted = asyncio.Event()
    original = runtime.claim_owner

    loop = asyncio.get_running_loop()

    def observed_claim(*args, **kwargs):
        result = original(*args, **kwargs)
        loop.call_soon_threadsafe(attempted.set)
        return result

    monkeypatch.setattr(runtime, "claim_owner", observed_claim)
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.wait_for_owner(
        db, runtime.CALC_OWNER, {"instance_id": "new", "pid": 1}, stop,
        retry_seconds=0.01,
    ))
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        assert not task.done()
        runtime.assert_owner(db, runtime.CALC_OWNER, old)
        if handoff == "expiry":
            stamp = old["expires_at"]
        else:
            runtime.release_owner(db, runtime.CALC_OWNER, old)
        new = await asyncio.wait_for(task, 2)
        assert new["instance_id"] == "new"
        assert new["owner_generation"] == old["owner_generation"] + 1
        with pytest.raises(RuntimeError, match="owner_lease_lost"):
            runtime.assert_owner(db, runtime.CALC_OWNER, old)
    finally:
        stop.set()
        await task


async def test_worker_conflict_waits_until_shutdown_without_starting_scanner(db):
    old = runtime.claim_owner(db, runtime.CALC_OWNER, {"instance_id": "old", "pid": os.getpid()})["owner"]
    worker, _ = _make_worker(db, FakeAdapter())
    worker.instance_id = "new"
    task = asyncio.create_task(worker.run())
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        assert worker.readiness.phase == "starting"
        runtime.assert_owner(db, runtime.CALC_OWNER, old)
        worker.stop()
        await asyncio.wait_for(task, 2)
        runtime.assert_owner(db, runtime.CALC_OWNER, old)
    finally:
        worker.stop()
        await task


async def test_telegram_conflict_waits_without_starting_polling(db, monkeypatch):
    old = runtime.claim_owner(db, runtime.TELEGRAM_OWNER, {"instance_id": "old", "pid": 1})["owner"]
    db.runtime_instance_id = "new"
    db.runtime_stop = asyncio.Event()
    application = AsyncMock()
    monkeypatch.setattr("app.main.build_application", lambda *args: application)
    task = asyncio.create_task(run_bot_polling(Settings(), db))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.1)
        application.updater.start_polling.assert_not_called()
        db.runtime_stop.set()
        await asyncio.wait_for(task, 2)
        runtime.assert_owner(db, runtime.TELEGRAM_OWNER, old)
    finally:
        db.runtime_stop.set()
        await task


async def test_telegram_initialization_failure_releases_lease(db, monkeypatch):
    application = AsyncMock()
    application.add_error_handler = lambda *args: None
    application.__aenter__.side_effect = RuntimeError("init failed")
    monkeypatch.setattr("app.main.build_application", lambda *args: application)
    with pytest.raises(RuntimeError, match="init failed"):
        await run_bot_polling(Settings(), db)
    assert runtime._read_claim(db, runtime.TELEGRAM_OWNER)["expires_at"] == 0
