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

    async def send_card(self, card, packet_id, *, quiet=False):
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


async def test_separate_assets_have_separate_cards():
    """§9: одновременные сигналы объединяются; пакет не скрывает второй актив."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    t = now_ms()
    events = [
        _new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t),
        _new_event(db, z["eth_zone"], EventKind.TOUCH, 3050.0, t),
    ]
    deliveries = await disp.dispatch(events)

    assert len(sender.cards) == 2  # separate instrument/timeframe cards
    text = "\n".join(card.text for card, _, _ in sender.cards)
    assert "BTC" in text and "ETH" in text  # оба актива видны
    assert len(deliveries) == 2  # delivery на каждое событие
    assert all(d.status == "sent" for d in deliveries)


async def test_redispatch_same_events_no_second_delivery():
    """Идемпотентность: повторный dispatch тех же событий не дублирует delivery."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    t = now_ms()
    # DEPTH_90 не подавляется по §8 — проверяем именно ключ идемпотентности
    events = [_new_event(db, z["btc_zone"], EventKind.DEPTH_90, 65600.0, t)]

    await disp.dispatch(events)
    await disp.dispatch(events)
    await disp.dispatch(events)

    assert _delivery_count(db) == 1
    assert len(sender.cards) == 1


async def test_retry_pending_resends_failed_without_duplicates():
    """§11 п.5: failed-доставка повторяется; sent не дублируется."""
    db, z = _make_db()
    t = now_ms()
    events = [
        _new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t),
        _new_event(db, z["eth_zone"], EventKind.TOUCH, 3050.0, t),
    ]

    failing = FailingSender()
    disp_fail = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), failing)
    deliveries = await disp_fail.dispatch(events)
    assert all(d.status == "pending" for d in deliveries)
    assert db.conn.execute("SELECT COUNT(*) FROM notification_packet WHERE status='failed'").fetchone()[0] == 2

    sender = LogSender()
    disp_ok = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await disp_ok.retry_pending()
    assert len(sender.cards) == 2  # по одному повтору на failed-доставку
    assert _delivery_count(db, "sent") == 2
    assert _delivery_count(db, "failed") == 0
    assert db.pending_deliveries() == []

    # повторный retry: отправлять нечего, sent не задваивается
    await disp_ok.retry_pending()
    assert len(sender.cards) == 2
    assert _delivery_count(db) == 2


async def test_delivery_retry_creates_no_market_events():
    """§9: технический ретрай не создаёт вторую запись рыночного события."""
    db, z = _make_db()
    t = now_ms()
    events = [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t)]
    before = _event_count(db)

    failing = FailingSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), failing)
    await disp.dispatch(events)
    await disp.dispatch(events)  # повторная обработка того же события

    disp_ok = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), LogSender())
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

    assert len(sender.cards) == 1
    text = sender.cards[0][0].text
    assert "ETH" in text and "BTC" not in text


