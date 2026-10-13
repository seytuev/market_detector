"""Этап 5 perf-спеки (§11, §15; AC01/AC14): фазы готовности и /api/ready,
фоновый backfill с чекпоинтами и догрузкой после перезапуска, устойчивость
seed к сетевой ошибке каталога, порядок этапов ручного job."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.adapters.base import AdapterError, QuoteTick, TIMEFRAME_MS
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Instrument, now_ms
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.data_sync import JOB_KEY, period_open, read_json
from app.services.metrics import get_registry
from app.web.api import create_app
from app.worker import Worker
from .conftest import FIXTURE_PATH, make_candle


class GateExchange:
    """In-memory источник с заслонкой: klines ждут gate, каталог и цены мгновенны."""

    venue = "binance"

    def __init__(self):
        self.gate = asyncio.Event()
        self.calls = []
        self.rows = {}
        self.catalog_fails = 0

    async def catalog(self):
        if self.catalog_fails > 0:
            self.catalog_fails -= 1
            raise AdapterError("каталог недоступен (сеть)")
        return [
            Instrument(None, s.replace("USDT", ""), "binance", "spot", s, "USDT")
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT")
        ]

    async def klines(self, symbol, tf, start, end, include_forming=False):
        self.calls.append((symbol, tf, start, end))
        await self.gate.wait()
        return [
            replace(c) for c in self.rows.get((symbol, tf), [])
            if start <= c.open_time <= end and (include_forming or c.closed)
        ]

    async def last_price(self, symbol):
        return QuoteTick(100.0, now_ms(), now_ms(), "trade")

    async def status(self):
        return {"ok": True}


def _rows(tf, iid, *, count=6):
    """count-1 закрытых свечей до текущего периода + формирующаяся."""
    opening = period_open(tf, now_ms())
    step = TIMEFRAME_MS[tf]
    rows = [
        make_candle(opening - i * step, 100, 101, 99, 100, tf, iid, source="binance")
        for i in range(count - 1, 0, -1)
    ]
    rows.append(make_candle(opening, 100, 101, 99, 100, tf, iid,
                            closed=False, source="binance"))
    return rows


def _etalon_rows(tf, iid):
    """Форма эталона BTC H4 (30 свечей, даёт зоны/события движка), перенесённая
    на сетку tf: последняя закрытая свеча — предыдущий период (канал свеж)."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    step = TIMEFRAME_MS[tf]
    opening = period_open(tf, now_ms())
    n = len(raw)
    return [
        make_candle(opening - (n - i) * step, r["open"], r["high"], r["low"],
                    r["close"], tf, iid, source="binance")
        for i, r in enumerate(raw)
    ]


def _make_worker(db, exchange, tmp_path):
    settings = Settings()
    settings.detector = DetectorConfig()
    settings.db_path = str(tmp_path / "test.db")  # миграции/настройки — не в репозиторий
    settings.auth_token = "test-token"
    settings.events_enabled = False
    dispatcher = EventDispatcher(db, settings.detector, LogSender())
    return Worker(
        db, settings, settings.detector, {"binance": exchange}, dispatcher,
        ltf_engine=LtfEngine(db, settings.detector),
    )


async def _wait_phase(worker, phase, timeout=10.0):
    for _ in range(int(timeout / 0.005)):
        if worker.readiness.phase == phase:
            return True
        await asyncio.sleep(0.005)
    return False


