"""Worker-owned OHLC reconciliation and durable, single-flight manual jobs."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from functools import wraps

from ..adapters.base import AdapterError, TIMEFRAME_MS
from ..models import now_ms

log = logging.getLogger(__name__)
ACTIVE = {"queued", "running"}
JOB_KEY = "data_sync:last_job"


def instrument_owned(fn):
    """Nested calls in the same task retain instrument ownership."""
    @wraps(fn)
    async def wrapped(worker, ins, *args, **kwargs):
        async with worker.data_sync.instrument(ins.id):
            return await fn(worker, ins, *args, **kwargs)
    return wrapped


def period_open(timeframe, at, anchor=None):
    # Source observations take precedence over the default UTC calendar.
    # Binance/Bybit weekly bars start on Monday (Unix epoch was Thursday).
    step = TIMEFRAME_MS[timeframe]
    origin = anchor if anchor is not None else (4 * 86_400_000 if timeframe == "W1" else 0)
    return origin + ((at - origin) // step) * step


def read_json(db, key, default=None):
    try:
        return json.loads(db.get_meta(key) or "null") or default
    except (ValueError, TypeError):
        return default


class SyncUnavailable(Exception):
    pass


class SyncCooldown(Exception):
    def __init__(self, next_at):
        self.next_at = next_at


class DataSync:
    def __init__(self, worker):
        self.worker = worker
        self.db = worker.db
        self.ready = False
        self._locks = {}
        self._owners = {}
        self._calculation_lock = asyncio.Lock()
        self._task = None
        self.job_timeout_seconds = 600
        self.request_timeout_seconds = 90
        self.cooldown_ms = 15_000
        self._boundary_due = {}
        self._request_limits = {}
        self._retry_until = {}

    def recover_interrupted(self):
        """Call only after the worker has acquired calculation ownership."""
        previous = read_json(self.db, JOB_KEY)
        if previous and previous.get("status") in ACTIVE:
            previous.update(status="failed", finished_at=now_ms(), error="interrupted_by_restart")
            for channel in previous.get("results", []):
                if channel.get("status") in ACTIVE | {"processing"}:
                    channel.update(status="failed", error="interrupted_by_restart")
            self._save_job(previous)

    async def shutdown(self):
        self.ready = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @asynccontextmanager
    async def instrument(self, iid):
        task = asyncio.current_task()
        if self._owners.get(iid) is task:
            yield
            return
        lock = self._locks.setdefault(iid, asyncio.Lock())
        async with lock:
            self._owners[iid] = task
            try:
                yield
            finally:
                self._owners.pop(iid, None)

    async def calculate(self, fn, *args, **kwargs):
        # LtfEngine has a shared batch, including across instruments.
        # Cancellation of an await does not terminate a Python thread.
        async with self._calculation_lock:
            task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task  # retain both locks until the actual calculation ends
                raise

    async def request(self, venue, fn, *args, **kwargs):
        limit = self._request_limits.setdefault(venue, asyncio.Semaphore(2))
        for attempt in range(2):
            async with limit:
                delay = self._retry_until.get(venue, 0) - time.monotonic()
                if delay > self.request_timeout_seconds:
                    raise AdapterError("rate_limited: ожидание разрешения биржи", retry_after=delay)
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.worker._stop.wait(), delay)
                        raise SyncUnavailable("worker_stopping")
                    except asyncio.TimeoutError:
                        pass
                try:
                    return await asyncio.wait_for(fn(*args, **kwargs), self.request_timeout_seconds)
                except AdapterError as exc:
                    if exc.retry_after is None:
                        raise
                    self._retry_until[venue] = max(self._retry_until.get(venue, 0),
                                                  time.monotonic() + exc.retry_after)
                    if attempt:
                        raise

    def _save_job(self, job):
        self.db.set_meta(JOB_KEY, json.dumps(job, ensure_ascii=False))
        self.db.set_meta(f"data_sync:job:{job['job_id']}", json.dumps(job, ensure_ascii=False))
        if self.worker.broadcast:
            self.worker.broadcast({"type": "data_sync", **job})

    def _key(self, ins, tf):
        return f"data_sync:channel:{ins.id}:{tf}"

    def _save_channel(self, ins, tf, result):
        self.db.set_meta(self._key(ins, tf), json.dumps(result, ensure_ascii=False))

    def channel_status(self, ins, tf, at=None):
        at = now_ms() if at is None else at
        result = dict(read_json(self.db, self._key(ins, tf), {}) or {})
        result.update(instrument_id=ins.id, symbol=ins.symbol, venue=ins.venue, timeframe=tf)
        if tf == "quote":
            from .quality import _quote_channel
            quote = _quote_channel(self.db, self.worker.settings, ins.id, at)
            ready = quote["status"] == "ok" and not result.get("error") and result.get("last_success_at")
            result.update(status="ok" if ready else "lagging",
                          quote_at=quote["last_at"], reason=result.get("error") or quote.get("reason") or (None if ready else "sync_stale"))
            return result
        last = self.db.last_candle(ins.id, tf)
        head = self.db.last_candle(ins.id, tf, closed_only=False)
        current_open = period_open(tf, at, head.open_time if head else None)
        rows = self.db.get_candles(ins.id, tf)[-200:]
        gaps = [a.open_time + TIMEFRAME_MS[tf] for a, b in zip(rows, rows[1:])
                if b.open_time - a.open_time > TIMEFRAME_MS[tf]]
        processed = int(self.db.get_meta(
            f"ltf:h1:last_close:{ins.id}" if tf == "H1" else f"data_sync:processed:{ins.id}:{tf}") or 0)
        scenario = int(self.db.get_meta(f"ltf:h1:scenario_last_close:{ins.id}") or 0) if tf == "H1" else None
        has_observations = tf == "H1" and any(o.state in ("active", "waiting_structure", "paused_data")
            for o in self.db.list_ltf_observations(instrument_id=ins.id))
        reason = None
        if result.get("processing"):
            reason = "processing_lag"
        elif result.get("error"):
            reason = "processing_lag" if result.get("error_stage") == "processing" else "source_error"
        elif not last or last.close_time < current_open - 1:
            reason = "missing_close"
        elif not head or head.closed or head.open_time != current_open:
            reason = "missing_current"
        elif gaps:
            reason = "history_gap"
        elif processed < last.close_time or (has_observations and scenario < last.close_time):
            reason = "processing_lag"
        elif not result.get("last_success_at") or at - result["last_success_at"] > (
                max(60, self.worker.cfg.ltf_poll_seconds * 2) * 1000 if tf == "H1"
                else max(60, self.worker.settings.poll_seconds * 2) * 1000):
            reason = "sync_stale"
        result.update(status="ok" if reason is None else "lagging", reason=reason,
                      expected_close=current_open - 1, received_close=last.close_time if last else None,
                      processed_close=processed or None, scenario_processed_close=scenario,
                      current_open=current_open, has_current=bool(head and not head.closed and head.open_time == current_open),
                      gaps=gaps)
        return result

    def status(self):
        channels = [self.channel_status(ins, tf) for ins in self.db.get_instruments(enabled_only=True)
                    for tf in (*dict.fromkeys(("H1", *self.worker.scan_tfs)), "quote")]
        return {"available": self.ready and not self.worker._stop.is_set(),
                "job": read_json(self.db, JOB_KEY), "channels": channels,
                "instruments": [{"instrument_id": ins.id,
                    "last_full_success_at": int(self.db.get_meta(f"data_sync:instrument_success:{ins.id}") or 0) or None,
                    "ready": all(c["status"] == "ok" for c in channels if c["instrument_id"] == ins.id)}
                    for ins in self.db.get_instruments(enabled_only=True)],
                "state_version": self.db.get_state_seq()}

    def mark_instrument_success(self, ins):
        channels = [self.channel_status(ins, tf) for tf in (*dict.fromkeys(("H1", *self.worker.scan_tfs)), "quote")]
        if all(c["status"] == "ok" for c in channels):
            self.db.set_meta(f"data_sync:instrument_success:{ins.id}", str(min(c["last_success_at"] for c in channels)))

    def start(self, reason="manual"):
        if not self.ready or self.worker._stop.is_set():
            raise SyncUnavailable()
        previous = read_json(self.db, JOB_KEY)
        if self._task and not self._task.done():
            return previous
        if previous and previous.get("finished_at", 0) + self.cooldown_ms > now_ms():
            raise SyncCooldown(previous["finished_at"] + self.cooldown_ms)
        instruments = self.db.get_instruments(enabled_only=True)
        # Reconcile parent context before evaluating the H1 scenarios.
        tfs = tuple(dict.fromkeys((*self.worker.scan_tfs, "H1")))
        job = {"job_id": str(uuid.uuid4()), "reason": reason, "status": "queued",
               "requested_at": now_ms(), "started_at": None, "finished_at": None,
               "channels_total": len(instruments) * (len(tfs) + 1), "channels_completed": 0,
               "results": [], "errors": []}
        self._save_job(job)
        self._task = asyncio.create_task(self._run_job(job, instruments, tfs))
        return job

    async def _run_job(self, job, instruments, tfs):
        job.update(status="running", started_at=now_ms())
        self._save_job(job)
        deadline = time.monotonic() + self.job_timeout_seconds

        async def watchdog():
            await asyncio.sleep(self.job_timeout_seconds)
            if job["status"] in ACTIVE:
                job["deadline_exceeded"] = True
                job["processing_continues"] = True
                job["status"] = "partial" if any(r.get("status") == "ok" for r in job["results"]) else "failed"
                job["finished_at"] = now_ms()
                job["errors"].append({"error": "deadline_exceeded_processing_continues"})
                for result in job["results"]:
                    if result.get("status") == "processing":
                        result["error"] = "deadline_exceeded_processing_continues"
                self._save_job(job)

        watcher = asyncio.create_task(watchdog())
        try:
            for ins in instruments:
                for tf in ("quote", *tfs):
                    result = {"instrument_id": ins.id, "symbol": ins.symbol, "venue": ins.venue,
                              "timeframe": tf, "status": "processing"}
                    job["results"].append(result)
                    self._save_job(job)
                    try:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("deadline_exceeded")
                        async with self.instrument(ins.id):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("deadline_exceeded")
                            if tf == "quote":
                                events = await self.worker._poll_quote(ins)
                                await self.worker._dispatch_events(events)
                                await self.worker._ltf_on_poll(ins, events)
                                result.update(self.channel_status(ins, tf))
                            else:
                                result.update(await self.sync_candles(ins, tf, job["reason"], job["job_id"]))
                        if result["status"] != "ok":
                            result["error"] = result.get("reason") or "channel_not_ready"
                    except Exception as exc:
                        result.update(status="failed", error=str(exc)[:300] or type(exc).__name__)
                        log.exception("data_sync job=%s symbol=%s tf=%s", job["job_id"], ins.symbol, tf)
                    if result.get("error"):
                        job["errors"].append({"instrument_id": ins.id, "symbol": ins.symbol,
                                              "timeframe": tf, "error": result["error"]})
                    job["channels_completed"] += 1
                    self._save_job(job)
                self.mark_instrument_success(ins)
        except asyncio.CancelledError:
            job["errors"].append({"error": "interrupted_by_shutdown"})
            for result in job["results"]:
                if result.get("status") == "processing":
                    result.update(status="failed", error="interrupted_by_shutdown")
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            good = sum(r.get("status") == "ok" for r in job["results"])
            job.update(status="succeeded" if not job["errors"] else "partial" if good else "failed",
                       finished_at=now_ms(), processing_continues=False)
            self._save_job(job)

    async def sync_candles(self, ins, tf, reason="periodic", job_id=None):
        async with self.instrument(ins.id):
            started = time.monotonic()
            result = dict(read_json(self.db, self._key(ins, tf), {}) or {})
            result.update(last_attempt_at=now_ms(), error=None, processing=True,
                          error_stage=None, new_bars=0, revised_bars=0, reason=reason)
            self._save_channel(ins, tf, result)
            was_replaying = self.db.get_meta(f"replaying:{ins.id}") == "1"
            self.worker._set_replaying(ins.id, True)
            try:
                rows = self.db.get_candles(ins.id, tf)
                step = TIMEFRAME_MS[tf]
                start = rows[-2].open_time if len(rows) >= 2 else rows[-1].open_time if rows else now_ms() - (
                    self.worker.cfg.ltf_history_days if tf == "H1" else self.worker.cfg.lookback_days_for(tf)) * 86_400_000
                # Extend the one paginated request over any internal tail gaps.
                tail = rows[-200:]
                for a, b in zip(tail, tail[1:]):
                    if b.open_time - a.open_time > step:
                        start = min(start, a.open_time + step)
                result["requested_from"] = start
                end = now_ms()
                result["requested_to"] = end
                request_started = time.monotonic()
                candles = await self.request(ins.venue, self.worker.adapters[ins.venue].klines,
                    ins.symbol, tf, start, end, include_forming=True)
                result["request_duration_s"] = round(time.monotonic() - request_started, 3)
                result["last_success_at"] = now_ms()
                result["error_stage"] = "processing"
                before = {c.open_time: c for c in rows}
                incoming = sorted(candles, key=lambda c: c.open_time)
                for c in incoming:
                    if c.timeframe != tf or c.source not in (ins.venue, f"{ins.venue}-spot", "test"):
                        raise ValueError("unexpected_candle_source_or_timeframe")
                    c.instrument_id = ins.id
                result["new_bars"] = sum(c.closed and c.open_time not in before for c in incoming)
                result["revised_bars"] = sum(c.closed and c.open_time in before and before[c.open_time].closed and
                    (c.open, c.high, c.low, c.close) != (before[c.open_time].open, before[c.open_time].high,
                                                      before[c.open_time].low, before[c.open_time].close) for c in incoming)
                # Record the old processed tail before any insertion so a
                # restart between persistence and calculation cannot skip it.
                processed_key = f"data_sync:processed:{ins.id}:{tf}"
                if tf != "H1" and self.db.get_meta(processed_key) is None:
                    self.db.set_meta(processed_key, str(max((c.close_time for c in rows), default=0)))
                self.db.insert_candles(incoming)
                if tf == "H1":
                    # An inserted hole behind the H1 cursor is not an OHLC
                    # revision. Explicitly rewind to make the engine see it.
                    key = f"ltf:h1:last_close:{ins.id}"
                    cursor = int(self.db.get_meta(key) or 0)
                    missed = [c for c in incoming if c.closed and c.open_time not in before and c.close_time <= cursor]
                    if missed:
                        earliest = min(c.open_time for c in missed)
                        previous = max((c.close_time for c in rows if c.open_time < earliest), default=0)
                        self.db.set_meta(key, str(previous))
                self._save_channel(ins, tf, result)
                processing_started = time.monotonic()
                processed_result = await self.worker._process_synced_channel(ins, tf, incoming, before)
                result["processing_duration_s"] = round(time.monotonic() - processing_started, 3)
                result.update(processing=False, error=None, error_stage=None, finished_at=now_ms())
                self._save_channel(ins, tf, result)
                self.worker._set_replaying(ins.id, was_replaying)
                status = self.channel_status(ins, tf)
                await self.worker._check_freshness(ins, tf)
                self.db.set_meta(f"stale:{ins.id}:{tf}", "0" if status["status"] == "ok" else "1")
                # Delivery must see recovered freshness, not the preceding
                # failed poll's stale marker.
                await self.worker._deliver_synced_channel(ins, tf, processed_result)
                self.mark_instrument_success(ins)
                if self.worker.broadcast:
                    self.worker.broadcast({"type": "candle", "instrument_id": ins.id, "timeframe": tf})
                log.info("data_sync reason=%s job=%s symbol=%s tf=%s from=%s to=%s new=%s revised=%s request_s=%s processing_s=%s duration=%.3f status=%s processed_close=%s",
                         reason, job_id, ins.symbol, tf, start, end, result["new_bars"], result["revised_bars"],
                         result["request_duration_s"], result["processing_duration_s"],
                         time.monotonic() - started, status["status"], status["processed_close"])
                return status
            except asyncio.CancelledError:
                result.update(processing=False, error="interrupted_by_shutdown", last_error_at=now_ms())
                self._save_channel(ins, tf, result)
                self.db.set_meta(f"stale:{ins.id}:{tf}", "1")
                raise
            except Exception as exc:
                if not result.get("error_stage"):
                    result["error_stage"] = "source"
                result.update(processing=False, error=str(exc)[:300] or type(exc).__name__, last_error_at=now_ms())
                self._save_channel(ins, tf, result)
                self.db.set_meta(f"stale:{ins.id}:{tf}", "1")
                raise
            finally:
                self.worker._set_replaying(ins.id, was_replaying)

    async def boundary_tick(self, at=None):
        at = now_ms() if at is None else at
        for ins in self.db.get_instruments(enabled_only=True):
            for tf in dict.fromkeys((*self.worker.scan_tfs, "H1")):
                head = self.db.last_candle(ins.id, tf, closed_only=False)
                opening = period_open(tf, at, head.open_time if head else None)
                key = (ins.id, tf)
                state = self._boundary_due.get(key)
                if state is None or state["opening"] != opening:
                    state = {"opening": opening, "attempt": 0, "due": opening + 5000}
                    self._boundary_due[key] = state
                if state["attempt"] >= 4 or at < state["due"]:
                    continue
                try:
                    status = await self.sync_candles(ins, tf, "boundary")
                    done = status["received_close"] is not None and status["received_close"] >= status["expected_close"] and status["has_current"]
                except Exception:
                    log.exception("boundary sync %s %s", ins.symbol, tf)
                    done = False
                attempt = state["attempt"] + 1
                state["attempt"] = 4 if done else attempt
                state["due"] = max(now_ms(), at) + (15, 30, 60, 0)[attempt - 1] * 1000

    async def boundary_loop(self):
        while not self.worker._stop.is_set():
            try:
                await self.boundary_tick()
            except Exception:
                log.exception("boundary scheduler failed; retrying next tick")
            try:
                await asyncio.wait_for(self.worker._stop.wait(), 1)
            except asyncio.TimeoutError:
                pass
