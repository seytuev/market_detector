"""Приёмочные сценарии §13 спеки: №3 (PRB), №8 (FVG 50%/filled), №12 (перезапуск),
№13 (восстановленные события), №14 (содержание сообщения), №18 (тишина без события).
"""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.scanner import Scanner
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.queue import EventDispatcher, EventView, MessagePayload
from app.notify.telegram import LogSender, render_text

from .conftest import load_etalon_candles, make_candle

H1_MS = TIMEFRAME_MINUTES["H1"] * 60_000
H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
T0 = 1780272000000  # 2026-06-01 00:00 UTC


def _second_instrument(db: Database) -> int:
    return db.upsert_instrument(Instrument(
        id=None, asset="ETH", venue="binance", market_type="spot",
        symbol="ETHUSDT", quote_asset="USDT",
    ))


# ------------------------------------------------------------------
# №3: H1-PRB с собственным FVG и связью parent_ob_id (§5)
# ------------------------------------------------------------------

def _h4_ob_candles(iid: int):
    """Медвежий OB на H4: бычья база [100,110], импульс с FVG [89.5,95]."""
    return [
        make_candle(T0 + 0 * H4_MS, 101.0, 110.0, 100.0, 109.0, "H4", iid),
        make_candle(T0 + 1 * H4_MS, 108.0, 108.5, 95.0, 96.0, "H4", iid),
        make_candle(T0 + 2 * H4_MS, 96.0, 97.0, 88.0, 89.0, "H4", iid),
        make_candle(T0 + 3 * H4_MS, 89.0, 89.5, 80.0, 81.0, "H4", iid),
    ]


def _h1_prb_candles(iid: int, start: int, with_fvg: bool = True):
    """H1-серия ниже HTF-OB: бычья база [70,72] и (опционально) FVG вниз."""
    third_high = 61.5 if with_fvg else 64.5
    return [
        make_candle(start + 0 * H1_MS, 71.0, 72.0, 70.0, 71.5, "H1", iid),
        make_candle(start + 1 * H1_MS, 71.0, 71.2, 64.0, 65.0, "H1", iid),
        make_candle(start + 2 * H1_MS, 65.0, 66.0, 60.0, 61.0, "H1", iid),
        make_candle(start + 3 * H1_MS, 61.0, third_high, 55.0, 56.0, "H1", iid),
    ]


def test_n3_h1_prb_with_own_fvg_and_parent(db, cfg, instrument_id):
    scanner = Scanner(db, cfg)
    for c in _h4_ob_candles(instrument_id):
        scanner.on_closed_candle(c)
    ob = db.get_zones(instrument_id, types=[ZoneType.OB])[0]
    assert ob.direction == Direction.BEAR and ob.confirmed_at is not None

    for c in _h1_prb_candles(instrument_id, T0 + 4 * H4_MS, with_fvg=True):
        scanner.on_closed_candle(c)

    prbs = db.get_zones(instrument_id, types=[ZoneType.PRB])
    assert len(prbs) == 1
    prb = prbs[0]
    assert prb.timeframe == "H1"
    assert prb.confirmed_at is not None  # собственный H1-FVG подтвердил (§5)
    rel = db.get_relation(prb.id)
    assert rel is not None and rel.parent_ob_id == ob.id
    assert prb.evidence["source_timeframe"] == "H1"

    # исходный OB не заменён и не изменён (§5)
    obs = db.get_zones(instrument_id, types=[ZoneType.OB])
    assert len(obs) == 1
    assert (obs[0].lower, obs[0].upper) == (100.0, 110.0)
    assert obs[0].status == ZoneStatus.CANDIDATE


def test_n3_prb_without_own_fvg_not_created(db, cfg, instrument_id):
    iid = _second_instrument(db)
    scanner = Scanner(db, cfg)
    for c in _h4_ob_candles(iid):
        scanner.on_closed_candle(c)
    for c in _h1_prb_candles(iid, T0 + 4 * H4_MS, with_fvg=False):
        scanner.on_closed_candle(c)
    # собственный подтверждающий FVG обязателен (§5) — PRB не создан
    assert db.get_zones(iid, types=[ZoneType.PRB]) == []