async def test_readiness_phases_background_backfill_and_ready_endpoint(tmp_path):
    """AC01/§11: web → realtime (ready=true, котировки текут) пока backfill
    заблокирован медленным источником; после разблокировки — history → calc.
    Фазы достигаются строго по порядку, прогресс backfill виден с пустой БД."""
    db = Database(":memory:")
    exchange = GateExchange()  # заслонка закрыта: klines ждут
    worker = _make_worker(db, exchange, tmp_path)
    worker.settings.quote_poll_seconds = 3600  # один проход котировок на старте
    worker.settings.poll_seconds = 3600
    # инструменты уже известны (БД прошлого запуска) + H1-хвост на месте —
    # LTF-restore не ждёт заблокированный источник и не мешает проверке фаз
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT"):
        iid = db.upsert_instrument(
            Instrument(None, symbol[:-4], "binance", "spot", symbol, "USDT"))
        opening = period_open("H1", now_ms())
        db.insert_candles([make_candle(opening - TIMEFRAME_MS["H1"], 100, 101, 99,
                                       100, "H1", iid, source="binance")])
    app = create_app(db, worker.settings)
    app.state.data_sync = worker.data_sync

    task = asyncio.create_task(worker.run())
    try:
        assert await _wait_phase(worker, "realtime"), worker.readiness.snapshot()
        # backfill-задача создаётся после LTF-restore — подождём её появления
        for _ in range(400):
            if worker._backfill_task is not None:
                break
            await asyncio.sleep(0.005)
        assert worker._backfill_task is not None
        # backfill идёт в фоне и НЕ завершён: источник заблокирован
        await asyncio.sleep(0.05)
        assert not worker._backfill_task.done()
        assert worker.readiness.phase == "realtime"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = (await client.get("/api/ready")).json()
            assert body["ready"] is True and body["phase"] == "realtime"
            assert body["phases"]["web"] and body["phases"]["realtime"]
            assert body["phases"]["history"] is None and body["phases"]["calc"] is None
            # пустая БД: индикаторы прогресса присутствуют с самого начала
            assert body["backfill"]["total"] == len(db.get_instruments(enabled_only=True)) * 2
            assert body["backfill"]["completed"] == 0
            assert {c["status"] for c in body["backfill"]["channels"]} == {"pending"}
        # котировки текут, пока история заблокирована (быстрый цикл отработал)
        ins = db.get_instruments(enabled_only=True)[0]
        for _ in range(200):
            if db.get_quote_detail(ins.id) is not None:
                break
            await asyncio.sleep(0.005)
        assert db.get_quote_detail(ins.id) is not None

        exchange.gate.set()  # источник отвечает — история догружается
        assert await _wait_phase(worker, "calc"), worker.readiness.snapshot()
        snap = worker.readiness.snapshot()
        ats = [snap["phases"][p]["at"] for p in ("web", "realtime", "history", "calc")]
        assert all(a is not None for a in ats) and ats == sorted(ats)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = (await client.get("/api/ready")).json()
            assert body["phase"] == "calc"
            assert body["backfill"]["completed"] == body["backfill"]["total"]
    finally:
        exchange.gate.set()
        worker.stop()
        await asyncio.wait_for(task, 10)


