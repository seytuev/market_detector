"""One market event across assets, parents, ranges and delivery attempts."""
from dataclasses import replace

import pytest

from app.config import DetectorConfig
from app.models import Direction, Instrument
from app.models_ltf import LtfEntryZone
from app.notify.ltf_projection import present_event
from app.notify.ltf_queue import LtfDispatcher
from app.notify.telegram import LogSender
from tests.test_ltf_notify import BOS_PAYLOAD, T0, _ctx, _event
from tests.test_notification_outbox import parent


@pytest.mark.parametrize("symbol,scale", [("BTC", 1000), ("ETH", 30), ("ZEC", 10),
                                         ("SOL", 1), ("HYPE", .3), ("XRP", .01), ("PEPE", .0000001)])
@pytest.mark.parametrize("direction", [Direction.BULL, Direction.BEAR])
async def test_one_break_and_same_close_zone_delta_for_every_asset(db, symbol, scale, direction):
    venue = "hyperliquid" if symbol == "HYPE" else "binance"
    display_symbol = symbol if venue == "hyperliquid" else symbol + "USDT"
    iid = db.upsert_instrument(Instrument(None, symbol, venue, "spot", display_symbol, "USDT"))
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    events = []
    for i in range(3):
        _, obs, sc = parent(db, iid, i)
        db.update_ltf_scenario(sc.id, direction=direction, trigger="SMS" if i else "BOS")
        payload = {**BOS_PAYLOAD, "direction": direction.value, "break_level": 110 * scale,
                   "stage": "secondary" if i else "primary",
                   "range": {"lower": (90 + i) * scale, "upper": (110 + i) * scale, "mid": (100 + i) * scale},
                   "entries": [{"type": ("SSL", "FVG", "OB")[i], "lower": (95 + i) * scale,
                                "upper": (95 + i) * scale, "mid": (95 + i) * scale}]}
        events.append(_event(db, obs, sc, "entries_ready" if i == 2 else "bos", payload, f"all:{i}"))
    # Arrival order must not turn the zone delta into a separate notification.
    await disp.deliver(list(reversed(events)))
    assert len(sender.cards) == 1
    card = sender.cards[0][0]
    assert display_symbol in card.text and "BOS подтверждён" in card.text
    assert "Ещё зон: 1" in card.text
    assert len(card.targets) == 3
    assert all(db.get_ltf_event(e.id).delivered for e in events)
    await disp.deliver(events)
    assert len(sender.cards) == 1


@pytest.mark.parametrize("first", ["bos", "entries_ready"])
async def test_late_same_close_delta_updates_original_in_both_orders(db, instrument_id, first):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    _, obs, sc = parent(db, instrument_id, 0)
    bos = _event(db, obs, sc, "bos", {**BOS_PAYLOAD, "entries": []}, "late:bos")
    delta = _event(db, obs, sc, "entries_ready", BOS_PAYLOAD, "late:delta")
    a, b = (bos, delta) if first == "bos" else (delta, bos)
    await disp.deliver([a])
    await disp.deliver([b])
    assert len(sender.cards) == 1 and len(sender.edits) == 1
    assert "BOS подтверждён" in sender.edits[0][1].text
    assert "FVG H1" in sender.edits[0][1].text


async def test_two_distinct_breaks_never_swallow_a_zone_delta(db, instrument_id):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    _, obs, sc = parent(db, instrument_id, 0)
    first = _event(db, obs, sc, "bos", BOS_PAYLOAD, "distinct:1")
    second = _event(db, obs, sc, "bos", {**BOS_PAYLOAD, "break_level": 111}, "distinct:2")
    delta = _event(db, obs, sc, "entries_ready", BOS_PAYLOAD, "distinct:delta")
    await disp.deliver([first, second, delta])
    assert len(sender.cards) == 3


def local_movement(db, iid):
    from tests.test_h1_local_pd import _bars, _pivots, _bull, _drive
    candles = [replace(c, instrument_id=iid) for c in _bars()]
    db.insert_candles(candles)
    _drive(db, iid, candles, _pivots(), [_bull()])
    return candles


def store_at(db, ev, moment):
    stored, _ = db.insert_ltf_event(replace(ev, id=None, occurred_at=moment,
                                          detected_at=moment, dedupe_key=ev.dedupe_key + ":timed"))
    return stored


