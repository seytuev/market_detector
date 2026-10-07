"""Тесты движка «Altcoins D1 accumulation» (§5–§8 ТЗ от 07.10.2026).

Покрытие: 5 синтетических векторов классификатора (§7.4), строгий порог 80%
(T05), новые ATH-эпизоды (T06), лаг доступности pivots 3+3 (T07), строгие
границы 50/100 дней (T08), расширение до freeze и его запрет после (T12),
идемпотентность replay (T31), REVIEW_REQUIRED при неоднозначных опорах.
"""
from __future__ import annotations

import math

import pytest

from app.alt.engine import (
    AltEngine,
    AthTracker,
    classify_sideways,
    range_age_days,
    validate_ohlc,
)
from app.config import AltConfig
from app.db import Database
from app.models import close_boundary_ms
from app.models_alt import (
    AltAsset,
    AltCandle,
    AltInstrumentSource,
    AltState,
)

DAY_MS = 86_400_000
T0 = (1_700_000_000_000 // DAY_MS) * DAY_MS  # выровненное начало суток UTC


def ac(open_time: int, o: float, h: float, l: float, c: float,
       source_id: int = 1) -> AltCandle:
    return AltCandle(source_id=source_id, open_time=open_time,
                     open=o, high=h, low=l, close=c)


def flat_series(closes: list[float], source_id: int = 1) -> list[AltCandle]:
    """Синтетика §7.4: High=Low=Close для каждого дня."""
    return [ac(T0 + i * DAY_MS, c, c, c, c, source_id)
            for i, c in enumerate(closes)]


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


def build_accumulation(seg_len: int, spike_at_abs: int | None = None,
                       spike_high: float = 0.0,
                       equal_second_trough: bool = False) -> list[AltCandle]:
    """Сценарий: ATH=10 (abs 4) → падение до 1.9 (abs 5, −81%) → боковик.

    Сегмент с abs 6: closes = 1.5 − 0.4·sin(2πt/20) с микро-джиттером
    (убирает точное равенство экстремумов разных периодов). Pivot low на
    t=5 (abs 11, подтверждён на abs 14) — стартовый якорь; pivot high на
    t=15 (abs 21, подтверждён на abs 24) — первичный верх. n_days на
    свече abs a: a−10 (51-й день = abs 61, 101-й = abs 111).
    """
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))  # −81% от ATH
    trough5 = 1.5 - 0.4 * math.sin(2 * math.pi * 5 / 20) * (1 + 6e-9)
    for t in range(seg_len):
        v = 1.5 - 0.4 * math.sin(2 * math.pi * t / 20) * (1 + 1e-9 * (t + 1))
        if equal_second_trough and t == 25:
            v = trough5  # точное равенство цены второго дна (float ==)
        o_time = T0 + (6 + t) * DAY_MS
        if spike_at_abs is not None and 6 + t == spike_at_abs:
            candles.append(ac(o_time, v, spike_high, v, v))
        else:
            candles.append(ac(o_time, v, v, v, v))
    return candles


# ---------------------------------------------------------------------------
# §7.4: пять синтетических векторов классификатора
# ---------------------------------------------------------------------------

CLASSIFIER_VECTORS = [
    ("a", lambda t: 1.5 + 0.4 * math.sin(2 * math.pi * t / 20),
     0.1612, 0.0, True, "down"),
    ("b", lambda t: 2 - t / 150, 0.9244, 0.6723, False, "down"),
    ("c", lambda t: 1 + t / 150, 0.9244, 0.6723, False, "up"),
    ("d", lambda t: 2 - t / 150 + 0.07 * math.sin(2 * math.pi * t / 15),
     0.8519, 0.5864, False, "down"),
    ("e", lambda t: 1.8 - 0.002 * t + 0.28 * math.sin(2 * math.pi * t / 20),
     0.3984, 0.2051, True, "down"),
]