async def test_second_touch_within_120h_not_sent():
    """§13 №6: новый заход на той же глубине до 120 ч молчит даже через dispatch."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    t = now_ms()
    await disp.dispatch([_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, t)])
    # новый заход: новое событие (другое occurred_at), тот же порог
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65510.0, t + 3_600_000)]
    )
    assert len(sender.cards) == 1
    assert _delivery_count(db) == 1


async def test_service_message_journaled_via_dispatcher():
    """§11: сервисное сообщение идёт общим транспортом и пишется в delivery."""
    db, _ = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    await disp.notify_service("данные устарели: binance BTCUSDT D1")

    rows = db.conn.execute("SELECT * FROM notification_packet WHERE channel='service'").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert len(sender.cards) == 1 and sender.cards[0][2]


async def test_service_failure_retries_durable_digest():
    db, _ = _make_db()
    failing = FailingSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), failing)
    await disp.notify_service("источник недоступен")
    row = db.conn.execute("SELECT * FROM notification_packet WHERE channel='digest'").fetchone()
    assert row["status"] == "failed" and "network down" in row["reason"]
    sender = LogSender()
    disp_ok = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await disp_ok.retry_pending()
    assert len(sender.cards) == 1 and sender.cards[0][2]
    await disp_ok.retry_pending()
    assert len(sender.cards) == 1


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
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, charts_dir=str(tmp_path))
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, now_ms())]
    )
    assert len(sender.cards) == 1
    img = sender.cards[0][0].image_path
    assert img is not None and img.endswith(".png")
    assert Path(img).exists() and Path(img).stat().st_size > 0


async def test_chart_image_uses_current_forming_close(tmp_path, monkeypatch):
    """Снимок уведомления заканчивается незакрытым баром текущей недели,
    а не последним закрытием."""
    db, z = _make_db()
    zone = db.get_zone(z["btc_zone"])
    assert zone is not None
    w1_ms = TIMEFRAME_MINUTES["W1"] * 60_000
    now = now_ms()
    week_open = now - (now % w1_ms)
    db.insert_candles([
        make_candle(
            week_open - w1_ms, 100, 110, 90, 105,
            timeframe="W1", instrument_id=zone.instrument_id,
        ),
        make_candle(
            week_open, 105, 112, 104, 111.5,
            timeframe="W1", instrument_id=zone.instrument_id, closed=False,
        ),
    ])
    captured: dict = {}

    def fake_render(candles, zone_, out, source, **kwargs):
        captured["last"] = candles[-1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return str(out)

    monkeypatch.setattr("app.notify.chartimg.render_zone_chart", fake_render)
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, charts_dir=str(tmp_path))
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 111.5, now)]
    )
    assert captured["last"].closed is False
    assert captured["last"].close == 111.5


async def test_chart_image_skipped_without_charts_dir():
    """Без charts_dir поведение прежнее: image_path пуст, текст доставляется."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, now_ms())]
    )
    assert len(sender.cards) == 1
    assert sender.cards[0][0].image_path is None


async def test_notification_marks_visually_merged_zone():
    """§10: зона из визуальной группы — в тексте перечислены участники
    объединения; сами зоны и их границы не меняются."""
    db, z = _make_db()
    t0 = now_ms()
    btc_zone = db.get_zone(z["btc_zone"])
    # вторая актуальная ACTIVE-зона того же инструмента, типа и ТФ,
    # пересекающаяся с исходной OB W1 65000–66000 (другой ТФ не сливается)
    db.insert_zone(
        Zone(None, btc_zone.instrument_id, ZoneType.OB, Direction.BULL, "W1",
             lower=65500.0, upper=66500.0, formed_at=t0 - 8_000,
             confirmed_at=t0 - 7_000, status=ZoneStatus.ACTIVE, created_at=t0)
    )
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65600.0, t0)]
    )
    assert len(sender.cards) == 1
    text = sender.cards[0][0].details
    assert "Визуально объединена с" in text
    assert "Orderblock W1" in text and "65 500,00" in text  # тип/ТФ и граница участника (ru-формат)


async def test_notification_does_not_merge_different_timeframes():
    """D1 и W1 одного типа пересекаются по цене, но в тексте не сливаются."""
    db, z = _make_db()
    t0 = now_ms()
    btc_zone = db.get_zone(z["btc_zone"])
    db.insert_zone(
        Zone(None, btc_zone.instrument_id, ZoneType.OB, Direction.BULL, "D1",
             lower=65500.0, upper=66500.0, formed_at=t0 - 8_000,
             confirmed_at=t0 - 7_000, status=ZoneStatus.ACTIVE, created_at=t0)
    )
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65600.0, t0)]
    )
    text = sender.cards[0][0].details or ""
    body = sender.cards[0][0].text or ""
    assert "Визуально объединена" not in text
    assert "Визуально объединена" not in body


async def test_notification_without_group_has_no_merge_mark():
    """Одиночная зона (группы нет) — пометки об объединении в тексте нет."""
    db, z = _make_db()
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender)
    await disp.dispatch(
        [_new_event(db, z["btc_zone"], EventKind.TOUCH, 65500.0, now_ms())]
    )
    text = sender.cards[0][0].text
    assert "Визуально объединена" not in text
