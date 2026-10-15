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
from .metrics import get_registry

log = logging.getLogger(__name__)
ACTIVE = {"queued", "running"}
JOB_KEY = "data_sync:last_job"

# Метка task для calc_*: только известные точки расчёта, остальное — other
_CALC_TASKS = {
    "on_price", "on_closed_candle", "replay_instrument", "process_h1_close",
    "process_candle_revisions", "on_htf_zone_touched", "check_parent_validity",
    "replay_observation",
}


def _calc_task(fn) -> str:
    name = getattr(fn, "__name__", type(fn).__name__)
    return name if name in _CALC_TASKS else "other"


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
        self._instrument_calculations = {}
        self._task = None
        self.job_timeout_seconds = 600
        self.request_timeout_seconds = 90
        self.cooldown_ms = 15_000
        self._boundary_due = {}
        self._request_limits = {}
        self._retry_until = {}
        # Этап 5 (§10): single-flight по каналу — параллельные вызовы
        # sync_candles(iid, tf) делят один запрос к источнику
        self._inflight_syncs: dict[tuple[int, str], asyncio.Future] = {}
        # Инструменты, отвергнутые источником (invalid symbol, §10):
        # пропускаются до следующего ручного job/рестарта процесса
        self._invalid_instruments: set[int] = set()
        # Единый поток-писатель для _write (FIFO-порядок записей канала).
        # Создаётся СРАЗУ: ленивая инициализация из двух конкурентных
        # корутин могла бы породить два исполнителя и разнести записи одного
        # канала по разным потокам (переупорядочивание по busy-ожиданию).
        # Поток создаётся при первой задаче — пустой исполнитель бесплатен.
        from concurrent.futures import ThreadPoolExecutor

        self._write_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ds-write")

    def recover_interrupted(self):
        """Call only after the worker has acquired calculation ownership."""
        # A crash can leave these markers outside a manually started job.
        # The old owner is gone; retaining replaying=1 would make every
        # subsequent successful sync restore the interrupted replay flag.
        for key in self.db.meta_prefix("replaying:"):
            if self.db.get_meta(key) == "1":
                self.db.set_meta(key, "0")
        for key in self.db.meta_prefix("data_sync:channel:"):
            channel = read_json(self.db, key)
            if channel and channel.get("processing"):
                channel.update(processing=False, error="interrupted_by_restart",
                               error_stage="processing", last_error_at=now_ms())
                self.db.set_meta(key, json.dumps(channel, ensure_ascii=False))
        previous = read_json(self.db, JOB_KEY)
        if previous and (previous.get("status") in ACTIVE or previous.get("processing_continues")):
            previous.update(status="failed", finished_at=now_ms(), error="interrupted_by_restart",
                            processing_continues=False)
            for channel in previous.get("results", []):
                if channel.get("status") in ACTIVE | {"processing", "pending"}:
                    channel.update(status="failed", error="interrupted_by_restart")
            for stage in previous.get("stages", []):
                if stage.get("status") not in ("completed", "failed"):
                    stage.update(status="failed", finished_at=now_ms())
            self._save_job(previous)

    async def shutdown(self):
        self.ready = False
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        # дождаться уборки идущих sync (single-flight): замки инструментов и
        # channel-маркеры processing освобождаются в их обработчиках отмены
        inflight = [f for f in self._inflight_syncs.values() if not f.done()]
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        self._write_executor.shutdown(wait=True)

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

    async def calculate(self, fn, *args, instrument_id=None, **kwargs):
        # Worker supplies an isolated engine for a keyed calculation. Work on
        # one instrument remains serialized; cancellation retains ownership
        # until the actual thread finishes. Unkeyed legacy work keeps its lock.
        metrics = get_registry()
        task_name = _calc_task(fn)
        wait_started = time.monotonic()
        lock = self._calculation_lock if instrument_id is None else self._instrument_calculations.setdefault(instrument_id, asyncio.Lock())
        async with lock:
            metrics.histogram("calc_lock_wait", time.monotonic() - wait_started, {"task": task_name})
            metrics.gauge("calc_lock_held", 1.0)
            task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
            calc_started = time.monotonic()
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task  # retain both locks until the actual calculation ends
                raise
            finally:
                metrics.histogram("calc_duration", time.monotonic() - calc_started, {"task": task_name})
                metrics.gauge("calc_lock_held", 0.0)

    async def _write(self, fn, *args, **kwargs):
        """Запись в БД из asyncio-кода, которая может столкнуться с потоковым
        расчётом (он держит блокировку писателя SQLite). Контракт storage:
        async-писатели идут через поток — у потокового соединения
        busy_timeout 15 с (ждёт), у соединения цикла событий — 0 (мгновенный
        SQLITE_BUSY). Все такие записи идут через ОДИН поток-писатель:
        FIFO-порядок сохраняется (запись «processing=True» не должна
        обогнать «processing=False» того же канала из-за busy-ожидания на
        разных потоках пула)."""
        import functools

        return await asyncio.get_running_loop().run_in_executor(
            self._write_executor, functools.partial(fn, *args, **kwargs))

    # Binance Spot: минутный вес 6000; предупреждение заранее (80%),
    # троттлинг в этом этапе не меняется — только наблюдение (§10)
    _WEIGHT_WARN = 4800

    def _note_weight(self, metrics, venue, fn):
        """Зафиксировать вес лимита источника, если адаптер его захватывает
        (Binance: X-MBX-USED-WEIGHT-1M, см. BinanceSpotAdapter.last_weight)."""
        adapter = getattr(fn, "__self__", None)
        weight = getattr(adapter, "last_weight", None)
        if weight is None:
            return
        metrics.gauge("rest_weight_used", weight, {"venue": venue})
        if weight > self._WEIGHT_WARN:
            log.warning(
                "%s: использовано %s веса REST за минуту (>80%% от 6000) — "
                "приближаемся к лимиту источника", venue, weight)

    async def request(self, venue, fn, *args, **kwargs):
        metrics = get_registry()
        endpoint = getattr(fn, "__name__", type(fn).__name__)
        # §10 (этап 5): ограниченная параллельность REST — настройка
        # rest_concurrency на venue; весь сетевой путь идёт через этот метод
        # (общий Retry-After venue не обойти через другую очередь, AC09)
        limit = self._request_limits.setdefault(
            venue, asyncio.Semaphore(
                max(1, int(getattr(self.worker.settings, "rest_concurrency", 2)))))
        for attempt in range(2):
            async with limit:
                delay = self._retry_until.get(venue, 0) - time.monotonic()
                if delay > self.request_timeout_seconds:
                    raise AdapterError("rate_limited: ожидание разрешения биржи", retry_after=delay)
                while delay > 0:
                    try:
                        await asyncio.wait_for(self.worker._stop.wait(), delay)
                        raise SyncUnavailable("worker_stopping")
                    except asyncio.TimeoutError:
                        # Windows timers may wake before the monotonic
                        # deadline. Never send a request ahead of Retry-After.
                        delay = self._retry_until.get(venue, 0) - time.monotonic()
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(fn(*args, **kwargs), self.request_timeout_seconds)
                except AdapterError as exc:
                    self._note_weight(metrics, venue, fn)
                    if exc.retry_after is None:
                        metrics.histogram("rest_request", time.monotonic() - started,
                                          {"venue": venue, "endpoint": endpoint, "outcome": "error"})
                        raise
                    self._retry_until[venue] = max(self._retry_until.get(venue, 0),
                                                  time.monotonic() + exc.retry_after)
                    metrics.counter("rest_retry_after", labels={"venue": venue})
                    metrics.gauge("rest_retry_cooldown_s", exc.retry_after, {"venue": venue})
                    metrics.histogram("rest_request", time.monotonic() - started,
                                      {"venue": venue, "endpoint": endpoint, "outcome": "retry"})
                    if attempt:
                        raise
                except asyncio.TimeoutError:
                    metrics.histogram("rest_request", time.monotonic() - started,
                                      {"venue": venue, "endpoint": endpoint, "outcome": "timeout"})
                    raise
                except Exception:
                    metrics.histogram("rest_request", time.monotonic() - started,
                                      {"venue": venue, "endpoint": endpoint, "outcome": "error"})
                    raise
                else:
                    self._note_weight(metrics, venue, fn)
                    metrics.histogram("rest_request", time.monotonic() - started,
                                      {"venue": venue, "endpoint": endpoint, "outcome": "ok"})
                    metrics.gauge("rest_retry_cooldown_s", 0.0, {"venue": venue})
                    return result

    def _save_job(self, job):
        # Промежуточное сохранение прогресса job не критично: следующий
        # переход несёт полный снимок; транзитный busy (потоковый расчёт
        # держит писателя) пропускаем — финальная запись идёт через
        # _save_job_final (FIFO-писатель ждёт окно)
        from ..db import _is_busy
        try:
            self.db.set_meta(JOB_KEY, json.dumps(job, ensure_ascii=False))
            self.db.set_meta(f"data_sync:job:{job['job_id']}", json.dumps(job, ensure_ascii=False))
        except Exception as exc:
            if not _is_busy(exc):
                raise
            log.warning("data_sync: сохранение job %s отложено (db busy)", job["job_id"])
        if self.worker.broadcast:
            self.worker.broadcast({"type": "data_sync", **job})

    async def _save_job_final(self, job):
        """Финальные состояния job (watchdog/отмена/завершение) — обязаны
        сохраниться: через FIFO-писателя, который ждёт busy-окно."""
        def _save():
            self.db.set_meta(JOB_KEY, json.dumps(job, ensure_ascii=False))
            self.db.set_meta(f"data_sync:job:{job['job_id']}", json.dumps(job, ensure_ascii=False))
        await self._write(_save)
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
        # Окно сканирования разрывов — 200 последних закрытых; ограничение
        # в SQL (DESC LIMIT по PK), а не срез полного чтения (§12, этап 7)
        rows = self.db.get_candles(ins.id, tf, limit=200)
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
        readiness = getattr(self.worker, "readiness", None)
        return {"available": self.ready and not self.worker._stop.is_set(),
                "job": read_json(self.db, JOB_KEY), "channels": channels,
                "instruments": [{"instrument_id": ins.id,
                    "last_full_success_at": int(self.db.get_meta(f"data_sync:instrument_success:{ins.id}") or 0) or None,
                    "ready": all(c["status"] == "ok" for c in channels if c["instrument_id"] == ins.id)}
                    for ins in self.db.get_instruments(enabled_only=True)],
                "state_version": self.db.get_state_seq(),
                # §13 (этап 6a): вектор доменных версий отображаемых каналов
                # (свечи/котировки по инструментам, zones/config/history —
                # глобальные) и фазы готовности — клиент рендерит прогрев,
                # не опрашивая /api/ready отдельно
                "versions": self.db.domain_versions(),
                "readiness": readiness.snapshot() if readiness is not None else None}

    async def mark_instrument_success(self, ins):
        channels = [self.channel_status(ins, tf) for tf in (*dict.fromkeys(("H1", *self.worker.scan_tfs)), "quote")]
        if all(c["status"] == "ok" for c in channels):
            await self._write(
                self.db.set_meta, f"data_sync:instrument_success:{ins.id}",
                str(min(c["last_success_at"] for c in channels)))

    def start(self, reason="manual"):
        if not self.ready or self.worker._stop.is_set():
            raise SyncUnavailable()
        previous = read_json(self.db, JOB_KEY)
        if self._task and not self._task.done():
            return previous
        if previous and previous.get("finished_at", 0) + self.cooldown_ms > now_ms():
            raise SyncCooldown(previous["finished_at"] + self.cooldown_ms)
        # §10: ручной запуск снимает invalid-symbol блокировку каналов —
        # источник получает один новый шанс на job вместо вечного пропуска
        self._invalid_instruments.clear()
        instruments = self.db.get_instruments(enabled_only=True)
        # Reconcile parent context before evaluating the H1 scenarios.
        tfs = tuple(dict.fromkeys((*self.worker.scan_tfs, "H1")))
        job = {"job_id": str(uuid.uuid4()), "reason": reason, "status": "queued",
               "requested_at": now_ms(), "started_at": None, "finished_at": None,
               "channels_total": len(instruments) * (len(tfs) + 1), "channels_completed": 0,
               # §15 (этап 5): этапы задания — котировки → свежие свечи →
               # восстановление истории; завершение только по факту этапов,
               # не по постановке в очередь
               "stages": [
                   {"name": name, "status": "pending", "started_at": None,
                    "finished_at": None, "detail": {}}
                   for name in ("quotes", "fresh_candles", "history")
               ],
               "results": [], "errors": []}
        # Шаблон каналов — прежний порядок (по инструменту: quote, *tfs);
        # этапы ниже заполняют свои записи, порядок результатов стабилен
        for ins in instruments:
            for tf in ("quote", *tfs):
                job["results"].append({"instrument_id": ins.id, "symbol": ins.symbol,
                                       "venue": ins.venue, "timeframe": tf,
                                       "status": "pending"})
        self._save_job(job)
        self._task = asyncio.create_task(self._run_job(job, instruments, tfs))
        return job

    async def _run_job(self, job, instruments, tfs):
        """Ручное обновление по этапам (§15, AC14).

        Этап A (quotes): котировки ВСЕХ инструментов сразу, ограниченная
        параллельность (4) и per-instrument сетевой таймаут — тяжёлая
        история одного актива не задерживает цену другого. Этап B
        (fresh_candles): свежие хвосты свечей — инструменты параллельно
        (сеть ограничена per-venue семафором §10), каналы инструмента
        последовательно под его замком; каждый канал ждёт свой расчёт
        (manual_tail) — завершение не на постановке в очередь. Этап C
        (history): восстановление незавершённых чекпоинтов backfill /
        углубившегося окна. Статусы этапов: pending/running/completed/
        failed/waiting_source (сеть, cooldown) /waiting_calculation (расчёт).
        """
        job.update(status="running", started_at=now_ms())
        self._save_job(job)
        deadline = time.monotonic() + self.job_timeout_seconds
        results = {(r["instrument_id"], r["timeframe"]): r for r in job["results"]}
        stages = {s["name"]: s for s in job["stages"]}

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
                await self._save_job_final(job)

        def _close_stage(stage, failed):
            stage["status"] = "failed" if failed else "completed"
            stage["finished_at"] = now_ms()
            self._save_job(job)

        def _channel_done(result, ins, tf):
            if result.get("error"):
                job["errors"].append({"instrument_id": ins.id, "symbol": ins.symbol,
                                      "timeframe": tf, "error": result["error"]})
            job["channels_completed"] += 1
            self._save_job(job)

        watcher = asyncio.create_task(watchdog())
        try:
            # ---------- этап A: котировки всех инструментов (независимо от свечей) ----------
            stage = stages["quotes"]
            stage.update(status="waiting_source", started_at=now_ms())
            self._save_job(job)
            quote_sem = asyncio.Semaphore(4)  # job-local лимит параллелизма котировок

            async def _quote_channel(ins):
                result = results[ins.id, "quote"]
                result["status"] = "processing"
                self._save_job(job)
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline_exceeded")
                    async with quote_sem:
                        # этап 2: быстрый ingest без instrument-lock;
                        # «канал обновлён» — тик дообработан очередью
                        # (замок ждать нельзя: потребителю нужен он же)
                        await asyncio.wait_for(
                            self.worker._poll_quote(ins), self.request_timeout_seconds)
                    left = max(0.1, deadline - time.monotonic())
                    if not await self.worker.market_queue.wait_idle(ins.id, left):
                        raise TimeoutError("market_queue_drain_timeout")
                    result.update(self.channel_status(ins, "quote"))
                    if result["status"] != "ok":
                        result["error"] = result.get("reason") or "channel_not_ready"
                except Exception as exc:
                    result.update(status="failed", error=str(exc)[:300] or type(exc).__name__)
                    log.exception("data_sync job=%s symbol=%s tf=quote", job["job_id"], ins.symbol)
                _channel_done(result, ins, "quote")

            await asyncio.gather(*(_quote_channel(ins) for ins in instruments))
            failed = [r["symbol"] for ins in instruments
                      if (r := results[ins.id, "quote"]).get("status") != "ok"]
            stage["detail"] = {"failed": failed}
            _close_stage(stage, bool(failed))

            # ---------- этап B: свежие хвосты свечей ----------
            stage = stages["fresh_candles"]
            stage.update(status="waiting_source", started_at=now_ms())
            self._save_job(job)
            candle_results = [results[ins.id, tf] for ins in instruments for tf in tfs]

            def _progress(result, phase):
                """Канал перешёл из сети в ожидание расчёта (или наоборот) —
                статус этапа отражает, чего job ждёт прямо сейчас (§15)."""
                result["wait_state"] = phase
                if any(r.get("status") == "processing" and r.get("wait_state") == "source"
                       for r in candle_results):
                    stage["status"] = "waiting_source"
                elif any(r.get("status") == "processing" for r in candle_results):
                    stage["status"] = "waiting_calculation"
                self._save_job(job)

            async def _candle_channel(ins, tf):
                result = results[ins.id, tf]
                result["status"] = "processing"
                self._save_job(job)
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline_exceeded")
                    result.update(await self.sync_candles(
                        ins, tf, job["reason"], job["job_id"],
                        progress=lambda phase: _progress(result, phase)))
                    if result["status"] != "ok":
                        result["error"] = result.get("reason") or "channel_not_ready"
                except Exception as exc:
                    result.update(status="failed", error=str(exc)[:300] or type(exc).__name__)
                    log.exception("data_sync job=%s symbol=%s tf=%s", job["job_id"], ins.symbol, tf)
                result.pop("wait_state", None)
                _channel_done(result, ins, tf)

            async def _instrument_candles(ins):
                async with self.instrument(ins.id):
                    for tf in tfs:
                        await _candle_channel(ins, tf)
                await self.mark_instrument_success(ins)

            await asyncio.gather(*(_instrument_candles(ins) for ins in instruments))
            failed = [f"{r['symbol']}:{r['timeframe']}" for r in candle_results
                      if r.get("status") != "ok"]
            stage["detail"] = {"failed": failed}
            _close_stage(stage, bool(failed))

            # ---------- этап C: восстановление истории ----------
            stage = stages["history"]
            stage.update(status="running", started_at=now_ms())
            self._save_job(job)
            recovered, history_failed = [], []
            for ins in instruments:
                try:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("deadline_exceeded")
                    if not self.worker._needs_history_recovery(ins):
                        continue
                    await self.worker.backfill(ins)
                    recovered.append(ins.symbol)
                except Exception as exc:
                    history_failed.append({"instrument_id": ins.id, "symbol": ins.symbol,
                                           "error": str(exc)[:300] or type(exc).__name__})
                    job["errors"].append({"instrument_id": ins.id, "symbol": ins.symbol,
                                          "timeframe": "history",
                                          "error": str(exc)[:300] or type(exc).__name__})
                    log.exception("data_sync job=%s history symbol=%s", job["job_id"], ins.symbol)
            stage["detail"] = {"recovered": recovered, "failed": history_failed}
            _close_stage(stage, bool(history_failed))
        except asyncio.CancelledError:
            job["errors"].append({"error": "interrupted_by_shutdown"})
            for result in job["results"]:
                if result.get("status") in ("processing", "pending"):
                    result.update(status="failed", error="interrupted_by_shutdown")
            for st in job["stages"]:
                if st.get("status") not in ("completed", "failed"):
                    st.update(status="failed", finished_at=now_ms())
            raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            good = sum(r.get("status") == "ok" for r in job["results"])
            job.update(status="succeeded" if not job["errors"] else "partial" if good else "failed",
                       finished_at=now_ms(), processing_continues=False)
            await self._save_job_final(job)

    def _note_invalid_symbol(self, ins, exc) -> bool:
        """Источник однозначно отверг инструмент (Binance -1121): не
        повторять в цикле (§10) — пометить и пропускать до следующего
        ручного job/рестарта. True — ошибка именно такого вида."""
        if not getattr(exc, "invalid_symbol", False):
            return False
        if ins.id not in self._invalid_instruments:
            self._invalid_instruments.add(ins.id)
            try:
                self.db.set_meta(f"instrument:invalid:{ins.id}", str(now_ms()))
            except Exception:
                # метка для диагностики не должна маскировать исходную ошибку
                log.warning("instrument:invalid:%s не записан (db busy)", ins.id)
            get_registry().counter("rest_invalid_symbol", labels={"venue": ins.venue})
            log.warning(
                "%s:%s: источник отверг инструмент (invalid symbol) — "
                "каналы пропускаются до следующего job/рестарта",
                ins.venue, ins.symbol)
        return True

    async def sync_candles(self, ins, tf, reason="periodic", job_id=None, *, progress=None):
        """Single-flight (§10): повторный вызов для канала, пока идёт синхронизация,
        ждёт тот же прогон — второго klines-запроса к источнику не уходит
        (boundary tick + ручной job + ws_recovery одного канала = один запрос).

        Первичный вызов выполняет работу В СВОЁЙ задаче (замок инструмента и
        владелец — прежние); ожидающие получают копию статуса, исключение
        разделяется. Отмена первичного вызывающего прерывает и сам прогон."""
        key = (ins.id, tf)
        fut = self._inflight_syncs.get(key)
        if fut is not None and self._owners.get(ins.id) is not asyncio.current_task():
            # Ожидание чужого прогона. ВАЖНО: вызывающий, уже держащий замок
            # инструмента (@instrument_owned, напр. _poll_instrument), joining
            # делать НЕ может — первичный прогон ждёт именно этот замок,
            # получилась бы взаимная блокировка; владелец замка выполняет
            # свою синхронизацию сам (реентрантность замка ниже сохраняется).
            return dict(await fut)
        fut = asyncio.get_running_loop().create_future()
        self._inflight_syncs[key] = fut
        # исключение общего future не оставляем «непрочитанным», когда
        # ожидающих нет (иначе asyncio ругается в лог на shutdown)
        fut.add_done_callback(
            lambda f: None if f.cancelled() else f.exception())
        try:
            result = await self._sync_candles(ins, tf, reason, job_id, progress=progress)
        except BaseException as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        else:
            if not fut.done():
                fut.set_result(result)
            return result
        finally:
            # окно single-flight закрывается вместе с прогоном — и при сбое
            # тоже: следующий вызов обязан получить свежий запрос, а не
            # устаревшее исключение (восстановление канала, AC08)
            if self._inflight_syncs.get(key) is fut:
                self._inflight_syncs.pop(key, None)

    async def _sync_candles(self, ins, tf, reason="periodic", job_id=None, *, progress=None):
        if ins.id in self._invalid_instruments:
            raise AdapterError(
                f"{ins.venue}:{ins.symbol} отвергнут источником — пропуск "
                f"до следующего job/рестарта", invalid_symbol=True)
        async with self.instrument(ins.id):
            started = time.monotonic()
            result = dict(read_json(self.db, self._key(ins, tf), {}) or {})
            result.update(last_attempt_at=now_ms(), error=None, processing=True,
                          error_stage=None, new_bars=0, revised_bars=0, reason=reason)
            was_replaying = self.db.get_meta(f"replaying:{ins.id}") == "1"
            try:
                # входные метки — внутри try: отмена (shutdown/дедлайн job)
                # обязана попасть в уборку processing/stale маркеров ниже
                await self._write(self._save_channel, ins, tf, result)
                if not was_replaying:
                    await self._write(self.worker._set_replaying, ins.id, True)
                # Хвост канала — ограниченное окно SQL (§12, этап 7):
                # окно запроса к источнику и без того ограничено сканом
                # разрывов по 200 последним закрытым (ниже), а снапшот before
                # нужен для классификации только свечей этого окна
                rows = self.db.get_candles(ins.id, tf, limit=200)
                step = TIMEFRAME_MS[tf]
                start = rows[-2].open_time if len(rows) >= 2 else rows[-1].open_time if rows else now_ms() - (
                    self.worker.cfg.ltf_history_days if tf == "H1" else self.worker.cfg.lookback_days_for(tf)) * 86_400_000
                # Extend the one paginated request over any internal tail gaps.
                for a, b in zip(rows, rows[1:]):
                    if b.open_time - a.open_time > step:
                        start = min(start, a.open_time + step)
                result["requested_from"] = start
                end = now_ms()
                result["requested_to"] = end
                request_started = time.monotonic()
                if progress:
                    progress("source")
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
                metrics = get_registry()
                metrics.counter("candles_received", len(incoming), {"timeframe": tf})
                metrics.counter("candles_inserted", result["new_bars"], {"timeframe": tf})
                metrics.counter("candles_revised", result["revised_bars"], {"timeframe": tf})
                metrics.counter("candles_unchanged",
                                sum(c.closed and c.open_time in before for c in incoming) - result["revised_bars"],
                                {"timeframe": tf})
                # Record the old processed tail before any insertion so a
                # restart between persistence and calculation cannot skip it.
                # Этап 7 (§12): курсор + вставка + rewind H1 + снимок канала —
                # одна единица записи (один COMMIT на FIFO-писателе); сбой
                # середины откатывает всё, курсор не уезжает вперёд данных.
                processed_key = f"data_sync:processed:{ins.id}:{tf}"
                h1_cursor_key = f"ltf:h1:last_close:{ins.id}" if tf == "H1" else None

                def _persist_incoming():
                    with self.db.batch_writes():
                        if tf != "H1" and self.db.get_meta(processed_key) is None:
                            self.db.set_meta(processed_key,
                                             str(max((c.close_time for c in rows), default=0)))
                        self.db.insert_candles(incoming)
                        if h1_cursor_key is not None:
                            # An inserted hole behind the H1 cursor is not an OHLC
                            # revision. Explicitly rewind to make the engine see it.
                            cursor = int(self.db.get_meta(h1_cursor_key) or 0)
                            missed = [c for c in incoming if c.closed and c.open_time not in before and c.close_time <= cursor]
                            if missed:
                                earliest = min(c.open_time for c in missed)
                                previous = max((c.close_time for c in rows if c.open_time < earliest), default=0)
                                self.db.set_meta(h1_cursor_key, str(previous))
                        self._save_channel(ins, tf, result)

                await self._write(_persist_incoming)
                processing_started = time.monotonic()
                # этап 4 (§9): при включённой очереди канал ставится в
                # calc_queue и ждёт финального прогона (семантика синхронной
                # обработки сохраняется); при выключенной — прежний прямой
                # вызов. Freshness и доставка ниже — общие для обоих путей.
                if progress:
                    progress("calculation")
                queue_mode = self.worker.calc_queue_enabled
                if queue_mode:
                    processed_result = await self.worker._submit_synced_channel(
                        ins, tf, incoming, before, reason=reason)
                else:
                    processed_result = await self.worker._process_synced_channel(ins, tf, incoming, before)
                result["processing_duration_s"] = round(time.monotonic() - processing_started, 3)
                result.update(processing=False, error=None, error_stage=None, finished_at=now_ms())
                await self._write(self._save_channel, ins, tf, result)
                status = self.channel_status(ins, tf)
                await self.worker._check_freshness(ins, tf)
                await self._write(
                    self.db.set_meta, f"stale:{ins.id}:{tf}",
                    "0" if status["status"] == "ok" else "1")
                # Delivery must see recovered freshness, not the preceding
                # failed poll's stale marker.
                await self.worker._deliver_synced_channel(ins, tf, processed_result)
                await self.mark_instrument_success(ins)
                if self.worker.broadcast:
                    payload = {"type": "candle", "instrument_id": ins.id, "timeframe": tf}
                    # §14 (этап 6b): OHLC бара для инкрементального обновления
                    # графика; при ревизии истории — без нагрузки (полный пересчёт клиента)
                    if not result.get("revised_bars"):
                        closed_bar = next((c for c in reversed(incoming) if c.closed), None)
                        if closed_bar is not None:
                            payload.update(
                                open_time=closed_bar.open_time, open=closed_bar.open,
                                high=closed_bar.high, low=closed_bar.low, close=closed_bar.close)
                    self.worker.broadcast(payload)
                log.info("data_sync reason=%s job=%s symbol=%s tf=%s from=%s to=%s new=%s revised=%s request_s=%s processing_s=%s duration=%.3f status=%s processed_close=%s",
                         reason, job_id, ins.symbol, tf, start, end, result["new_bars"], result["revised_bars"],
                         result["request_duration_s"], result["processing_duration_s"],
                         time.monotonic() - started, status["status"], status["processed_close"])
                metrics.histogram("sync_candles_duration", time.monotonic() - started,
                                  {"timeframe": tf, "reason": str(reason)})
                return status
            except asyncio.CancelledError:
                result.update(processing=False, error="interrupted_by_shutdown", last_error_at=now_ms())
                await self._write(self._save_channel, ins, tf, result)
                await self._write(self.db.set_meta, f"stale:{ins.id}:{tf}", "1")
                raise
            except Exception as exc:
                self._note_invalid_symbol(ins, exc)
                if not result.get("error_stage"):
                    result["error_stage"] = "source"
                result.update(processing=False, error=str(exc)[:300] or type(exc).__name__, last_error_at=now_ms())
                await self._write(self._save_channel, ins, tf, result)
                await self._write(self.db.set_meta, f"stale:{ins.id}:{tf}", "1")
                raise
            finally:
                # A borrowed marker belongs to restore/backfill. That owner
                # may have finished while this request was running.
                if not was_replaying:
                    await self._write(self.worker._set_replaying, ins.id, False)

    async def boundary_tick(self, at=None):
        """Тик у границ баров (§10, AC08): каналы, которым пора, синхронизируются
        параллельно по инструментам — зависший источник одного актива (до
        request_timeout_seconds) не останавливает закрытия остальных; сеть
        ограничена per-venue семафором, канал — single-flight."""
        at = now_ms() if at is None else at
        due: list[tuple[object, str, dict]] = []
        for ins in self.db.get_instruments(enabled_only=True):
            if ins.id in self._invalid_instruments:
                continue  # источник отверг инструмент — до следующего job/рестарта
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
                due.append((ins, tf, state))

        async def _one(ins, tf):
            try:
                status = await self.sync_candles(ins, tf, "boundary")
                return status["received_close"] is not None \
                    and status["received_close"] >= status["expected_close"] \
                    and status["has_current"]
            except Exception:
                log.exception("boundary sync %s %s", ins.symbol, tf)
                return False

        outcomes = await asyncio.gather(*(_one(ins, tf) for ins, tf, _ in due))
        for (ins, tf, state), done in zip(due, outcomes):
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
