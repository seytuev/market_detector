"""ТЗ бота п.11: /history (фильтры актив → период → тип) и /status
(service_status + render_status). Паттерн — как в test_bot_cards.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.cards import render_history, render_status
from app.db import Database
from app.models import (
    Delivery,
    Direction,
    Event,
    EventKind,
    now_ms,
)
from app.models_ltf import LtfEvent
from app.services.overview import service_status
from tests.conftest import make_candle
from tests.test_bot_cards import (  # noqa: F401 — фикстуры/хелперы
    _cb_update,
    _command,
    _context,
    _make_live,
    _msg_update,
    _nav_callback,
    _settings,
    db,
    seeded,
)
from tests.test_ltf_notify import _event, _setup
from app.notify.telegram import build_application

CHAT_ID = "42"
T0 = 1_780_000_000_000
H1 = 3_600_000


def _seed_events(db, seeded):
    """HTF-событие (touch) и LTF-события (bos, cancellation) ETH."""
    eth = seeded["eth"]
    now = now_ms()
    ev_id = db.insert_event(Event(
        id=None, zone_id=seeded["z_d1"], cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=now - 3_600_000, detected_at=now - 3_600_000,
        price=100.0, depth=0.0,
    ))
    assert ev_id is not None
    obs = seeded["obs_bear"]
    sc = seeded["sc"]
    bos = _event(db, obs, sc, "bos", {
        "direction": "bear", "kind": "BOS", "stage": "primary",
        "break_level": 110.0, "range_pending": True, "entries": [],
    }, "bos:hist:1")
    # occurred_at фиксируем «сейчас» (фабрика _event пишет T0 — поправим)
    db.conn.execute("UPDATE ltf_event SET occurred_at=? WHERE id=?",
                    (now - 7_200_000, bos.id))
    db.conn.commit()
    cancel = _event(db, obs, sc, "cancellation", {"reason": "reverse_bos"},
                    "cancellation:hist:1")
    db.conn.execute("UPDATE ltf_event SET occurred_at=? WHERE id=?",
                    (now - 1_800_000, cancel.id))
    db.conn.commit()
    return {"bos": bos, "cancel": cancel}


# ------------------------------ service_status ------------------------------

def test_service_status_quotes_ok_and_stale(db, seeded):
    settings = _settings()
    st = service_status(db, settings)
    # seeded: котировка ETH живая, BTC-инструменты — без данных
    assert st["quotes"]["last_quote_at"] is not None
    assert "BTCUSDT" in st["quotes"]["stale"]
    assert st["quotes"]["ok"] is False  # BTC без котировки
    # протухшая котировка — тоже просрочена
    db.set_quote(seeded["eth"], 100.0, now_ms() - 10 * 3_600_000)
    st = service_status(db, settings)
    assert "ETHUSDT" in st["quotes"]["stale"]


def test_service_status_h1_lag_and_gaps(db, seeded):
    settings = _settings()
    st = service_status(db, settings)
    # worker-метки нет — H1 не обработаны
    assert "ETHUSDT" in st["h1"]["lagging"]
    assert st["h1"]["last_closed"] is not None
    # метка обработки на последней свече — отставания нет
    db.set_meta(f"ltf:h1:last_close:{seeded['eth']}",
                str(st["h1"]["last_closed"]))
    st = service_status(db, settings)
    assert "ETHUSDT" not in st["h1"]["lagging"]
    # пропуски данных: BTC-инструменты (нет свечей/котировки)
    assert any(g["symbol"] == "BTCUSDT" for g in st["data_gaps"])


def test_service_status_delivery_pending(db, seeded):
    settings = _settings()
    assert service_status(db, settings)["delivery_pending"] == 0
    db.record_delivery(Delivery(
        id=None, event_ids=[1], destination="telegram", status="failed",
        idempotency_key="k:1",
    ))
    assert service_status(db, settings)["delivery_pending"] == 1


def test_render_status_markers(db, seeded):
    settings = _settings()
    db.set_meta(f"ltf:h1:last_close:{seeded['eth']}", "0")
    text = render_status(db, settings)
    assert "Состояние сервиса:" in text
    assert "⚠️" in text  # есть просрочки/пропуски (SOL)
    assert "Очередь доставки пуста" in text


# ------------------------------ render_history ------------------------------

def test_render_history_all_and_now_stage(db, seeded):
    settings = _settings()
    _seed_events(db, seeded)
    text = render_history(db, settings, seeded["eth"], 24, "all")
    assert "История: ETHUSDT · binance · spot" in text
    assert "первое касание" in text            # HTF touch (KIND_RU)
    assert "слом структуры BOS: уровень 110.00" in text
    assert "отмена сценария: причина: обратный BOS H1" in text
    assert "Сейчас: " in text                  # последующее состояние


def test_render_history_type_filters(db, seeded):
    settings = _settings()
    _seed_events(db, seeded)
    eth = seeded["eth"]
    htf = render_history(db, settings, eth, 24, "htf")
    assert "первое касание" in htf and "BOS" not in htf
    ltf = render_history(db, settings, eth, 24, "ltf")
    assert "слом структуры BOS" in ltf and "первое касание" not in ltf
    cancel = render_history(db, settings, eth, 24, "cancel")
    assert "отмена сценария" in cancel and "слом структуры BOS" not in cancel
    entry = render_history(db, settings, eth, 24, "entry")
    assert "Событий за период нет" in entry  # touch-событий LTF нет


def test_render_history_period_cutoff(db, seeded):
    settings = _settings()
    _seed_events(db, seeded)
    # «старое» событие — 10 дней назад: в 24ч не попадает, в 30д — попадает
    zid = seeded["z_d1"]
    ev_id = db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.DEPTH_90,
        occurred_at=now_ms() - 10 * 86_400_000,
        detected_at=now_ms() - 10 * 86_400_000, price=100.0, depth=0.9,
    ))
    assert ev_id is not None
    day = render_history(db, settings, seeded["eth"], 24, "htf")
    assert "90%" not in day
    month = render_history(db, settings, seeded["eth"], 720, "htf")
    assert "90%" in month


# ------------------------------ роутинг ------------------------------

async def test_history_command_flow(db, seeded):
    _seed_events(db, seeded)
    app = build_application(_settings(), db)
    # без аргумента — выбор актива
    update, replies = _msg_update("/history")
    await _command(app, "history")(update, _context([]))
    text, markup = replies[0]
    assert "выберите актив" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:hist:{seeded['eth']}" in callbacks
    # /history ETH — сразу выбор периода
    update, replies = _msg_update("/history ETH")
    await _command(app, "history")(update, _context(["ETH"]))
    callbacks = [b.callback_data for row in replies[0][1].inline_keyboard
                 for b in row]
    assert f"nav:histp:{seeded['eth']}:24" in callbacks


async def test_nav_history_flow(db, seeded):
    _seed_events(db, seeded)
    app = build_application(_settings(), db)
    eth = seeded["eth"]
    # nav:hist (кнопка «История» из /ltf) → период
    update, _, edits = _cb_update(f"nav:hist:{eth}")
    await _nav_callback(app)(update, _context())
    assert "Период" in edits[0][0]
    # период → тип
    update, _, edits = _cb_update(f"nav:histp:{eth}:168")
    await _nav_callback(app)(update, _context())
    assert "Тип события" in edits[0][0]
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard
                 for b in row]
    assert f"nav:histt:{eth}:168:cancel" in callbacks
    # тип → карточка истории
    update, answers, edits = _cb_update(f"nav:histt:{eth}:168:cancel")
    await _nav_callback(app)(update, _context())
    assert "отмена сценария: причина: обратный BOS H1" in edits[0][0]
    assert answers == [None]


async def test_nav_history_guard(db, seeded):
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update(
        f"nav:histt:{seeded['eth']}:24:all", chat_id="999"
    )
    await _nav_callback(app)(update, _context())
    assert edits == [] and answers == []


# ------------------------------ /status ------------------------------

async def test_status_command(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/status")
    await _command(app, "status")(update, _context())
    assert "Состояние сервиса:" in replies[0][0]
    # чужой — молчание
    update, replies = _msg_update("/status", chat_id="999")
    await _command(app, "status")(update, _context())
    assert replies == []


async def test_menu_history_and_status(db, seeded):
    """Третий ряд reply-меню: «История», «Состояние сервиса»."""
    from tests.test_bot_commands import _text_handler

    app = build_application(_settings(), db)
    update, replies = _msg_update("История")
    await _text_handler(app)(update, SimpleNamespace(user_data={}))
    assert "выберите актив" in replies[0][0]
    update, replies = _msg_update("Состояние сервиса")
    await _text_handler(app)(update, SimpleNamespace(user_data={}))
    assert "Состояние сервиса:" in replies[0][0]