# ------------------------------------------------------------------
# №8: FVG после 50% ослаблен, но отслеживается; filled и касание OB — раздельно
# ------------------------------------------------------------------

def _bull_fvg_candles(iid: int):
    """Бычий FVG [101.0, 101.5] на H4."""
    return [
        make_candle(T0 + 0 * H4_MS, 100.0, 101.0, 99.0, 100.5, "H4", iid),
        make_candle(T0 + 1 * H4_MS, 100.5, 103.0, 100.0, 102.5, "H4", iid),
        make_candle(T0 + 2 * H4_MS, 102.5, 104.0, 101.5, 103.5, "H4", iid),
    ]


def test_n8_fvg_weakened_keeps_tracking_until_filled(db, cfg, instrument_id):
    scanner = Scanner(db, cfg)
    for c in _bull_fvg_candles(instrument_id):
        scanner.on_closed_candle(c)
    fvg = db.get_zones(instrument_id, types=[ZoneType.FVG])[0]
    t = T0 + 10 * H4_MS

    ev = scanner.on_price(instrument_id, 101.5, t + 1)
    assert [e.kind for e in ev] == [EventKind.TOUCH]

    assert scanner.on_price(instrument_id, 101.3, t + 2) == []  # глубина 0.4 — молчим
    ev = scanner.on_price(instrument_id, 101.2, t + 3)  # глубина 0.6 → 50%
    assert [e.kind for e in ev] == [EventKind.FVG_WEAKENED]
    assert db.get_zone(fvg.id).status == ZoneStatus.WEAKENED

    # ослабленный FVG продолжает отслеживаться до полного перекрытия (§3)
    ev = scanner.on_price(instrument_id, 100.9, t + 4)
    assert [e.kind for e in ev] == [EventKind.FVG_FILLED]
    assert db.get_zone(fvg.id).status == ZoneStatus.ARCHIVED

    kinds = [e.kind for e in db.get_events(fvg.id)]
    assert sorted(kinds, key=str) == sorted(
        [EventKind.TOUCH, EventKind.FVG_WEAKENED, EventKind.FVG_FILLED], key=str
    )


def _bull_ob_with_fvg_candles(iid: int):
    """Бычий OB [100,106] с подтверждающим FVG [110,110.5] выше базы."""
    return [
        make_candle(T0 + 0 * H4_MS, 105.0, 106.0, 100.0, 101.0, "H4", iid),
        make_candle(T0 + 1 * H4_MS, 101.0, 110.0, 101.0, 109.0, "H4", iid),
        make_candle(T0 + 2 * H4_MS, 109.0, 111.0, 105.0, 110.0, "H4", iid),
        make_candle(T0 + 3 * H4_MS, 110.0, 113.0, 110.5, 112.0, "H4", iid),
    ]


def test_n8_filled_and_ob_touch_are_independent_events(db, cfg, instrument_id):
    """Заполнение FVG совпало с касанием связанного OB: два отдельных события,
    касание OB — по его реальным границам, а не из факта filled (§3)."""
    scanner = Scanner(db, cfg)
    for c in _bull_ob_with_fvg_candles(instrument_id):
        scanner.on_closed_candle(c)
    fvg = db.get_zones(instrument_id, types=[ZoneType.FVG])[0]
    ob = db.get_zones(instrument_id, types=[ZoneType.OB])[0]
    assert (fvg.lower, fvg.upper) == (110.0, 110.5)
    assert (ob.lower, ob.upper) == (100.0, 106.0)
    rel = db.get_relation(ob.id)
    assert rel is not None and rel.confirming_fvg_id == fvg.id
    t = T0 + 10 * H4_MS

    # возврат сверху: сначала 50% FVG (110.2 → глубина 0.6)
    ev = scanner.on_price(instrument_id, 110.2, t + 1)
    assert [e.kind for e in ev] == [EventKind.FVG_WEAKENED]
    # OB ещё не коснутся (OB_CONFIRMED — структурное событие сетапа, §7 ТЗ
    # 06.10.2026: эмитится и при подтверждении в момент создания)
    assert [e for e in db.get_events(ob.id)
            if e.kind != EventKind.OB_CONFIRMED] == []

    # цена проходит FVG насквозь и касается OB по его границе — одним тиком
    ev = scanner.on_price(instrument_id, 105.5, t + 2)
    by_zone = {}
    for e in ev:
        by_zone.setdefault(e.zone_id, []).append(e.kind)
    assert by_zone[fvg.id] == [EventKind.FVG_FILLED]
    assert by_zone[ob.id] == [EventKind.TOUCH]  # самостоятельная геометрия OB
    ob_touch = [e for e in db.get_events(ob.id) if e.kind == EventKind.TOUCH][0]
    assert ob_touch.price == 105.5 and ob_touch.occurred_at == t + 2


