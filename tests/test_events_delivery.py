"""Реальная доставка пакетов «Событий» через общий outbox (§9): pending —
не доставка (AC22), отметка sent только после отправки, дедуп при повторе и
перезапуске (AC17), лента всегда (AC16), устаревший пакет — актуальная сводка."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.db import Database
from app.events import memory, publish, store
from app.events.rules import ENGINE_RULE_VERSION
from app.models import now_ms
from app.notify.outbox import Card
from app.notify.telegram import LogSender

T0 = 1_800_000_000_000
DAY = 86_400_000


@pytest.fixture(autouse=True)
def _unwire():
    publish._WIRED = False
    publish._DB = None
    publish._SENDER = None
    publish._SETTINGS = None
    publish._OUTBOX = None
    yield
    publish._WIRED = False
    publish._DB = None
    publish._SENDER = None
    publish._SETTINGS = None
    publish._OUTBOX = None


def _db() -> Database:
    return Database(":memory:")


def _settings(token: str = "test-token", notify: bool = True) -> Settings:
    settings = Settings()
    settings.events_group_seconds = 60
    settings.telegram_token = token
    settings.events_notify_enabled = notify
    return settings


def _asmt(*, direction="neutral", events="neutral", time="neutral",
          contradictory=False, weekday=3, aid="a1", evaluated=T0,
          market_line="линия") -> dict:
    return {
        "assessment_id": aid,
        "evaluated_at": evaluated,
        "available_at": evaluated,
        "direction": direction,
        "events_direction": events,
        "time_direction": time,
        "contradictory": contradictory,
        "coverage": None,
        "statistics": None,
        "time_context": {
            "weekday": weekday,
            "day_start_ms": evaluated - (evaluated % DAY),
        },
        "market_line": market_line,
        "quality": "ok",
        "reasons": [],
        "unknown_dependencies": [],
    }


def _published_bundle(db, settings) -> int:
    """Одно изменение направления → открытый пакет → flush_due → published."""
    curr = _asmt(direction="long", aid="a1", market_line="↑ LONG")
    store.insert_assessment(db, curr)
    publish.process_cycle(
        db, settings, prev=_asmt(direction="neutral", aid="a0"),
        curr=curr, transitions=[], now_ms=T0,
    )
    published = publish.flush_due(db, settings, T0 + 60_000)
    assert len(published) == 1
    assert published[0]["status"] == "published"
    return published[0]["id"]


def _delivery(db, bundle_id: int, channel: str) -> dict:
    return next(
        d for d in store.list_deliveries(db, bundle_id=bundle_id)
        if d["channel"] == channel
    )


def _events_packets(db) -> list:
    return db.conn.execute(
        "SELECT * FROM notification_packet WHERE channel='events'"
    ).fetchall()


@pytest.mark.parametrize("long_usd,short_usd,window_ms,send", [
    (3103163, 2691757, 14_400_000, False),
    (1555369, 2438185, 14_400_000, False),
    (25_000_000, 24_999_999, 14_400_000, False),
    (25_000_000, 25_000_000, 14_400_000, False),
    (25_000_000, 25_000_001, 14_400_000, True),
    (51_000_000, 0, 3_600_000, False),
    (None, 51_000_000, 14_400_000, False),
    ("NaN", 51_000_000, 14_400_000, False),
    (-1, 51_000_000, 14_400_000, False),
])
async def test_plain_liquidation_notification_threshold(long_usd, short_usd, window_ms, send):
    db, settings = _db(), _settings()
    sender = LogSender()
    publish.wire(db, sender, settings)
    curr = _asmt()
    store.insert_assessment(db, curr)
    fact, _ = memory.begin_fact(
        db, rule_id="LIQ_FACT", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="volume", direction=None,
        occurred_at=T0, detected_at=T0, available_at=T0,
        payload={"summary": "объём ликвидаций", "sum_long_usd": long_usd,
                 "sum_short_usd": short_usd, "window_ms": window_ms},
    )
    pubs = publish.process_cycle(
        db, settings, prev=None, curr=curr,
        transitions=store.fact_transitions_since(db, 0), now_ms=T0,
    )
    assert bool(pubs) is send
    assert store.get_fact(db, fact["fact_key"]) is not None
    publish.flush_due(db, settings, T0 + 60_000)
    await publish._OUTBOX.flush()
    assert len(sender.cards) == int(send)
    assert len(_events_packets(db)) == int(send)


async def test_ac22_pending_is_not_delivery_until_real_send():
    db, settings = _db(), _settings()
    sender = LogSender()  # изолированный тестовый канал (AC22)
    publish.wire(db, sender, settings)
    assert publish._OUTBOX is not None
    bundle_id = _published_bundle(db, settings)

    telegram = _delivery(db, bundle_id, "telegram")
    assert telegram["status"] == "pending"
    assert telegram["delivered_at"] is None  # pending не считается доставкой
    assert sender.cards == []
    packets = _events_packets(db)
    assert len(packets) == 1
    assert packets[0]["status"] == "pending"
    assert packets[0]["quiet"] == 0  # тихих часов нет (§9)
    assert packets[0]["semantic_key"] == f"events:bundle:{bundle_id}"
    pubs = store.bundle_publications(db, bundle_id)
    members = publish._OUTBOX.members(packets[0]["id"])
    assert [m["event_id"] for m in members] == [p["id"] for p in pubs]

    await publish._OUTBOX.flush()

    telegram = _delivery(db, bundle_id, "telegram")
    assert telegram["status"] == "sent"
    assert telegram["delivered_at"] is not None
    packet = _events_packets(db)[0]
    assert packet["status"] == "sent" and packet["message_id"] is not None
    assert len(sender.cards) == 1
    text = sender.cards[0][0].text
    assert "перевес в сторону роста" in text and "Направление изменилось" in text
    assert sender.cards[0][2] is False  # quiet не используется
    # shadow/quiet_hold/pending нигде не осталось: доставка — только sent
    bad = db.conn.execute(
        "SELECT COUNT(*) FROM events_delivery "
        "WHERE status IN ('shadow', 'quiet_hold', 'pending')"
    ).fetchone()[0]
    assert bad == 0
    feed = _delivery(db, bundle_id, "feed")
    assert feed["status"] == "sent"


def test_telegram_not_configured_marks_unavailable():
    db, settings = _db(), _settings(token="")
    publish.wire(db, LogSender(), settings)
    assert publish._OUTBOX is None  # без токена канал не регистрируется
    bundle_id = _published_bundle(db, settings)

    telegram = _delivery(db, bundle_id, "telegram")
    assert telegram["status"] == "unavailable"
    assert telegram["detail"] == "Доставка недоступна"
    assert telegram["delivered_at"] is None
    feed = _delivery(db, bundle_id, "feed")  # лента — всегда (AC16)
    assert feed["status"] == "sent" and feed["delivered_at"] is not None
    assert _events_packets(db) == []


def test_notify_disabled_marks_unavailable():
    db, settings = _db(), _settings(notify=False)
    publish.wire(db, LogSender(), settings)
    assert publish._OUTBOX is None
    bundle_id = _published_bundle(db, settings)

    telegram = _delivery(db, bundle_id, "telegram")
    assert telegram["status"] == "unavailable"
    assert telegram["detail"] == "Доставка недоступна"
    assert _delivery(db, bundle_id, "feed")["status"] == "sent"
    assert _events_packets(db) == []


async def test_ac17_repeat_and_reput_do_not_duplicate_packet():
    db, settings = _db(), _settings()
    sender = LogSender()
    publish.wire(db, sender, settings)
    bundle_id = _published_bundle(db, settings)

    # повторный flush: пакет уже published, повторной постановки нет
    assert publish.flush_due(db, settings, T0 + 61_000) == []
    # повторная постановка того же пакета (перезапуск): тот же semantic_key
    pubs = store.bundle_publications(db, bundle_id)
    packet_id = publish._OUTBOX.put(
        "events", f"events:bundle:{bundle_id}", Card(text="повтор"),
        [p["id"] for p in pubs], quiet=False,
    )
    assert packet_id == _events_packets(db)[0]["id"]
    assert len(_events_packets(db)) == 1
    assert len(publish._OUTBOX.members(packet_id)) == len(pubs)
    telegram_rows = [
        d for d in store.list_deliveries(db, bundle_id=bundle_id)
        if d["channel"] == "telegram"
    ]
    assert len(telegram_rows) == 1  # доставка — (пакет, получатель, канал)

    await publish._OUTBOX.flush()
    assert len(sender.cards) == 1  # одно сообщение, без дублей


async def test_stale_bundle_rendered_as_actualized_summary():
    db, settings = _db(), _settings()
    publish.wire(db, LogSender(), settings)
    store.insert_assessment(db, _asmt(aid="a1", market_line="↑ LONG"))
    fact, _ = memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=T0, detected_at=T0, available_at=T0,
        payload={"summary": "давление"},
    )
    transitions = store.fact_transitions_since(db, 0)
    publish.process_cycle(
        db, settings, prev=None, curr=_asmt(aid="a1", market_line="↑ LONG"),
        transitions=transitions, now_ms=T0,
    )
    bundle_id = publish.flush_due(db, settings, T0 + 60_000)[0]["id"]

    # до отправки факт инвалидирован, появилась новая актуальная оценка
    memory.invalidate_fact(db, fact["fact_key"], T0 + 61_000, "условие сломано")
    store.insert_assessment(db, _asmt(aid="a2", evaluated=T0 + 61_000, direction="short",
                                      market_line="↓ SHORT"))

    row = _events_packets(db)[0]
    card = await publish._render_packet(row, publish._OUTBOX.members(row["id"]))
    assert "Сводка актуализирована: Последняя сохранённая оценка" in card.text
    assert "перевес в сторону снижения" in card.text
    assert "перевес в сторону роста" not in card.text
    assert "Новое событие: давление" not in card.text
    assert "Новое событие: давление" in card.details
    assert "Исходные изменения устарели до отправки" in card.text
    assert card.targets[0]["url"].endswith("?assessment=a2")

    # исходный пакет сохранён, не перезаписан (§10)
    pubs = store.bundle_publications(db, bundle_id)
    assert pubs[0]["payload"]["text"] == "Новое событие: давление"
    assert store.get_bundle(db, bundle_id)["status"] == "published"


async def test_old_bundle_rendered_as_actualized_summary():
    db, settings = _db(), _settings()
    publish.wire(db, LogSender(), settings)
    bundle_id = _published_bundle(db, settings)

    # пакет старше 10 минут к моменту отправки (§9 retry)
    db.conn.execute(
        "UPDATE events_bundle SET opened_at=? WHERE id=?",
        (now_ms() - 11 * 60_000, bundle_id),
    )
    db.conn.commit()

    row = _events_packets(db)[0]
    card = await publish._render_packet(row, publish._OUTBOX.members(row["id"]))
    assert "Сводка актуализирована: Последняя сохранённая оценка" in card.text
    assert "Исходные изменения устарели до отправки" in card.text


async def test_finish_marks_delivery_sent_per_publication():
    """finish-колбэк: publication id → пакет → upsert доставки telegram."""
    db, settings = _db(), _settings()
    publish.wire(db, LogSender(), settings)
    bundle_id = _published_bundle(db, settings)
    pub = store.bundle_publications(db, bundle_id)[0]

    publish._finish_packet(pub["id"], "sent")
    telegram = _delivery(db, bundle_id, "telegram")
    assert telegram["status"] == "sent"
    assert telegram["delivered_at"] is not None

    # неизвестная публикация — без строк и без ошибок
    publish._finish_packet(999_999, "sent")
    assert len(store.list_deliveries(db, bundle_id=bundle_id)) == 2
