"""W1 touch delivery is limited per coin using successful send times."""
import json

import pytest

from app.alt.store_v3 import insert_event
from app.notify.alt_queue import AltDispatcher, W1_TOUCH_COOLDOWN_MS
from app.notify.outbox import Card
from app.notify.telegram import LogSender
from tools.preview_alt_w1_notification import example


@pytest.fixture
def touches(db, monkeypatch):
    ctx, _ = example(db)
    clock = [ctx.lead["event_time_ms"]]
    monkeypatch.setattr("app.notify.alt_queue.now_ms", lambda: clock[0])
    monkeypatch.setattr("app.notify.outbox.now_ms", lambda: clock[0])
    sender = LogSender()

    def dispatcher():
        result = AltDispatcher(db, None, sender)

        async def render(row, members):
            return Card(",".join(e["event_type"] for e in result._v3_rows(members)))

        result.outbox.renderers["alt_v3"] = render
        return result

    def add(event_type="retest", **payload):
        event = {**ctx.lead, "id": f"{event_type}-{clock[0]}", "event_type": event_type,
                 "event_time_ms": clock[0], "payload": {**ctx.lead["payload"], **payload}}
        insert_event(db, ctx.asset.id, ctx.source.id, ctx.frame["structure_key"], event)

    return db, clock, sender, dispatcher, add


async def test_repeated_touches_do_not_extend_cooldown_and_restart_preserves_it(touches):
    db, clock, sender, dispatcher, add = touches
    first = clock[0]
    await dispatcher().dispatch_v3()
    for elapsed in (60_000, 120_000, W1_TOUCH_COOLDOWN_MS - 1):
        clock[0] = first + elapsed
        add(zone_id="another-zone", kind="OB")
        await dispatcher().dispatch_v3()
    assert len(sender.cards) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM notification_member WHERE reason='touch cooldown: 4 hours'").fetchone()[0] == 3
    clock[0] = first + W1_TOUCH_COOLDOWN_MS
    add()
    await dispatcher().dispatch_v3()
    assert len(sender.cards) == 2


@pytest.mark.parametrize("kind", ["bos_confirmed", "cancelled", "zone_invalidated"])
async def test_important_event_survives_mixed_packet_during_cooldown(touches, kind):
    db, clock, sender, dispatcher, add = touches
    dispatch = dispatcher()
    await dispatch.dispatch_v3()
    clock[0] += 60_000
    add()
    add(kind)
    await dispatch.dispatch_v3()
    assert len(sender.cards) == 2
    assert sender.cards[-1][0].text == kind
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0


async def test_failed_delivery_does_not_start_touch_cooldown(touches):
    db, clock, sender, dispatcher, add = touches
    dispatch = dispatcher()
    original = sender.send_card

    async def fail(*args, **kwargs):
        raise RuntimeError("temporary failure")

    sender.send_card = fail
    await dispatch.dispatch_v3()
    assert not sender.cards
    sender.send_card = original
    clock[0] += 60_000
    add()
    await dispatch.dispatch_v3()
    assert len(sender.cards) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0


async def test_another_coin_is_not_blocked(touches):
    db, clock, sender, dispatcher, add = touches
    from app.models_alt import AltAsset, AltInstrumentSource
    dispatch = dispatcher()
    await dispatch.dispatch_v3()
    row = db.conn.execute("SELECT * FROM alt_v3_event WHERE event_type='retest' LIMIT 1").fetchone()
    asset = db.upsert_alt_asset(AltAsset(None, 2, "LTC", name="Litecoin"))
    source = db.upsert_alt_instrument_source(AltInstrumentSource(None, asset.id, "binance", "LTCUSDT"))
    # Reuse the fixture's valid base under a different coin/source.
    db.conn.execute(
        "INSERT INTO alt_structure(asset_id,source_id,structure_key,rules_version,state,eligible,"
        "lower,upper,base_start_open_time,available_at_ms,computed_at_ms,payload_json) "
        "SELECT ?,?,structure_key,rules_version,state,eligible,lower,upper,base_start_open_time,"
        "available_at_ms,computed_at_ms,payload_json FROM alt_structure WHERE source_id=?",
        (asset.id, source.id, row["source_id"]))
    event = json.loads(row["payload_json"])
    insert_event(db, asset.id, source.id, row["structure_key"], event)
    await dispatch.dispatch_v3()
    assert len(sender.cards) == 2
