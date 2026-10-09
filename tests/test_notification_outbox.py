"""Regression scenarios from the October 9 notification flood."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.models import Event, EventKind, now_ms
from app.notify.compact import ltf_key, notification_html
from app.notify.ltf_queue import LtfDispatcher
from app.notify.outbox import Card, Outbox
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from tests.test_ltf_notify import _setup, _event, _ctx, BOS_PAYLOAD


def make_box(db, sender=None, interval=900):
    sender = sender or LogSender()
    box = Outbox(db, sender, DetectorConfig(notification_digest_seconds=interval))
    box.register("test", lambda r, m: None, lambda i, s: None)
    box.renderers["digest"] = box.render_digest
    return box, sender


def due(db):
    db.conn.execute("UPDATE notification_packet SET due_at=0 WHERE status IN ('pending','failed')")
    db.conn.commit()


def parent(db, iid, index):
    if index == 0:
        return _setup(db, iid)
    from app.models_ltf import LtfObservation, LtfScenario
    from app.models import Direction
    source = db.get_zones(instrument_id=iid)[0]
    zid = db.insert_zone(replace(source, id=None, formed_at=source.formed_at + index,
                                 lower=80 - index, upper=120 + index))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zid, zone_version=1, cycle_id=1,
        direction=Direction.BEAR, state="active", activated_at=source.formed_at))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR, trigger="BOS",
        stage="primary", state="monitoring_entries"))
    return db.get_zone(zid), obs, sc


async def test_ltf_same_entry_many_parents_one_card(db, instrument_id):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    events = []
    for i in range(12):
        _, obs, sc = parent(db, instrument_id, i)
        payload = {**BOS_PAYLOAD, "scenario_id": sc.id,
                   "entries": [{**BOS_PAYLOAD["entries"][0], "entry_zone_id": 100 + i}]}
        events.append(_event(db, obs, sc, "entries_ready", payload, f"entry:{i}"))
    assert await disp.deliver(events) == 12
    assert len(sender.cards) == 1
    card = sender.cards[0][0]
    assert "HTF-контекстов: 12" in card.text
    assert len(card.targets) == 12
    assert len(db.conn.execute("SELECT * FROM ltf_event").fetchall()) == 12
    await disp.deliver(events)
    assert len(sender.cards) == 1


async def test_late_equivalent_edits_original(db, instrument_id):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    for i in range(2):
        _, obs, sc = parent(db, instrument_id, i)
        ev = _event(db, obs, sc, "entries_ready", BOS_PAYLOAD, f"e:{i}")
        await disp.deliver([ev])
    assert len(sender.cards) == 1
    assert len(sender.edits) == 1
    assert "HTF-контекстов: 2" in sender.edits[0][1].text


def test_ltf_identity_preserves_real_differences(db, instrument_id):
    _, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "entries_ready", BOS_PAYLOAD, "a")
    ctx = _ctx(db, obs, sc)
    original = ltf_key(ev, ctx)
    assert original != ltf_key(replace(ev, occurred_at=ev.occurred_at + 3_600_000), ctx)
    changed = {**BOS_PAYLOAD, "entries": [{**BOS_PAYLOAD["entries"][0], "lower": 97}]}
    assert original != ltf_key(replace(ev, payload=changed), ctx)
    assert original != ltf_key(ev, replace(ctx, instrument=replace(ctx.instrument, venue="bybit")))
    assert original != ltf_key(ev, replace(ctx, instrument=replace(ctx.instrument, market_type="futures")))


async def test_restart_keeps_successful_packet(tmp_path):
    path = str(tmp_path / "db.sqlite")
    db = Database(path)
    box, sender = make_box(db)
    box.put("test", "one", Card("First"), [1])
    await box.flush()
    db.close()
    db = Database(path)
    box, second = make_box(db)
    box.put("test", "one", Card("First"), [1])
    await box.flush()
    assert not second.cards
    db.close()


async def test_concurrent_flush_claims_once(db):
    class Slow(LogSender):
        async def send_card(self, *args, **kwargs):
            await asyncio.sleep(.02)
            return await super().send_card(*args, **kwargs)
    sender = Slow()
    a, _ = make_box(db, sender)
    b, _ = make_box(db, sender)
    a.put("test", "one", Card("First"), [1])
    await asyncio.gather(a.flush(), b.flush())
    assert len(sender.cards) == 1


async def test_unknown_timeout_never_blindly_retried(db):
    class Timeout(LogSender):
        async def send_card(self, *args, **kwargs):
            await super().send_card(*args, **kwargs)
            raise TimeoutError("accepted but response lost")
    box, sender = make_box(db, Timeout())
    pid = box.put("test", "one", Card("First"), [1])
    await box.flush()
    due(db)
    await box.flush()
    assert box.get(pid)["status"] == "uncertain"
    assert len(sender.cards) == 1


async def test_rate_limit_respects_retry_after(db, monkeypatch):
    from telegram.error import RetryAfter
    class Limited(LogSender):
        fail = True
        async def send_card(self, *args, **kwargs):
            if self.fail:
                raise RetryAfter(120)
            return await super().send_card(*args, **kwargs)
    box, sender = make_box(db, Limited())
    pid = box.put("test", "one", Card("First"), [1])
    await box.flush()
    assert box.get(pid)["due_at"] >= now_ms() + 115_000
    sender.fail = False
    await box.flush()
    assert not sender.cards
    future = now_ms() + 121_000
    monkeypatch.setattr("app.notify.outbox.now_ms", lambda: future)
    due(db)
    await box.flush()
    assert len(sender.cards) == 1


async def test_quiet_window_one_digest_and_no_empty_messages(db):
    box, sender = make_box(db)
    box.put("test", "a", Card("A"), [1], quiet=True)
    box.put("test", "b", Card("B"), [2], quiet=True)
    await box.flush()
    assert not sender.cards
    due(db)
    await box.flush()
    assert len(sender.cards) == 1
    assert sender.cards[0][2] is True
    assert "A" in sender.cards[0][0].text and "B" in sender.cards[0][0].text
    await box.flush()
    assert len(sender.cards) == 1


async def test_muted_during_retry_is_suppressed(db):
    box, sender = make_box(db)
    pid = box.put("test", "a", Card("A"), [1], quiet=True)
    box.validators["test"] = lambda r, m: "muted"
    due(db)
    await box.flush()
    assert box.get(pid)["status"] == "suppressed"
    assert not sender.cards


async def test_service_incident_recovers_once_for_all_timeframes(db, instrument_id):
    from app.notify.service import ServiceNotifications
    box, sender = make_box(db)
    services = ServiceNotifications(box, None)
    ins = db.get_instrument(instrument_id)
    services.failed(ins, ["D1", "W1"])
    services.failed(ins, ["D1", "W1"])
    services.recovered(ins, "D1")
    row = db.conn.execute("SELECT * FROM notification_incident").fetchone()
    assert row["recovered_at"] is None
    services.recovered(ins, "W1")
    due(db)
    await box.flush()
    assert len(sender.cards) == 1
    assert "Данные восстановлены" in sender.cards[0][0].text
    assert "D1, W1" in sender.cards[0][0].text


async def test_chart_retry_does_not_repeat_market_text(db):
    box, sender = make_box(db)
    pid = box.put("test", "a", Card("Market event", "detail", chart_pending=True), [1])
    await box.flush()
    async def rendered(row, members):
        return Card("Market event", "detail", "graph.png")
    box.renderers["test"] = rendered
    assert await box.retry_media() == 1
    await box.flush()
    assert len(sender.cards) == 2
    assert sender.cards[1][0].text.startswith("📊 График к событию")
    assert sender.cards[1][2]
    assert not json.loads(box.get(pid)["card"])["chart_pending"]


def test_html_safe_at_caption_boundary():
    text = "<BTC&USDT>\n" + "🟢" * 1500
    html = notification_html(text, 1000)
    assert "<BTC" not in html and "&lt;BTC&amp;USDT&gt;" in html
    from html import unescape
    visible = unescape(html.replace("<b>", "").replace("</b>", ""))
    assert len(visible.encode("utf-16-le")) // 2 <= 1000


async def test_panels_reused_without_overwriting_signal(db):
    from app.notify.navigation import reusable_panel
    class Bot:
        sends, edits = [], []
        async def send_message(self, **kw):
            self.sends.append(kw)
            return SimpleNamespace(message_id=42)
        async def edit_message_text(self, **kw):
            self.edits.append(kw)
    bot = Bot()
    await reusable_panel(db, bot, "1", "packet:1", "Details")
    await reusable_panel(db, bot, "1", "packet:1", "More details")
    assert len(bot.sends) == 1 and len(bot.edits) == 1
    assert bot.edits[0]["message_id"] == 42


async def test_terminal_htf_duplicate_even_after_mixed_packet(db, instrument_id):
    from tests.test_bot_alerts import _htf_event
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    ev = _htf_event(db, instrument_id, EventKind.FVG_FILLED)
    another = _htf_event(db, instrument_id, EventKind.TOUCH)
    await disp.dispatch([ev, another])
    duplicate = replace(ev, id=None, occurred_at=ev.occurred_at + 1000, price=ev.price - .01)
    eid = db.insert_event(duplicate)
    await disp.dispatch([db.get_event(eid)])
    assert len(sender.cards) == 1
    assert len(sender.edits) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 3


async def test_superseding_one_zone_does_not_lose_other_zone(db, instrument_id):
    from tests.test_bot_alerts import _htf_event
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    a = _htf_event(db, instrument_id, EventKind.APPROACH)
    b = _htf_event(db, instrument_id, EventKind.APPROACH)
    await disp.dispatch([a, b])
    touch_id = db.insert_event(replace(a, id=None, kind=EventKind.TOUCH, occurred_at=a.occurred_at + 1))
    await disp.dispatch([db.get_event(touch_id)])
    assert len(sender.cards) == 1
    due(db)
    await disp.outbox.flush()
    assert len(sender.cards) == 2
    digest = sender.cards[1][0]
    assert len(digest.targets) == 1 and digest.targets[0]["zone_id"] == b.zone_id


async def test_finalized_packet_repairs_journal_after_crash(db, instrument_id):
    _, obs, sc = _setup(db, instrument_id)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "crash")
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    await disp.deliver([ev])
    db.conn.execute("UPDATE ltf_event SET delivered=0 WHERE id=?", (ev.id,))
    db.conn.commit()
    await disp.outbox.flush()
    assert db.get_ltf_event(ev.id).delivered
    assert len(sender.cards) == 1


async def test_expired_send_lease_requires_review(db):
    box, sender = make_box(db)
    pid = box.put("test", "lease", Card("A"), [1])
    db.conn.execute("UPDATE notification_packet SET status='sending',lease_until=0 WHERE id=?", (pid,))
    db.conn.commit()
    await box.flush()
    assert box.get(pid)["status"] == "uncertain" and not sender.cards


def test_migration_preserves_delivered_history(tmp_path):
    path = str(tmp_path / "old.sqlite")
    db = Database(path)
    db.conn.execute("INSERT INTO notification_packet(destination,channel,semantic_key,card,status,created_at,due_at) "
                    "VALUES('owner','test','old','{}','sent',1,1)")
    db.conn.execute("ALTER TABLE notification_member DROP COLUMN reason")
    db.conn.commit()
    db.close()
    db = Database(path)
    assert "reason" in {r["name"] for r in db.conn.execute("PRAGMA table_info(notification_member)")}
    assert db.conn.execute("SELECT status FROM notification_packet").fetchone()[0] == "sent"
    db.close()


def test_group_buttons_never_target_first_object():
    from app.notify.navigation import card_keyboard
    targets = [dict(chart="nav:chartz:1"), dict(chart="nav:chartz:2")]
    keyboard = card_keyboard(7, targets)
    assert keyboard.inline_keyboard[0][0].callback_data == "nf:g:7"
    assert sum(len(row) for row in keyboard.inline_keyboard) == 3
