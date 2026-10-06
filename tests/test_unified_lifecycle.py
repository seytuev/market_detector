"""ТЗ «Единый движок HTF/LTF» (22.09.2026), приёмка §11 — ядро lifecycle:
A (одинаковые события на любом ТФ), C/D (порог 90% для повторного входа),
E (пробой только закрытием строго за границей), F (база/импульс — не тесты),
G (нет пересечения — нет теста), O (Breaker >50%), P (ранние тесты кандидата).
"""
from __future__ import annotations

import pytest

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

T0 = 1780272000000
H4_MS = TIMEFRAME_MINUTES["H4"] * 60_000
D1_MS = TIMEFRAME_MINUTES["D1"] * 60_000


def _ob(db, instrument_id, tf="D1", direction=Direction.BULL, lower=100.0,
        upper=110.0, confirmed=True):
    z = Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB, direction=direction,
        timeframe=tf, lower=lower, upper=upper, formed_at=T0,
        confirmed_at=T0 if confirmed else None, status=ZoneStatus.ACTIVE,
    )
    return db.get_zone(db.insert_zone(z))


# ------------------------------------------------------------------
# A: одинаковая последовательность → одинаковые события на любом ТФ
# ------------------------------------------------------------------

def test_a_same_sequence_same_events_any_tf(db, cfg, instrument_id):
    zones = {}
    for tf in ("H1", "D1", "W1"):
        zones[tf] = _ob(db, instrument_id, tf=tf).id
    scanner = Scanner(db, cfg)
    prices = [115.0, 109.0, 104.0, 101.0, 112.0]  # уход, касание, 50%, 90%, возврат
    for i, p in enumerate(prices):
        events = scanner.on_price(instrument_id, p, T0 + (i + 1) * 1000)
    for tf, zid in zones.items():
        kinds = sorted((e.kind for e in db.get_events(zid)), key=str)
        assert kinds == sorted(
            [EventKind.TOUCH, EventKind.DEPTH_50, EventKind.DEPTH_90], key=str
        ), tf
        z = db.get_zone(zid)
        assert z.status == ZoneStatus.ACTIVE            # OB не завершается по 90%
        assert z.market_validity == "active"
        assert z.max_test_depth == pytest.approx(0.9)
        assert z.entry_eligible is False                # ровно 90% — недопустимо


# ------------------------------------------------------------------
# C: 89% — повторный выбор допустим; 90%/95% — нет; OB остаётся актуальным
# ------------------------------------------------------------------

@pytest.mark.parametrize("price,expected_depth,eligible", [
    (101.1, 0.89, True),
    (101.0, 0.90, False),
    (100.5, 0.95, False),
])
def test_c_reuse_threshold_90(db, cfg, instrument_id, price, expected_depth, eligible):
    z = _ob(db, instrument_id)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 115.0, T0 + 1000)   # уход из зоны
    scanner.on_price(instrument_id, price, T0 + 2000)   # тест
    scanner.on_price(instrument_id, 112.0, T0 + 3000)   # возврат
    after = db.get_zone(z.id)
    assert after.max_test_depth == pytest.approx(expected_depth)
    assert after.has_tests is True
    assert after.entry_eligible is eligible
    assert after.market_validity == "active"            # сам OB актуален
    assert after.status == ZoneStatus.ACTIVE


def test_d_late_shallow_test_does_not_restore(db, cfg, instrument_id):
    """После теста 95% более поздний тест 20% не восстанавливает пригодность."""
    z = _ob(db, instrument_id)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 115.0, T0 + 1000)
    scanner.on_price(instrument_id, 100.5, T0 + 2000)   # 95%
    scanner.on_price(instrument_id, 112.0, T0 + 3000)   # возврат
    scanner.on_price(instrument_id, 108.0, T0 + 4000)   # 20%
    scanner.on_price(instrument_id, 112.0, T0 + 5000)   # возврат
    after = db.get_zone(z.id)
    assert after.max_test_depth == pytest.approx(0.95)  # мелкий поздний не стирает глубокий
    assert after.entry_eligible is False
    assert after.market_validity == "active"


# ------------------------------------------------------------------
# E: тень за границей — не пробой; Close строго за — пробой; ровно на — нет
# ------------------------------------------------------------------

def test_e_breakout_only_by_strict_close(db, cfg, instrument_id):
    z = _ob(db, instrument_id)
    scanner = Scanner(db, cfg)
    # тень ниже L без закрытия за ней — актуальность сохраняется
    scanner.on_closed_candle(make_candle(T0 + D1_MS, 105.0, 112.0, 95.0, 105.0))
    assert db.get_zone(z.id).market_validity == "active"
    # закрытие ровно на границе — не пробой
    scanner.on_closed_candle(make_candle(T0 + 2 * D1_MS, 105.0, 107.0, 99.0, 100.0))
    assert db.get_zone(z.id).market_validity == "active"
    # закрытие строго за дальней границей — потеря актуальности
    scanner.on_closed_candle(make_candle(T0 + 3 * D1_MS, 100.0, 101.0, 98.0, 99.9))
    after = db.get_zone(z.id)
    assert after.market_validity == "invalid"
    assert after.entry_eligible is False
    assert after.display_until == T0 + 4 * D1_MS
    assert after.end_reason == "close_beyond (ТЗ §3)"


# ------------------------------------------------------------------
# F: свечи базы и первоначальный проход импульса — не тесты
# ------------------------------------------------------------------

