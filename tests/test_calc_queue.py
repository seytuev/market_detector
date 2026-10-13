"""Этап 4 (спека §9): очередь расчётов — приоритеты, коалесцинг, fairness,
durable-восстановление calc_task и безопасность отката (AC18)."""
from __future__ import annotations

import asyncio

import pytest

from app.db import Database
from app.services.calc_queue import TASK_PRIORITIES, CalcQueue
from app.services.metrics import MetricsRegistry


def _rows(db):
    return [
        dict(r)
        for r in db.conn.execute(
            "SELECT instrument_id, timeframe, task_kind, status, error,"
            " target_cursor, earliest_revised_open, input_version, run_count"
            " FROM calc_task ORDER BY id"
        ).fetchall()
    ]


async def _until(cond, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.005)


async def test_priority_classes_are_bounded_and_ordered():
    assert TASK_PRIORITIES == {
        "live_h1": 0, "manual_tail": 1, "live_htf": 2,
        "recovery": 3, "backfill": 4,
    }
    q = CalcQueue(Database(":memory:"), None, metrics=MetricsRegistry())
    with pytest.raises(ValueError):
        # live_price осознанно отсутствует: тиковый путь изолирован в
        # market_queue (sub-second on_price), второй очереди не нужно
        q.enqueue(1, "D1", "live_price")


async def test_coalesce_merges_boundaries_and_reenqueues_running(db):
    """Слияние дубля по ключу: max target_cursor, min earliest_revised_open,
    свежая input_version; данные, пришедшие во время прогона, не теряются —
    слитое задание выполняется повторно, а очередь не растёт."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def executor(task):
        calls.append((task.target_cursor, task.earliest_revised_open,
                      task.input_version))
        if len(calls) == 1:
            started.set()
            await release.wait()
        return "ok"

    metrics = MetricsRegistry()
    q = CalcQueue(db, executor, metrics=metrics)
    try:
        q.enqueue(1, "D1", "live_htf", target_cursor=100,
                  earliest_revised_open=50, input_version=1)
        await started.wait()
        # слияние с ВЫПОЛНЯЮЩИМСЯ заданием
        q.enqueue(1, "D1", "live_htf", target_cursor=200,
                  earliest_revised_open=40, input_version=2)
        q.enqueue(1, "D1", "live_htf", target_cursor=150,
                  earliest_revised_open=60, input_version=3)
        release.set()
        assert await q.wait_done(1, "D1", "live_htf", 5) == "ok"
        assert calls == [(100, 50, 1), (200, 40, 3)]
        assert q.depth() == 0
        assert _rows(db) == []  # успешное задание удалено
        series = metrics.snapshot()["series"]
        assert series["calc_queue_coalesced{task_kind=live_htf}"]["value"] == 2
        assert series["calc_queue_enqueued{task_kind=live_htf}"]["value"] == 1
        assert series["calc_queue_completed{task_kind=live_htf}"]["value"] == 2
    finally:
        await q.stop()


async def test_priority_order_then_fifo(db):
    """Потребитель берёт срочнейшее задание; при равном классе — старейшее."""
    calls = []
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def executor(task):
        calls.append(task.task_kind)
        if len(calls) == 1:
            entered.set()
            await gate.wait()  # первый прогон держит потребителя
        return task.task_kind

    q = CalcQueue(db, executor, metrics=MetricsRegistry())
    try:
        q.enqueue(9, "D1", "backfill")
        await entered.wait()
        q.enqueue(1, "D1", "live_htf")
        q.enqueue(2, "D1", "recovery")
        q.enqueue(1, "H1", "live_h1")
        q.enqueue(1, "W1", "manual_tail")
        gate.set()
        await asyncio.gather(
            q.wait_done(1, "D1", "live_htf", 5),
            q.wait_done(2, "D1", "recovery", 5),
            q.wait_done(1, "H1", "live_h1", 5),
            q.wait_done(1, "W1", "manual_tail", 5),
        )
        assert calls == ["backfill", "live_h1", "manual_tail", "live_htf",
                         "recovery"]
    finally:
        await q.stop()


async def test_fairness_aging_prevents_starvation(db):
    """§9: низкий приоритет не голодает вечно — за каждый период ожидания
    fairness_max_wait_s класс повышается; после старения backfill обгоняет
    свежепоставленный live_htf (равный класс — старейшее первым)."""
    clock = [1000.0]
    calls = []
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def executor(task):
        calls.append(task.task_kind)
        if len(calls) == 1:
            entered.set()
            await gate.wait()
        return task.task_kind

    q = CalcQueue(db, executor, fairness_max_wait_s=60.0,
                  clock=lambda: clock[0], metrics=MetricsRegistry())
    try:
        q.enqueue(1, "H1", "live_h1")
        await entered.wait()
        q.enqueue(9, "D1", "backfill")          # t=1000, класс 4
        clock[0] += 121.0                        # два периода ожидания → 4−2=2
        q.enqueue(1, "D1", "live_htf")           # класс 2, но свежее
        q.enqueue(2, "D1", "recovery")           # класс 3
        gate.set()
        await asyncio.gather(
            q.wait_done(9, "D1", "backfill", 5),
            q.wait_done(1, "D1", "live_htf", 5),
            q.wait_done(2, "D1", "recovery", 5),
        )
        assert calls == ["live_h1", "backfill", "live_htf", "recovery"]
    finally:
        await q.stop()


async def test_overflow_rejects_only_lowest_priority_new(db):
    """Переполнение: отклоняется НОВОЕ задание, не срочнее самого
    низкоприоритетного в очереди; уже поставленные не выбрасываются."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def executor(task):
        entered.set()
        await gate.wait()
        return None

    metrics = MetricsRegistry()
    q = CalcQueue(db, executor, max_queue=3, metrics=metrics)
    try:
        assert q.enqueue(1, "H1", "live_h1")     # уходит в running (ждёт gate)
        await entered.wait()
        assert q.enqueue(1, "D1", "backfill")
        assert q.enqueue(2, "D1", "backfill")
        assert q.enqueue(3, "D1", "backfill")
        assert q.depth() == 3
        # новый backfill не срочнее худшего в очереди — отклонён
        assert not q.enqueue(4, "D1", "backfill")
        # live_h1 срочнее — принят сверх лимита, очередь задачи не теряет
        assert q.enqueue(4, "D1", "live_h1")
        assert q.depth() == 4
        rows = _rows(db)
        assert len(rows) == 5 and all(r["status"] in ("pending", "running")
                                      for r in rows)
        series = metrics.snapshot()["series"]
        assert series["calc_queue_overload{task_kind=backfill}"]["value"] == 1
        # stop(): текущий прогон дообрабатывает (строка удаляется),
        # ожидающие остаются pending для восстановления — не выбрасываются
        stop_task = asyncio.create_task(q.stop())
        await asyncio.sleep(0)  # stop() успевает выставить _stopping
        gate.set()
        await stop_task
    finally:
        await q.stop()
    assert len(_rows(db)) == 4


