"""Воркер end-to-end без сети (§11): seed инструментов, backfill + replay_done,
live-опрос с доставкой события через диспетчер, freshness-контроль
(stale → сервисное сообщение, восстановление → сообщение, без повторов)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.adapters.base import AdapterError
from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import (
    TIMEFRAME_MINUTES,
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.worker import SEED, Worker

from .conftest import make_candle

D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000
W1_MS = TIMEFRAME_MINUTES["W1"] * 60_000


class FakeAdapter:
    """In-memory источник: каталог SEED-инструментов, свечи из списка."""

    venue = "binance"

    def __init__(self, symbols=None):
        self._symbols = list(symbols if symbols is not None else [s for _, s in SEED])
        self.candles = []
        self.price = (110.0, now_ms())
        self.fail = False
        self.klines_calls = 0

    async def catalog(self):
        return [
            Instrument(None, s.replace("USDT", ""), "binance", "spot", s, "USDT")
            for s in self._symbols
        ]

    async def klines(self, symbol, timeframe, start_ms, end_ms, include_forming=False):
        if self.fail:
            raise AdapterError("источник недоступен (тест)")
        self.klines_calls += 1
        return [
            replace(c) for c in self.candles
            if c.timeframe == timeframe and start_ms <= c.open_time <= end_ms
        ]

    async def last_price(self, symbol):
        if self.fail:
            raise AdapterError("источник недоступен (тест)")
        return self.price

    async def status(self):
        return {"ok": not self.fail}


def _make_worker(db, adapter, sender=None):
    settings = Settings()
    settings.detector = DetectorConfig()
    # быстрый цикл котировок выключен — тесты гоняют котировку через poll_once
    settings.quote_poll_seconds = 0
    sender = sender or LogSender()
    dispatcher = EventDispatcher(db, settings.detector, sender)
    worker = Worker(db, settings, settings.detector, {"binance": adapter}, dispatcher)
    return worker, sender


def _deliveries(db):
    return db.conn.execute(
        "SELECT status, event_ids FROM delivery ORDER BY id"
    ).fetchall()


def _fresh_candles(close: float = 110.0):
    """По свежей свече D1 и W1 — freshness-контроль молчит."""
    now = now_ms()
    return [
        make_candle(now - D1_MS, close - 1, close + 1, close - 2, close, "D1"),
        make_candle(now - W1_MS, close - 1, close + 1, close - 2, close, "W1"),
    ]


async def test_seed_instruments_from_catalog():
    """§1: стартовые инструменты заводятся по каталогу источника, повторный
    seed не плодит дублей; отсутствующий символ — сервисное сообщение."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    worker, _ = _make_worker(db, adapter)

    await worker.seed_instruments()
    await worker.seed_instruments()
    instruments = db.get_instruments()
    assert len(instruments) == sum(venue in worker.adapters for venue, _ in SEED)
    assert {i.symbol for i in instruments} == {s for venue, s in SEED if venue in worker.adapters}

    # символа нет в каталоге — явное сервисное сообщение владельцу (без подмены)
    db2 = Database(":memory:")
    worker2, _ = _make_worker(db2, FakeAdapter(symbols=["BTCUSDT"]))
    await worker2.seed_instruments()
    missing = [s for venue, s in SEED if venue in worker2.adapters and s != "BTCUSDT"]
    rows = db2.conn.execute("SELECT * FROM notification_packet WHERE channel='service'").fetchall()
    assert len(rows) == len(missing)
    assert all(r["status"] == "pending" and r["quiet"] for r in rows)


async def test_backfill_loads_history_and_marks_replay_done():
    """§11: backfill догружает свечи и гоняет replay; повторный запуск
    без новых свечей ничего не перекачивает (replay_done)."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _fresh_candles()
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = db.get_instruments()[0]

    await worker.backfill(ins)
    assert db.get_candles(ins.id, "D1")
    assert db.get_meta(f"replay_done:{ins.id}")

    calls = worker.klines_calls = adapter.klines_calls
    await worker.backfill(ins)
    assert adapter.klines_calls == calls  # повторный backfill — без сети


async def test_poll_dispatches_market_event():
    """Live-путь: новая цена внутри активной зоны → событие доставлено
    через диспетчер (LogSender), delivery зафиксирована как sent."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _fresh_candles(close=110.0)  # закрытия ВНЕ зоны
    adapter.price = (99.0, now_ms())               # текущая цена — в зоне
    worker, sender = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    base = now_ms() - 10 * D1_MS
    zid = db.insert_zone(Zone(
        id=None, instrument_id=ins.id, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=90.0, upper=100.0, formed_at=base,
        confirmed_at=base, status=ZoneStatus.ACTIVE,
    ))
    assert zid is not None

    await worker.poll_once()

    assert len(sender.cards) == 1
    assert "Цена коснулась зоны" in sender.cards[0][0].text
    market = [r for r in _deliveries(db) if r["event_ids"] != "[]"]
    assert len(market) == 1 and market[0]["status"] == "sent"


