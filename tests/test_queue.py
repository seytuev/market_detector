"""Очередь доставки: идемпотентность, объединение, ретрай (§8, §9, §11).

Без сети: отправитель — LogSender / падающая заглушка.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.models import (
    TIMEFRAME_MINUTES,
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.queue import EventDispatcher, MessagePayload
from app.notify.telegram import LogSender, render_text

from .conftest import make_candle


class FailingSender:
    """Транспорт, который всегда падает — для проверки failed-ретрая."""

    def __init__(self):
        self.calls = 0

    async def send(self, payload: MessagePayload) -> None:
        self.calls += 1
        raise RuntimeError("network down")

    async def send_text(self, text: str) -> None:
        raise RuntimeError("network down")


def _make_db() -> tuple[Database, dict]:
    db = Database(":memory:")
    btc = db.upsert_instrument(
        Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT")
    )
    eth = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    t0 = now_ms()
    btc_zone = db.insert_zone(
        Zone(None, btc, ZoneType.OB, Direction.BULL, "W1",
             lower=65000.0, upper=66000.0, formed_at=t0 - 10_000,
             confirmed_at=t0 - 9_000, status=ZoneStatus.ACTIVE, created_at=t0)
    )
    eth_zone = db.insert_zone(
        Zone(None, eth, ZoneType.FVG, Direction.BEAR, "D1",
             lower=3000.0, upper=3100.0, formed_at=t0 - 10_000,
             confirmed_at=t0 - 9_000, status=ZoneStatus.ACTIVE, created_at=t0)
    )
    return db, {"btc_zone": btc_zone, "eth_zone": eth_zone}


def _new_event(db: Database, zone_id: int, kind: EventKind,
               price: float, occurred_at: int) -> Event:
    """Рыночное событие сначала пишет движок — dispatcher только доставляет."""
    eid = db.insert_event(
        Event(None, zone_id, 1, kind, occurred_at=occurred_at,
              detected_at=occurred_at, price=price)
    )
    assert eid is not None
    return next(e for e in db.get_events(zone_id=zone_id) if e.id == eid)


def _delivery_count(db: Database, status: str | None = None) -> int:
    q = "SELECT COUNT(*) AS c FROM delivery"
    if status:
        q += f" WHERE status='{status}'"
    return db.conn.execute(q).fetchone()["c"]


def _event_count(db: Database) -> int:
    return db.conn.execute("SELECT COUNT(*) AS c FROM event").fetchone()["c"]


async def test_merge_two_assets_into_one_message():
    """§9: одновременные сигналы объединяются; пакет не скрывает второй актив."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    t = now_ms()
    events = [
        _new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t),
        _new_event(db, z["eth_zone"], EventKind.TOUCH, 3050.0, t),
    ]
    deliveries = await disp.dispatch(events)

    assert len(sender.sent) == 1  # одно сообщение на вызов
    payload = sender.sent[0]
    assert len(payload.events) == 2  # отдельные объекты сохранены
    text = render_text(payload)
    assert "BTC" in text and "ETH" in text  # оба актива видны
    assert len(deliveries) == 2  # delivery на каждое событие
    assert all(d.status == "sent" for d in deliveries)


