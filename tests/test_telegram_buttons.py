"""Обработчики Telegram-кнопок и заметок (§9): ack / snooze / mute / note,
доступ только из чата владельца (§11 п.8). Сеть не используется — хендлеры
вызываются напрямую с фейковыми update/context."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import Database
from app.models import (
    AlertState,
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.telegram import (
    MUTE_FOREVER_MS,
    SNOOZE_HOURS,
    _parse_callback,
    build_application,
)

TOKEN = "123:test-token"
CHAT_ID = "42"


def _settings() -> Settings:
    s = Settings()
    s.telegram_token = TOKEN
    s.telegram_chat_id = CHAT_ID
    return s


def _handlers(app):
    """(on_callback, on_text) из зарегистрированных хендлеров приложения."""
    cb = app.handlers[0][0].callback
    msg = app.handlers[0][1].callback
    return cb, msg


def _cb_update(data: str, chat_id: str = CHAT_ID):
    answers: list = []

    async def answer(text=None):
        answers.append(text)

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        callback_query=SimpleNamespace(data=data, answer=answer),
    ), answers


def _text_update(text: str, chat_id: str = CHAT_ID):
    replies: list = []

    async def reply_text(t):
        replies.append(t)

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    ), replies


def test_parse_callback():
    assert _parse_callback("htf:ack:10:2:touch") == ("ack", 10, 2, "touch")
    assert _parse_callback("htf:mute:1:1:depth_50") == ("mute", 1, 1, "depth_50")
    assert _parse_callback("bad") is None
    assert _parse_callback("htf:ack:x:2:touch") is None
    assert _parse_callback("") is None


def test_build_application_without_token_returns_none():
    s = Settings()
    s.telegram_token = ""
    assert build_application(s, Database(":memory:")) is None


async def test_ack_marks_acknowledged():
    db = Database(":memory:")
    db.set_alert_state(AlertState(
        zone_id=10, cycle_id=2, event_kind="touch", last_delivered_at=1,
    ))
    on_callback, _ = _handlers(build_application(_settings(), db))
    update, answers = _cb_update("htf:ack:10:2:touch")
    await on_callback(update, SimpleNamespace(user_data={}))

    st = db.get_alert_state(10, 2, "touch")
    assert st is not None and st.acknowledged is True
    assert answers == ["Отмечено: изучаю."]


async def test_snooze_sets_muted_until_24h():
    db = Database(":memory:")
    on_callback, _ = _handlers(build_application(_settings(), db))
    update, _ = _cb_update("htf:snooze:10:2:touch")
    await on_callback(update, SimpleNamespace(user_data={}))

    st = db.get_alert_state(10, 2, "touch")
    assert st is not None
    assert st.muted_until is not None
    assert st.muted_until >= now_ms() + (SNOOZE_HOURS - 1) * 3_600_000


async def test_mute_forever():
    db = Database(":memory:")
    on_callback, _ = _handlers(build_application(_settings(), db))
    update, _ = _cb_update("htf:mute:10:2:touch")
    await on_callback(update, SimpleNamespace(user_data={}))

    st = db.get_alert_state(10, 2, "touch")
    assert st is not None and st.muted_until == MUTE_FOREVER_MS


async def test_note_flow_saves_review():
    db = Database(":memory:")
    # FK: заметка привязана к реальной зоне
    ins = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=ins, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=100.0, upper=110.0, formed_at=1,
        confirmed_at=1, status=ZoneStatus.ACTIVE,
    ))
    on_callback, on_text = _handlers(build_application(_settings(), db))
    context = SimpleNamespace(user_data={})

    update, _ = _cb_update(f"htf:note:{zone_id}:1:touch")
    await on_callback(update, context)
    assert context.user_data["pending_note_zone"] == zone_id

    text_update, replies = _text_update("выглядит сомнительно")
    await on_text(text_update, context)
    reviews = db.get_reviews(zone_id)
    assert len(reviews) == 1
    assert reviews[0].decision == "note"
    assert reviews[0].text == "выглядит сомнительно"
    assert replies == ["Заметка сохранена."]
    # контекст одноразовый: повторный текст без кнопки заметки не сохраняет
    assert "pending_note_zone" not in context.user_data


async def test_non_owner_ignored():
    """Чужой чат: ни кнопки, ни заметки не применяются (§11 п.8)."""
    db = Database(":memory:")
    db.set_alert_state(AlertState(
        zone_id=10, cycle_id=2, event_kind="touch", last_delivered_at=1,
    ))
    on_callback, on_text = _handlers(build_application(_settings(), db))

    update, answers = _cb_update("htf:ack:10:2:touch", chat_id="999")
    await on_callback(update, SimpleNamespace(user_data={}))
    assert db.get_alert_state(10, 2, "touch").acknowledged is False
    assert answers == []

    context = SimpleNamespace(user_data={"pending_note_zone": 10})
    text_update, replies = _text_update("чужой текст", chat_id="999")
    await on_text(text_update, context)
    assert db.get_reviews(10) == []
    assert replies == []