async def test_stale_and_recovery_service_messages():
    """§11: недоступность источника → сервисное сообщение на каждый ТФ один
    раз; после восстановления — сообщение о восстановлении; повторов нет."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.fail = True
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()

    await worker.poll_once()
    incidents = db.conn.execute("SELECT * FROM notification_incident").fetchall()
    assert len(incidents) == 1  # one source-wide incident
    assert incidents[0]["recovered_at"] is None
    await worker.poll_once()
    assert db.conn.execute("SELECT COUNT(*) FROM notification_incident").fetchone()[0] == 1
    adapter.fail = False
    adapter.candles = _fresh_candles()
    await worker.poll_once()
    assert db.conn.execute("SELECT recovered_at FROM notification_incident").fetchone()[0] is not None
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await worker.dispatcher.outbox.flush()
    digests = db.conn.execute("SELECT * FROM notification_packet WHERE channel='digest'").fetchall()
    assert len(digests) == 1 and digests[0]["status"] == "sent"


async def test_backfill_extends_history_backwards():
    """Увеличение окна lookback догружает раннюю историю назад от первой
    сохранённой свечи; повторный запуск с тем же окном — без сети."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _fresh_candles()
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = db.get_instruments()[0]

    await worker.backfill(ins)
    first_d1 = db.first_candle(ins.id, "D1")
    assert first_d1 is not None

    # окно D1 расширили, у источника нашлись более ранние свечи
    old = [
        make_candle(first_d1.open_time - 2 * D1_MS, 100.0, 105.0, 95.0, 102.0, "D1"),
        make_candle(first_d1.open_time - D1_MS, 102.0, 106.0, 100.0, 104.0, "D1"),
    ]
    adapter.candles += old
    worker.cfg.lookback_days_d1 = 36500  # окно заведомо глубже хвоста

    await worker.backfill(ins)
    assert db.first_candle(ins.id, "D1").open_time == old[0].open_time

    calls = adapter.klines_calls
    await worker.backfill(ins)
    assert adapter.klines_calls == calls  # то же окно — без сети


async def test_backfill_tail_keeps_existing_candidate():
    """Рестарт с новой закрытой свечой не переигрывает историю и не
    снимает уже записанного кандидата."""
    db = Database(":memory:")
    adapter = FakeAdapter()
    old = now_ms() - 5 * D1_MS
    adapter.candles = [
        make_candle(old, 100.0, 106.0, 99.0, 104.0, "D1"),
        make_candle(now_ms() - W1_MS, 100.0, 106.0, 99.0, 104.0, "W1"),
    ]
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    replays: list[int] = []
    original = worker.scanner.replay_instrument

    def _spy(*args, **kwargs):
        replays.append(1)
        return original(*args, **kwargs)

    worker.scanner.replay_instrument = _spy
    await worker.backfill(ins)
    assert replays == [1]

    zid = db.insert_zone(Zone(
        id=None, instrument_id=ins.id, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=90.0, upper=100.0, formed_at=old,
        confirmed_at=None, status=ZoneStatus.CANDIDATE,
    ))
    assert zid is not None
    adapter.candles.append(
        make_candle(old + D1_MS, 94.0, 96.0, 93.0, 95.0, "D1")
    )
    await worker.backfill(ins)

    assert replays == [1]
    kept = db.get_zone(zid)
    assert kept is not None
    assert kept.status == ZoneStatus.CANDIDATE
    assert kept.market_validity == "active"
    assert kept.display_until is None
    assert db.last_candle(ins.id, "D1").open_time == old + D1_MS