async def test_wait_done_timeout(db):
    gate = asyncio.Event()

    async def executor(task):
        await gate.wait()
        return None

    q = CalcQueue(db, executor, metrics=MetricsRegistry())
    try:
        q.enqueue(1, "D1", "live_htf")
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await q.wait_done(1, "D1", "live_htf", 0.05)
        gate.set()
        assert await q.wait_done(1, "D1", "live_htf", 5) is None
    finally:
        await q.stop()


async def test_failed_task_rolls_back_and_retries(db):
    """AC18: исполнитель упал — задание остаётся failed-строкой с ошибкой
    (диагностика), ожидающий получает исходное исключение, следующая
    постановка (следующий цикл синхронизации) перезапускает задание."""
    attempts = []

    async def executor(task):
        attempts.append(task.target_cursor)
        if len(attempts) == 1:
            raise RuntimeError("calculation_failed")
        return "ok"

    metrics = MetricsRegistry()
    q = CalcQueue(db, executor, metrics=metrics)
    try:
        q.enqueue(1, "D1", "live_htf", target_cursor=10)
        with pytest.raises(RuntimeError, match="calculation_failed"):
            await q.wait_done(1, "D1", "live_htf", 5)
        (row,) = _rows(db)
        assert row["status"] == "failed"
        assert "RuntimeError: calculation_failed" in row["error"]
        assert q.depth() == 0
        # повторная постановка того же ключа — задание выполняется снова
        q.enqueue(1, "D1", "live_htf", target_cursor=11)
        assert await q.wait_done(1, "D1", "live_htf", 5) == "ok"
        assert attempts == [10, 11]
        assert _rows(db) == []
        series = metrics.snapshot()["series"]
        assert series["calc_queue_failed{task_kind=live_htf}"]["value"] == 1
        assert series["calc_queue_completed{task_kind=live_htf}"]["value"] == 1
    finally:
        await q.stop()


