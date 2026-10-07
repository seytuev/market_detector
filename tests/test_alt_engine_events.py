"""Тесты пост-freeze логики «Altcoins D1 accumulation» — §9–§13 ТЗ (07.10.2026).

Манипуляция и SSL (§9), структура D1 и вход A (§10), breakout/ретест/14 дней
(§11), отмена K (§12), подтверждение и цели (§13). Синтетические серии:
базовая разгонка до freeze (build_accumulation из test_alt_engine) + явные
пост-freeze свечи, построенные от фактических L/U/M/K замороженного диапазона.
"""
from __future__ import annotations

import json
import math

import pytest

from app.alt.engine import AltEngine
from app.config import AltConfig
from app.db import Database
from app.models import close_boundary_ms
from app.models_alt import AltAsset, AltInstrumentSource, AltState

from tests.test_alt_engine import DAY_MS, T0, ac, build_accumulation

FIRST_POST = 112  # abs-индекс первой свечи после freeze (seg_len=106)


@pytest.fixture
def alt_cfg() -> AltConfig:
    return AltConfig()


@pytest.fixture
def alt_db():
    d = Database(":memory:")
    d.upsert_alt_asset(AltAsset(id=None, cmc_id=101, symbol="TST", name="Test"))
    d.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="bybit", symbol="TSTUSDT",
        earliest_available_ms=T0, history_scope="full",
    ))
    yield d
    d.close()


def post(specs: list[tuple[float, float, float, float]], start: int = FIRST_POST):
    """Пост-freeze свечи из кортежей (o, h, l, c) на abs start, start+1, ..."""
    return [
        ac(T0 + (start + i) * DAY_MS, o, h, l, c)
        for i, (o, h, l, c) in enumerate(specs)
    ]


def run_to_freeze(db: Database, cfg: AltConfig, candles=None):
    eng = AltEngine(db, cfg)
    if candles is None:
        candles = build_accumulation(seg_len=106)
    s = eng.process_asset_history(1, 1, candles)
    assert s["frozen_range_id"] is not None, s
    frozen = db.get_alt_frozen_range(s["frozen_range_id"])
    setup = db.get_alt_setup(s["setup_id"])
    return eng, candles, s, frozen, setup


def build_custom_geometry(trough_low: float = 1.0, peak_high: float = 2.0,
                          seg_len: int = 106):
    """Базовая серия с точными границами: тенями trough/peak задаём L и U.

    L=trough_low (свеча abs 11, стартовый pivot low), U=peak_high (abs 21,
    rebound pivot high) — остальные свечи сегмента строго внутри.
    """
    candles = build_accumulation(seg_len=seg_len)
    candles[11] = ac(T0 + 11 * DAY_MS, 1.30, 1.35, trough_low, 1.32)
    candles[21] = ac(T0 + 21 * DAY_MS, 1.80, peak_high, 1.75, 1.85)
    return candles


def events_of(db: Database, setup_id: int):
    return [
        e for e in db.pending_alt_events(limit=1000) if e.setup_id == setup_id
    ]


def live_structure_rows(db: Database, setup_id: int):
    return [
        r for r in db.list_alt_structure_events(setup_id)
        if not json.loads(r.anchors_json)["historical"]
    ]


def lh_bos_specs(level: float = 1.70, bos_close: float = 1.75):
    """Подтверждаемый pivot high 3+3 (роль LH — ниже пиков сегмента ~1.9) и
    свеча слома Close>level. Индексы: peak на +3, подтверждение на +6,
    BOS на +9 после wick-свечи (+7) и свечи равенства (+8).
    Все кортежи OHLC-валидны (l <= o,c <= h) — движок отбраковывает мусор."""
    return [
        (1.40, 1.50, 1.30, 1.45),
        (1.45, 1.55, 1.35, 1.50),
        (1.50, 1.60, 1.40, 1.55),
        (1.55, level, 1.50, 1.65),          # pivot high (LH)
        (1.56, 1.62, 1.45, 1.55),
        (1.55, 1.62, 1.46, 1.56),
        (1.56, 1.62, 1.47, 1.57),           # подтверждение pivot high
        (1.57, 1.72, 1.50, 1.60),           # тень выше уровня — НЕ слом
        (1.60, level, 1.50, level),         # Close == level — НЕ слом
        (1.65, 1.76, 1.55, bos_close),      # Close > level → BOS, вход A
    ]