async def test_ready_endpoint_without_worker(tmp_path):
    """Веб без присоединённого воркера: 200, ready=false, фаза web."""
    db = Database(":memory:")
    settings = Settings()
    settings.detector = DetectorConfig()
    settings.db_path = str(tmp_path / "test.db")
    app = create_app(db, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is False and body["phase"] == "web"
        assert body["backfill"]["total"] == 0
    db.close()


def _event_rows(db):
    return [
        tuple(r)
        for r in db.conn.execute(
            "SELECT zone_id, cycle_id, kind, occurred_at, price, delayed"
            " FROM event ORDER BY id"
        ).fetchall()
    ]


async def test_backfill_checkpoint_resume_after_interruption(tmp_path):
    """§11: обрыв backfill на середине (отмена задачи на заблокированном
    запросе) → перезапуск продолжает с чекпоинта, перекрытие хвоста
    перепроверяется повторной выборкой, дублей событий нет — таблица event
    совпадает с эталоном одного непрерывного прогона."""
    # --- эталон: один непрерывный прогон по тому же инструменту ---
    ref_db = Database(":memory:")
    ref_exchange = GateExchange()
    ref_exchange.gate.set()
    ref_worker = _make_worker(ref_db, ref_exchange, tmp_path)
    await ref_worker.seed_instruments()
    ref_ins = ref_db.get_instruments(enabled_only=True)[0]
    for tf in ("D1", "W1"):
        ref_exchange.rows[ref_ins.symbol, tf] = _etalon_rows(tf, ref_ins.id)
    await ref_worker.backfill(ref_ins)
    reference = _event_rows(ref_db)
    assert reference, "эталонная форма должна давать события — иначе сравнение пустое"
    ref_candles = {
        tf: [c.open_time for c in ref_db.get_candles(ref_ins.id, tf)]
        for tf in ("D1", "W1")
    }
    await ref_worker.calc_queue.stop()
    ref_db.close()

    # --- прерванный прогон: источник «умирает» после хвоста D1 ---
    db = Database(":memory:")
    exchange = GateExchange()
    exchange.gate.set()
    worker = _make_worker(db, exchange, tmp_path)
    await worker.seed_instruments()
    ins = db.get_instruments(enabled_only=True)[0]
    assert ins.id == ref_ins.id
    for tf in ("D1", "W1"):
        exchange.rows[ins.symbol, tf] = _etalon_rows(tf, ins.id)

    original_klines = exchange.klines

    async def klines_stall(symbol, tf, start, end, include_forming=False):
        if exchange.calls:  # первая выборка (хвост D1) проходит, дальше — ступор
            exchange.gate.clear()
        return await original_klines(symbol, tf, start, end,
                                     include_forming=include_forming)

    exchange.klines = klines_stall
    task = asyncio.create_task(worker.backfill(ins))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # частичное состояние: хвост D1 вставлен, чекпоинт D1 НЕ завершён, W1 пуст
    assert db.last_candle(ins.id, "D1") is not None
    cp = json.loads(db.get_meta(f"backfill:checkpoint:{ins.id}:D1"))
    assert cp["completed"] is False
    assert db.last_candle(ins.id, "W1") is None

    # --- «перезапуск»: то же состояние БД, источник исправен ---
    exchange.klines = original_klines
    exchange.gate.set()
    calls_before_resume = len(exchange.calls)
    await worker.backfill(ins)
    # перекрытие хвоста D1 перепроверено повторным запросом (сверка при резюме):
    # запрос начинается РАНЬШЕ последней известной свечи, вставка идемпотентна
    refetched = [c for c in exchange.calls[calls_before_resume:]
                 if c[0] == ins.symbol and c[1] == "D1"]
    assert any(c[2] < db.last_candle(ins.id, "D1").open_time for c in refetched), \
        "резюме должно переспросить перекрывающийся участок хвоста"
    assert _event_rows(db) == reference
    for tf, opens in ref_candles.items():
        assert [c.open_time for c in db.get_candles(ins.id, tf)] == opens
    for tf in ("D1", "W1"):
        assert json.loads(db.get_meta(f"backfill:checkpoint:{ins.id}:{tf}"))["completed"] is True
    await worker.calc_queue.stop()


async def test_seed_catalog_failure_falls_back_to_db_and_retries(tmp_path):
    """§11: сетевая ошибка каталога не блокирует известные инструменты из БД;
    seed откладывается с метрикой и фоновым retry, который досеивает
    инструменты при восстановлении источника."""
    db = Database(":memory:")
    known = db.upsert_instrument(
        Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT"))
    exchange = GateExchange()
    exchange.gate.set()
    exchange.catalog_fails = 1  # первый каталог падает по сети
    worker = _make_worker(db, exchange, tmp_path)
    worker.seed_catalog_retry_seconds = 0.01
    worker.seed_catalog_max_attempts = 3

    await worker.seed_instruments()  # не падает и не блокируется
    assert any(i.id == known for i in db.get_instruments())
    assert "binance" in worker._seed_pending_venues
    task = worker._catalog_retry_task
    assert task is not None
    series = get_registry().snapshot()["series"]
    assert series["seed_catalog_fallback{venue=binance}"]["value"] >= 1

    # фоновый retry: каталог восстановился → недостающие SEED досеяны и
    # прошли backfill внутри retry-задачи
    await asyncio.wait_for(asyncio.shield(task), 10)
    assert not worker._seed_pending_venues
    symbols = {i.symbol for i in db.get_instruments()}
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT"} <= symbols
    for i in db.get_instruments(enabled_only=True):
        for tf in ("D1", "W1"):
            cp = db.get_meta(f"backfill:checkpoint:{i.id}:{tf}")
            assert cp is None or json.loads(cp)["completed"] is True
    await worker.calc_queue.stop()


async def test_manual_job_stage_ordering_quotes_before_history(tmp_path):
    """AC14/§15: котировки ВСЕХ инструментов завершаются до старта свечной
    стадии; медленная история одного актива не задерживает котировки
    остальных. JSON job: stages с именами/статусами, прежние ключи на месте."""
    db = Database(":memory:")
    exchange = GateExchange()
    exchange.gate.set()
    worker = _make_worker(db, exchange, tmp_path)
    worker.data_sync.ready = True
    await worker.seed_instruments()
    instruments = db.get_instruments(enabled_only=True)
    for ins in instruments:
        for tf in ("D1", "W1", "H1"):
            exchange.rows[ins.symbol, tf] = _rows(tf, ins.id)

    quote_done = {}
    klines_started = {}
    original_poll = worker._poll_quote
    original_klines = exchange.klines

    async def poll_spy(ins, **kw):
        try:
            return await original_poll(ins, **kw)
        finally:
            quote_done[ins.id] = asyncio.get_running_loop().time()

    async def klines_spy(symbol, tf, start, end, include_forming=False):
        klines_started.setdefault(symbol, asyncio.get_running_loop().time())
        await asyncio.sleep(0.02)  # медленная история
        return await original_klines(symbol, tf, start, end,
                                     include_forming=include_forming)

    worker._poll_quote = poll_spy
    exchange.klines = klines_spy
    job = worker.data_sync.start()
    await worker.data_sync._task
    result = read_json(db, JOB_KEY)
    assert result["status"] == "succeeded", result.get("errors")
    # все котировки завершены до первого запроса свечей
    assert len(quote_done) == len(instruments)
    assert klines_started, "каналы свечей не запрашивались"
    assert max(quote_done.values()) <= min(klines_started.values())
    # этапы: имена и статусы (§15), завершение по факту, не по постановке
    stages = {s["name"]: s for s in result["stages"]}
    assert list(stages) == ["quotes", "fresh_candles", "history"]
    assert stages["quotes"]["status"] == "completed"
    assert stages["fresh_candles"]["status"] == "completed"
    assert stages["quotes"]["finished_at"] <= stages["fresh_candles"]["started_at"]
    for st in result["stages"]:
        assert set(st) == {"name", "status", "started_at", "finished_at", "detail"}
    # обратная совместимость (§13): прежние поля job и порядок каналов
    assert result["job_id"] == job["job_id"]
    assert result["channels_total"] == result["channels_completed"] == len(instruments) * 4
    assert [(r["instrument_id"], r["timeframe"]) for r in result["results"][:4]] == [
        (instruments[0].id, tf) for tf in ("quote", "D1", "W1", "H1")
    ]
    assert all(r["status"] == "ok" for r in result["results"])
    await worker.calc_queue.stop()
    await worker.market_queue.stop()


async def test_diagnostics_expose_readiness_and_backfill(tmp_path):
    """/api/diagnostics (авторизованный) несёт фазы готовности и прогресс
    backfill по чекпоинтам — тёплое состояние UI (этап 5, данные для §14)."""
    db = Database(":memory:")
    exchange = GateExchange()
    exchange.gate.set()
    worker = _make_worker(db, exchange, tmp_path)
    worker.readiness.reach("web")
    worker.readiness.reach("realtime")
    await worker.seed_instruments()
    ins = db.get_instruments(enabled_only=True)[0]
    worker._save_backfill_checkpoint(ins, "D1", done_through_ms=1, completed=True)
    app = create_app(db, worker.settings)
    app.state.data_sync = worker.data_sync
    auth = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        diag = (await client.get("/api/diagnostics", headers=auth)).json()
    assert diag["readiness"]["phase"] == "realtime"
    channels = {(c["symbol"], c["timeframe"]): c["status"] for c in diag["backfill"]["channels"]}
    assert channels[ins.symbol, "D1"] == "completed"
    assert channels[ins.symbol, "W1"] == "pending"
    assert diag["backfill"]["completed"] == 1
    await worker.calc_queue.stop()
