"""T03–T07, T12, T20 (ТЗ 06.10.2026 §3.2, §3.3, §6, §10).

Первое закрытие за границей OB ищется ретросканированием от момента
наблюдаемости зоны, display_until — граница закрытия первой пробойной
свечи; поздние свечи первое событие не подменяют; candidate с recorded
close_beyond получает согласованный lifecycle и не попадает в рабочие
сигналы; строгое сравнение (тень и Close=границе — не пробой); Breaker
якорится на первую пробойную свечу, breaker_forbidden не держит OB active.

Синтетика: «тихие» свечи целиком за ближней границей (без визитов), тела
подобраны так, чтобы не создавать трёхсвечных FVG, кроме явно заданных.
"""
import pytest

from app.engine.scanner import Scanner
from app.models import (
    Direction,
    EventKind,
    TIMEFRAME_MINUTES,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
)
from tests.conftest import make_candle

W1_MS = TIMEFRAME_MINUTES["W1"] * 60_000
T0 = 1_730_000_000_000


def _w1(i: int, o: float, h: float, l: float, c: float):
    return make_candle(T0 + i * W1_MS, o, h, l, c, timeframe="W1")


def _quiet_bull(i: int):
    """Тихая свеча целиком выше bull-зоны [100,110] — без визитов и FVG."""
    return _w1(i, 112, 115, 111, 113)


def _ob(direction=Direction.BULL, lower=100.0, upper=110.0, confirmed_w=2,
        status=ZoneStatus.ACTIVE):
    confirmed_at = None if confirmed_w is None else T0 + confirmed_w * W1_MS
    return Zone(
        id=None, instrument_id=1, type=ZoneType.OB, direction=direction,
        timeframe="W1", lower=lower, upper=upper, formed_at=T0,
        confirmed_at=confirmed_at, status=status, source="auto",
        source_candles=[T0], evidence={}, created_at=T0,
    )


def _replay(db, cfg, iid, candles):
    db.insert_candles(candles)
    Scanner(db, cfg).replay_instrument(iid, timeframes={"W1"})


def test_first_break_candle_sets_display_until(db, cfg, instrument_id):
    """T03/T04: пробой неделей 5 — display_until = граница закрытия недели 5,
    а не свечи детекции (кейс №547/№286)."""
    zid = db.insert_zone(_ob())
    candles = [_quiet_bull(i) for i in range(5)]
    candles.append(_w1(5, 104, 112, 90, 95))        # Close 95 < L=100 — пробой
    candles += [_w1(i, 96, 112, 92, 94) for i in range(6, 10)]
    _replay(db, cfg, instrument_id, candles)

    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.display_until == close_boundary_ms(T0 + 5 * W1_MS, "W1")
    assert z.end_reason == "close_beyond (ТЗ §3)"
    assert z.evidence["first_invalidating_candle_open_time"] == T0 + 5 * W1_MS
    inv = [e for e in db.get_events(zid) if e.kind == EventKind.OB_INVALIDATED]
    assert len(inv) == 1
    assert inv[0].occurred_at == z.display_until
    assert inv[0].evidence["close"] == 95
    assert inv[0].evidence["boundary_compared"] == 100.0


def test_positive_control_bear_ob(db, cfg, instrument_id):
    """T05 (контрольный №548): bear [215,247]; неделя с Close 252.42 > 247 —
    её граница закрытия и есть display_until, без лишнего сдвига."""
    zid = db.insert_zone(_ob(direction=Direction.BEAR, lower=215.0, upper=247.0))
    candles = [_w1(i, 210, 214, 205, 208) for i in range(5)]  # ниже L=215
    candles.append(_w1(5, 240, 255, 238, 252.42))   # Close > U=247
    _replay(db, cfg, instrument_id, candles)

    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.display_until == close_boundary_ms(T0 + 5 * W1_MS, "W1")


def test_late_candles_do_not_move_first_break(db, cfg, instrument_id):
    """T06: добавление новых свечей не заменяет первое событие пробоя;
    возврат цены внутрь не воскрешает OB."""
    zid = db.insert_zone(_ob())
    candles = [_quiet_bull(i) for i in range(5)]
    candles.append(_w1(5, 104, 112, 90, 95))
    _replay(db, cfg, instrument_id, candles)
    first_until = db.get_zone(zid).display_until

    more = [_w1(6, 95, 112, 88, 90),               # ещё один пробой позже
            _w1(7, 100, 109, 96, 107)]            # возврат внутрь зоны
    _replay(db, cfg, instrument_id, more)

    z = db.get_zone(zid)
    assert z.display_until == first_until
    assert z.market_validity == "invalid"
    assert z.entry_eligible is False
    inv = [e for e in db.get_events(zid) if e.kind == EventKind.OB_INVALIDATED]
    assert len(inv) == 1  # дубликатов события нет


