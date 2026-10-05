"""ТЗ бота п.8: watchlist — CRUD, seed при /start, команды /watchlist,
/add, /remove, watchlist-фильтр /now. Удаление трогает только bot_watchlist.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from app.db import Database
from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms
from app.bot.cards import render_now
from tests.test_bot_cards import (  # noqa: F401 — фикстуры/хелперы
    _cb_update,
    _command,
    _context,
    _msg_update,
    _nav_callback,
    _settings,
    db,
    seeded,
)
from app.notify.telegram import build_application

CHAT_ID = "42"


def _nav(app):
    return _nav_callback(app)


# ------------------------------ CRUD / seed ------------------------------

def test_watchlist_crud(db, seeded):
    eth = seeded["eth"]
    assert db.list_watchlist(CHAT_ID) == []
    db.watchlist_add(CHAT_ID, eth)
    db.watchlist_add(CHAT_ID, eth)  # идемпотентно
    rows = db.list_watchlist(CHAT_ID)
    assert len(rows) == 1 and rows[0]["alerts_enabled"] is True
    db.watchlist_set_alerts(CHAT_ID, eth, False)
    assert db.list_watchlist(CHAT_ID)[0]["alerts_enabled"] is False
    assert db.watchlist_alerts_disabled(CHAT_ID, eth) is True
    db.watchlist_remove(CHAT_ID, eth)
    assert db.list_watchlist(CHAT_ID) == []
    assert db.watchlist_alerts_disabled(CHAT_ID, eth) is False


def test_seed_watchlist_from_enabled(db, seeded):
    added = db.seed_watchlist(CHAT_ID)
    assert added == 3  # eth + btc_bin + btc_hl
    assert {w["instrument_id"] for w in db.list_watchlist(CHAT_ID)} == {
        seeded["eth"], seeded["btc_bin"], seeded["btc_hl"],
    }
    # повторный seed непустого списка — без изменений
    assert db.seed_watchlist(CHAT_ID) == 0
    assert len(db.list_watchlist(CHAT_ID)) == 3


async def test_start_seeds_watchlist(db, seeded):
    app = build_application(_settings(), db)
    update, _ = _msg_update("/start")
    await _command(app, "start")(update, _context())
    assert len(db.list_watchlist(CHAT_ID)) == 3


# ------------------------------ /watchlist, /add, /remove ------------------------------

async def test_watchlist_command_and_toggle(db, seeded):
    db.seed_watchlist(CHAT_ID)
    app = build_application(_settings(), db)
    update, replies = _msg_update("/watchlist")
    await _command(app, "watchlist")(update, _context())
    text, markup = replies[0]
    assert "ETHUSDT · binance · spot — уведомления вкл" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:wal:{seeded['eth']}" in callbacks
    assert "nav:wadd" in callbacks

    # переключатель уведомлений
    update, answers, edits = _cb_update(f"nav:wal:{seeded['eth']}")
    await _nav(app)(update, _context())
    assert "уведомления выкл" in edits[0][0]
    assert db.list_watchlist(CHAT_ID)[
        [w["instrument_id"] for w in db.list_watchlist(CHAT_ID)].index(
            seeded["eth"])
    ]["alerts_enabled"] is False


async def test_watchlist_remove_confirm_flow(db, seeded):
    db.seed_watchlist(CHAT_ID)
    # удаление не трогает зоны/события — только bot_watchlist
    zid = db.insert_zone(Zone(
        id=None, instrument_id=seeded["eth"], type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=1.0, upper=2.0,
        formed_at=1, confirmed_at=2, status=ZoneStatus.ACTIVE,
    ))
    app = build_application(_settings(), db)
    update, _, edits = _cb_update(f"nav:wrm:{seeded['eth']}")
    await _nav(app)(update, _context())
    assert "Удалить ETHUSDT" in edits[0][0]
    assert db.list_watchlist(CHAT_ID) != []  # ещё не удалено — подтверждение
    update, answers, edits = _cb_update(f"nav:wrmok:{seeded['eth']}")
    await _nav(app)(update, _context())
    assert all(w["instrument_id"] != seeded["eth"]
               for w in db.list_watchlist(CHAT_ID))
    assert db.get_zone(zid) is not None  # зона на месте


async def test_add_remove_commands_and_ambiguity(db, seeded):
    app = build_application(_settings(), db)
    # /add ETH — точное совпадение
    update, replies = _msg_update("/add ETH")
    await _command(app, "add")(update, _context(["ETH"]))
    assert [w["instrument_id"] for w in db.list_watchlist(CHAT_ID)] == \
        [seeded["eth"]]
    # /add BTC — неоднозначность → выбор конкретного инструмента
    update, replies = _msg_update("/add BTC")
    await _command(app, "add")(update, _context(["BTC"]))
    text, markup = replies[0]
    assert "Несколько совпадений" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:waddok:{seeded['btc_bin']}" in callbacks
    assert len(db.list_watchlist(CHAT_ID)) == 1  # ничего не добавлено
    # /add XRP — нет такого
    update, replies = _msg_update("/add XRP")
    await _command(app, "add")(update, _context(["XRP"]))
    assert "не найден" in replies[0][0]
    # /remove ETH — удаляет только watchlist
    update, replies = _msg_update("/remove ETH")
    await _command(app, "remove")(update, _context(["ETH"]))
    assert db.list_watchlist(CHAT_ID) == []


async def test_watchlist_add_picker_excludes_listed(db, seeded):
    db.seed_watchlist(CHAT_ID)
    app = build_application(_settings(), db)
    update, _, edits = _cb_update("nav:wadd")
    await _nav(app)(update, _context())
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard
                 for b in row]
    assert not any(cb.startswith("nav:waddok:") for cb in callbacks)


# ------------------------------ watchlist-фильтр /now ------------------------------

def test_render_now_watchlist_filter(db, seeded):
    settings = _settings()
    # пустой список — fallback: все активы (поведение до /start)
    text_all = render_now(db, settings, CHAT_ID)
    assert "ETHUSDT" in text_all and "BTCUSDT" in text_all
    # непустой watchlist — только его активы
    db.watchlist_add(CHAT_ID, seeded["eth"])
    text = render_now(db, settings, CHAT_ID)
    assert "ETHUSDT" in text and "BTCUSDT" not in text