# ---------------------------------------------------------------------------
# §9: манипуляция и SSL
# ---------------------------------------------------------------------------

def test_manipulation_episode_lifecycle(alt_db, alt_cfg):
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    L = frozen.lower
    specs = [(1.07, 1.08, 1.04, 1.06)] * 20     # 20 дней Low<L и Close<L — TTL нет
    specs.append((1.06, 1.10, 1.04, L))         # Close == L — НЕ возврат (конвенция v1)
    specs.append((L, 1.16, 1.05, 1.15))         # Close > L — конец эпизода
    specs.append((1.15, 1.16, 1.045, 1.06))     # новый Low < L — НОВЫЙ эпизод
    s2 = eng.process_asset_history(1, 1, candles + post(specs))

    eps = alt_db.list_alt_manipulation_episodes(setup.id)
    assert len(eps) == 2
    ep1, ep2 = eps
    assert ep1.started_candle_open_time == T0 + FIRST_POST * DAY_MS
    assert ep1.ended_candle_open_time == T0 + (FIRST_POST + 21) * DAY_MS
    assert ep1.min_price == 1.04
    assert ep1.days_below == 22                 # 20 + свеча ==L + свеча возврата
    assert ep2.started_candle_open_time == T0 + (FIRST_POST + 22) * DAY_MS
    assert ep2.ended_candle_open_time is None   # эпизод открыт, TTL нет
    assert ep2.min_price == 1.045

    flags = s2["setup_flags"]
    assert flags["manipulation_active"] is True
    types = [t for t, _c in s2["events"]]
    assert types.count("manipulation_started") == 2
    assert types.count("manipulation_ended") == 1

    # Границы не расширяются ни при манипуляции, ни после freeze (T12)
    frozen2 = alt_db.get_alt_frozen_range(s2["frozen_range_id"])
    assert frozen2.lower == L and frozen2.upper == frozen.upper
    row = alt_db.get_alt_range_candidate(s2["candidate_id"])
    assert row.lower == L and row.upper == frozen.upper