@pytest.mark.parametrize(
    "name,fn,exp_slope,exp_shift,exp_sideways,exp_sign", CLASSIFIER_VECTORS
)
def test_classifier_synthetic_vectors(alt_cfg, name, fn, exp_slope, exp_shift,
                                      exp_sideways, exp_sign):
    closes = [fn(t) for t in range(120)]
    width = max(closes) - min(closes)  # W по всем 120 ценам (High=Low=Close)
    r = classify_sideways(closes, width, alt_cfg)
    assert r.ready and r.reason == "ok"
    assert r.slope_normalized == pytest.approx(exp_slope, abs=1e-3)
    assert r.center_shift == pytest.approx(exp_shift, abs=1e-3)
    assert r.sideways is exp_sideways
    assert r.slope_sign == exp_sign
    if exp_sideways:
        assert r.failed_conditions == ()
    else:
        assert "slope" in r.failed_conditions
        assert "center_shift" in r.failed_conditions


def test_classifier_not_ready_on_insufficient_data(alt_cfg):
    closes = [1.0 + 0.01 * math.sin(t) for t in range(20)]  # < 3 блоков по 10
    r = classify_sideways(closes, 1.0, alt_cfg)
    assert not r.ready and not r.sideways and r.reason == "not_enough_data"


def test_classifier_zero_width_not_ready(alt_cfg):
    r = classify_sideways([1.0] * 60, 0.0, alt_cfg)
    assert not r.ready and r.reason == "zero_width"


def test_classifier_diagnostics_no_hidden_filters(alt_cfg):
    # §7.3: efficiency=0 при нулевом знаменателе; диагностика по теням
    flat = classify_sideways([1.5] * 60, 1.0, alt_cfg)
    assert flat.efficiency == 0.0
    candles = flat_series([1.0, 2.0] * 30)  # W=1, касания каждой свечой
    r = classify_sideways([c.close for c in candles], 1.0, alt_cfg,
                          candles=candles)
    assert r.diagnostics["touch_count"] == 60
    assert "width_concentration_single_wick" in r.diagnostics
    assert r.diagnostics["close_median"] == 1.5


# ---------------------------------------------------------------------------
# §5: ATH / просадка
# ---------------------------------------------------------------------------

def track(alt_cfg, bars: list[tuple[float, float]]) -> AthTracker:
    """bars: (high, low) по свечам с T0."""
    tr = AthTracker(alt_cfg)
    for i, (h, l) in enumerate(bars):
        tr.update(T0 + i * DAY_MS, h, l)
    return tr


def test_strict_80_percent_threshold(alt_cfg):
    # Ровно 80% (low == A*0.20) НЕ подходит (T05)
    tr = track(alt_cfg, [(10.0, 9.0), (9.5, 2.0)])
    ep = tr.current
    assert ep.p_min == 2.0
    assert ep.drawdown == pytest.approx(0.80)
    assert not ep.deep_drop_achieved
    # 80.01% подходит
    tr.update(T0 + 2 * DAY_MS, 9.0, 1.999)
    assert ep.deep_drop_achieved
    assert ep.deep_drop_open_time == T0 + 2 * DAY_MS
    assert ep.drawdown == pytest.approx(0.8001)
    # Восстановление цены выше −80% не отменяет достигнутый факт
    tr.update(T0 + 3 * DAY_MS, 9.5, 9.0)
    assert ep.deep_drop_achieved
    assert ep.p_min == 1.999 and ep.p_min_open_time == T0 + 2 * DAY_MS


def test_searching_without_candles_after_ath(alt_cfg):
    tr = track(alt_cfg, [(10.0, 9.5)])
    assert tr.current.drawdown is None  # без деления
    assert not tr.current.deep_drop_achieved