async def test_crash_restore_reruns_pending_and_running(tmp_path):
    """AC18: крах без stop() — running/pending строки calc_task поднимаются
    новым процессом (running → pending) и выполняются заново."""
    path = str(tmp_path / "calcq.db")
    db = Database(path)
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = []

    async def executor(task):
        calls.append(task.key)
        if len(calls) == 1:
            entered.set()
            await gate.wait()
        return "ok"

    q = CalcQueue(db, executor, metrics=MetricsRegistry())
    q.enqueue(9, "D1", "backfill", target_cursor=5)
    await entered.wait()
    q.enqueue(1, "D1", "live_htf", target_cursor=10)
    q.enqueue(1, "H1", "live_h1", target_cursor=7)
    await _until(lambda: len(_rows(db)) == 3)
    statuses = {r["task_kind"]: r["status"] for r in _rows(db)}
    assert statuses == {"backfill": "running", "live_htf": "pending",
                        "live_h1": "pending"}
    # крах: потребитель отменяется посреди выполнения, без stop()
    for consumer in q._consumers:
        consumer.cancel()
    await asyncio.gather(*q._consumers, return_exceptions=True)
    db.close()

    db2 = Database(path)
    done = []

    async def executor2(task):
        done.append((task.key, task.target_cursor))
        return "ok"

    q2 = CalcQueue(db2, executor2, metrics=MetricsRegistry())
    try:
        q2.start()  # running/failed/pending → pending
        assert {r["task_kind"]: r["status"] for r in _rows(db2)} == {
            "backfill": "pending", "live_htf": "pending", "live_h1": "pending"}
        await asyncio.gather(
            q2.wait_done(9, "D1", "backfill", 5),
            q2.wait_done(1, "D1", "live_htf", 5),
            q2.wait_done(1, "H1", "live_h1", 5),
        )
        # live_h1 (0) → live_htf (2) → backfill (4)
        assert [k for k, _ in done] == [
            (1, "H1", "live_h1"), (1, "D1", "live_htf"), (9, "D1", "backfill")]
        assert [c for _, c in done] == [7, 10, 5]  # границы из строк сохранились
        assert _rows(db2) == []
    finally:
        await q2.stop()
        db2.close()


async def test_failed_row_retried_at_restart(tmp_path):
    """failed-строка переживает рестарт и перезапускается start() (§9/AC18)."""
    path = str(tmp_path / "calcq_fail.db")
    db = Database(path)

    async def bad(task):
        raise RuntimeError("boom")

    q = CalcQueue(db, bad, metrics=MetricsRegistry())
    q.enqueue(1, "D1", "live_htf")
    with pytest.raises(RuntimeError):
        await q.wait_done(1, "D1", "live_htf", 5)
    for consumer in q._consumers:
        consumer.cancel()
    await asyncio.gather(*q._consumers, return_exceptions=True)
    assert _rows(db)[0]["status"] == "failed"
    db.close()

    db2 = Database(path)
    done = []

    async def good(task):
        done.append(task.key)
        return "ok"

    q2 = CalcQueue(db2, good, metrics=MetricsRegistry())
    try:
        q2.start()
        assert await q2.wait_done(1, "D1", "live_htf", 5) == "ok"
        assert done == [(1, "D1", "live_htf")]
        assert _rows(db2) == []
    finally:
        await q2.stop()
        db2.close()


async def test_diagnostics_and_gauges(db):
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def executor(task):
        entered.set()
        await gate.wait()
        return None

    q = CalcQueue(db, executor, max_queue=10, fairness_max_wait_s=30,
                  metrics=MetricsRegistry())
    try:
        q.enqueue(1, "H1", "live_h1")
        await entered.wait()
        q.enqueue(1, "D1", "live_htf")
        q.enqueue(2, "D1", "live_htf")
        diag = q.diagnostics()
        assert diag["depth"] == 2
        assert diag["pending_by_kind"] == {"live_htf": 2}
        assert diag["running"] == [
            {"instrument_id": 1, "timeframe": "H1", "task_kind": "live_h1",
             "run_count": 1}]
        assert diag["concurrency"] == 1 and diag["max_queue"] == 10
        assert diag["counters"]["calc_queue_enqueued{task_kind=live_h1}"] == 1
        gate.set()
    finally:
        await q.stop()


def test_diagnostics_section_in_build_diagnostics(db):
    """/api/diagnostics: секция calc_queue — persisted-счётчики и последняя
    failed-задача видны и без in-memory очереди (web-роль, без секретов)."""
    from app.config import Settings
    from app.services.runtime import build_diagnostics

    now = 1_790_000_000_000
    db.conn.execute(
        "INSERT INTO calc_task (instrument_id, timeframe, task_kind, priority,"
        " status, created_at, updated_at) VALUES (1, 'D1', 'live_htf', 2,"
        " 'pending', ?, ?)", (now, now))
    db.conn.execute(
        "INSERT INTO calc_task (instrument_id, timeframe, task_kind, priority,"
        " status, error, created_at, updated_at) VALUES (2, 'H1', 'live_h1', 0,"
        " 'failed', 'RuntimeError: boom', ?, ?)", (now, now + 1))
    db.conn.commit()
    diag = build_diagnostics(
        db, Settings(db_path=":memory:"), role="web", instance_id="t",
        started_at=now, build_id="t")
    section = diag["calc_queue"]
    assert section["persisted"] == {"pending": 1, "running": 0, "failed": 1}
    assert section["last_failed"] == {
        "instrument_id": 2, "timeframe": "H1", "task_kind": "live_h1",
        "error": "RuntimeError: boom", "updated_at": now + 1}
    assert "queue" not in section  # in-memory детали — только из calc-роли