def test_summary_uses_local_range_and_event_close_keeps_raw_history(db, instrument_id):
    candles = local_movement(db, instrument_id)
    _, obs, sc = parent(db, instrument_id, 0)
    db.update_ltf_scenario(sc.id, direction=Direction.BULL)
    moment = candles[5].close_time
    old = db.insert_ltf_entry_zone(LtfEntryZone(None, instrument_id, "FVG", Direction.BULL,
                                               415.36, 415.59, T0 - 100 * 3_600_000, T0 - 99 * 3_600_000))
    raw = {**BOS_PAYLOAD, "direction": "bull", "range": {"lower": 109000, "upper": 112000},
           "entries": [{"entry_zone_id": old.id, "type": "FVG", "lower": old.lower,
                        "upper": old.upper, "mid": old.mid}]}
    ev = store_at(db, _event(db, obs, sc, "bos", raw, "local:bos"), moment)
    shown = present_event(db, DetectorConfig(), ev, _ctx(db, obs, sc))
    assert shown.payload["range"] == {"lower": 80400, "upper": 83400, "mid": 81900}
    assert shown.payload["range_status"] == "provisional"
    assert shown.payload["close"] == 83300  # not the later 83500 candle
    assert shown.payload["entries"] == []
    assert shown.payload["excluded_entries"][0]["presentation_exclusion"] == "other_movement"
    assert db.get_ltf_event(ev.id).payload == raw


def test_unknown_zone_provenance_is_not_an_entry_on_a_known_local_leg(db, instrument_id):
    candles = local_movement(db, instrument_id)
    _, obs, sc = parent(db, instrument_id, 0)
    db.update_ltf_scenario(sc.id, direction=Direction.BULL)
    ev = store_at(db, _event(db, obs, sc, "bos", {
        "direction": "bull", "entries": [dict(type="FVG", lower=415.36, upper=415.59)]
    }, "unknown:zone"), candles[5].close_time)
    shown = present_event(db, DetectorConfig(), ev, _ctx(db, obs, sc))
    assert shown.payload["entries"] == []
    assert shown.payload["excluded_entries"][0]["presentation_exclusion"] == "provenance_unavailable"


async def test_old_zone_list_is_not_renotified_as_new_entry(db, instrument_id):
    candles = local_movement(db, instrument_id)
    _, obs, sc = parent(db, instrument_id, 0)
    db.update_ltf_scenario(sc.id, direction=Direction.BULL)
    payload = {"direction": "bull", "entries": [dict(type="FVG", lower=415.36, upper=415.59,
                                                       formed_at=T0 - 100 * 3_600_000)]}
    ev = store_at(db, _event(db, obs, sc, "entries_ready", payload, "old:list"), candles[5].close_time)
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    await disp.deliver([ev])
    assert not sender.cards
    assert db.get_ltf_event(ev.id).delivered


async def test_retry_rechecks_zones_without_suppressing_valid_bos(db, instrument_id):
    candles = local_movement(db, instrument_id)
    _, obs, sc = parent(db, instrument_id, 0)
    db.update_ltf_scenario(sc.id, direction=Direction.BULL)
    payload = {**BOS_PAYLOAD, "direction": "bull", "entries": []}
    bos = store_at(db, _event(db, obs, sc, "bos", payload, "retry:bos"), candles[5].close_time)
    zone = db.insert_ltf_entry_zone(LtfEntryZone(None, instrument_id, "FVG", Direction.BULL,
                                                81000, 81200, T0, T0 + 1000))
    delta = store_at(db, _event(db, obs, sc, "entries_ready", {
        "direction": "bull", "entries": [dict(entry_zone_id=zone.id, type="FVG", lower=81000, upper=81200, mid=81100)]
    }, "retry:delta"), candles[5].close_time)
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    disp.outbox.inline_delivery = False
    await disp.deliver([bos, delta])
    db.update_ltf_entry_zone(zone.id, validity="invalid")
    await disp.outbox.flush()
    assert len(sender.cards) == 1
    assert "BOS подтверждён" in sender.cards[0][0].text
    assert "FVG H1" not in sender.cards[0][0].text
    assert "81 000,00" in sender.cards[0][0].details


async def test_key_upgrade_does_not_replay_already_sent_event(db, instrument_id):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    _, obs, sc = parent(db, instrument_id, 0)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "legacy:key")
    await disp.deliver([ev])
    db.conn.execute("UPDATE notification_packet SET semantic_key='old-version-key'")
    db.conn.commit()
    await disp.deliver([db.get_ltf_event(ev.id)])
    assert len(sender.cards) == 1


async def test_parent_range_changes_do_not_merge_different_markets(db):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    for i, (venue, market) in enumerate([("binance", "spot"), ("bybit", "futures"), ("binance", "futures")]):
        iid = db.upsert_instrument(Instrument(None, "ZEC", venue, market, "ZECUSDT", "USDT"))
        _, obs, sc = parent(db, iid, 0)
        await disp.deliver([_event(db, obs, sc, "bos", BOS_PAYLOAD, f"market:{i}")])
    assert len(sender.cards) == 3


