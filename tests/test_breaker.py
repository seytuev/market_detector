"""§6 + §15.6/§9.5–6: Breaker — закрытие за дальней границей + НОВЫЙ FVG
пробойного движения; предыдущий отдельный тест >50% исключает преобразование
навсегда (ровно 50% допустимо); глубины пробойного движения — не тесты;
пробитый Breaker архивируется без возврата; PRB архивируется без Breaker
(§13.9, §13.10, §13.16, приёмка §15.6)."""
from __future__ import annotations

from app.engine.scanner import Scanner
from app.models import (
    Direction,
    EventKind,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneStatus,
    ZoneType,
)

from .conftest import make_candle

H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
T0 = 1780272000000


def _insert_block(db, instrument_id, ztype, direction, status=ZoneStatus.ACTIVE):
    z = Zone(
        id=None, instrument_id=instrument_id, type=ztype, direction=direction,
        timeframe="H4", lower=100.0, upper=110.0, formed_at=T0,
        confirmed_at=T0, status=status,
    )
    zid = db.insert_zone(z)
    return db.get_zone(zid)


def _candle(idx, close, high=None, low=None, open_=None):
    return make_candle(
        T0 + (idx + 1) * H4_MS, open_ if open_ is not None else 105.0,
        high if high is not None else close + 1,
        low if low is not None else close - 1,
        close, "H4",
    )


def _feed_breakout_fvg(scanner):
    """Свечи idx2..4 образуют бычий FVG пробойного движения [112.5, 114.0],
    подтверждённый на закрытии idx4 — позже пробойного закрытия (§15.6)."""
    scanner.on_closed_candle(_candle(2, close=112.0, high=112.5, low=111.0, open_=111.5))
    scanner.on_closed_candle(_candle(3, close=117.0, high=118.0, low=111.8, open_=112.0))
    scanner.on_closed_candle(_candle(4, close=119.0, high=120.0, low=114.0, open_=117.0))


def test_shadow_does_not_create_breaker(db, cfg, instrument_id):
    """Выход тенью из OB недостаточен — только закрытие (§6, §13.9)."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    events = scanner.on_closed_candle(_candle(0, close=109.0, high=115.0))
    assert [e for e in events if e.kind == EventKind.BREAKER_CREATED] == []
    assert db.get_zone(ob.id).status == ZoneStatus.ACTIVE
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []


def test_close_beyond_without_new_fvg_no_breaker(db, cfg, instrument_id):
    """§9.6: закрытие за границей без нового FVG пробоя — Breaker нет,
    OB помечается ожиданием (breakout_close_at)."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_closed_candle(_candle(1, close=111.0))
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []
    updated = db.get_zone(ob.id)
    assert updated.status == ZoneStatus.ACTIVE  # не CONVERTED
    assert updated.breakout_close_at is not None


def test_breaker_created_with_new_fvg_confirmed_later(db, cfg, instrument_id):
    """§15.6: FVG пробоя подтверждается ПОЗЖЕ пробойного закрытия — Breaker
    активируется в момент подтверждения FVG (max двух условий)."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_closed_candle(_candle(1, close=111.0))  # пробойное закрытие, ждём FVG
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []
    _feed_breakout_fvg(scanner)  # FVG [112.5, 114.0], подтверждён на idx4

    breakers = db.get_zones(instrument_id, types=[ZoneType.BREAKER])
    assert len(breakers) == 1
    b = breakers[0]
    assert b.direction == Direction.BULL
    assert (b.lower, b.upper) == (100.0, 110.0)  # прежние границы
    assert b.status == ZoneStatus.ACTIVE
    assert b.cycle_id == ob.cycle_id + 1
    fvg_confirmed = T0 + 6 * H4_MS  # закрытие idx4 (3-я свеча FVG)
    assert b.confirmed_at == fvg_confirmed  # позже пробойного закрытия (T0+3*H4)
    assert b.evidence.get("breakout_fvg_range") == [112.5, 114.0]
    rel = db.get_relation(b.id)
    assert rel is not None and rel.predecessor_ob_id == ob.id
    ob_after = db.get_zone(ob.id)
    assert ob_after.status == ZoneStatus.CONVERTED
    # §15.1.4: сегмент исходного OB заканчивается конверсией
    assert ob_after.display_until == fvg_confirmed
    assert ob_after.end_reason == "converted_to_breaker (§6/§15.6)"


def test_previous_test_over_50_forbids_breaker_forever(db, cfg, instrument_id):
    """§9.5: предыдущий отдельный тест >50% навсегда исключает Breaker."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    # отдельный тест: вход на 70% глубины и возврат обратно
    scanner.on_price(instrument_id, 107.0, T0 + 10 * H4_MS)
    scanner.on_price(instrument_id, 95.0, T0 + 11 * H4_MS)
    # пробой с новым FVG — Breaker всё равно запрещён
    scanner.on_closed_candle(_candle(12, close=111.0))
    _feed_breakout_fvg_late(scanner)
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []
    assert db.get_zone(ob.id).breaker_forbidden is True