def test_wick_and_equal_close_are_not_breaks(db, cfg, instrument_id):
    """T12: тень за границей и Close ровно на границе — не пробой; строгое
    закрытие за границей — пробой."""
    zid = db.insert_zone(_ob())
    candles = [_quiet_bull(i) for i in range(5)]
    candles.append(_w1(5, 112, 113, 90, 105))      # тень 90 < L, Close внутри
    candles.append(_w1(6, 105, 112, 99, 100))      # Close ровно на L
    _replay(db, cfg, instrument_id, candles)
    z = db.get_zone(zid)
    assert z.market_validity == "active"
    assert z.display_until is None

    _replay(db, cfg, instrument_id, [_w1(7, 100, 101, 95, 99.99)])
    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.display_until == close_boundary_ms(T0 + 7 * W1_MS, "W1")


def test_candidate_with_close_beyond_consistent_lifecycle(db, cfg, instrument_id):
    """T07: candidate + recorded close_beyond — согласованный lifecycle:
    рыночно невалиден, вне очереди проверки, invalidated-доказательства есть."""
    zid = db.insert_zone(_ob(status=ZoneStatus.CANDIDATE))
    candles = [_quiet_bull(i) for i in range(5)]
    candles.append(_w1(5, 104, 112, 90, 95))
    _replay(db, cfg, instrument_id, candles)

    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.display_until is not None
    assert z.evidence.get("invalidated_at") is not None
    assert all(c.id != zid for c in db.get_unreviewed_candidates())
    assert db.count_candidate_zones().get(instrument_id, 0) == 0


def test_base_destroyed_before_confirmation(db, cfg, instrument_id):
    """T06/§6: кандидат без confirmed_at, уничтоженный после конца базы,
    инвалидируется из истории раннего кандидата — поздний FVG его не
    воскресит (подтверждение пропускается для market_validity=invalid)."""
    zid = db.insert_zone(_ob(confirmed_w=None, status=ZoneStatus.CANDIDATE))
    # source_candles=[T0] → конец базы T0+W1; пробой неделей 3
    candles = [_quiet_bull(i) for i in range(3)]
    candles.append(_w1(3, 104, 112, 90, 95))
    _replay(db, cfg, instrument_id, candles)

    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.display_until == close_boundary_ms(T0 + 3 * W1_MS, "W1")
    assert z.confirmed_at is None


def test_breaker_forbidden_archives_ob(db, cfg, instrument_id):
    """T20: прежний тест >50% запрещает Breaker — пробитый OB уходит в
    архив (close_beyond_no_breaker), а не остаётся active/candidate."""
    zid = db.insert_zone(_ob())
    candles = [_quiet_bull(i) for i in range(3)]
    # отдельный тест глубже 50%: заход к 103.9 (глубина 61%) и возврат
    candles.append(_w1(3, 112, 113, 103.9, 104.5))
    candles.append(_w1(4, 111, 113, 110.5, 112))    # целиком выше U — return
    candles.append(_quiet_bull(5))
    candles.append(_w1(6, 104, 112, 90, 95))        # пробой
    _replay(db, cfg, instrument_id, candles)

    z = db.get_zone(zid)
    assert z.market_validity == "invalid"
    assert z.breaker_forbidden is True
    assert z.status == ZoneStatus.ARCHIVED
    assert z.end_reason == "close_beyond_no_breaker (§15.6)"
    assert db.get_zones(instrument_id, types=[ZoneType.BREAKER]) == []


def test_breaker_anchored_at_first_breakout(db, cfg, instrument_id):
    """T20: конверсия использует историю до первого пробоя — Breaker
    создаётся от окна первой пробойной свечи, activate_at не раньше
    границы её закрытия."""
    zid = db.insert_zone(_ob(direction=Direction.BEAR, lower=100.0, upper=110.0))
    candles = [_w1(i, 95, 99, 91, 93) for i in range(3)]  # ниже L=100
    candles.append(_w1(3, 108, 112, 106, 111))     # Close 111 > U — пробой
    # новый FVG пробойного движения (bull): High1 < Low3
    candles.append(_w1(4, 111, 113, 110.5, 112))
    candles.append(_w1(5, 112, 114, 111.5, 113))
    candles.append(_w1(6, 113, 120, 115, 119))
    _replay(db, cfg, instrument_id, candles)

    ob = db.get_zone(zid)
    breakers = db.get_zones(instrument_id, types=[ZoneType.BREAKER])
    assert ob.market_validity == "invalid"
    assert ob.evidence.get("invalidated_at") == close_boundary_ms(T0 + 3 * W1_MS, "W1")
    assert len(breakers) == 1
    assert breakers[0].confirmed_at >= close_boundary_ms(T0 + 3 * W1_MS, "W1")
    assert ob.status == ZoneStatus.CONVERTED