async def test_redispatch_same_events_no_second_delivery():
    """Идемпотентность: повторный dispatch тех же событий не дублирует delivery."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    t = now_ms()
    # DEPTH_90 не подавляется по §8 — проверяем именно ключ идемпотентности
    events = [_new_event(db, z["btc_zone"], EventKind.DEPTH_90, 65600.0, t)]

    await disp.dispatch(events)
    await disp.dispatch(events)
    await disp.dispatch(events)

    assert _delivery_count(db) == 1
    assert len(sender.sent) == 1


async def test_retry_pending_resends_failed_without_duplicates():
    """§11 п.5: failed-доставка повторяется; sent не дублируется."""
    db, z = _make_db()
    t = now_ms()
    events = [
        _new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t),
        _new_event(db, z["eth_zone"], EventKind.TOUCH, 3050.0, t),
    ]

    failing = FailingSender()
    disp_fail = EventDispatcher(db, DetectorConfig(), failing)
    deliveries = await disp_fail.dispatch(events)
    assert all(d.status == "failed" for d in deliveries)
    assert _delivery_count(db, "failed") == 2

    sender = LogSender()
    disp_ok = EventDispatcher(db, DetectorConfig(), sender)
    await disp_ok.retry_pending()
    assert len(sender.sent) == 2  # по одному повтору на failed-доставку
    assert _delivery_count(db, "sent") == 2
    assert _delivery_count(db, "failed") == 0
    assert db.pending_deliveries() == []

    # повторный retry: отправлять нечего, sent не задваивается
    await disp_ok.retry_pending()
    assert len(sender.sent) == 2
    assert _delivery_count(db) == 2


async def test_delivery_retry_creates_no_market_events():
    """§9: технический ретрай не создаёт вторую запись рыночного события."""
    db, z = _make_db()
    t = now_ms()
    events = [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t)]
    before = _event_count(db)

    failing = FailingSender()
    disp = EventDispatcher(db, DetectorConfig(), failing)
    await disp.dispatch(events)
    await disp.dispatch(events)  # повторная обработка того же события

    disp_ok = EventDispatcher(db, DetectorConfig(), LogSender())
    await disp_ok.retry_pending()
    await disp_ok.retry_pending()

    assert _event_count(db) == before  # событие одно — меняется только доставка


async def test_notify_only_reviewed_silences_candidates():
    """§10: режим «только подтверждённые» — авто-кандидаты молчат."""
    db, z = _make_db()
    db.update_zone(z["btc_zone"], status=ZoneStatus.CANDIDATE)
    sender = LogSender()
    cfg = DetectorConfig(notify_only_reviewed=True)
    disp = EventDispatcher(db, cfg, sender)
    t = now_ms()
    events = [
        _new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t),      # кандидат
        _new_event(db, z["eth_zone"], EventKind.TOUCH, 3050.0, t),       # active
    ]
    await disp.dispatch(events)

    assert len(sender.sent) == 1
    text = render_text(sender.sent[0])
    assert "ETH" in text and "BTC" not in text


async def test_second_touch_within_120h_not_sent():
    """§13 №6: новый заход на той же глубине до 120 ч молчит даже через dispatch."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    t = now_ms()
    await disp.dispatch([_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t)])
    # новый заход: новое событие (другое occurred_at), тот же порог
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65510.0, t + 3_600_000)]
    )
    assert len(sender.sent) == 1
    assert _delivery_count(db) == 1


async def test_service_message_journaled_via_dispatcher():
    """§11: сервисное сообщение идёт общим транспортом и пишется в delivery."""
    db, _ = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    await disp.notify_service("данные устарели: binance BTCUSDT D1")

    rows = db.conn.execute("SELECT * FROM delivery").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["event_ids"] == "[]"


async def test_service_message_failure_journaled_not_retried():
    """Сбой сервисной доставки фиксируется failed; retry_pending его не трогает
    (текст не хранится) и не перезаписывает ошибку «события не найдены»."""
    db, _ = _make_db()
    failing = FailingSender()
    disp = EventDispatcher(db, DetectorConfig(), failing)
    await disp.notify_service("источник недоступен")
    assert _delivery_count(db, "failed") == 1

    sender = LogSender()
    disp_ok = EventDispatcher(db, DetectorConfig(), sender)
    await disp_ok.retry_pending()
    assert len(sender.sent) == 0
    row = db.conn.execute("SELECT status, error FROM delivery").fetchone()
    assert row["status"] == "failed"
    assert "network down" in row["error"]


async def test_chart_image_attached_when_charts_dir(tmp_path):
    """§11 п.7: при настроенном charts_dir к сообщению прикладывается PNG-снимок."""
    db, z = _make_db()
    zone = db.get_zone(z["btc_zone"])
    assert zone is not None
    w1_ms = TIMEFRAME_MINUTES["W1"] * 60_000
    t0 = now_ms() - 30 * w1_ms
    price = 60000.0
    candles = []
    for i in range(30):
        o, c = price, price + 100.0
        candles.append(make_candle(
            t0 + i * w1_ms, o, c + 200.0, o - 200.0, c,
            timeframe="W1", instrument_id=zone.instrument_id,
        ))
        price = c
    db.insert_candles(candles)

    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender, charts_dir=str(tmp_path))
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, now_ms())]
    )
    assert len(sender.sent) == 1
    img = sender.sent[0].image_path
    assert img is not None and img.endswith(".png")
    assert Path(img).exists() and Path(img).stat().st_size > 0


async def test_chart_image_skipped_without_charts_dir():
    """Без charts_dir поведение прежнее: image_path пуст, текст доставляется."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, now_ms())]
    )
    assert len(sender.sent) == 1
    assert sender.sent[0].image_path is None
