"""ТЗ 07.10.2026 §13.6: просроченный текущий вход не отправляется —
ни при первой доставке, ни при ретрае."""
from __future__ import annotations

import pytest

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
from app.models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
    LtfObservation,
    LtfScenario,
)
from app.notify.ltf_queue import LtfDispatcher
from app.notify.queue import EventDispatcher, MessagePayload


class _RecSender:
    def __init__(self):
        self.payloads: list[MessagePayload] = []
        self.ltf_texts: list[str] = []
        self.ltf_photos: list[tuple[str, str, object]] = []

    async def send_card(self, card, packet_id, *, quiet=False):
        await self.send(card)
        self.ltf_texts.append(card.text)
        return len(self.payloads)

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        pass

    async def send(self, payload: MessagePayload) -> None:
        self.payloads.append(payload)

    async def send_text(self, text: str) -> None:
        pass

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        self.ltf_texts.append(text)

    async def send_ltf_photo(self, image_path: str, caption: str,
                             reply_markup=None) -> None:
        self.ltf_photos.append((image_path, caption, reply_markup))


def _htf_db() -> tuple[Database, int]:
    db = Database(":memory:")
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zid = db.insert_zone(
        Zone(None, iid, ZoneType.SSL, Direction.BULL, "D1",
             lower=2600.15, upper=2600.15, formed_at=now_ms() - 10_000,
             confirmed_at=now_ms() - 9_000, status=ZoneStatus.ACTIVE,
             created_at=now_ms())
    )
    return db, zid


def _event(db: Database, zone_id: int, kind: EventKind, ts: int) -> Event:
    eid = db.insert_event(
        Event(None, zone_id, 1, kind, occurred_at=ts, detected_at=ts, price=2600.15)
    )
    return next(e for e in db.get_events(zone_id=zone_id) if e.id == eid)


async def test_htf_entry_event_delivered_while_zone_active():
    db, zid = _htf_db()
    sender = _RecSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    ev = _event(db, zid, EventKind.TOUCH, now_ms())
    deliveries = await disp.dispatch([ev])
    assert len(sender.payloads) == 1
    assert deliveries[0].status == "sent"


async def test_htf_entry_event_stale_after_level_taken():
    """Уровень снят между детекцией и отправкой — вход не уходит (§13.6)."""
    db, zid = _htf_db()
    sender = _RecSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    db.update_zone(zid, status=ZoneStatus.TAKEN, market_validity="invalid")
    ev = _event(db, zid, EventKind.TOUCH, now_ms())
    deliveries = await disp.dispatch([ev])
    assert sender.payloads == []
    assert deliveries[0].status == "stale"
    # ретрай просроченное не подбирает
    assert await disp.retry_pending() == []
    assert sender.payloads == []


async def test_htf_entry_event_stale_when_entry_eligible_lost():
    db, zid = _htf_db()
    sender = _RecSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    db.update_zone(zid, entry_eligible=False)
    ev = _event(db, zid, EventKind.APPROACH, now_ms())
    deliveries = await disp.dispatch([ev])
    assert sender.payloads == []
    assert deliveries[0].status == "stale"


async def test_htf_fact_event_delivered_despite_zone_terminal():
    """LEVEL_TAKEN — факт инвалидации, доставляется даже по снятой зоне."""
    db, zid = _htf_db()
    sender = _RecSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    db.update_zone(zid, status=ZoneStatus.TAKEN, market_validity="invalid")
    ev = _event(db, zid, EventKind.LEVEL_TAKEN, now_ms())
    deliveries = await disp.dispatch([ev])
    assert len(sender.payloads) == 1
    assert deliveries[0].status == "sent"


async def test_htf_retry_skips_zone_that_became_taken():
    """Событие встало в очередь по активной зоне, зона снята до ретрая."""
    db, zid = _htf_db()

    class _FailOnce(_RecSender):
        async def send(self, payload: MessagePayload) -> None:
            raise RuntimeError("network down")

    sender = _FailOnce()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    ev = _event(db, zid, EventKind.TOUCH, now_ms())
    await disp.dispatch([ev])  # упало в failed
    db.update_zone(zid, status=ZoneStatus.TAKEN, market_validity="invalid")
    db.conn.execute("UPDATE notification_packet SET due_at=0")
    db.conn.commit()
    await disp.retry_pending()
    statuses = {
        r["status"]
        for r in db.conn.execute("SELECT status FROM delivery").fetchall()
    }
    assert statuses == {"stale"}


# ---------- LTF ----------


def _ltf_db() -> tuple[Database, int, int]:
    db = Database(":memory:")
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zid = db.insert_zone(
        Zone(None, iid, ZoneType.OB, Direction.BEAR, "D1",
             lower=3000.0, upper=3100.0, formed_at=now_ms() - 10_000,
             confirmed_at=now_ms() - 9_000, status=ZoneStatus.ACTIVE,
             created_at=now_ms())
    )
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active",
        activated_at=now_ms() - 8_000,
    ))
    return db, iid, obs.id


def _scenario(db: Database, obs_id: int, state: str = "monitoring_entries") -> int:
    sc = db.insert_ltf_scenario(
        LtfScenario(None, obs_id, Direction.BEAR, "BOS", "primary", state=state,
                    created_at=now_ms(), updated_at=now_ms())
    )
    return sc.id


def _ltf_event(db: Database, obs_id: int, sc_id: int, kind: str,
               payload: dict) -> LtfEvent:
    ts = now_ms()
    ev = LtfEvent(None, obs_id, kind, ts, ts, f"{kind}:{ts}:{kind}",
                  scenario_id=sc_id, payload=payload, processing_mode="live")
    stored, _ = db.insert_ltf_event(ev)
    return db.get_ltf_event(stored.id)


async def test_ltf_touch_stale_when_scenario_cancelled():
    db, iid, obs_id = _ltf_db()
    sc_id = _scenario(db, obs_id, state="cancelled")
    ev = _ltf_event(db, obs_id, sc_id, "touch", {"entry_zone_id": 1, "type": "OB"})
    sender = _RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    assert await disp.deliver([ev]) == 0
    assert sender.ltf_texts == []
    assert db.get_ltf_event(ev.id).delivered


async def test_ltf_touch_stale_when_level_swept():
    """Снятый уровень BSL/SSL не предлагается как вход (§3, §13.6)."""
    db, iid, obs_id = _ltf_db()
    sc_id = _scenario(db, obs_id)
    ez = db.insert_ltf_entry_zone(
        LtfEntryZone(None, iid, "BSL", Direction.BEAR, 3200.0, 3200.0,
                     formed_at=now_ms())
    )
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        None, ez.id, sc_id, 3200.0, touch_at=now_ms(), candle_open_time=now_ms(),
        state="confirmed",
    ))
    ev = _ltf_event(db, obs_id, sc_id, "touch",
                    {"entry_zone_id": ez.id, "type": "BSL"})
    sender = _RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    assert await disp.deliver([ev]) == 0
    assert sender.ltf_texts == []


async def test_ltf_bos_fact_delivered_despite_cancellation():
    """BOS/SMS — рыночный факт: доставляется и по закрытому сценарию."""
    db, iid, obs_id = _ltf_db()
    sc_id = _scenario(db, obs_id, state="closed")
    ev = _ltf_event(db, obs_id, sc_id, "bos", {})
    sender = _RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    assert await disp.deliver([ev]) == 1
    assert len(sender.ltf_texts) >= 1