async def test_incomplete_legacy_geometry_cannot_block_the_whole_card(db, instrument_id):
    _, obs, sc = parent(db, instrument_id, 0)
    ev = _event(db, obs, sc, "bos", {**BOS_PAYLOAD,
        "range": {"lower": 90, "upper": 110},
        "entries": [{"type": "FVG", "lower": 98, "upper": 102}]}, "legacy:partial")
    sender = LogSender()
    await LtfDispatcher(db, DetectorConfig(), sender).deliver([ev])
    assert len(sender.cards) == 1
    assert "FVG H1" in sender.cards[0][0].text


async def test_closed_alt_setup_keeps_facts_without_old_entry_offer(db):
    from app.notify.alt_queue import AltDispatcher
    from tests.test_alt_notify import _seed, _settings, _event as alt_event, FakeSender
    _, _, _, setup = _seed(db)
    db.update_alt_setup(setup.id, state="cancelled")
    alt_event(db, setup.id, "entry_a", {"price": 1.9})
    alt_event(db, setup.id, "ssl_taken", {"level_price": 1.1, "close": 1.2})
    sender = FakeSender()
    await AltDispatcher(db, _settings(), sender).dispatch_pending()
    assert len(sender.alt) == 1
    assert "Возможность A" not in sender.alt[0]
    assert "Сетап уже завершён" in sender.alt[0]
    assert "Возможность A" in sender.details[0]


async def test_changed_photo_survives_failed_edit_and_retry(db):
    import json
    from app.notify.outbox import Card
    from tests.test_notification_outbox import make_box

    class Sender(LogSender):
        def __init__(self):
            super().__init__()
            self.replacements = []

        async def edit_card(self, message_id, card, packet_id, *, photo=False):
            self.replacements.append(card.replace_image)
            if len(self.replacements) == 1:
                raise RuntimeError("temporary edit failure")
            return await super().edit_card(message_id, card, packet_id, photo=photo)

    sender = Sender()
    box, _ = make_box(db, sender)
    pid = box.put("test", "photo-change", Card("BOS", image_path="first.png"), [1])
    await box.flush()

    async def render(row, members):
        return Card("BOS + zones", image_path="combined.png")

    box.renderers["test"] = render
    box.put("test", "photo-change", Card(""), [2])
    await box.flush()
    key = f"notification:edit:{pid}"
    state = json.loads(db.get_meta(key))
    assert state["image_path"] == "first.png"
    db.set_meta(key, json.dumps({**state, "due_at": 0}))
    await box.flush()
    assert sender.replacements == [True, True]
    assert box.get(pid)["dirty"] == 0


async def test_telegram_replaces_changed_photo_with_matching_caption(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.notify.outbox import Card
    from app.notify.telegram import TelegramSender
    path = tmp_path / "event.png"
    path.write_bytes(b"test image")
    card = Card("BOS + зоны", image_path=str(path))
    card.replace_image = True
    sender = object.__new__(TelegramSender)
    sender.chat_id = "test"
    sender._bot = SimpleNamespace(edit_message_media=AsyncMock(), edit_message_caption=AsyncMock())
    await sender.edit_card(1, card, 1, photo=True)
    sender._bot.edit_message_media.assert_awaited_once()
    sender._bot.edit_message_caption.assert_not_awaited()
    assert "BOS + зоны" in sender._bot.edit_message_media.call_args.kwargs["media"].caption


def test_notification_chart_does_not_pin_the_old_parent_zone_catalog(db, instrument_id, monkeypatch):
    from app.config import Settings
    from app.notify.h1_image import event_snapshot
    from tests.conftest import make_candle
    from tests.test_h1_zone_selection import zone
    _, obs, sc = parent(db, instrument_id, 0)
    candle = make_candle(T0, 100, 105, 95, 102, timeframe="H1", instrument_id=instrument_id)
    db.insert_candles([candle])
    ev = store_at(db, _event(db, obs, sc, "bos", {
        "entries": [dict(entry_zone_id=8, type="OB", lower=118, upper=119)]
    }, "chart:catalog"), candle.close_time)
    monkeypatch.setattr("app.services.h1_chart.assemble_h1_layers", lambda *a, **k: {
        "setup": {"lower": 90, "upper": 110},
        "detected_zones": [zone(i, 110 + i, 111 + i) for i in range(1, 20)],
        "scenario_admission": [dict(zone_id=i, eligibility="eligible") for i in range(1, 20)]})
    snap = event_snapshot(db, obs.id, Settings(), ev)
    assert {z["id"] for z in snap["layers"]["detected_zones"]} == {1, 2, 3, 4, 5, 8}
