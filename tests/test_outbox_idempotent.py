"""AC20 (спека §9/§18): outbox не отправляет повторное уведомление после
рестарта/replay — ровно одна строка notification_packet на семантический
ключ и одна строка delivery на ключ идемпотентности."""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.engine import Scanner
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
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from tests.conftest import make_candle

T0 = 1_780_000_000_000
H1_MS = 3_600_000


def _instrument(db, symbol="BTCUSDT"):
    iid = db.upsert_instrument(Instrument(
        id=None, asset=symbol.replace("USDT", ""), venue="binance",
        market_type="spot", symbol=symbol, quote_asset="USDT"))
    return iid


def _zone_with_touch(db, iid, n=1):
    """Активная зона + недоставленное событие TOUCH (как из движка)."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=95.0 + n, upper=105.0 + n, formed_at=T0,
        confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE))
    ev_id = db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=T0 + 2000, detected_at=T0 + 2000, price=100.0 + n,
        depth=0.0))
    return db.get_event(ev_id)


def _counts(db):
    return {
        "packets": db.conn.execute(
            "SELECT COUNT(*) FROM notification_packet").fetchone()[0],
        "deliveries": db.conn.execute("SELECT COUNT(*) FROM delivery").fetchone()[0],
        "dup_keys": db.conn.execute(
            "SELECT COUNT(*) FROM (SELECT idempotency_key FROM delivery"
            " GROUP BY idempotency_key HAVING COUNT(*) > 1)").fetchone()[0],
    }


async def test_redispatch_after_restart_sends_nothing_new(tmp_path):
    """Доставленное событие переживает рестарт: повторный прогон (replay)
    и повторная доставка не создают второй карточки/пакета/delivery."""
    path = str(tmp_path / "outbox.db")
    db = Database(path)
    iid = _instrument(db)
    ev = _zone_with_touch(db, iid)
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)
    await disp.dispatch([ev])
    assert len(sender.cards) == 1
    counts = _counts(db)
    assert counts == {"packets": 1, "deliveries": 1, "dup_keys": 0}
    key = ev.idempotency_key("owner")
    db.close()

    # «рестарт»: то же событие читается из журнала и диспетчерится снова
    db = Database(path)
    try:
        ev2 = db.get_event(ev.id)
        sender2 = LogSender()
        disp2 = EventDispatcher(db, DetectorConfig(), sender2)
        await disp2.dispatch([ev2])
        await disp2.retry_pending()
        assert not sender2.cards
        assert _counts(db) == counts
        rows = db.conn.execute(
            "SELECT status FROM delivery WHERE idempotency_key=?", (key,)
        ).fetchall()
        assert len(rows) == 1
    finally:
        db.close()


async def test_restart_between_event_insert_and_flush(tmp_path):
    """Крах между записью события и отправкой: после рестарта событие
    доставляется ровно один раз (delivery pending подхватывается)."""
    path = str(tmp_path / "outbox_crash.db")
    db = Database(path)
    iid = _instrument(db)
    ev = _zone_with_touch(db, iid)
    # крах ДО диспетча: событие в журнале, delivery/packet ещё нет
    db.close()

    db = Database(path)
    try:
        sender = LogSender()
        disp = EventDispatcher(db, DetectorConfig(), sender)
        await disp.retry_pending()  # подхват недоставленного после рестарта
        await disp.dispatch([db.get_event(ev.id)])
        assert len(sender.cards) == 1
        counts = _counts(db)
        assert counts["deliveries"] == 1 and counts["dup_keys"] == 0
        # повторный прогон после «ещё одного рестарта» — тишина
        db.conn.commit()
    finally:
        db.close()
    db = Database(path)
    try:
        sender2 = LogSender()
        disp2 = EventDispatcher(db, DetectorConfig(), sender2)
        await disp2.dispatch([db.get_event(ev.id)])
        await disp2.retry_pending()
        assert not sender2.cards
        assert _counts(db) == counts
    finally:
        db.close()


async def test_engine_replay_twice_keeps_outbox_stable(db, instrument_id, monkeypatch):
    """Движок: повторный replay не создаёт дублей событий (UNIQUE в event),
    повторная доставка — дублей пакетов (idempotency_key/semantic_key)."""
    iid = instrument_id
    # часы движка — через 60 с после границы последней свечи: события live
    # (не delayed), доставка реальная
    opening = now_ms() - (now_ms() % H1_MS)
    t_end = opening + 60_000
    monkeypatch.setattr("app.engine.scanner.now_ms", lambda: t_end)
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=95.0, upper=105.0, formed_at=t_end - 50 * H1_MS,
        confirmed_at=t_end - 49 * H1_MS, status=ZoneStatus.ACTIVE))
    # серия H1 выше зоны; последняя закрытая свеча заходит в зону (TOUCH,
    # depth 50%) — occurred_at = граница её закрытия (60 с назад)
    bars = []
    price = 110.0
    for i in range(20):
        ot = opening - (20 - i) * H1_MS
        o = price
        h, l, c = o + 0.5, o - 0.5, o
        if i == 19:
            h, l, c = 110.5, 100.0, 101.0  # касание зоны [95, 105]
        bars.append(make_candle(ot, o, h, l, c, "H1", iid, source="test"))
        price = c
    db.insert_candles(bars)
    scanner = Scanner(db, DetectorConfig())
    sender = LogSender()
    disp = EventDispatcher(db, DetectorConfig(), sender)

    first = scanner.replay_instrument(iid, None)
    events = db.get_events(zone_id=zid)
    assert events and all(not e.delayed for e in events)
    await disp.dispatch(first)
    assert _counts(db)["dup_keys"] == 0
    assert _counts(db)["deliveries"] >= 1
    # quiet-события уходят одной сводкой после дедлайна (дайджест — тоже пакет)
    _due(db)
    await disp.outbox.flush()
    cards1 = len(sender.cards)
    assert cards1 == 1
    counts1 = _counts(db)  # установившееся состояние после первой доставки

    # повторный replay (рестарт/восстановление) — журнал стабилен;
    # повторная доставка тех же фактов и retry — ни строк, ни карточек
    second = scanner.replay_instrument(iid, None)
    assert [(e.zone_id, e.kind, e.occurred_at) for e in db.get_events(zone_id=zid)
            ] == [(e.zone_id, e.kind, e.occurred_at) for e in events]
    await disp.dispatch(second)
    await disp.dispatch([db.get_event(e.id) for e in events])
    await disp.retry_pending()
    _due(db)
    await disp.outbox.flush()
    assert len(sender.cards) == cards1
    assert _counts(db) == counts1


def _due(db):
    db.conn.execute(
        "UPDATE notification_packet SET due_at=0 WHERE status IN ('pending','failed')")
    db.conn.commit()