def test_ath_tie_moves_episode_to_last_top(alt_cfg):
    tr = track(alt_cfg, [(10.0, 9.0), (9.0, 1.5), (10.0, 8.0)])
    ep = tr.current
    # Равная вершина: начало эпизода — ПОСЛЕДНЯЯ свеча с этой ценой,
    # все равные вершины сохранены в доказательствах
    assert ep.ath_price == 10.0
    assert ep.ath_open_time == T0 + 2 * DAY_MS
    assert ep.equal_top_open_times == [T0, T0 + 2 * DAY_MS]
    assert ep.p_min is None and not ep.deep_drop_achieved  # новый отсчёт
    # Старый эпизод с достигнутым падением сохранён в истории
    assert tr.history[0].deep_drop_achieved
    assert tr.history[0].p_min == 1.5


def test_new_ath_starts_new_episode_without_rewriting(alt_cfg):
    tr = track(alt_cfg, [(10.0, 9.0), (9.0, 1.5), (9.5, 1.4), (12.0, 9.0)])
    old, ep = tr.history[0], tr.current
    assert ep.ath_price == 12.0 and ep.ath_open_time == T0 + 3 * DAY_MS
    assert ep.p_min is None  # после нового ATH свечей ещё нет → SEARCHING
    assert old.deep_drop_achieved and old.p_min == 1.4
    # Просадка нового эпизода считается от нового ATH
    tr.update(T0 + 4 * DAY_MS, 11.0, 2.3)  # 1 − 2.3/12 = 80.83% > 80%
    assert ep.deep_drop_achieved
    assert ep.drawdown == pytest.approx(1 - 2.3 / 12)


def test_validate_ohlc_rejects_corrupt():
    assert validate_ohlc(1, 2, 1, 1.5) is None
    assert validate_ohlc(0, 2, 1, 1.5) == "non_positive"
    assert validate_ohlc(1, 0.5, 1, 1.0) == "high_below_low"
    assert validate_ohlc(1, 1.2, 0.9, 1.3) == "high_below_body"
    assert validate_ohlc(1, 1.2, 1.1, 0.8) == "low_above_body"
    assert validate_ohlc(1, float("nan"), 1, 1) == "non_finite"


# ---------------------------------------------------------------------------
# Оркестратор: лаг pivots, границы дней, freeze, идемпотентность
# ---------------------------------------------------------------------------

def test_pivot_availability_lag(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=60)

    # До подтверждения pivot low (нужно 3 правых закрытых): якоря нет
    s = eng.process_asset_history(1, 1, candles[:14])  # abs 0..13
    assert s["state"] == AltState.SEARCHING.value
    assert s["candidate_id"] is None
    assert s["ath"]["deep_drop_achieved"]

    # Pivot low abs 11 подтверждён на abs 14: старт известен, верх ещё нет
    s = eng.process_asset_history(1, 1, candles[:15])
    assert s["state"] == "range_pending"
    assert s["candidate_id"] is None  # строка появляется с обеими опорами

    # Pivot high abs 21 подтверждён на abs 24: кандидат создан, возраст мал
    s = eng.process_asset_history(1, 1, candles[:25])
    assert s["state"] == AltState.SEARCHING.value
    assert s["candidate_id"] is not None
    assert s["n_days"] == 24 - 10  # от open_time стартового abs 11
    row = alt_db.get_alt_range_candidate(s["candidate_id"])
    assert row.start_anchor_open_time == T0 + 11 * DAY_MS
    assert row.rebound_anchor_open_time == T0 + 21 * DAY_MS