# ------------------------------------------------------------------
# №12: перезапуск процесса — зоны, события, доставки и подавление сохраняются
# ------------------------------------------------------------------

async def test_n12_restart_preserves_state(tmp_path, cfg):
    path = str(tmp_path / "htf.db")
    db1 = Database(path)
    iid = db1.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    db1.insert_candles(load_etalon_candles(iid))
    events1 = Scanner(db1, cfg).replay_instrument(iid)
    assert events1, "первый replay должен создать события"
    dispatcher1 = EventDispatcher(db1, cfg, LogSender())
    deliveries1 = await dispatcher1.dispatch(events1)
    assert deliveries1, "события должны быть доставлены"

    n_zones = len(db1.get_zones(iid))
    n_events = len(db1.get_events(limit=10000))
    n_deliveries = db1.conn.execute("SELECT COUNT(*) c FROM delivery").fetchone()["c"]
    # сроки подавления зафиксированы (§8)
    approach = next(e for e in events1 if e.kind == EventKind.APPROACH)
    st = db1.get_alert_state(approach.zone_id, approach.cycle_id, "approach")
    assert st is not None and st.last_delivered_at > 0
    db1.close()

    # «перезапуск процесса»: новый Database и новые Scanner/Dispatcher на том же файле
    db2 = Database(path)
    events2 = Scanner(db2, cfg).replay_instrument(iid)
    assert events2 == []  # повторный replay — ноль новых событий
    assert len(db2.get_zones(iid)) == n_zones
    assert len(db2.get_events(limit=10000)) == n_events

    dispatcher2 = EventDispatcher(db2, cfg, LogSender())
    assert await dispatcher2.dispatch(events2) == []
    # повторный dispatch уже доставленных событий — ни одной новой доставки
    assert await dispatcher2.dispatch(events1) == []
    n_deliveries2 = db2.conn.execute("SELECT COUNT(*) c FROM delivery").fetchone()["c"]
    assert n_deliveries2 == n_deliveries

    # сроки подавления пережили перезапуск (§8/§13.12)
    st2 = db2.get_alert_state(approach.zone_id, approach.cycle_id, "approach")
    assert st2 is not None and st2.last_delivered_at == st.last_delivered_at
    db2.close()


# ------------------------------------------------------------------
# №13: восстановленные события — исходное occurred_at, delayed, пометка в тексте
# ------------------------------------------------------------------

def _sample_view(db: Database, iid: int, delayed: bool = False) -> EventView:
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="W1", lower=65000.0, upper=66000.0, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    ))
    zone = db.get_zone(zid)
    now = now_ms()
    ev = Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=now - 3_600_000 * 5 if delayed else now,
        detected_at=now, price=65900.0, depth=0.02,
        delayed=delayed,
    )
    ev.id = db.insert_event(ev)
    ins = db.get_instrument(iid)
    return EventView(event=ev, zone=zone, instrument=ins)


def test_n13_recovered_events_have_original_time_and_delay(db, cfg, instrument_id):
    db.insert_candles(load_etalon_candles(instrument_id))
    events = Scanner(db, cfg).replay_instrument(instrument_id)
    assert events, "ожидались исторические события"
    first_open = 1780272000000
    last_boundary = 1780689600000 + H4_MS
    for e in events:
        # §11: исходное рыночное время, а не время обнаружения; пометка задержки
        assert first_open <= e.occurred_at <= last_boundary
        assert e.delayed is True
        assert e.detected_at > e.occurred_at