def test_previous_test_exactly_50_allowed(db, cfg, instrument_id):
    """§9.5: ровно 50% допустимо — Breaker создаётся."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 105.0, T0 + 10 * H4_MS)  # ровно 50%
    scanner.on_price(instrument_id, 95.0, T0 + 11 * H4_MS)   # возврат
    scanner.on_closed_candle(_candle(12, close=111.0))
    _feed_breakout_fvg_late(scanner)
    assert len(db.get_zones(instrument_id, types=[ZoneType.BREAKER])) == 1


def test_90_within_same_move_allowed(db, cfg, instrument_id):
    """§15.6: 90% в том же пробойном движении — не предыдущий тест.
    ТЗ §3: глубина 90%+ НЕ завершает OB (WORKED больше нет) — зона остаётся
    ACTIVE, заход продолжается и не считается закрытым отдельным тестом."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 109.9, T0 + 10 * H4_MS)  # 99% — тот же заход
    after = db.get_zone(ob.id)
    assert after.status == ZoneStatus.ACTIVE           # ТЗ §3: OB не worked по 90%
    assert after.entry_eligible is False               # но для нового входа недоступен
    scanner.on_closed_candle(_candle(12, close=111.0))
    _feed_breakout_fvg_late(scanner)
    assert len(db.get_zones(instrument_id, types=[ZoneType.BREAKER])) == 1


def test_90_as_separate_previous_test_forbids(db, cfg, instrument_id):
    """§15.6: 90% отдельным предыдущим тестом (после него был новый заход)
    исключает Breaker."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 109.9, T0 + 10 * H4_MS)  # тест 99%
    # возврат на сторону входа (медвежий OB: цена ниже L) — заход закрыт
    # отдельным тестом, а не пробойным движением
    scanner.on_price(instrument_id, 97.0, T0 + 11 * H4_MS)
    scanner.on_closed_candle(_candle(11, close=97.0))
    scanner.on_closed_candle(_candle(13, close=111.0))
    _feed_breakout_fvg_late(scanner, start=14)
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []
    assert db.get_zone(ob.id).breaker_forbidden is True


def _feed_breakout_fvg_late(scanner, start=13):
    """Тот же бычий FVG пробоя [112.5, 114.0], но на свечах start..start+2."""
    scanner.on_closed_candle(_candle(start, close=112.0, high=112.5, low=111.0, open_=111.5))
    scanner.on_closed_candle(_candle(start + 1, close=117.0, high=118.0, low=111.8, open_=112.0))
    scanner.on_closed_candle(_candle(start + 2, close=119.0, high=120.0, low=114.0, open_=117.0))


def test_broken_breaker_archived_no_way_back(db, cfg, instrument_id):
    """Пробитый по закрытию своего ТФ Breaker архивируется; обратного
    превращения в OB нет (§6, §13.16)."""
    ob = _insert_block(db, instrument_id, ZoneType.OB, Direction.BEAR)
    scanner = Scanner(db, cfg)
    scanner.on_closed_candle(_candle(1, close=111.0))
    _feed_breakout_fvg(scanner)  # OB → бычий Breaker
    breaker = db.get_zones(instrument_id, types=[ZoneType.BREAKER])[0]

    # тень ниже L без закрытия — недостаточно
    scanner.on_closed_candle(_candle(5, close=105.0, low=95.0))
    assert db.get_zone(breaker.id).status == ZoneStatus.ACTIVE

    # закрытие бычьего Breaker ниже L → архив, рисунок завершён (§15.1.3)
    events = scanner.on_closed_candle(_candle(6, close=99.0))
    br_after = db.get_zone(breaker.id)
    assert br_after.status == ZoneStatus.ARCHIVED
    assert br_after.display_until == T0 + 8 * H4_MS
    assert any(e.kind == EventKind.BREAKER_ARCHIVED for e in events)

    # дальнейшие закрытия за границами не воскрешают ни OB, ни Breaker
    scanner.on_closed_candle(_candle(7, close=120.0))
    scanner.on_closed_candle(_candle(8, close=90.0))
    assert db.get_zone(ob.id).status == ZoneStatus.CONVERTED
    assert db.get_zone(breaker.id).status == ZoneStatus.ARCHIVED
    assert len(db.get_zones(instrument_id, types=[ZoneType.BREAKER])) == 1


def test_prb_archived_without_breaker(db, cfg, instrument_id):
    """PRB при закрытии за дальней границей архивируется, Breaker не создаётся
    (§5, §13.10)."""
    prb = _insert_block(db, instrument_id, ZoneType.PRB, Direction.BULL)
    scanner = Scanner(db, cfg)
    # бычий PRB, Close < L → архив
    events = scanner.on_closed_candle(_candle(0, close=99.0))
    assert db.get_zone(prb.id).status == ZoneStatus.ARCHIVED
    assert any(e.kind == EventKind.PRB_ARCHIVED for e in events)
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []
