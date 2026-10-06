"""Каркас бота: /start, /help, /now, nav-callback, кнопки reply-меню.

Паттерн tests/test_telegram_buttons.py: хендлеры достаются из
build_application и вызываются напрямую с фейковыми update/context,
без сети. Реальные этапы считает app/services/overview.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
from telegram import ReplyKeyboardMarkup

from app.config import Settings
from app.db import Database
from app.models import (
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import LtfObservation
from app.notify.telegram import build_application
from tests.conftest import make_candle

TOKEN = "123:test-token"
CHAT_ID = "42"


def _settings() -> Settings:
    s = Settings()
    s.telegram_token = TOKEN
    s.telegram_chat_id = CHAT_ID
    return s


@pytest.fixture()
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


def _command(app, name: str):
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler) and name in h.commands:
            return h.callback
    raise AssertionError(f"команда /{name} не зарегистрирована")


def _nav_callback(app):
    for h in app.handlers[0]:
        if isinstance(h, CallbackQueryHandler) and h.pattern.match("nav:x"):
            return h.callback
    raise AssertionError("nav-callback не зарегистрирован")


def _text_handler(app):
    for h in app.handlers[0]:
        if isinstance(h, MessageHandler):
            return h.callback
    raise AssertionError("MessageHandler не зарегистрирован")


def _msg_update(text: str, chat_id: str = CHAT_ID):
    replies: list = []

    async def reply_text(t, reply_markup=None):
        replies.append((t, reply_markup))

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    ), replies


def _cb_update(data: str, chat_id: str = CHAT_ID):
    answers: list = []
    edits: list = []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(t, reply_markup=None):
        edits.append((t, reply_markup))

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        callback_query=SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text
        ),
    ), answers, edits


def _instrument(db, symbol: str, ltf_analyze: bool = True) -> int:
    iid = db.upsert_instrument(Instrument(
        id=None, asset=symbol.removesuffix("USDT"), venue="binance",
        market_type="spot", symbol=symbol, quote_asset="USDT",
    ))
    db.set_instrument_ltf_analyze(iid, ltf_analyze)
    return iid


def _make_live(db, instrument_id: int, price: float = 100.0) -> None:
    """Свежая закрытая H1 + котировка → data_state ok (как в test_ltf_current_setup)."""
    now = now_ms()
    c = make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )
    db.insert_candles([c])
    db.set_quote(instrument_id, price, now)
    # F03: курсор обработки на последней закрытой — иначе processing_lag
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))


@pytest.fixture()
def seeded(db):
    """Три актива с разными этапами: ETH «Ждём BOS/SMS» (активное наблюдение
    без сценария), BTC «Ждём HTF-зону» (данные есть, наблюдений нет),
    SOL «Недостаточно данных» (нет свечей/котировки)."""
    eth = _instrument(db, "ETHUSDT")
    btc = _instrument(db, "BTCUSDT")
    sol = _instrument(db, "SOLUSDT")
    _make_live(db, eth)
    _make_live(db, btc)
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=eth, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=95.0, upper=105.0, formed_at=1, confirmed_at=2,
        status=ZoneStatus.ACTIVE,
    ))
    db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=eth, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active",
        activated_at=now_ms(),
    ))
    return {"eth": eth, "btc": btc, "sol": sol}


# ------------------------------ owner-guard ------------------------------

async def test_start_stranger_ignored(db, caplog):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/start", chat_id="999")
    await _command(app, "start")(update, SimpleNamespace(user_data={}))
    assert replies == []


async def test_now_stranger_ignored(db):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/now", chat_id="999")
    await _command(app, "now")(update, SimpleNamespace(user_data={}))
    assert replies == []


# ------------------------------ /start, /help ------------------------------

async def test_start_sends_greeting_and_menu(db):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/start")
    await _command(app, "start")(update, SimpleNamespace(user_data={}))
    assert len(replies) == 1
    text, markup = replies[0]
    assert "HTF" in text
    assert isinstance(markup, ReplyKeyboardMarkup)
    labels = [b.text for row in markup.keyboard for b in row]
    assert "Сейчас" in labels and "Открыть приложение" in labels


async def test_help_lists_all_commands(db):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/help")
    await _command(app, "help")(update, SimpleNamespace(user_data={}))
    assert len(replies) == 1
    text, _ = replies[0]
    for cmd in ("/start", "/now", "/asset", "/htf", "/ltf", "/chart",
                "/watchlist", "/alerts", "/history", "/status", "/help",
                "/add", "/remove", "/mute", "/unmute"):
        assert cmd in text
    assert "скоро" not in text  # все команды ТЗ реализованы (шаги 2–7)


# ------------------------------ /now ------------------------------

async def test_now_lists_assets_sorted_by_stage(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/now")
    await _command(app, "now")(update, SimpleNamespace(user_data={}))
    assert len(replies) == 1
    text, markup = replies[0]
    assert "ETHUSDT" in text and "BTCUSDT" in text and "SOLUSDT" in text
    assert "Ждём BOS/SMS" in text
    # сортировка: «Ждём BOS/SMS» выше «Ждём HTF-зону»/«Недостаточно данных»,
    # равные этапы — по символу
    assert text.index("ETHUSDT") < text.index("BTCUSDT") < text.index("SOLUSDT")
    # inline-клавиатура: активы + «Обновить»
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:asset:{seeded['eth']}" in callbacks
    assert "nav:now:refresh" in callbacks


async def test_now_empty_shows_hint(db):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/now")
    await _command(app, "now")(update, SimpleNamespace(user_data={}))
    assert "нет активов" in replies[0][0]


# ------------------------------ nav-callback ------------------------------

async def test_nav_refresh_edits_message(db, seeded):
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update("nav:now:refresh")
    await _nav_callback(app)(update, SimpleNamespace(user_data={}))
    assert len(edits) == 1
    text, markup = edits[0]
    assert "ETHUSDT" in text
    assert markup is not None
    assert answers == [None]  # answer() без текста


async def test_nav_asset_renders_card(db, seeded):
    """nav:asset:<iid> — реальная карточка актива (шаг 3), не заглушка."""
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update(f"nav:asset:{seeded['eth']}")
    await _nav_callback(app)(update, SimpleNamespace(user_data={}))
    assert len(edits) == 1
    assert "ETHUSDT · binance · spot" in edits[0][0]
    assert answers == [None]  # answer() без текста


# ------------------------- кнопки reply-меню (on_text) -------------------------

async def test_menu_button_now(db, seeded):
    """«Сейчас» из reply-меню проходит через единый on_text telegram.py."""
    app = build_application(_settings(), db)
    update, replies = _msg_update("Сейчас")
    await _text_handler(app)(update, SimpleNamespace(user_data={}))
    assert len(replies) == 1
    assert "ETHUSDT" in replies[0][0]


async def test_menu_open_app_sends_url(db):
    app = build_application(_settings(), db)
    update, replies = _msg_update("Открыть приложение")
    await _text_handler(app)(update, SimpleNamespace(user_data={}))
    assert replies == [(f"Приложение: {_settings().effective_base_url()}", None)]


async def test_pending_note_wins_over_menu_button(db, seeded):
    """Текст кнопки меню при pending-заметке сохраняется как заметка,
    а не как команда меню (единый on_text)."""
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=seeded["btc"], type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="H4", lower=1.0, upper=2.0,
        formed_at=1, confirmed_at=2, status=ZoneStatus.ACTIVE,
    ))
    app = build_application(_settings(), db)
    context = SimpleNamespace(user_data={"pending_note_zone": zone_id})
    update, replies = _msg_update("Сейчас")
    await _text_handler(app)(update, context)
    reviews = db.get_reviews(zone_id)
    assert len(reviews) == 1 and reviews[0].text == "Сейчас"
    assert replies == [("Заметка сохранена.", None)]
