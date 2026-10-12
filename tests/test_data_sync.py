"""Synchronization acceptance: real engines, deterministic exchange and clock."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import httpx
import pytest

from app.adapters.base import AdapterError, QuoteTick, TIMEFRAME_MS
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Instrument, Zone, ZoneStatus, ZoneType
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.data_sync import DataSync, JOB_KEY, SyncCooldown, period_open, read_json
from app.web.api import create_app
from app.worker import Worker
from .conftest import make_candle

AT = 1_791_806_407_000  # arbitrary fixed clock; period_open determines its grid


class Exchange:
    def __init__(self, clock):
        self.clock = clock
        self.rows = {}
        self.fail = set()
        self.calls = []
        self.current_after = {}
        self.active = 0
        self.max_active = 0

    async def klines(self, symbol, tf, start, end, include_forming=False):
        self.calls.append((symbol, tf, start, end))
        if (symbol, tf) in self.fail:
            raise AdapterError("exchange unavailable")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        count = sum(s == symbol and t == tf for s, t, _, _ in self.calls)
        rows = self.rows[symbol, tf]
        if count < self.current_after.get((symbol, tf), 0):
            rows = rows[:-2]  # source has not published the latest close or current
        return [replace(c) for c in rows if start <= c.open_time <= end and (include_forming or c.closed)]

    async def last_price(self, symbol):
        if (symbol, "quote") in self.fail:
            raise AdapterError("quote unavailable")
        return QuoteTick(100, self.clock[0], self.clock[0], "trade")


@pytest.fixture
def market(monkeypatch, tmp_path):
    clock = [AT]
    monkeypatch.setattr("app.services.data_sync.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.worker.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.engine.scanner.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.engine.ltf.engine._real_now_ms", lambda: clock[0])
    db = Database(":memory:")
    cfg = DetectorConfig()
    cfg.ltf_enabled = True
    settings = Settings()
    settings.detector = cfg
    settings.db_path = str(tmp_path / "test.db")
    settings.auth_token = "test-token"
    exchange = Exchange(clock)
    sender = LogSender()
    engine = LtfEngine(db, cfg)
    worker = Worker(db, settings, cfg, {"binance": exchange}, EventDispatcher(db, cfg, sender), ltf_engine=engine)
    worker.data_sync.ready = True
    instruments = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        iid = db.upsert_instrument(Instrument(None, symbol[:-4], "binance", "spot", symbol, "USDT"))
        ins = db.get_instrument(iid)
        instruments.append(ins)
        for tf in ("H1", "D1", "W1"):
            opening = period_open(tf, clock[0])
            step = TIMEFRAME_MS[tf]
            rows = [make_candle(opening - i * step, 100, 101, 99, 100, tf, iid, source="binance")
                    for i in range(7, 0, -1)]
            rows.append(make_candle(opening, 100, 101, 99, 100, tf, iid, closed=False, source="binance"))
            exchange.rows[symbol, tf] = rows
            db.insert_candles(rows[:3])
    yield db, worker, exchange, clock, instruments, sender
    db.close()


async def finish(worker):
    job = worker.data_sync.start()
    await worker.data_sync._task
    return read_json(worker.db, JOB_KEY)


async def test_manual_backlog_all_channels_and_idempotence(market):
    db, worker, exchange, clock, instruments, sender = market
    first = worker.data_sync.start()
    duplicate = worker.data_sync.start()
    assert first["job_id"] == duplicate["job_id"]
    await worker.data_sync._task
    result = read_json(db, JOB_KEY)
    assert result["status"] == "succeeded", result
    assert result["channels_completed"] == result["channels_total"] == 8
    assert all(c["status"] == "ok" for c in worker.data_sync.status()["channels"])
    for ins in instruments:
        assert len(db.get_candles(ins.id, "H1")) == 7
        assert int(db.get_meta(f"ltf:h1:last_close:{ins.id}")) == period_open("H1", clock[0]) - 1
    with pytest.raises(SyncCooldown):
        worker.data_sync.start()
    before_events = db.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    before_cards = len(sender.cards)
    clock[0] += 16_000
    repeat = await finish(worker)
    assert repeat["status"] == "succeeded"
    assert all(r.get("new_bars", 0) == 0 and r.get("revised_bars", 0) == 0 for r in repeat["results"])
    assert db.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == before_events
    assert len(sender.cards) == before_cards


async def test_manual_quote_touch_delivered_once_across_resynchronizations(market, monkeypatch):
    db, worker, exchange, clock, instruments, sender = market
    ins = instruments[0]
    for module in ("app.notify.queue", "app.notify.outbox", "app.notify.suppress"):
        monkeypatch.setattr(module + ".now_ms", lambda: clock[0])
    zid = db.insert_zone(Zone(None, ins.id, ZoneType.FVG, Direction.BULL, "D1",
                             90, 95, clock[0] - 10 * TIMEFRAME_MS["D1"],
                             clock[0] - 10 * TIMEFRAME_MS["D1"], status=ZoneStatus.ACTIVE))
    # Establish a preceding outside quote, so synchronization discovers a
    # live touch rather than the intentionally quiet "already in zone" fact.
    clock[0] -= 1000
    await worker._poll_quote(ins)
    clock[0] += 1000

    async def price(symbol):
        return QuoteTick(94, clock[0], clock[0], "trade")

    exchange.last_price = price
    job = await finish(worker)
    assert job["status"] == "succeeded", job
    events = db.conn.execute("SELECT COUNT(*) FROM event WHERE zone_id=?", (zid,)).fetchone()[0]
    assert events > 0 and len(sender.cards) > 0, {
        "packets": [dict(r) for r in db.conn.execute("SELECT status,reason,quiet,due_at,created_at FROM notification_packet")],
        "events": [dict(r) for r in db.conn.execute("SELECT kind,delayed FROM event WHERE zone_id=?", (zid,))],
    }
    cards = len(sender.cards)
    clock[0] += 16_000
    await finish(worker)
    assert db.conn.execute("SELECT COUNT(*) FROM event WHERE zone_id=?", (zid,)).fetchone()[0] == events
    assert len(sender.cards) == cards


async def test_partial_failure_missing_period_is_not_success(market):
    db, worker, exchange, clock, instruments, _ = market
    exchange.fail.add(("ETHUSDT", "H1"))
    exchange.current_after["BTCUSDT", "D1"] = 100
    job = await finish(worker)
    assert job["status"] == "partial"
    errors = {(e["symbol"], e["timeframe"]): e["error"] for e in job["errors"]}
    assert ("ETHUSDT", "H1") in errors
    assert errors["BTCUSDT", "D1"] == "missing_close"
    status = worker.data_sync.channel_status(instruments[0], "D1")
    assert status["last_success_at"] and status["status"] == "lagging"
    assert db.get_meta(f"stale:{instruments[1].id}:H1") == "1"


async def test_internal_h1_hole_is_filled_and_processed(market):
    db, worker, exchange, clock, instruments, _ = market
    ins = instruments[0]
    rows = exchange.rows[ins.symbol, "H1"]
    db.insert_candles([c for i, c in enumerate(rows) if i != 4])
    db.set_meta(f"ltf:h1:last_close:{ins.id}", str(rows[-2].close_time))
    status = await worker.data_sync.sync_candles(ins, "H1")
    assert status["status"] == "ok", status
    assert not status["gaps"]
    assert status["processed_close"] == rows[-2].close_time
    assert exchange.calls[0][2] <= rows[4].open_time


async def test_h1_revision_is_processed_without_new_closes(market):
    db, worker, exchange, _, instruments, _ = market
    ins = instruments[0]
    await worker.data_sync.sync_candles(ins, "H1")
    exchange.rows[ins.symbol, "H1"][-2].high = 102
    status = await worker.data_sync.sync_candles(ins, "H1")
    assert status["revised_bars"] == 1
    assert status["status"] == "ok"
    assert int(db.get_meta(f"ltf:h1:revision_done:{ins.id}")) >= db.latest_candle_revision_at(ins.id, "H1")
    assert (await worker.data_sync.sync_candles(ins, "H1"))["revised_bars"] == 0


async def test_boundary_retries_until_third_publication(market):
    _, worker, exchange, clock, instruments, _ = market
    sync = worker.data_sync
    for ins in instruments:
        for tf in ("D1", "W1"):
            sync._boundary_due[ins.id, tf] = {"opening": period_open(tf, clock[0]), "attempt": 4, "due": 0}
    ins = instruments[0]
    exchange.current_after[ins.symbol, "H1"] = 3
    await sync.boundary_tick(clock[0])
    state = sync._boundary_due[ins.id, "H1"]
    assert state["attempt"] == 1
    assert sync.channel_status(ins, "H1")["status"] == "lagging"
    clock[0] = state["due"]
    await sync.boundary_tick(clock[0])
    assert state["attempt"] == 2
    clock[0] = state["due"]
    await sync.boundary_tick(clock[0])
    assert state["attempt"] == 4
    assert sync.channel_status(ins, "H1")["status"] == "ok"


@pytest.mark.parametrize("tf", ["H1", "D1", "W1"])
async def test_first_boundary_request_is_five_seconds_after_close(market, tf):
    _, worker, exchange, clock, instruments, _ = market
    clock[0] = period_open(tf, clock[0]) + 4999
    for ins in instruments:
        for other in ("H1", "D1", "W1"):
            if ins == instruments[0] and other == tf:
                continue
            worker.data_sync._boundary_due[ins.id, other] = {
                "opening": period_open(other, clock[0]), "attempt": 4, "due": 0}
    await worker.data_sync.boundary_tick(clock[0])
    assert not exchange.calls
    clock[0] += 1
    await worker.data_sync.boundary_tick(clock[0])
    assert len(exchange.calls) == 1
    assert exchange.calls[0][:2] == (instruments[0].symbol, tf)


@pytest.mark.parametrize("tf", ["H1", "D1", "W1"])
def test_period_boundary_uses_observed_source_calendar(tf):
    anchor = 1234567
    step = TIMEFRAME_MS[tf]
    assert period_open(tf, anchor + 3 * step + 5000, anchor) == anchor + 3 * step
    assert period_open(tf, anchor + step - 1, anchor) == anchor


async def test_manual_and_periodic_do_not_overlap(market):
    _, worker, exchange, _, instruments, _ = market
    job = worker.data_sync.start()
    await asyncio.gather(worker.data_sync._task, worker._poll_instrument(instruments[0]), worker._poll_ltf_instrument(instruments[0]))
    assert read_json(worker.db, JOB_KEY)["job_id"] == job["job_id"]
    assert exchange.max_active == 1


async def test_deadline_keeps_ownership_until_thread_finishes(market):
    _, worker, _, _, instruments, _ = market
    sync = worker.data_sync
    sync.job_timeout_seconds = .05
    entered = threading.Event()
    release = threading.Event()
    original = worker._process_synced_channel

    def blocking():
        entered.set()
        release.wait(2)

    async def slow(ins, tf, incoming, before):
        await worker._calculate(blocking)
        return await original(ins, tf, incoming, before)

    worker._process_synced_channel = slow
    job = sync.start()
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        await asyncio.sleep(.07)
        status = read_json(worker.db, JOB_KEY)
        assert status["status"] == "partial" and status["processing_continues"]
        assert status["results"][0]["timeframe"] == "quote"
        assert status["results"][0]["status"] == "ok"
        assert sync.start()["job_id"] == job["job_id"]
        assert sync._locks[instruments[0].id].locked()
    finally:
        release.set()
        await sync._task
    assert not read_json(worker.db, JOB_KEY)["processing_continues"]
    assert not sync._locks[instruments[0].id].locked()


async def test_restart_marks_interrupted_job(market):
    db, worker, _, _, _, _ = market
    db.set_meta(JOB_KEY, '{"job_id":"interrupted","status":"running","results":[]}')
    DataSync(worker).recover_interrupted()
    assert read_json(db, JOB_KEY)["error"] == "interrupted_by_restart"
    assert read_json(db, "data_sync:job:interrupted")["status"] == "failed"


async def test_api_auth_availability_status_and_job(market):
    db, worker, _, _, _, _ = market
    app = create_app(db, worker.settings, ltf_engine=worker.ltf_engine)
    auth = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/data-sync")).status_code == 401
        assert (await client.post("/api/data-sync", headers=auth)).status_code == 503
        app.state.data_sync = worker.data_sync
        started = await client.post("/api/data-sync", headers=auth)
        duplicate = await client.post("/api/data-sync", headers=auth)
        assert started.status_code == 202
        assert duplicate.json()["job_id"] == started.json()["job_id"]
        await worker.data_sync._task
        status = await client.get("/api/data-sync/status", headers=auth)
        assert status.json()["available"] and len(status.json()["channels"]) == 8
        job = await client.get("/api/data-sync/" + started.json()["job_id"], headers=auth)
        assert job.json()["status"] == "succeeded"
        assert (await client.post("/api/data-sync", headers=auth)).status_code == 429
        assert (await client.get("/api/data-sync/missing", headers=auth)).status_code == 404
        candles = await client.get(f"/api/candles?instrument_id=1&timeframe=H1&with_version=true", headers=auth)
        assert len(candles.json()["candles"]) == 8
        assert candles.json()["state_version"] == db.get_state_seq()


async def test_disabled_instruments_are_excluded(market):
    db, worker, exchange, _, instruments, _ = market
    ins = instruments[1]
    db.upsert_instrument(replace(ins, enabled=False))
    job = await finish(worker)
    assert job["channels_total"] == 4
    assert all(s != ins.symbol for s, _, _, _ in exchange.calls)


async def test_processing_error_and_retry_do_not_skip_persisted_closes(market):
    db, worker, _, _, instruments, _ = market
    ins = instruments[0]
    original = worker._process_synced_channel

    async def failed(*args):
        raise RuntimeError("calculation_failed")

    worker._process_synced_channel = failed
    with pytest.raises(RuntimeError):
        await worker.data_sync.sync_candles(ins, "D1")
    status = worker.data_sync.channel_status(ins, "D1")
    assert status["reason"] == "processing_lag"
    assert status["received_close"] > status["processed_close"]
    worker._process_synced_channel = original
    status = await worker.data_sync.sync_candles(ins, "D1")
    assert status["status"] == "ok" and status["received_close"] == status["processed_close"]
    assert db.get_meta(f"replaying:{ins.id}") == "0"


async def test_exchange_retry_after_is_honored_and_bounded(market):
    _, worker, _, _, _, _ = market
    calls = []

    async def throttled():
        import time
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise AdapterError("throttled", retry_after=.02)
        return "ok"

    assert await worker.data_sync.request("binance", throttled) == "ok"
    assert calls[1] - calls[0] >= .02
    calls.clear()

    async def persistent():
        calls.append(True)
        raise AdapterError("throttled", retry_after=.001)

    with pytest.raises(AdapterError):
        await worker.data_sync.request("binance", persistent)
    assert len(calls) == 2


async def test_recovered_freshness_is_visible_before_delivery(market):
    db, worker, _, _, instruments, _ = market
    ins = instruments[0]
    db.set_meta(f"stale:{ins.id}:H1", "1")
    delivered = []
    original = worker._deliver_synced_channel

    async def check(ins, tf, result):
        delivered.append(db.get_meta(f"stale:{ins.id}:{tf}"))
        await original(ins, tf, result)

    worker._deliver_synced_channel = check
    await worker.data_sync.sync_candles(ins, "H1")
    assert delivered == ["0"]


async def test_shutdown_finishes_job_and_clears_processing_marker(market):
    db, worker, exchange, _, instruments, _ = market
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    exchange.klines = pending
    worker.data_sync.start()
    await entered.wait()
    await worker.data_sync.shutdown()
    job = read_json(db, JOB_KEY)
    assert job["status"] == "partial" and not job["processing_continues"]
    assert job["results"][0]["timeframe"] == "quote"
    assert db.get_quote_detail(instruments[0].id) is not None
    assert job["results"][1]["error"] == "interrupted_by_shutdown"
    assert not read_json(db, f"data_sync:channel:{instruments[0].id}:D1")["processing"]
    assert db.get_meta(f"replaying:{instruments[0].id}") == "0"