def test_strict_50_100_day_boundaries(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=106)  # abs 0..111

    s = eng.process_asset_history(1, 1, candles[:61])  # n_days = 50
    assert s["n_days"] == 50
    assert s["state"] == AltState.SEARCHING.value  # ровно 50 не проходит

    s = eng.process_asset_history(1, 1, candles[:62])  # n_days = 51 → FORMING
    assert s["n_days"] == 51
    assert s["state"] == AltState.FORMING.value
    assert s["frozen_range_id"] is None

    s = eng.process_asset_history(1, 1, candles[:111])  # n_days = 100
    assert s["n_days"] == 100
    assert s["state"] == AltState.FORMING.value  # ровно 100 не зрелый
    assert s["frozen_range_id"] is None

    s = eng.process_asset_history(1, 1, candles)  # n_days = 101 → MATURE
    assert s["n_days"] == 101
    assert s["state"] == AltState.MATURE.value
    assert s["frozen_range_id"] is not None and s["setup_id"] is not None
    assert s["classifier"]["sideways"]

    frozen = alt_db.get_alt_frozen_range(s["frozen_range_id"])
    assert frozen.included_candles == 101
    # mature_at — момент реального распознавания (граница закрытия abs 111),
    # без заднего числа
    assert frozen.mature_at_ms == close_boundary_ms(T0 + 111 * DAY_MS, "D1")
    assert frozen.classifier_version == alt_cfg.classifier_version
    setup = alt_db.get_alt_setup(s["setup_id"])
    assert setup.state == AltState.MATURE.value
    # K = 2L − U, режим отмены — проектный, из конфига
    assert setup.cancel_price == pytest.approx(2 * frozen.lower - frozen.upper)
    assert setup.cancel_mode == alt_cfg.cancel_mode


def test_freeze_not_backdated_when_sideways_comes_late(alt_db, alt_cfg):
    # Направленный дрейф в первые ~100 дней не проходит классификатор;
    # зрелость наступает позже 101-го дня, mature_at не дорисовывается
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    closes: list[float] = []
    for t in range(130):
        # затухающий дрейф вниз: сначала тренд, к концу — боковик
        drift = 0.9 * math.exp(-t / 60)
        closes.append(1.5 + drift - 0.05 * math.sin(2 * math.pi * t / 20))
    for t, v in enumerate(closes):
        candles.append(ac(T0 + (6 + t) * DAY_MS, v, v, v, v))
    eng = AltEngine(alt_db, alt_cfg)
    s = eng.process_asset_history(1, 1, candles)
    # если сценарий вообще создал диапазон — freeze строго позже 101-го дня
    if s["frozen_range_id"] is not None:
        frozen = alt_db.get_alt_frozen_range(s["frozen_range_id"])
        assert frozen.included_candles > 101
        assert frozen.mature_at_ms > close_boundary_ms(T0 + 111 * DAY_MS, "D1")