def test_ssl_taken_by_real_pivot_only(alt_db, alt_cfg):
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    L = frozen.lower
    # abs 111 (трог t=105) — самый низкий pivot low участка; его уровень
    # становится внутренним SSL после подтверждения тремя правыми свечами
    pivot_low = L  # Low свечи freeze — минимум сегмента
    specs = [
        (1.40, 1.50, 1.30, 1.45),   # 112: выше уровня — pivot не сломан
        (1.45, 1.55, 1.35, 1.50),   # 113
        (1.50, 1.55, 1.40, 1.50),   # 114: pivot low abs 111 подтверждён здесь
        (1.50, 1.52, 1.20, 1.45),   # 115: Low выше уровня — не снятие
        (1.45, 1.50, 1.05, 1.08),   # 116: Low < уровня → SSL_TAKEN (+ Low < L)
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    ssl_events = [e for e in events_of(alt_db, setup.id)
                  if e.event_type == "ssl_taken"
                  and json.loads(e.payload_json)["anchors"][0]["formed_at"]
                  == T0 + 111 * DAY_MS]
    # Ровно одно снятие уровня pivot low abs 111 — свечой abs 116;
    # произвольные минимумы (abs 112/115) уровней не создают
    assert len(ssl_events) == 1
    payload = json.loads(ssl_events[0].payload_json)
    assert payload["level_price"] == pivot_low
    assert payload["anchors"][0]["formed_at"] == T0 + 111 * DAY_MS
    assert ssl_events[0].event_time_ms == close_boundary_ms(
        T0 + 116 * DAY_MS, "D1")
    assert s2["setup_flags"]["ssl_event"] is True
    # Снятие SSL необязательно и ничего не отменяет
    assert s2["setup_state"] == AltState.MATURE.value


# ---------------------------------------------------------------------------
# §10: структура D1 и вход A
# ---------------------------------------------------------------------------

def test_bos_below_l_during_manipulation_gives_entry_a(alt_db, alt_cfg):
    """T14: BOS ниже L при активной манипуляции даёт вход A; флаги
    manipulation_active и entry_a_confirmed сосуществуют."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    L = frozen.lower
    peak = 1.08   # внутренний high НИЖЕ L (~1.0999...)
    assert peak < L
    specs = [
        (1.05, 1.06, 1.02, 1.05),   # 112: начало манипуляции (Low < L)
        (1.05, 1.06, 1.01, 1.04),   # 113
        (1.04, 1.05, 1.005, 1.03),  # 114
        (1.03, peak, 1.02, 1.06),   # 115: pivot high ниже L
        (1.06, 1.07, 1.03, 1.05),   # 116
        (1.05, 1.07, 1.03, 1.05),   # 117
        (1.05, 1.07, 1.03, 1.05),   # 118: pivot подтверждён
        (1.05, 1.09, 1.02, 1.085),  # 119: Close > 1.08, но Close < L — BOS ниже L
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    flags = s2["setup_flags"]
    assert flags["manipulation_active"] is True    # Close < L — эпизод открыт
    assert flags["entry_a_confirmed"] is True      # одновременно, §9/T14
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value
    entry_a = alt_db.find_alt_entry_opportunity(setup.id, "A")
    assert entry_a is not None and entry_a.price == 1.085
    bos = [r for r in live_structure_rows(alt_db, setup.id) if r.kind == "BOS"]
    assert len(bos) == 1 and bos[0].level_price == peak
    assert bos[0].candle_open_time == T0 + 119 * DAY_MS


def test_wick_and_equal_close_do_not_confirm_bos(alt_db, alt_cfg):
    """T15: тень и Close==level не подтверждают слом; повторные закрытия за
    пробитым уровнем не создают новый BOS и второй вход A."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    specs = lh_bos_specs(level=1.70, bos_close=1.75)
    specs.append((1.75, 1.78, 1.60, 1.77))  # повторное закрытие выше уровня
    s2 = eng.process_asset_history(1, 1, candles + post(specs))

    bos = [r for r in live_structure_rows(alt_db, setup.id) if r.kind == "BOS"]
    # Ровно один BOS — на свече +9 (Close 1.75 > 1.70); тень (+7) и
    # равенство (+8) слома не дали, повторное закрытие (+10) — тоже
    assert len(bos) == 1
    assert bos[0].candle_open_time == T0 + (FIRST_POST + 9) * DAY_MS
    assert bos[0].close_price == 1.75
    entry_a = alt_db.find_alt_entry_opportunity(setup.id, "A")
    assert entry_a is not None and entry_a.price == 1.75
    assert len(alt_db.list_alt_entry_opportunities(setup.id)) == 1
    a_events = [e for e in events_of(alt_db, setup.id)
                if e.event_type == "entry_a"]
    assert len(a_events) == 1
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value


def test_pre_maturity_bos_historical_no_retro_entry(alt_db, alt_cfg):
    """T16: BOS внутри формирования — только историческая строка, без заднего
    входа; первый допущенный вход A — от послe-freeze события."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    rows = alt_db.list_alt_structure_events(setup.id)
    pre = [r for r in rows if json.loads(r.anchors_json)["historical"]]
    # Базовая серия: пики с микроджиттером дают BOS на abs 41 до зрелости
    assert any(r.kind == "BOS" for r in pre)
    assert all(json.loads(r.anchors_json)["historical"] for r in pre)
    # Ни входа A, ни уведомления bos_confirmed задним числом
    assert alt_db.find_alt_entry_opportunity(setup.id, "A") is None
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_type in ("bos_confirmed", "entry_a")]

    # Послe-freeze BOS — настоящий вход A по его Close
    s2 = eng.process_asset_history(
        1, 1, candles + post(lh_bos_specs(1.70, 1.75))
    )
    entry_a = alt_db.find_alt_entry_opportunity(setup.id, "A")
    assert entry_a is not None and entry_a.price == 1.75


def test_maturity_and_bos_same_candle(alt_db, alt_cfg):
    """T16: maturity и BOS на одном закрытии — сначала доступность зрелого
    диапазона, затем событие структуры этой свечи; будущие pivots не нужны."""
    candles: list = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    for t in range(106):
        v = 1.35 - 0.15 * math.sin(2 * math.pi * t / 20) * (1 + 1e-9 * (t + 1))
        h, l = v + 0.05, v - 0.05
        if t == 15:
            h = 1.9     # rebound-якорь (U)
        elif t == 55:
            h = 1.8     # высокий pivot середины (роль HH, не трогает LH)
        candles.append(ac(T0 + (6 + t) * DAY_MS, v, h, l, v))
    # Свеча зрелости (abs 111): Close 1.56 выше подтверждённого LH (~1.55,
    # pivot abs 81, подтверждён abs 84) — BOS на том же закрытии, что и freeze
    candles[-1] = ac(T0 + 111 * DAY_MS, 1.45, 1.57, 1.30, 1.56)

    eng = AltEngine(alt_db, alt_cfg)
    s = eng.process_asset_history(1, 1, candles)
    assert s["frozen_range_id"] is not None
    assert s["setup_state"] == AltState.ACTIVE_CONFIRMED.value
    entry_a = alt_db.find_alt_entry_opportunity(s["setup_id"], "A")
    assert entry_a is not None and entry_a.price == 1.56
    assert entry_a.event_time_ms == close_boundary_ms(T0 + 111 * DAY_MS, "D1")
    bos = [r for r in live_structure_rows(alt_db, s["setup_id"])
           if r.kind == "BOS"]
    assert len(bos) == 1
    assert bos[0].candle_open_time == T0 + 111 * DAY_MS
    # Уровень подтверждён за 27 свечей до слома — будущие pivots не использованы
    anchor = json.loads(bos[0].anchors_json)["anchors"][0]
    assert anchor["formed_at"] == T0 + 81 * DAY_MS


# ---------------------------------------------------------------------------
# §11: breakout, ретест, 14 дней
# ---------------------------------------------------------------------------

def test_breakout_strict_close_and_immutable(alt_db, alt_cfg):
    """T17: только Close>U запускает breakout; первый breakout неизменен,
    новые закрытия сверху таймер не перезапускают."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    U = frozen.upper
    specs = [
        (1.60, 2.50, 1.50, 1.70),   # 112: High>U, Close<=U — вынос, не breakout
        (1.70, 2.60, 1.60, 2.20),   # 113: Close 2.2 > U — BREAKOUT
        (2.20, 2.40, 2.10, 2.35),   # 114: выше — таймер НЕ перезапускается
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    b113 = close_boundary_ms(T0 + 113 * DAY_MS, "D1")
    assert setup.breakout_close == 2.2
    assert setup.breakout_closed_at == b113
    assert setup.retest_deadline_ms == b113 + alt_cfg.retest_window_days * DAY_MS
    bo = [e for e in events_of(alt_db, setup.id) if e.event_type == "breakout"]
    assert len(bo) == 1 and bo[0].event_time_ms == b113
    flags = s2["setup_flags"]
    assert flags["upper_excursion"] and flags["breakout_confirmed"]
    # Подтверждение состоялось по breakout — снимок целей с bases=[breakout]
    assert flags["target_snapshot"]["bases"] == ["breakout"]
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value


def test_retest_zone_rules(alt_db, alt_cfg):
    """T18: свеча breakout — не ретест; позднее пересечение [M,U] засчитывается;
    повторные касания — журнал без второго B; Low<M — глубина, не отмена."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    U, M = frozen.upper, frozen.mid
    specs = [
        (1.60, 2.10, 1.55, 1.95),   # 112: breakout; Low в [M,U] — НЕ ретест
        (1.95, 2.15, 1.95, 2.10),   # 113: Low > U — касания нет
        (2.10, 2.14, 1.80, 1.90),   # 114: Low<=U и High>=M — РЕТЕСТ + вход B
        (1.90, 1.95, 1.40, 1.55),   # 115: повторное касание, Low<M — глубина
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    retests = [e for e in events_of(alt_db, setup.id)
               if e.event_type == "retest"]
    assert len(retests) == 2
    assert retests[0].event_time_ms == close_boundary_ms(
        T0 + 114 * DAY_MS, "D1")
    first_payload = json.loads(retests[0].payload_json)
    assert first_payload["zone"] == {"lower": M, "upper": U}
    # Вход B — тот же setup_id, область [M,U], цели первоначальные
    entry_b = alt_db.find_alt_entry_opportunity(setup.id, "B")
    assert entry_b is not None
    assert json.loads(entry_b.zone_json) == {"lower": M, "upper": U}
    assert len(alt_db.list_alt_entry_opportunities(setup.id)) == 1  # только B
    # Второе касание — журнал, глубина ниже M зафиксирована, отмены нет
    assert json.loads(retests[1].payload_json)["journal"] is True
    assert s2["setup_flags"]["retest_received"] is True
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value
    # Цели не пересчитаны ни ретестом, ни новыми pivots
    targets = json.loads(alt_db.get_alt_setup(setup.id).targets_json)
    assert [t["price"] for t in targets] == [
        U + n * frozen.width for n in range(1, 5)
    ]


def test_gap_below_mid_no_retest(alt_db, alt_cfg):
    """T19: gap сразу под M (High < M) — пересечения области нет, ретест не
    выдумывается; глубина ниже M не отменяет."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    M = frozen.mid
    specs = [
        (1.60, 2.10, 1.55, 1.95),   # 112: breakout
        (1.40, M - 0.05, 1.30, 1.42),  # 113: gap под M — High < M, касания нет
        (1.42, M - 0.01, 1.35, 1.45),  # 114: всё ещё High < M
        (1.45, M + 0.10, 1.38, 1.55),  # 115: High >= M и Low <= U — касание
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    retests = [e for e in events_of(alt_db, setup.id)
               if e.event_type == "retest"]
    assert len(retests) == 1
    assert retests[0].event_time_ms == close_boundary_ms(
        T0 + 115 * DAY_MS, "D1")
    payload = json.loads(retests[0].payload_json)
    assert payload["depth_below_mid"] == pytest.approx(M - 1.38)


def test_retest_deadline_inclusive(alt_db, alt_cfg):
    """T20: свеча, закрывающаяся ровно на deadline, ещё проверяется —
    касание на ней засчитывается."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    U = frozen.upper
    specs = [(1.60, 2.10, 1.55, 1.95)]          # 112: breakout
    specs += [(2.00, 2.20, 2.00, 2.10)] * 13    # 113..125: без касания
    specs.append((2.10, 2.15, 1.80, 1.90))      # 126: закрытие РОВНО на deadline
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    touch_candle_boundary = close_boundary_ms(T0 + 126 * DAY_MS, "D1")
    assert touch_candle_boundary == setup.retest_deadline_ms  # ровно deadline
    assert s2["setup_flags"]["retest_received"] is True
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_type == "expired_no_retest"]


def test_expired_no_retest_terminates_setup(alt_db, alt_cfg):
    """T20/T21: без касания до deadline включительно — EXPIRED_NO_RETEST
    завершает ВЕСЬ сетап, даже если вход A уже был; поздний ретест не
    оживляет завершённый сетап."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    specs = lh_bos_specs(1.70, 1.75)            # 112..121: вход A на 121
    specs.append((1.77, 2.10, 1.70, 1.95))      # 122: breakout
    specs += [(2.00, 2.20, 2.00, 2.10)] * 15    # 123..137: без касания
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    assert setup.entry_a_id is not None         # вход A был
    assert s2["setup_state"] == AltState.EXPIRED_NO_RETEST.value
    deadline = close_boundary_ms(T0 + 122 * DAY_MS, "D1") + 14 * DAY_MS
    assert setup.terminated_ms == deadline
    exp = [e for e in events_of(alt_db, setup.id)
           if e.event_type == "expired_no_retest"]
    assert len(exp) == 1
    # event_time — исторический deadline, не время обнаружения (поздний job)
    assert exp[0].event_time_ms == deadline

    # Позднее касание не воскрешает завершённый сетап
    n_events = len(events_of(alt_db, setup.id))
    specs.append((2.10, 2.15, 1.80, 1.90))      # 138: касание ПОСЛЕ expiry
    s3 = eng.process_asset_history(1, 1, candles + post(specs))
    assert s3["setup_state"] == AltState.EXPIRED_NO_RETEST.value
    assert len(events_of(alt_db, setup.id)) == n_events
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_type == "retest"]


def test_entry_a_then_b_same_setup(alt_db, alt_cfg):
    """T22: вход B после A — тот же setup_id, цели первоначальные;
    антиспам: один первый A и один первый B."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    U, W = frozen.upper, frozen.width
    specs = lh_bos_specs(1.70, 1.75)            # вход A на +9
    specs.append((1.77, 2.30, 1.70, 2.20))      # breakout
    specs.append((2.20, 2.25, 1.80, 1.90))      # ретест → вход B
    specs.append((1.90, 1.95, 1.70, 1.80))      # ещё касание — журнал
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    opps = alt_db.list_alt_entry_opportunities(setup.id)
    assert [o.kind for o in opps] == ["A", "B"]
    assert opps[0].setup_id == opps[1].setup_id == setup.id
    assert setup.entry_a_id == opps[0].id and setup.entry_b_id == opps[1].id
    targets = json.loads(setup.targets_json)
    assert [t["price"] for t in targets] == [U + n * W for n in range(1, 5)]
    # snapshot взят на первом подтверждении (BOS), до breakout
    flags = json.loads(setup.flags_json)
    assert flags["target_snapshot"]["bases"] == ["bos"]


# ---------------------------------------------------------------------------
# §12: отмена
# ---------------------------------------------------------------------------

def test_cancel_k_formula_and_unreachable(alt_db, alt_cfg):
    """T24/T25: K=2L−U; K<=0 не обрезается и сетап не исключается —
    cancel_reachable=false, положительная цена K не достигает."""
    # L=1, U=2 → K=0
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_custom_geometry(trough_low=1.0, peak_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    setup = alt_db.get_alt_setup(s["setup_id"])
    assert setup.cancel_price == 0.0            # не обрезан, не заменён
    assert setup.cancel_reachable is False
    # Падение к 0.5 (глубоко под L) не отменяет: K=0 недостижим
    s2 = eng.process_asset_history(
        1, 1, candles + post([(1.40, 1.45, 0.50, 1.40)])
    )
    assert s2["setup_state"] == AltState.MATURE.value
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_type == "cancelled"]

    # L=1, U=2.2 → K=−0.2 — на том же активе новый диапазон не нужен,
    # проверяем формулу на свежей БД
    db2 = Database(":memory:")
    db2.upsert_alt_asset(AltAsset(id=None, cmc_id=102, symbol="TS2"))
    db2.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="bybit", symbol="TS2USDT",
        earliest_available_ms=T0, history_scope="full",
    ))
    eng2 = AltEngine(db2, alt_cfg)
    s3 = eng2.process_asset_history(
        1, 1, build_custom_geometry(trough_low=1.0, peak_high=2.2)
    )
    setup3 = db2.get_alt_setup(s3["setup_id"])
    assert setup3.cancel_price == pytest.approx(-0.2)
    assert setup3.cancel_reachable is False
    db2.close()


def test_cancel_modes_wick_vs_close(alt_db, alt_cfg):
    """T24: проектный режим v1 — wick_on_closed_d1 (Low<=K); альтернатива
    close_on_closed_d1 (Close<=K); режим подписан в событии."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    K = setup.cancel_price
    assert 0 < K < frozen.lower and setup.cancel_mode == "wick_on_closed_d1"
    wick_candle = (1.40, 1.45, K - 0.01, 1.40)   # Low <= K, Close > K
    s2 = eng.process_asset_history(1, 1, candles + post([wick_candle]))
    assert s2["setup_state"] == AltState.CANCELLED.value
    cancel = [e for e in events_of(alt_db, setup.id)
              if e.event_type == "cancelled"]
    assert len(cancel) == 1
    payload = json.loads(cancel[0].payload_json)
    assert payload["cancel_mode"] == "wick_on_closed_d1"
    assert payload["cancel_mode_note"] == "project_default_v1"
    assert payload["cancel_price"] == K
    assert alt_db.get_alt_setup(setup.id).terminated_ms == (
        close_boundary_ms(T0 + FIRST_POST * DAY_MS, "D1")
    )

    # Режим close_on_closed_d1: та же свеча НЕ отменяет, нужен Close <= K
    db2 = Database(":memory:")
    db2.upsert_alt_asset(AltAsset(id=None, cmc_id=103, symbol="TS3"))
    db2.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="bybit", symbol="TS3USDT",
        earliest_available_ms=T0, history_scope="full",
    ))
    cfg2 = AltConfig(cancel_mode="close_on_closed_d1")
    eng2 = AltEngine(db2, cfg2)
    candles2 = build_accumulation(seg_len=106)
    s3 = eng2.process_asset_history(1, 1, candles2)
    setup2 = db2.get_alt_setup(s3["setup_id"])
    K2 = setup2.cancel_price
    s4 = eng2.process_asset_history(1, 1, candles2 + post([
        (1.40, 1.45, K2 - 0.01, 1.40),          # Low <= K2, Close > K2 — живой
        (1.40, 1.42, 0.05, K2 - 0.01),          # Close <= K2 — отмена
    ]))
    assert s4["setup_state"] == AltState.CANCELLED.value
    cancel2 = [e for e in events_of(db2, setup2.id)
               if e.event_type == "cancelled"]
    assert len(cancel2) == 1
    assert cancel2[0].event_time_ms == close_boundary_ms(
        T0 + (FIRST_POST + 1) * DAY_MS, "D1")
    assert json.loads(cancel2[0].payload_json)["cancel_mode"] == (
        "close_on_closed_d1")
    db2.close()


