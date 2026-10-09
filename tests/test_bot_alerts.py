"""ТЗ бота п.9: настройки уведомлений (/alerts, bot_alert_pref) и мьютинг
(/mute, /unmute, bot_mute). Мьют/выключение останавливают ТОЛЬКО доставку:
событие помечается доставленным, после unmute накопившееся не уходит.
Пустые таблицы — старое поведение.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.handlers import _parse_duration_ms
from app.config import DetectorConfig
from app.db import Database
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.ltf_queue import LtfDispatcher
from app.notify.queue import EventDispatcher
from app.notify.suppress import bot_delivery_blocked, bot_group_for_ltf_kind
from tests.test_bot_cards import (  # noqa: F401 — фикстуры/хелперы
    _command,
    _context,
    _msg_update,
    _settings,
    db,
    seeded,
)
from tests.test_ltf_notify import BOS_PAYLOAD, RecSender, _event, _setup
from app.notify.telegram import build_application

CHAT_ID = "42"
T0 = 1_780_000_000_000


class RecAllSender:
    """Журнал send/send_text/send_ltf для EventDispatcher/LtfDispatcher."""

    def __init__(self):
        self.payloads: list = []
        self.texts: list = []
        self.ltf: list = []

    async def send_card(self, card, packet_id, *, quiet=False):
        if card.targets:
            self.payloads.append(card)
        else:
            self.texts.append(card.text)
        return len(self.payloads) + len(self.texts)

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        pass

    async def send(self, payload) -> None:
        self.payloads.append(payload)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        self.ltf.append((text, reply_markup))


def _htf_event(db, iid: int, kind: EventKind = EventKind.TOUCH) -> Event:
    # границы уникальны на каждый вызов: insert_zone идемпотентен по
    # параметрам, одинаковая зона дала бы дубликат события (UNIQUE)
    n = getattr(_htf_event, "_n", 0) + 1
    _htf_event._n = n
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=95.0 + n, upper=105.0 + n, formed_at=T0,
        confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    ev_id = db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=kind, occurred_at=T0,
        detected_at=T0, price=100.0, depth=0.0,
    ))
    assert ev_id is not None
    return db.get_events(zone_id=zid)[0]


# ------------------------------ парсинг длительности ------------------------------

def test_parse_duration():
    assert _parse_duration_ms("8h") == 8 * 3_600_000
    assert _parse_duration_ms("2d") == 2 * 86_400_000
    assert _parse_duration_ms("8") is None
    assert _parse_duration_ms("abc") is None
    assert _parse_duration_ms("") is None


# ------------------------------ prefs ------------------------------

def test_alert_pref_default_enabled_and_toggle(db):
    assert db.alert_pref_enabled(CHAT_ID, "global", "", "htf", "touch") is True
    db.set_alert_pref(CHAT_ID, "global", "", "htf", "touch", False)
    assert db.alert_pref_enabled(CHAT_ID, "global", "", "htf", "touch") is False
    # другой kind не затронут; instrument-scope не затронут
    assert db.alert_pref_enabled(CHAT_ID, "global", "", "htf", "approach")
    assert db.alert_pref_enabled(CHAT_ID, "instrument", "1", "htf", "touch")
    prefs = db.get_alert_prefs(CHAT_ID)
    assert len(prefs) == 1 and prefs[0]["enabled"] is False


def test_bot_delivery_blocked_empty_tables(db, seeded):
    assert not bot_delivery_blocked(
        db, CHAT_ID, grp="htf", kind="touch",
        instrument_id=seeded["eth"], zone_id=1,
    )
    assert not bot_delivery_blocked(db, None, grp="htf", kind="touch")


def test_bot_delivery_blocked_scopes(db, seeded):
    eth = seeded["eth"]
    now = now_ms()
    db.set_alert_pref(CHAT_ID, "instrument", str(eth), "htf", "touch", False)
    assert bot_delivery_blocked(db, CHAT_ID, grp="htf", kind="touch",
                                instrument_id=eth, now=now)
    # другой инструмент — не блокируется
    assert not bot_delivery_blocked(db, CHAT_ID, grp="htf", kind="touch",
                                    instrument_id=seeded["btc_bin"], now=now)
    # context-уровень (зона)
    db.set_alert_pref(CHAT_ID, "context", "7", "htf", "touch", False)
    assert bot_delivery_blocked(db, CHAT_ID, grp="htf", kind="touch",
                                instrument_id=seeded["btc_bin"], zone_id=7,
                                now=now)
    # watchlist alerts_enabled=0
    db.watchlist_add(CHAT_ID, seeded["btc_hl"])
    db.watchlist_set_alerts(CHAT_ID, seeded["btc_hl"], False)
    assert bot_delivery_blocked(db, CHAT_ID, grp="ltf", kind="bos",
                                instrument_id=seeded["btc_hl"], now=now)
    # истёкший мьют не действует
    db.set_mute_scope(CHAT_ID, "all", "", now - 1)
    assert not bot_delivery_blocked(db, CHAT_ID, grp="service", kind="service",
                                    now=now)


def test_ltf_group_mapping():
    assert bot_group_for_ltf_kind("touch") == "entry"
    assert bot_group_for_ltf_kind("cancellation") == "scenario"
    assert bot_group_for_ltf_kind("bos") == "ltf"


# ------------------------------ /mute /unmute команды ------------------------------

async def test_mute_unmute_commands(db, seeded):
    app = build_application(_settings(), db)
    # /mute ETH 8h
    update, replies = _msg_update("/mute ETH 8h")
    await _command(app, "mute")(update, _context(["ETH", "8h"]))
    mutes = db.get_mutes(CHAT_ID, now_ms())
    assert len(mutes) == 1 and mutes[0]["scope"] == "instrument"
    assert mutes[0]["scope_ref"] == str(seeded["eth"])
    assert "заглушены до" in replies[0][0]
    # /mute BTC 2d — неоднозначность → выбор
    update, replies = _msg_update("/mute BTC 2d")
    await _command(app, "mute")(update, _context(["BTC", "2d"]))
    assert "Несколько совпадений" in replies[0][0]
    # /mute all (дефолт 8h)
    update, replies = _msg_update("/mute all")
    await _command(app, "mute")(update, _context(["all"]))
    assert any(m["scope"] == "all" for m in db.get_mutes(CHAT_ID, now_ms()))
    assert "Сервисные продолжают" in replies[0][0]
    # /mute без аргументов — список активных
    update, replies = _msg_update("/mute")
    await _command(app, "mute")(update, _context([]))
    assert "Активные заглушения" in replies[0][0]
    # /unmute all
    update, replies = _msg_update("/unmute all")
    await _command(app, "unmute")(update, _context(["all"]))
    assert not any(m["scope"] == "all" for m in db.get_mutes(CHAT_ID, now_ms()))
    # /unmute ETH
    update, replies = _msg_update("/unmute ETH")
    await _command(app, "unmute")(update, _context(["ETH"]))
    assert db.get_mutes(CHAT_ID, now_ms()) == []
    assert "включены" in replies[0][0]


# ------------------------------ фильтрация доставки (HTF) ------------------------------

async def test_htf_mute_marks_delivered_no_resend(db, instrument_id):
    """Мьют инструмента: событие не отправляется, но помечается sent;
    после unmute retry_pending старое не шлёт, новое доставляется."""
    sender = RecAllSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, chat_id=CHAT_ID)
    db.set_mute_scope(CHAT_ID, "instrument", str(instrument_id),
                      now_ms() + 3_600_000)
    ev = _htf_event(db, instrument_id)
    deliveries = await disp.dispatch([ev])
    assert sender.payloads == []
    assert deliveries[0].status == "stale"  # подавлено, но «доставлено»
    db.clear_mute_scope(CHAT_ID, "instrument", str(instrument_id))
    await disp.retry_pending()
    assert sender.payloads == []  # накопившееся не ушло
    ev2 = _htf_event(db, instrument_id)
    await disp.dispatch([ev2])
    assert len(sender.payloads) == 1  # новое доставляется


async def test_htf_global_pref_blocks_kind(db, instrument_id):
    sender = RecAllSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, chat_id=CHAT_ID)
    db.set_alert_pref(CHAT_ID, "global", "", "htf", "touch", False)
    await disp.dispatch([_htf_event(db, instrument_id, EventKind.TOUCH)])
    assert sender.payloads == []
    # другой вид события проходит
    await disp.dispatch([_htf_event(db, instrument_id, EventKind.DEPTH_90)])
    assert len(sender.payloads) == 1


async def test_mute_all_keeps_service_messages(db, instrument_id):
    """/mute all глушит торговые, сервисные продолжают (ТЗ п.9)."""
    sender = RecAllSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, chat_id=CHAT_ID)
    db.set_mute_scope(CHAT_ID, "all", "", now_ms() + 3_600_000)
    await disp.dispatch([_htf_event(db, instrument_id)])
    assert sender.payloads == []
    await disp.notify_service("данные восстановлены")
    assert len(sender.texts) == 1 and "данные восстановлены" in sender.texts[0]


async def test_service_pref_blocks_service(db):
    sender = RecAllSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, chat_id=CHAT_ID)
    db.set_alert_pref(CHAT_ID, "global", "", "service", "service", False)
    await disp.notify_service("тихо")
    assert sender.texts == []
    pending = db.pending_deliveries()
    assert pending == []  # помечено доставленным — ретрая не будет


async def test_watchlist_alerts_disabled_blocks(db, instrument_id):
    sender = RecAllSender()
    disp = EventDispatcher(db, DetectorConfig(notification_digest_seconds=0), sender, chat_id=CHAT_ID)
    db.watchlist_add(CHAT_ID, instrument_id)
    db.watchlist_set_alerts(CHAT_ID, instrument_id, False)
    await disp.dispatch([_htf_event(db, instrument_id)])
    assert sender.payloads == []
    await disp.notify_service("сервис")
    assert len(sender.texts) == 1 and "сервис" in sender.texts[0]  # сервисные не зависят от watchlist


# ------------------------------ фильтрация доставки (LTF) ------------------------------

async def test_ltf_mute_marks_delivered(db, instrument_id):
    zone, obs, sc = _setup(db, instrument_id)
    settings = _settings()
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=settings)
    db.set_mute_scope(CHAT_ID, "instrument", str(instrument_id),
                      now_ms() + 3_600_000)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:mute:1")
    await disp([ev])
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is True
    db.clear_mute_scope(CHAT_ID, "instrument", str(instrument_id))
    await disp.retry_pending()
    assert sender.texts == []  # старое не уходит после unmute
    ev2 = _event(db, obs, sc, "sms", {
        "direction": "bear", "kind": "SMS", "stage": "primary",
        "break_level": 108.0, "range_pending": True, "entries": [],
    }, "sms:mute:2")
    await disp([ev2])
    assert len(sender.texts) == 1


async def test_ltf_pref_by_group(db, instrument_id):
    """Выключение группы «Сценарий» (cancellation) глушит только её."""
    zone, obs, sc = _setup(db, instrument_id)
    settings = _settings()
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=settings)
    db.set_alert_pref(CHAT_ID, "global", "", "scenario", "cancellation", False)
    ev = _event(db, obs, sc, "cancellation", {"reason": "manual"},
                "cancellation:pref:1")
    await disp([ev])
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is True


# ------------------------------ /alerts UI ------------------------------

async def test_alerts_menu_and_toggles(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/alerts")
    await _command(app, "alerts")(update, _context())
    text, markup = replies[0]
    assert "Группы уведомлений" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "nav:algrp:htf" in callbacks and "nav:algrp:ltf" in callbacks

    from tests.test_bot_cards import _cb_update, _nav_callback

    # группа → переключатели (дефолт ✅ на кнопках)
    update, _, edits = _cb_update("nav:algrp:htf")
    await _nav_callback(app)(update, _context())
    labels = [b.text for row in edits[0][1].inline_keyboard for b in row]
    assert any(t.startswith("✅") for t in labels)
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard
                 for b in row]
    assert "nav:altg:htf:touch" in callbacks
    # toggle → запись в bot_alert_pref, кнопка стала ☐
    update, _, edits = _cb_update("nav:altg:htf:touch")
    await _nav_callback(app)(update, _context())
    assert db.alert_pref_enabled(CHAT_ID, "global", "", "htf", "touch") is False
    labels = [b.text for row in edits[0][1].inline_keyboard for b in row]
    assert any(t.startswith("☐") for t in labels)
    # по активу: выбор → переключатели instrument scope
    update, _, edits = _cb_update("nav:alins:htf")
    await _nav_callback(app)(update, _context())
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard
                 for b in row]
    assert f"nav:alinsi:htf:{seeded['eth']}" in callbacks
    update, _, edits = _cb_update(f"nav:altgi:htf:touch:{seeded['eth']}")
    await _nav_callback(app)(update, _context())
    assert db.alert_pref_enabled(
        CHAT_ID, "instrument", str(seeded["eth"]), "htf", "touch"
    ) is False


async def test_zone_context_alerts(db, seeded):
    from tests.test_bot_cards import _cb_update, _nav_callback

    app = build_application(_settings(), db)
    zid = seeded["z_d1"]
    update, _, edits = _cb_update(f"nav:alz:{zid}")
    await _nav_callback(app)(update, _context())
    assert "Уведомления зоны" in edits[0][0]
    update, _, edits = _cb_update(f"nav:altgc:touch:{zid}")
    await _nav_callback(app)(update, _context())
    assert db.alert_pref_enabled(
        CHAT_ID, "context", str(zid), "htf", "touch"
    ) is False
