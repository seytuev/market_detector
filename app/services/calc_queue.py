"""Очередь расчётных заданий (этап 4, спека §9).

Единая точка постановки тяжёлой обработки закрытых свечей: приоритеты
(live H1 → ручной хвост → live HTF → восстановление → backfill),
коалесцинг дублей по ключу (instrument_id, timeframe, task_kind),
fairness-старение низких приоритетов, bounded-лимит и durable-строка
calc_task — падение после вставки свечи, но до расчёта, оставляет
восстанавливаемое задание (AC18).

Тиковый путь котировок сюда НЕ входит: on_price уже изолирован в
market_queue (этап 2) с sub-second латентностью на тик; вторая очередь
добавила бы задержку без выигрыша — market_queue продолжает вызывать
Worker._calculate напрямую.

Семантика строк calc_task: одна запись на ключ (UNIQUE по ключу);
pending → running → done (строка удаляется) | failed (строка остаётся
с error для диагностики). start() поднимает pending/running/failed
обратно в обработку — running при крахе неотличим от pending.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..models import now_ms
from .metrics import get_registry

log = logging.getLogger(__name__)

# Классы приоритетов (меньше — раньше), спека §9: непрерывные текущие
# сигналы/закрытие H1 → ручная сверка хвоста → текущий HTF →
# восстановление разрыва → полный исторический backfill.
TASK_PRIORITIES = {
    "live_h1": 0,
    "manual_tail": 1,
    "live_htf": 2,
    "recovery": 3,
    "backfill": 4,
}

DONE_CACHE = 256  # сколько последних завершённых ключей помнить для wait_done


@dataclass
class CalcTask:
    """Задание на расчёт канала (instrument_id, timeframe, task_kind).

    target_cursor — макс. open_time закрытой свечи, которую надо
    обработать; earliest_revised_open — мин. граница необходимого replay
    (None — инкрементальный хвост); input_version — версия входа
    (state_seq на момент постановки); enqueued_at — monotonic ПЕРВОЙ
    постановки (не освежается при слиянии — иначе fairness-старение
    никогда бы не наступило); payload — только в памяти, в БД не пишется.
    """

    instrument_id: int
    timeframe: str
    task_kind: str
    priority: int
    target_cursor: Optional[int] = None
    earliest_revised_open: Optional[int] = None
    input_version: int = 0
    enqueued_at: float = 0.0
    payload: dict = field(default_factory=dict)
    row_id: Optional[int] = None
    run_count: int = 0
    running: bool = False
    pending_reenqueue: bool = False

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.instrument_id, self.timeframe, self.task_kind)


class _KeyState:
    __slots__ = ("task", "done", "error", "result")

    def __init__(self, task: CalcTask) -> None:
        self.task = task
        self.done = asyncio.Event()
        self.error: Optional[BaseException] = None
        self.result: Any = None


class CalcQueue:
    """Приоритетная durable-очередь расчётов с коалесцингом и fairness.

    executor — async callable(CalcTask); воркер подставляет обработчик
    канала (процессинг через self._calculate — глобальный calc-замок
    остаётся единственной точкой сериализации движка, AC19).
    concurrency — число потребителей (по умолчанию 1: прежняя семантика
    строгой очередности). max_queue — предел ожидающих (не running)
    заданий; переполнение отклоняет НОВОЕ задание, если оно не срочнее
    самого низкоприоритетного в очереди — уже поставленные задания не
    выбрасываются никогда.
    """

    def __init__(
        self,
        db,
        executor: Callable[[CalcTask], Awaitable[Any]],
        *,
        concurrency: int = 1,
        max_queue: int = 1000,
        fairness_max_wait_s: float = 60.0,
        metrics=None,
        clock=time.monotonic,
    ) -> None:
        self.db = db
        self._executor = executor
        self.concurrency = max(1, int(concurrency))
        self.max_queue = int(max_queue)
        self.fairness_max_wait_s = float(fairness_max_wait_s)
        self._clock = clock  # шов для детерминированных тестов старения
        self._states: dict[tuple[int, str, str], _KeyState] = {}
        self._done: dict[tuple[int, str, str], tuple[Optional[BaseException], Any]] = {}
        self._wake = asyncio.Event()
        self._consumers: list[asyncio.Task] = []
        self._started = False
        self._stopping = False
        metrics = metrics or get_registry()
        for name in (
            "calc_queue_enqueued", "calc_queue_coalesced",
            "calc_queue_overload", "calc_queue_completed", "calc_queue_failed",
            "calc_queue_db_busy",
        ):
            metrics.register(name, "counter", ("task_kind",))
        metrics.register("calc_queue_depth", "gauge", ())
        metrics.register("calc_queue_oldest_wait_s", "gauge", ())
        metrics.register("calc_task_duration", "histogram", ("task_kind",))
        self._metrics = metrics

    # ---------- persistence ----------

    def _safe_db(self, fn, *args) -> None:
        """Durable-запись состояния задания с деградацией при transient busy.

        Источник истины внутри процесса — in-memory состояние; строка
        calc_task нужна только для восстановления после краха (running при
        крахе неотличим от pending). Транзитный SQLITE_BUSY (потоковый расчёт
        держит блокировку писателя, busy_timeout цикла событий — 0) не должен
        убивать потребителя или валить продюсера: пропущенная запись
        дочитывается рестартом как pending."""
        import sqlite3

        try:
            fn(*args)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            self._metrics.counter("calc_queue_db_busy", labels={"task_kind": "persist"})
            log.warning("calc_queue: durable-запись отложена (db busy): %s", exc)

    def _db_upsert(self, task: CalcTask, status: str) -> None:
        now = now_ms()
        self.db.conn.execute(
            """INSERT INTO calc_task
               (instrument_id, timeframe, task_kind, priority, target_cursor,
                earliest_revised_open, input_version, status, run_count,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (instrument_id, timeframe, task_kind) DO UPDATE SET
                   priority=excluded.priority,
                   target_cursor=excluded.target_cursor,
                   earliest_revised_open=excluded.earliest_revised_open,
                   input_version=excluded.input_version,
                   status=excluded.status,
                   run_count=excluded.run_count,
                   error=CASE WHEN excluded.status='pending' THEN NULL
                              ELSE calc_task.error END,
                   updated_at=excluded.updated_at""",
            (task.instrument_id, task.timeframe, task.task_kind, task.priority,
             task.target_cursor, task.earliest_revised_open, task.input_version,
             status, task.run_count, now, now),
        )
        self.db.conn.commit()

    def _db_delete(self, task: CalcTask) -> None:
        self.db.conn.execute(
            "DELETE FROM calc_task WHERE instrument_id=? AND timeframe=?"
            " AND task_kind=?",
            (task.instrument_id, task.timeframe, task.task_kind),
        )
        self.db.conn.commit()

    def _db_fail(self, task: CalcTask, error: BaseException) -> None:
        self.db.conn.execute(
            "UPDATE calc_task SET status='failed', error=?, updated_at=?"
            " WHERE instrument_id=? AND timeframe=? AND task_kind=?",
            (f"{type(error).__name__}: {error}"[:500], now_ms(),
             task.instrument_id, task.timeframe, task.task_kind),
        )
        self.db.conn.commit()

    # ---------- продюсер ----------

    def enqueue(
        self,
        instrument_id: int,
        timeframe: str,
        task_kind: str,
        *,
        target_cursor: Optional[int] = None,
        earliest_revised_open: Optional[int] = None,
        input_version: int = 0,
        payload: Optional[dict] = None,
    ) -> bool:
        """Поставить задание. True — принято (новое или слито с текущим);
        False — отклонено переполнением (calc_queue_overload).

        Слияние дубля по ключу: max target_cursor, min earliest_revised_open,
        более срочный приоритет (численно min), свежая input_version; при
        слиянии с ВЫПОЛНЯЮЩИМСЯ заданием ставится pending_reenqueue — после
        прогона слитое задание идёт снова, новые данные не теряются, а
        очередь от повторяющегося ключа не растёт."""
        if task_kind not in TASK_PRIORITIES:
            raise ValueError(f"unknown calc task_kind: {task_kind!r}")
        priority = TASK_PRIORITIES[task_kind]
        key = (instrument_id, timeframe, task_kind)
        st = self._states.get(key)
        if st is not None:
            task = st.task
            if target_cursor is not None:
                task.target_cursor = (
                    target_cursor if task.target_cursor is None
                    else max(task.target_cursor, target_cursor)
                )
            if earliest_revised_open is not None:
                task.earliest_revised_open = (
                    earliest_revised_open
                    if task.earliest_revised_open is None
                    else min(task.earliest_revised_open, earliest_revised_open)
                )
            task.priority = min(task.priority, priority)
            task.input_version = input_version
            if payload:
                task.payload.update(payload)
            if task.running:
                task.pending_reenqueue = True
            self._metrics.counter("calc_queue_coalesced", labels={"task_kind": task_kind})
            self._safe_db(self._db_upsert, task, "running" if task.running else "pending")
            self._wake.set()
            return True
        pending = [s for s in self._states.values() if not s.task.running]
        if len(pending) >= self.max_queue:
            worst = max(s.task.priority for s in pending)
            if priority >= worst:
                self._metrics.counter("calc_queue_overload", labels={"task_kind": task_kind})
                log.warning(
                    "calc_queue: переполнение (%d ожидающих) — задание %s "
                    "инструмента %s (%s) отклонено",
                    len(pending), task_kind, instrument_id, timeframe,
                )
                return False
            # новое задание срочнее самого низкоприоритетного в очереди:
            # принимаем сверх лимита — поставленные задания не выбрасываем
        task = CalcTask(
            instrument_id=instrument_id, timeframe=timeframe, task_kind=task_kind,
            priority=priority, target_cursor=target_cursor,
            earliest_revised_open=earliest_revised_open,
            input_version=input_version, enqueued_at=self._clock(),
            payload=dict(payload or {}),
        )
        self._states[key] = _KeyState(task)
        self._safe_db(self._db_upsert, task, "pending")
        self._metrics.counter("calc_queue_enqueued", labels={"task_kind": task_kind})
        self._update_gauges()
        self._ensure_consumers()
        self._wake.set()
        return True

    async def wait_done(
        self,
        instrument_id: int,
        timeframe: str,
        task_kind: str,
        timeout: Optional[float] = None,
    ) -> Any:
        """Дождаться финального завершения задания по ключу (включая раунды
        pending_reenqueue) и вернуть его результат. Исключение исполнителя
        поднимается ожидающему как есть (семантика синхронного вызова);
        TimeoutError — задание не завершилось за timeout."""
        key = (instrument_id, timeframe, task_kind)
        st = self._states.get(key)
        if st is None:
            done = self._done.get(key)
            if done is None:
                return None
            error, result = done
            if error is not None:
                raise error
            return result
        await asyncio.wait_for(st.done.wait(), timeout)
        if st.error is not None:
            raise st.error
        return st.result

    # ---------- жизненный цикл ----------

    def start(self) -> None:
        """Восстановить durable-задания: pending/running/failed строки
        calc_task снова становятся pending (running при крахе неотличим
        от pending; failed перезапускается на рестарте — внутри процесса
        повторяется только при новой постановке, чтобы не крутить ядное
        задание в горячем цикле)."""
        if self._started:
            return
        self._started = True
        rows = self.db.conn.execute(
            "SELECT * FROM calc_task WHERE status IN ('pending','running','failed')"
            " ORDER BY created_at, id"
        ).fetchall()
        for r in rows:
            task = CalcTask(
                instrument_id=int(r["instrument_id"]), timeframe=r["timeframe"],
                task_kind=r["task_kind"],
                priority=TASK_PRIORITIES.get(r["task_kind"], int(r["priority"])),
                target_cursor=r["target_cursor"],
                earliest_revised_open=r["earliest_revised_open"],
                input_version=int(r["input_version"] or 0),
                enqueued_at=self._clock(), row_id=int(r["id"]),
                run_count=int(r["run_count"]),
            )
            self._states[task.key] = _KeyState(task)
            self._safe_db(self._db_upsert, task, "pending")
        if rows:
            log.info("calc_queue: восстановлено заданий из БД: %d", len(rows))
        self._update_gauges()
        if rows:
            self._ensure_consumers()
            self._wake.set()

    async def stop(self) -> None:
        """Дообработать текущее задание и остановиться; ожидающие строки
        остаются pending в БД — их поднимет start() после рестарта."""
        self._stopping = True
        self._wake.set()
        tasks = [t for t in self._consumers if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._consumers = []

    # ---------- потребители ----------

    def _ensure_consumers(self) -> None:
        if self._stopping:
            return
        self._consumers = [c for c in self._consumers if not c.done()]
        while len(self._consumers) < self.concurrency:
            idx = len(self._consumers)
            self._consumers.append(asyncio.create_task(self._consume(idx)))

    def _effective_priority(self, task: CalcTask) -> int:
        """Fairness-старение (§9): за каждый полный период ожидания
        fairness_max_wait_s класс повышается на один — backfill не может
        голодать вечно под непрерывной live-нагрузкой."""
        if self.fairness_max_wait_s <= 0:
            return task.priority
        waited = self._clock() - task.enqueued_at
        bump = int(waited / self.fairness_max_wait_s)
        return max(0, task.priority - bump)

    def _pick(self) -> Optional[CalcTask]:
        """Срочнейшее (эффективный приоритет, затем старейшее) ожидающее
        задание; выбранное сразу помечается running — без await между
        проверкой и пометкой, гонок между потребителями нет."""
        best: Optional[CalcTask] = None
        best_rank: Optional[tuple[int, float]] = None
        for st in self._states.values():
            task = st.task
            if task.running:
                continue
            rank = (self._effective_priority(task), task.enqueued_at)
            if best_rank is None or rank < best_rank:
                best, best_rank = task, rank
        if best is not None:
            best.running = True
            best.run_count += 1
            self._safe_db(self._db_upsert, best, "running")
            self._update_gauges()
        return best

    async def _consume(self, idx: int) -> None:
        try:
            while not self._stopping:
                task = self._pick()
                if task is None:
                    self._wake.clear()
                    task = self._pick()
                    if task is None:
                        try:
                            await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        continue
                await self._run_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # сбой вне уровня задания (БД и т.п.): потребитель умирает,
            # следующий enqueue перезапустит — очередь не теряется
            log.exception("calc_queue: потребитель %s завершился сбоем", idx)

    async def _run_task(self, task: CalcTask) -> None:
        st = self._states.get(task.key)
        started = self._clock()
        error: Optional[BaseException] = None
        result: Any = None
        try:
            result = await self._executor(task)
        except asyncio.CancelledError:
            # отмена потребителя (крах-семантика): строка остаётся running,
            # start() после рестарта вернёт её в pending
            raise
        except Exception as exc:
            error = exc
        self._metrics.histogram(
            "calc_task_duration", self._clock() - started,
            {"task_kind": task.task_kind},
        )
        if error is None:
            # счётчик успешных прогонов (пара к calc_task_duration), incl.
            # промежуточные прогоны слитого задания
            self._metrics.counter("calc_queue_completed", labels={"task_kind": task.task_kind})
        if error is None and task.pending_reenqueue:
            # за время прогона пришли новые данные — слитое задание идёт
            # снова (строка уже несёт слитые границы)
            task.pending_reenqueue = False
            task.running = False
            self._safe_db(self._db_upsert, task, "pending")
            self._update_gauges()
            self._wake.set()
            return
        self._states.pop(task.key, None)
        if st is not None:
            st.error, st.result = error, result
            st.done.set()
        if error is None:
            self._safe_db(self._db_delete, task)
        else:
            self._safe_db(self._db_fail, task, error)
            self._metrics.counter("calc_queue_failed", labels={"task_kind": task.task_kind})
            log.warning(
                "calc_queue: задание %s инструмента %s (%s) не удалось: %s",
                task.task_kind, task.instrument_id, task.timeframe, error,
            )
        done = self._done
        done[task.key] = (error, result)
        while len(done) > DONE_CACHE:
            done.pop(next(iter(done)))
        self._update_gauges()
        # задание освободило место/завершилось — проверить очередь
        self._wake.set()

    # ---------- диагностика ----------

    def _update_gauges(self) -> None:
        pending = [s.task for s in self._states.values() if not s.task.running]
        self._metrics.gauge("calc_queue_depth", len(pending))
        if pending:
            oldest = min(t.enqueued_at for t in pending)
            self._metrics.gauge(
                "calc_queue_oldest_wait_s", max(0.0, self._clock() - oldest))
        else:
            self._metrics.gauge("calc_queue_oldest_wait_s", 0.0)

    def depth(self) -> int:
        return sum(1 for s in self._states.values() if not s.task.running)

    def busy(self) -> bool:
        """Есть ожидающие или выполняющиеся задания (для фазы готовности calc)."""
        return bool(self._states)

    def diagnostics(self) -> dict:
        """Детали очереди для /api/diagnostics (без секретов)."""
        pending: dict[str, int] = {}
        running: list[dict] = []
        oldest_wait: dict[str, float] = {}
        for st in self._states.values():
            task = st.task
            if task.running:
                running.append({
                    "instrument_id": task.instrument_id,
                    "timeframe": task.timeframe,
                    "task_kind": task.task_kind,
                    "run_count": task.run_count,
                })
            else:
                pending[task.task_kind] = pending.get(task.task_kind, 0) + 1
                # ожидание старейшего pending-задания на ключ канала (§13, 6a)
                ch_key = f"{task.instrument_id}:{task.timeframe}"
                waited = max(0.0, self._clock() - task.enqueued_at)
                if waited > oldest_wait.get(ch_key, -1.0):
                    oldest_wait[ch_key] = round(waited, 3)
        counters = {
            k: v["value"]
            for k, v in self._metrics.snapshot()["series"].items()
            if v.get("kind") == "counter" and k.startswith(
                ("calc_queue_", "calc_skipped_unchanged"))
        }
        return {
            "started": self._started,
            "stopping": self._stopping,
            "concurrency": self.concurrency,
            "max_queue": self.max_queue,
            "fairness_max_wait_s": self.fairness_max_wait_s,
            "depth": self.depth(),
            "pending_by_kind": pending,
            "pending_oldest_wait_s": oldest_wait,
            "running": running,
            "counters": counters,
        }