def test_same_candle_cancel_and_entry_cancel_wins(alt_db, alt_cfg):
    """T26: отмена и вход на одной D1 — отмена приоритетна для новой
    возможности; пересечение сохранено с флагом неизвестной последовательности."""
    eng, candles, s, frozen, setup = run_to_freeze(alt_db, alt_cfg)
    K = setup.cancel_price
    specs = lh_bos_specs(1.70, 1.75)[:9]        # до подтверждения LH (112..120)
    # Свеча +9: Close пробивает LH (BOS/вход A) И Low <= K (отмена)
    specs.append((1.70, 1.76, K - 0.01, 1.75))
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    assert s2["setup_state"] == AltState.CANCELLED.value
    # Вход A НЕ выдан
    assert alt_db.list_alt_entry_opportunities(setup.id) == []
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_type == "entry_a"]
    # Пересечение уровня сохранено как факт с оговоркой о последовательности
    bos = [e for e in events_of(alt_db, setup.id)
           if e.event_type == "bos_confirmed"]
    assert len(bos) == 1
    assert json.loads(bos[0].payload_json)[
        "intra_candle_sequence_unknown"] is True
    cancel = [e for e in events_of(alt_db, setup.id)
              if e.event_type == "cancelled"]
    assert json.loads(cancel[0].payload_json)[
        "intra_candle_sequence_unknown"] is True