def test_n13_render_text_marks_delayed_event(db, instrument_id):
    view = _sample_view(db, instrument_id, delayed=True)
    text = render_text(MessagePayload(events=[view.event], zones=[view.zone], views=[view]))
    assert "задержкой" in text
    assert "исходное время" in text and "обнаружено" in text
    # исходное время события, а не подмена текущим (§11); формат МСК (§6 ТЗ 07.10.2026)
    assert "МСК" in text and "UTC" not in text


# ------------------------------------------------------------------
# №14: содержание сообщения (§9)
# ------------------------------------------------------------------

def test_n14_render_text_message_content(db, instrument_id):
    view = _sample_view(db, instrument_id, delayed=False)
    text = render_text(MessagePayload(events=[view.event], zones=[view.zone], views=[view]))
    assert "BTCUSDT" in text                              # точный символ (§5.1 ТЗ 07.10.2026)
    # §5.1: строки «Источник» нет; площадка/рынок — только в строке бренда
    # первой строкой (ребрендинг LevelFrame §8)
    assert "Источник" not in text
    assert text.splitlines()[0] == "LevelFrame · BTCUSDT · binance spot"
    assert "TradingView" not in text
    assert "W1" in text                                   # таймфрейм
    assert "Orderblock" in text and "бычий" in text       # тип и направление
    assert "65 000,00" in text and "66 000,00" in text    # границы (ru-формат)
    assert "65 500,00" in text                            # середина
    assert "65 900,00" in text                            # цена события
    assert "касание ближайшей границы" in text            # причина
    assert "МСК" in text and "UTC" not in text            # время МСК (§6)
    assert "активна" in text                              # статус


# ------------------------------------------------------------------
# №18: без нового события — тишина; ежедневных напоминаний нет
# ------------------------------------------------------------------

async def test_n18_silence_without_new_event(db, cfg, instrument_id):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="H4", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE,
    ))
    scanner = Scanner(db, cfg)
    sender = LogSender()
    dispatcher = EventDispatcher(db, cfg, sender)
    t = T0 + H4_MS

    touch = scanner.on_price(instrument_id, 109.0, t)
    assert [e.kind for e in touch] == [EventKind.TOUCH]
    deliveries = await dispatcher.dispatch(touch)
    assert len(deliveries) == 1 and len(sender.sent) == 1
    n_events = len(db.get_events(zid))

    # цена постоянно внутри зоны, в том числе «на следующий день» — без новых
    # порогов Scanner не порождает событий вообще (§8/§13.18)
    for dt in (3_600_000, 12 * 3_600_000, 25 * 3_600_000, 49 * 3_600_000):
        assert scanner.on_price(instrument_id, 108.5, t + dt) == []
    assert len(db.get_events(zid)) == n_events

    # ни пустой dispatch, ни повтор старого события не создают новых delivery;
    # непрочитанный сигнал не порождает ежедневных повторов
    assert await dispatcher.dispatch([]) == []
    assert await dispatcher.dispatch(touch) == []
    n_deliveries = db.conn.execute("SELECT COUNT(*) c FROM delivery").fetchone()["c"]
    assert n_deliveries == 1
    assert len(sender.sent) == 1


# ------------------------------------------------------------------
# кэш get_zones: повторные выборки без лишней десериализации,
# инвалидация любой записью в zone (кнопка ревью ждала полный replay)
# ------------------------------------------------------------------

def test_zone_cache_consistent_and_invalidated_on_write(db, instrument_id):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE,
    ))
    first = db.get_zones(instrument_id)
    assert db.get_zones(instrument_id) == first  # повтор — тот же результат
    assert db.get_zones(instrument_id) is not first  # но не тот же список

    zid2 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BEAR, timeframe="D1", lower=200.0, upper=210.0,
        formed_at=T0 + 1, confirmed_at=None, status=ZoneStatus.CANDIDATE,
    ))
    after_insert = db.get_zones(instrument_id)
    assert {z.id for z in after_insert} == {zid, zid2}

    db.update_zone(zid, status=ZoneStatus.REJECTED)
    active = db.get_zones(instrument_id, statuses=[ZoneStatus.ACTIVE])
    assert [z.id for z in active] == []  # апдейт виден сразу, без протухшего кэша
