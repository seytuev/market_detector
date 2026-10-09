"""ТЗ бота п.10: единый набор кнопок под автоматическими сигналами.

HTF — TelegramSender.build_keyboard (График/Подробнее/Открыть приложение/
Заглушить/Обновить + «Показать LTF» под касанием); LTF — LtfDispatcher
через Sender.send_ltf с клавиатурой по виду события; «Обновить» шлёт
новое сообщение, не редактируя исторический сигнал; range_ready при
пересчёте без изменений не дублируется (UNIQUE dedupe_key).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import DetectorConfig, Settings
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
from app.models_ltf import LtfEvent
from app.notify.ltf_queue import LtfDispatcher
from app.notify.queue import EventView, MessagePayload
from app.notify.telegram import LogSender, TelegramSender, build_application
from tests.test_bot_cards import (  # noqa: F401 — фикстуры/хелперы
    _command,
    _context,
    _make_live,
    _nav_callback,
    db,
    seeded,
)
from tests.test_ltf_notify import (
    BOS_PAYLOAD,
    RecSender,
    _dispatcher,
    _event,
    _setup,
)

T0 = 1_780_000_000_000
CHAT_ID = "42"
PUBLIC_URL = "https://htf.example.com"


def _settings() -> Settings:
    s = Settings()
    s.telegram_token = "123:test-token"
    s.telegram_chat_id = CHAT_ID
    s.public_base_url = PUBLIC_URL
    return s


def _htf_payload(db, kind: EventKind):
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=95.0, upper=105.0, formed_at=T0,
        confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    zone = db.get_zone(zid)
    ins = db.get_instrument(iid)
    ev = Event(
        id=None, zone_id=zid, cycle_id=1, kind=kind, occurred_at=T0,
        detected_at=T0, price=100.0, depth=0.0,
    )
    view = EventView(event=ev, zone=zone, instrument=ins)
    return MessagePayload(events=[ev], zones=[zone], views=[view]), iid, zid


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data]


def _urls(markup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


# ------------------------------ HTF-клавиатура ------------------------------

def test_htf_keyboard_unified_set(db):
    payload, iid, zid = _htf_payload(db, EventKind.TOUCH)
    sender = TelegramSender("123:t", CHAT_ID, db, site_base_url=PUBLIC_URL)
    markup = sender.build_keyboard(payload)
    cbs = _callbacks(markup)
    # единый набор: График (рендер в чате), Подробнее, Заглушить, Обновить
    assert f"nav:chartz:{zid}" in cbs
    assert f"nav:zone:{zid}" in cbs
    assert f"nav:refresh:{iid}" in cbs
    assert any(cb.startswith(f"htf:mute:{zid}:") for cb in cbs)  # «Заглушить»
    # «Открыть приложение» — публичный URL, не localhost
    assert f"{PUBLIC_URL}/?zone={zid}" in _urls(markup)
    assert all("127.0.0.1" not in u and "localhost" not in u
               for u in _urls(markup))
    # старые кнопки на месте
    assert any(cb.startswith("htf:ack:") for cb in cbs)
    assert any(cb.startswith("htf:snooze:") for cb in cbs)
    assert any(cb.startswith("htf:note:") for cb in cbs)


def test_htf_keyboard_show_ltf_only_on_touch(db):
    sender = TelegramSender("123:t", CHAT_ID, db, site_base_url=PUBLIC_URL)
    payload, iid, _ = _htf_payload(db, EventKind.TOUCH)
    assert f"nav:ltf:{iid}" in _callbacks(sender.build_keyboard(payload))
    payload2, iid2, _ = _htf_payload(db, EventKind.APPROACH)
    assert f"nav:ltf:{iid2}" not in _callbacks(sender.build_keyboard(payload2))


# ------------------------------ LTF-клавиатура ------------------------------

def _setup_ltf(db, instrument_id):
    return _setup(db, instrument_id)


async def test_ltf_keyboard_per_kind(db, instrument_id):
    zone, obs, sc = _setup_ltf(db, instrument_id)
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=_settings())
    # F03: entries_ready/touch доставляются только при data_state ok —
    # свежие H1/котировка/курсор (иначе гейт единой оценки качества)
    _make_live(db, instrument_id)

    cases = {
        "bos": BOS_PAYLOAD,
        "entries_ready": {
            "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
            "entries": [{"entry_zone_id": 3, "type": "BSL", "lower": 115.0,
                         "upper": 115.0, "mid": 115.0, "overlap": "full"}],
        },
        "touch": {
            "entry_zone_id": 3, "type": "BSL", "lower": 115.0, "upper": 115.0,
            "mid": 115.0, "candle_open_time": T0,
        },
        "cancellation": {"reason": "reverse_bos"},
    }
    for kind, payload in cases.items():
        sender.texts.clear()
        sender.markups.clear()
        ev = _event(db, obs, sc, kind, payload, f"{kind}:kb:1")
        await disp([ev])
        assert len(sender.texts) == 1, kind
        markup = sender.markups[-1]
        assert markup is not None, kind
        cbs = _callbacks(markup)
        iid = instrument_id
        # Карточка: график этого наблюдения, подробности и действия.
        # Обновить и заглушить открываются из «Действия», не с первой клавиатуры.
        assert f"nav:charto:{iid}:{obs.id}" in cbs
        assert any(cb.startswith("nf:d:") for cb in cbs), kind
        assert any(cb.startswith("nf:a:") for cb in cbs), kind


async def test_ltf_dispatcher_uses_send_ltf_and_logsender_journals(db, instrument_id):
    zone, obs, sc = _setup_ltf(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:log:1")
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=_settings())
    await disp([ev])
    assert len(sender.cards) == 1
    card, packet_id, _quiet = sender.cards[0]
    assert "BOS подтверждён" in card.text
    from app.notify.navigation import card_keyboard
    assert card_keyboard(packet_id, card.targets) is not None
    # Длинный список зон остаётся одной карточкой: подробности за кнопкой,
    # а не пачкой сообщений.
    entries = [
        {"entry_zone_id": i, "type": "FVG", "lower": 100.0 + i,
         "upper": 101.0 + i, "mid": 100.5 + i, "overlap": "full"}
        for i in range(200)
    ]
    ev2 = _event(db, obs, sc, "bos", dict(BOS_PAYLOAD, entries=entries),
                 "bos:log:2")
    await disp([ev2])
    assert len(sender.cards) == 2
    assert sender.cards[1][0].details


# ------------------------------ «Обновить» ------------------------------

async def test_nav_refresh_sends_new_message(db, seeded):
    """nav:refresh:<iid> — карточка актива новым сообщением, исходный
    текст сигнала не редактируется."""
    app = build_application(_settings(), db)
    answers: list = []
    edits: list = []
    replies: list = []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(t, reply_markup=None):
        edits.append(t)

    async def reply_text(t, reply_markup=None):
        replies.append((t, reply_markup))

    sent: list = []

    async def send_message(chat_id, text, reply_markup=None, disable_web_page_preview=None):
        sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=77)

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(CHAT_ID)),
        callback_query=SimpleNamespace(
            data=f"nav:refresh:{seeded['eth']}", answer=answer,
            edit_message_text=edit_message_text,
            message=SimpleNamespace(reply_text=reply_text),
        ),
    )
    context = _context()
    context.bot = SimpleNamespace(send_message=send_message)
    await _nav_callback(app)(update, context)
    assert edits == []  # исторический сигнал не редактируется
    assert replies == []
    assert len(sent) == 1
    assert "ETHUSDT" in sent[0][1]
    assert answers == [None]


# ------------------------------ range_recalc dedupe ------------------------------

async def test_range_ready_not_resent_on_recalc(db, instrument_id):
    """Пересчёт диапазона без изменений не порождает повторный сигнал:
    dedupe_key range_ready/entries_ready стабилен (UNIQUE — событие не
    создаётся), а доставленное событие dispatcher не шлёт повторно."""
    zone, obs, sc = _setup_ltf(db, instrument_id)
    payload = {
        "scenario_id": sc.id,
        "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
        "note": "подходящих свежих Entry Zones пока нет",
    }
    key = f"range_ready:{sc.id}:1"
    ev1, created1 = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id, kind="range_ready",
        payload=payload, occurred_at=T0, detected_at=T0, dedupe_key=key,
        delayed=False,
    ))
    assert created1
    # тот же пересчёт ещё раз — событие не создаётся (UNIQUE)
    _, created2 = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id, kind="range_ready",
        payload=payload, occurred_at=T0 + 1000, detected_at=T0 + 1000,
        dedupe_key=key, delayed=False,
    ))
    assert not created2
    sender = RecSender()
    disp = _dispatcher(db, sender)
    await disp([ev1])
    await disp([ev1])          # тот же ключ не создаёт второй пакет
    await disp.retry_pending()
    packets = db.conn.execute(
        "SELECT id FROM notification_packet WHERE channel='ltf'"
    ).fetchall()
    assert len(packets) == 1
    assert sender.texts == []  # range_ready тихий и ждёт сводку
    db.conn.execute(
        "UPDATE notification_packet SET due_at=0 WHERE channel='ltf' AND status='pending'"
    )
    db.conn.commit()
    await disp.outbox.flush()
    assert len(sender.texts) == 1
    await disp.outbox.flush()
    assert len(sender.texts) == 1

    # entries_ready с тем же составом зон — тот же ключ, тоже без дубля
    ekey = f"entries_ready:{sc.id}:3,4"
    e1, c1 = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id,
        kind="entries_ready",
        payload={"range": payload["range"], "entries": [
            {"entry_zone_id": 3, "type": "BSL", "lower": 115.0,
             "upper": 115.0, "mid": 115.0, "overlap": "full"}]},
        occurred_at=T0, detected_at=T0, dedupe_key=ekey, delayed=False,
    ))
    _, c2 = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id,
        kind="entries_ready", payload={}, occurred_at=T0 + 1,
        detected_at=T0 + 1, dedupe_key=ekey, delayed=False,
    ))
    assert c1 and not c2
    await disp([e1])
    assert len(sender.texts) == 2  # range_ready + один entries_ready