# ---------------------------------------------------------------------------
# §13: подтверждение и цели
# ---------------------------------------------------------------------------

def test_targets_math_multi_hit_completion(alt_db, alt_cfg):
    """T23: TP_n=U+nW (L=1,U=2 → 3,4,5,6); несколько целей на одной D1 — одно
    объединённое событие; каждый уровень один раз; TARGETS_COMPLETED — терминал."""
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_custom_geometry(trough_low=1.0, peak_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    setup = alt_db.get_alt_setup(s["setup_id"])
    specs = [
        (1.60, 2.60, 1.55, 2.50),   # 112: breakout, Close 2.5 < TP1 — подтверждение
        (2.50, 4.50, 2.40, 4.40),   # 113: High 4.5 — TP1 и TP2 одной свечой
        (4.40, 4.60, 4.30, 4.50),   # 114: TP2 уже уведомлён — тишина
        (4.50, 6.50, 4.40, 6.40),   # 115: TP3 и TP4 — все цели
        (6.40, 7.00, 6.30, 6.90),   # 116: терминал — новых событий нет
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    targets = json.loads(setup.targets_json)
    assert [t["price"] for t in targets] == [3.0, 4.0, 5.0, 6.0]
    assert all(not t["passed_at_confirmation"] for t in targets)

    hits = [e for e in events_of(alt_db, setup.id)
            if e.event_type == "target_hit"]
    assert len(hits) == 2                          # по одному событию на свечу
    levels = [json.loads(e.payload_json)["levels"] for e in hits]
    assert levels == [[1, 2], [3, 4]]
    assert sorted(n for lv in levels for n in lv) == [1, 2, 3, 4]  # каждый раз
    assert s2["setup_state"] == AltState.TARGETS_COMPLETED.value
    assert setup.terminated_ms == close_boundary_ms(T0 + 115 * DAY_MS, "D1")
    completed = [e for e in events_of(alt_db, setup.id)
                 if e.event_type == "targets_completed"]
    assert len(completed) == 1
    # Свеча 116 после терминала ничего не добавила
    assert not [e for e in events_of(alt_db, setup.id)
                if e.event_time_ms > close_boundary_ms(T0 + 115 * DAY_MS, "D1")]


def test_target_passed_at_confirmation(alt_db, alt_cfg):
    """T23: Close подтверждения уже выше цели — «пройдена к моменту
    подтверждения», не будущий потенциал; High той же свечи не засчитывается."""
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_custom_geometry(trough_low=1.0, peak_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    setup = alt_db.get_alt_setup(s["setup_id"])
    specs = [
        (1.60, 3.60, 1.55, 3.50),   # 112: breakout Close 3.5 >= TP1=3
        (3.50, 3.80, 3.30, 3.60),   # 113: High >= TP1, но она пройдена — тишина
        (3.60, 4.20, 3.50, 4.10),   # 114: High >= TP2=4 — только уровень 2
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    setup = alt_db.get_alt_setup(setup.id)
    targets = json.loads(setup.targets_json)
    assert targets[0]["passed_at_confirmation"] is True
    assert targets[1]["passed_at_confirmation"] is False
    hits = [e for e in events_of(alt_db, setup.id)
            if e.event_type == "target_hit"]
    assert len(hits) == 1
    assert json.loads(hits[0].payload_json)["levels"] == [2]
    assert s2["setup_flags"]["targets_hit"] == [1, 2]


def test_retest_and_target_same_candle_flagged(alt_db, alt_cfg):
    """T23/T26: ретест и цель на одной поздней D1 — оба факта с оговоркой
    о неизвестной внутрисвечной последовательности."""
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_custom_geometry(trough_low=1.0, peak_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    setup = alt_db.get_alt_setup(s["setup_id"])
    specs = [
        (1.60, 2.60, 1.55, 2.50),   # 112: breakout → подтверждение, TP1=3
        (2.20, 3.20, 1.80, 2.00),   # 113: касание [M,U] И High >= TP1
    ]
    s2 = eng.process_asset_history(1, 1, candles + post(specs))
    evs = events_of(alt_db, setup.id)
    retest = [e for e in evs if e.event_type == "retest"]
    hit = [e for e in evs if e.event_type == "target_hit"]
    assert len(retest) == 1 and len(hit) == 1
    assert json.loads(retest[0].payload_json)[
        "intra_candle_sequence_unknown"] is True
    assert json.loads(hit[0].payload_json)[
        "intra_candle_sequence_unknown"] is True
    assert json.loads(hit[0].payload_json)["levels"] == [1]
    assert alt_db.find_alt_entry_opportunity(setup.id, "B") is not None
    assert s2["setup_state"] == AltState.ACTIVE_CONFIRMED.value


# ---------------------------------------------------------------------------
# Идемпотентность полного набора событий (§18/T31)
# ---------------------------------------------------------------------------

def test_full_replay_idempotency_events(alt_db, alt_cfg):
    """Повторный replay богатого сценария: те же строки, created=False,
    ни одного дубля событий/эпизодов/входов/структуры."""
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_custom_geometry(trough_low=1.0, peak_high=2.0)
    specs = [
        (1.30, 1.35, 0.70, 0.80),   # 112: манипуляция (Low < L=1)
        (0.80, 1.25, 0.75, 1.20),   # 113: возврат Close > L — конец эпизода
        (1.60, 2.60, 1.55, 2.50),   # 114: breakout → подтверждение
        (2.20, 3.20, 1.80, 2.00),   # 115: ретест + TP1 (вход B), сетап жив
    ]
    all_candles = candles + post(specs)
    s1 = eng.process_asset_history(1, 1, all_candles)
    assert s1["setup_state"] == AltState.ACTIVE_CONFIRMED.value
    s2 = eng.process_asset_history(1, 1, all_candles)

    assert all(created for _t, created in s1["events"])
    assert all(not created for _t, created in s2["events"])
    setup_id = s1["setup_id"]
    assert s2["setup_id"] == setup_id
    # SMS (цепочка min=1.0 → high=2.0 → HL) подтверждается на свече
    # breakout — подтверждение с двумя основаниями, вход A и позднее вход B
    assert {t for t, _c in s1["events"]} >= {
        "manipulation_started", "manipulation_ended", "sms_confirmed",
        "breakout", "entry_a", "retest", "entry_b", "target_hit",
        "forming_started", "mature_frozen",
    }
    n_events = len(events_of(alt_db, setup_id))
    assert n_events == len(s1["events"])
    assert len(alt_db.list_alt_manipulation_episodes(setup_id)) == 1
    opps = alt_db.list_alt_entry_opportunities(setup_id)
    assert [o.kind for o in opps] == ["A", "B"]  # антиспам: один A и один B
    n_struct = len(alt_db.list_alt_structure_events(setup_id))
    # Третий прогон — та же картина
    s3 = eng.process_asset_history(1, 1, all_candles)
    assert all(not created for _t, created in s3["events"])
    assert len(events_of(alt_db, setup_id)) == n_events
    assert len(alt_db.list_alt_structure_events(setup_id)) == n_struct