def test_f_impulse_passage_is_not_a_test(db, cfg, instrument_id):
    # кандидат OB (ещё не подтверждён): проход импульса насквозь через диапазон
    z = _ob(db, instrument_id, confirmed=False)
    scanner = Scanner(db, cfg)
    # ТЗ 06.10.2026 §3.1: наблюдения имеют смысл только после завершения базы
    be = T0 + D1_MS
    # проход через диапазон (95→115) одним движением — не тест (фаза forming)
    scanner.on_price(instrument_id, 105.0, be + 1000)
    assert db.get_visits(z.id, 1) == []
    assert db.get_events(z.id) == []
    # наблюдение полностью за ближней границей — фиксация выхода
    scanner.on_price(instrument_id, 115.0, be + 2000)
    assert db.get_visits(z.id, 1) == []
    # самостоятельный возврат — это тест (пусть и silent у кандидата)
    scanner.on_price(instrument_id, 108.0, be + 3000)
    scanner.on_price(instrument_id, 112.0, be + 4000)
    after = db.get_zone(z.id)
    assert after.has_tests is True
    assert after.max_test_depth == pytest.approx(0.2)
    assert db.get_events(z.id) == []  # кандидат: уведомлений нет (ТЗ §4)


def test_g_candle_outside_range_creates_no_test(db, cfg, instrument_id):
    """Свеча полностью вне диапазона не создаёт тест по одной формуле глубины."""
    z = _ob(db, instrument_id)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 115.0, T0 + 1000)   # выход
    # наблюдение полностью за дальней границей без открытого захода
    scanner.on_price(instrument_id, 95.0, T0 + 2000)
    after = db.get_zone(z.id)
    assert after.has_tests is False
    assert after.max_test_depth == 0.0
    assert db.get_visits(z.id, 1) == []
    # допустим только JUMP_THROUGH (факт скачка, §6) — не тест и не глубина
    kinds = {e.kind for e in db.get_events(z.id)}
    assert kinds <= {EventKind.JUMP_THROUGH}


# ------------------------------------------------------------------
# O: Breaker — собственный тест строго >50% невалиден; ровно 50% допустим
# ------------------------------------------------------------------

def _make_breaker(db, instrument_id, cfg):
    """Bear OB [100,110] H4 → бычий Breaker (как в test_breaker.py)."""
    ob = Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="H4", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )
    obid = db.insert_zone(ob)
    scanner = Scanner(db, cfg)

    def candle(idx, close, high=None, low=None, open_=None):
        return make_candle(T0 + (idx + 1) * H4_MS, open_ if open_ is not None else 105.0,
                           high if high is not None else close + 1,
                           low if low is not None else close - 1, close, "H4")

    scanner.on_closed_candle(candle(1, close=111.0))  # пробойное закрытие
    scanner.on_closed_candle(candle(2, close=112.0, high=112.5, low=111.0, open_=111.5))
    scanner.on_closed_candle(candle(3, close=117.0, high=118.0, low=111.8, open_=112.0))
    scanner.on_closed_candle(candle(4, close=119.0, high=120.0, low=114.0, open_=117.0))
    breakers = db.get_zones(instrument_id, types=[ZoneType.BREAKER])
    assert len(breakers) == 1
    return scanner, db.get_zone(obid), breakers[0]


def test_o_breaker_own_test_over_50_invalidates(db, cfg, instrument_id):
    scanner, ob, br = _make_breaker(db, instrument_id, cfg)
    # бычий Breaker [100,110]: тест глубже 50% (104.9) — невалиден
    events = scanner.on_price(instrument_id, 104.9, T0 + 10 * H4_MS)
    after = db.get_zone(br.id)
    assert after.status == ZoneStatus.ARCHIVED
    assert after.end_reason == "breaker_test_gt50 (ТЗ §6)"
    assert any(e.kind == EventKind.BREAKER_ARCHIVED for e in events)


def test_o_breaker_own_test_exactly_50_allowed(db, cfg, instrument_id):
    scanner, ob, br = _make_breaker(db, instrument_id, cfg)
    scanner.on_price(instrument_id, 105.0, T0 + 10 * H4_MS)  # ровно 50%
    assert db.get_zone(br.id).status == ZoneStatus.ACTIVE


# ------------------------------------------------------------------
# P: ранний тест до подтверждения FVG сохраняется после подтверждения
# ------------------------------------------------------------------

@pytest.mark.parametrize("price,expected_depth,eligible", [
    (100.5, 0.95, False),   # ранний тест 95%: актуален, но не для входа
    (108.0, 0.20, True),    # ранний тест 20%: порог не препятствует выбору
])
def test_p_early_test_before_confirmation_persists(db, cfg, instrument_id,
                                                   price, expected_depth, eligible):
    z = _ob(db, instrument_id, confirmed=False)  # кандидат до FVG
    scanner = Scanner(db, cfg)
    # ТЗ 06.10.2026 §3.1: наблюдения — после завершения базы
    be = T0 + D1_MS
    scanner.on_price(instrument_id, 115.0, be + 1000)   # выход из базы
    scanner.on_price(instrument_id, price, be + 2000)   # ранний тест (silent)
    scanner.on_price(instrument_id, 112.0, be + 3000)   # возврат
    assert db.get_events(z.id) == []                    # без ретро-уведомлений

    # подтверждение FVG позже: история тестов НЕ обнуляется,
    # confirmed_at не переносится назад
    confirm_ts = T0 + 10 * D1_MS
    db.update_zone(z.id, confirmed_at=confirm_ts)
    after = db.get_zone(z.id)
    assert after.confirmed_at == confirm_ts
    assert after.has_tests is True
    assert after.max_test_depth == pytest.approx(expected_depth)
    assert after.market_validity == "active"
    assert after.entry_eligible is eligible
    assert after.status == ZoneStatus.ACTIVE
    # Breaker-запрет по прежним тестам >50% видит ранний тест
    from app.engine.breaker import breaker_forbidden
    visits = db.get_visits(z.id, z.cycle_id)
    assert breaker_forbidden(after, visits, [], cfg) is (expected_depth > 0.5)