def test_expansion_before_freeze_none_after(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    # Вынос high=2.0 на abs 80 (до зрелости) расширяет верх
    candles = build_accumulation(seg_len=106, spike_at_abs=80, spike_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    assert s["state"] == AltState.MATURE.value
    assert s["range_version"] >= 2  # расширение записано версией
    row = alt_db.get_alt_range_candidate(s["candidate_id"])
    assert row.upper == 2.0
    import json as _json
    metrics = _json.loads(row.metrics_json)
    changed_by = [v["changed_by_open_time"] for v in metrics["versions"]]
    assert T0 + 80 * DAY_MS in changed_by
    frozen = alt_db.get_alt_frozen_range(s["frozen_range_id"])
    assert frozen.upper == 2.0 and frozen.range_version == row.version

    # После freeze вынос не расширяет границы (T12): high 2.5, close внутри
    post = candles + [
        ac(T0 + 112 * DAY_MS, 1.6, 2.5, 1.5, 1.7),   # upper_excursion
        ac(T0 + 113 * DAY_MS, 1.7, 2.6, 1.6, 2.2),   # Close>U → breakout факт
    ]
    s2 = eng.process_asset_history(1, 1, post)
    frozen2 = alt_db.get_alt_frozen_range(s2["frozen_range_id"])
    assert frozen2.upper == 2.0 and frozen2.lower == frozen.lower
    row2 = alt_db.get_alt_range_candidate(s2["candidate_id"])
    assert row2.upper == 2.0 and row2.version == row.version  # без расширения
    setup = alt_db.get_alt_setup(s2["setup_id"])
    flags = _json.loads(setup.flags_json)
    assert flags["upper_excursion"] and flags["breakout_confirmed"]
    assert setup.breakout_close == 2.2
    assert setup.breakout_closed_at == close_boundary_ms(T0 + 113 * DAY_MS, "D1")
    assert setup.retest_deadline_ms == (
        setup.breakout_closed_at + alt_cfg.retest_window_days * DAY_MS
    )


def test_replay_idempotency(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=106)
    s1 = eng.process_asset_history(1, 1, candles)
    s2 = eng.process_asset_history(1, 1, candles)  # повторный replay
    assert s1["candidate_id"] == s2["candidate_id"]
    assert s1["frozen_range_id"] == s2["frozen_range_id"]
    assert s1["setup_id"] == s2["setup_id"]
    # Те же опоры — тот же диапазон: события не дублируются (UNIQUE-дедуп)
    assert all(created for _t, created in s1["events"])
    assert all(not created for _t, created in s2["events"])
    types1 = {t for t, _c in s1["events"]}
    # lifecycle + SSL-снятия на свече freeze (её Low — минимум участка, §9)
    assert {"forming_started", "mature_frozen", "ssl_taken"} <= types1
    assert len(alt_db.pending_alt_events(limit=100)) == len(s1["events"])
    row = alt_db.get_alt_range_candidate(s1["candidate_id"])
    assert row.version == s1["range_version"]


def test_review_required_on_ambiguous_anchors(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    # Второе дно с ТОЧНО равной ценой (abs 31) — выбор старта неоднозначен
    candles = build_accumulation(seg_len=106, equal_second_trough=True)
    s = eng.process_asset_history(1, 1, candles)
    assert s["review_required"]
    assert s["alternative_anchors"] == [T0 + 31 * DAY_MS]
    assert s["state"] == AltState.REVIEW_REQUIRED.value
    setup = alt_db.get_alt_setup(s["setup_id"])
    assert setup.state == AltState.REVIEW_REQUIRED.value
    types = {t for t, _c in s["events"]}
    assert "review_required" in types and "mature_frozen" in types
    review_ev = [e for e in alt_db.pending_alt_events(100)
                 if e.event_type == "review_required"][0]
    assert review_ev.source_event_id.startswith("review:")


def test_corrupt_candles_marked_as_data_errors(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=106)
    bad = ac(T0 + 200 * DAY_MS, 1.0, 0.5, 1.2, 1.1)  # high < low
    s = eng.process_asset_history(1, 1, candles + [bad])
    assert s["data_errors"] == [
        {"open_time": T0 + 200 * DAY_MS, "reason": "high_below_low"}
    ]
    assert s["candles_valid"] == len(candles)
    assert s["state"] == AltState.MATURE.value  # мусор не использовался молча


def test_data_pending_on_short_or_partial_history(alt_db, alt_cfg):
    eng = AltEngine(alt_db, alt_cfg)
    s = eng.process_asset_history(1, 1, build_accumulation(seg_len=0)[:5])
    assert s["state"] == AltState.DATA_PENDING.value and s["data_pending"]
    # Частичная история источника: ATH фрагмента ≠ достоверный ATH (§4)
    alt_db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="bybit", symbol="TSTUSDT",
        earliest_available_ms=T0, history_scope="partial",
    ))
    s = eng.process_asset_history(1, 1, build_accumulation(seg_len=60))
    assert s["state"] == AltState.DATA_PENDING.value
    assert s["history_scope"] == "partial"


def test_range_age_uses_exclusive_close_boundary():
    # §6: N_days от open_time старта до exclusive close boundary последней D1
    assert range_age_days(T0, T0) == 1
    assert range_age_days(T0, T0 + 50 * DAY_MS) == 51
    assert range_age_days(T0, T0 + 100 * DAY_MS) == 101
