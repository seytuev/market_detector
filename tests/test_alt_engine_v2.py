"""Тесты движка v2 «Altcoins D1 accumulation» (этап 4 ТЗ от 07.10.2026).

Покрытие: ATR и допуск кластера (R-03), кластеры реакций и медианная линия,
один затяжной контакт = одна реакция, выбор пары L/U без абсолютных
High/Low и одиночных теней, классификатор v2 с нормировкой на ATR (R-04),
мультикандидатный поиск после импульса (R-01/R-02), окно допуска после
глубокой просадки и перевзвод новым ATH (R-01), отрицательные случаи §8.6,
детерминизм/идемпотентность/префиксное свойство (R-10), неизменность
замороженной геометрии (R-09). Смоук-тесты на эталонных фикстурах этапа 1
(полная приёмка — этап 6).
"""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

import pytest

from app.alt.engine_v2 import (
    AltEngineV2,
    classify_sideways_v2,
    cluster_pivots,
    cluster_tolerance,
    compute_atr,
    count_independent_reactions,
    choose_lu_pair,
)
from app.config import AltConfig
from app.db import Database
from app.engine.liquidity import PivotRecord
from app.models import ZoneType
from app.models_alt import (
    ALT_RULE_VERSION_V2,
    AltAsset,
    AltCandle,
    AltEpisodeState,
    AltInstrumentSource,
)
from tests.test_alt_engine import T0, ac, build_accumulation

DAY_MS = 86_400_000
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "alt_v2"


# ---------------------------------------------------------------------------
# Хелперы и фикстуры
# ---------------------------------------------------------------------------


def pv(price: float, day: int, kind: ZoneType = ZoneType.SSL) -> PivotRecord:
    """Подтверждённый pivot для чистых функций (formed/confirmed по дню)."""
    return PivotRecord(
        kind, price, T0 + day * DAY_MS, T0 + (day + 3) * DAY_MS, "D1", ()
    )


def osc(t: int, mid: float, amp: float, per: int = 20) -> float:
    return mid - amp * math.sin(2 * math.pi * t / per) * (1 + 1e-9 * (t + 1))


def osc_candles(n: int, mid: float, amp: float, start_abs: int,
                per: int = 20) -> list[AltCandle]:
    """Синус-консолидация High=Low=Close (как в v1-хелперах)."""
    return [
        ac(T0 + (start_abs + t) * DAY_MS, (v := osc(t, mid, amp, per)), v, v, v)
        for t in range(n)
    ]


def build_two_bases() -> list[AltCandle]:
    """NEAR/HBAR-паттерн: ATH=10 → −81% → база1 (140д) → импульс → база2 (140д)."""
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))   # deep drop abs 5
    candles += osc_candles(140, 1.5, 0.4, 6)                  # база1 abs 6..145
    for k in range(15):                                       # ралли abs 146..160
        v = 2.0 + (k + 1) * 0.2
        candles.append(ac(T0 + (146 + k) * DAY_MS, v, v, v, v))
    candles += osc_candles(140, 4.0, 0.4, 161)                # база2 abs 161..300
    return candles


def build_rearm(gap_after_drop2: int) -> list[AltCandle]:
    """Два ATH-эпизода: просадка #1 (abs 5), новый ATH=12 (abs 70), просадка
    #2 (abs 71), затем `gap_after_drop2` плоских дней без pivots и боковик."""
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))   # deep drop #1
    for k in range(64):                                       # строго вверх abs 6..69
        v = 2.6 + k * 0.147
        candles.append(ac(T0 + (6 + k) * DAY_MS, v, v, v, v))
    candles.append(ac(T0 + 70 * DAY_MS, 11.9, 12.0, 11.8, 11.9))  # новый ATH
    candles.append(ac(T0 + 71 * DAY_MS, 11.8, 11.8, 2.3, 3.0))    # deep drop #2
    for k in range(gap_after_drop2):                          # плоско, без pivots
        candles.append(ac(T0 + (72 + k) * DAY_MS, 3.0, 3.0, 3.0, 3.0))
    candles += osc_candles(140, 3.4, 0.4, 72 + gap_after_drop2)
    return candles


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


def fresh_db() -> Database:
    d = Database(":memory:")
    d.upsert_alt_asset(AltAsset(id=None, cmc_id=101, symbol="TST", name="Test"))
    d.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="bybit", symbol="TSTUSDT",
        earliest_available_ms=T0, history_scope="full",
    ))
    return d


def episode_tuple(e) -> tuple:
    """Содержательная проекция эпизода для сравнения прогонов."""
    return (
        e.origin_key, e.state, e.anchor_start_open_time,
        round(e.lower, 9), round(e.upper, 9), round(e.mid, 9),
        round(e.width, 9),
        None if e.wick_low is None else round(e.wick_low, 9),
        None if e.wick_high is None else round(e.wick_high, 9),
    )


# ---------------------------------------------------------------------------
# R-03: ATR, допуск, кластеры, независимые реакции
# ---------------------------------------------------------------------------


def test_atr_known_vectors():
    # недостаточно данных: None (первый TR требует предыдущего Close)
    assert compute_atr([ac(T0, 1, 1, 1, 1)], 14) is None
    # плоские свечи: TR = |ΔClose|
    closes = [1.0, 2.0, 3.0, 2.0]
    candles = [ac(T0 + i * DAY_MS, c, c, c, c) for i, c in enumerate(closes)]
    assert compute_atr(candles, 3) == pytest.approx(1.0)      # TR: 1,1,1
    assert compute_atr(candles, 2) == pytest.approx(1.0)
    # гэп: TR = max(H−L, |H−C_prev|, |L−C_prev|)
    gap = [
        ac(T0, 1.0, 1.0, 1.0, 1.0),
        ac(T0 + DAY_MS, 4.5, 5.0, 4.0, 4.5),                   # TR = max(1,4,3)=4
        ac(T0 + 2 * DAY_MS, 4.5, 4.6, 4.4, 4.5),               # TR = 0.2
    ]
    assert compute_atr(gap, 2) == pytest.approx((4.0 + 0.2) / 2)
    # чистота: повторный вызов на тех же данных — то же значение
    assert compute_atr(candles, 3) == compute_atr(list(candles), 3)


def test_cluster_tolerance_uses_max_of_atr_and_pct(alt_cfg):
    tol = cluster_tolerance(0.1, 100.0, alt_cfg)
    assert tol == pytest.approx(max(0.5 * 0.1, 0.02 * 100.0))  # pct доминирует
    tol2 = cluster_tolerance(10.0, 1.0, alt_cfg)
    assert tol2 == pytest.approx(5.0)                          # ATR доминирует


def test_cluster_pivots_tolerance_and_median_line():
    pivots = [pv(1.00, 0), pv(1.03, 20), pv(1.50, 40)]
    clusters = cluster_pivots(pivots, tol=0.05)
    assert len(clusters) == 2
    assert clusters[0].price == pytest.approx(1.015)   # медиана, не минимум
    assert clusters[1].price == pytest.approx(1.50)
    # одиночный выброс не двигает линию основной зоны (R-03)
    pivots2 = [pv(1.00, 0), pv(1.04, 20), pv(1.02, 40), pv(1.50, 15)]
    clusters2 = cluster_pivots(pivots2, tol=0.05)
    main = max(clusters2, key=lambda c: len(c.pivots))
    assert main.price == pytest.approx(1.02)
    assert len(main.pivots) == 3


def test_one_prolonged_contact_counts_once(alt_cfg):
    gap_ms = alt_cfg.v2_min_reaction_gap_days * DAY_MS
    # три касания за 6 дней — ОДНА реакция; через разрыв ≥10 дней — вторая
    pivots = [pv(1.0, 0), pv(1.01, 3), pv(1.0, 6), pv(1.02, 16)]
    assert count_independent_reactions(pivots, gap_ms) == 2
    # возврат за середину между касаниями делает их независимыми (R-03)
    candles = [ac(T0 + d * DAY_MS, 2.0, 2.0, 2.0, 2.0) for d in range(1, 6)]
    close_pivots = [pv(1.0, 0), pv(1.01, 3)]
    assert count_independent_reactions(
        close_pivots, gap_ms, candles=candles, midline=1.5, side="low"
    ) == 2
    assert count_independent_reactions(close_pivots, gap_ms) == 1


def test_choose_lu_pair_ignores_single_wick_and_history_extremes(alt_cfg):
    # боковик 1.0..2.0 (синус) + одиночный pivot high 3.0 (тень)
    candles = osc_candles(100, 1.5, 0.5, 0)
    lows = cluster_pivots([pv(1.0, 5), pv(1.02, 25), pv(1.01, 45), pv(1.0, 65)],
                          tol=0.05)
    highs = cluster_pivots(
        [pv(2.0, 15), pv(2.01, 35), pv(1.99, 55), pv(2.0, 75), pv(3.0, 18)],
        tol=0.05,
    )
    atr = compute_atr(candles, alt_cfg.v2_atr_period)
    pair = choose_lu_pair(lows, highs, candles, alt_cfg, atr)
    assert pair is not None
    assert pair.upper == pytest.approx(2.0)   # НЕ одиночная тень 3.0
    assert pair.lower == pytest.approx(1.005)  # медиана 1.0,1.0,1.01,1.02
    assert pair.reactions_lower >= alt_cfg.v2_min_reactions
    assert pair.reactions_upper >= alt_cfg.v2_min_reactions
    assert pair.crossings >= 1
    # без повторных реакций у верха пары нет вовсе
    highs_single = cluster_pivots([pv(2.0, 15)], tol=0.05)
    assert choose_lu_pair(lows, highs_single, candles, alt_cfg, atr) is None


# ---------------------------------------------------------------------------
# R-04: классификатор v2 — качество независимо от ширины
# ---------------------------------------------------------------------------


def test_classifier_v2_not_ready_cases(alt_cfg):
    r = classify_sideways_v2(osc_candles(10, 1.5, 0.4, 0), 1.1, 1.9, alt_cfg)
    assert not r.ready and r.reason == "not_enough_data"
    r = classify_sideways_v2(osc_candles(120, 1.5, 0.4, 0), 1.9, 1.1, alt_cfg)
    assert not r.ready and r.reason == "zero_width"


def test_classifier_v2_width_independence_same_shape(alt_cfg):
    """Одна и та же форма при разных масштабах цены — одинаковый вердикт
    (нормировка на ATR, а не на ширину; ширина сама оценку не улучшает)."""
    base = osc_candles(120, 1.5, 0.4, 0)
    scaled = [ac(c.open_time, c.open * 3, c.high * 3, c.low * 3, c.close * 3)
              for c in base]
    r1 = classify_sideways_v2(base, 1.1, 1.9, alt_cfg)
    r2 = classify_sideways_v2(scaled, 3.3, 5.7, alt_cfg)
    assert r1.ready and r1.sideways
    assert r2.ready and r2.sideways
    assert r1.metrics["width_atr_mult"] == pytest.approx(
        r2.metrics["width_atr_mult"], rel=1e-6
    )
    assert r1.metrics["center_shift_atr"] == pytest.approx(
        r2.metrics["center_shift_atr"], rel=1e-6
    )


def test_classifier_v2_huge_width_does_not_pass(alt_cfg):
    candles = osc_candles(120, 1.5, 0.4, 0)
    r = classify_sideways_v2(candles, 0.5, 5.0, alt_cfg)
    assert r.ready and not r.sideways
    assert "width_too_wide" in r.failed_conditions
    # дегенеративно узкая рамка тоже отсекается
    r2 = classify_sideways_v2(candles, 1.49, 1.51, alt_cfg)
    assert not r2.sideways
    assert "width_too_narrow" in r2.failed_conditions


def test_classifier_v2_single_wick_does_not_create_upper(alt_cfg):
    candles = osc_candles(120, 1.5, 0.4, 0)
    wick_day = 80
    c0 = candles[wick_day]
    candles[wick_day] = ac(c0.open_time, c0.open, 3.0, c0.low, c0.close)
    # U по одиночной тени: реакций у верха нет, тень не образует U (R-03/R-04)
    r = classify_sideways_v2(candles, 1.1, 3.0, alt_cfg)
    assert r.ready and not r.sideways
    assert "reactions_upper" in r.failed_conditions
    assert "wick_concentration" in r.failed_conditions
    # та же тень ВНЕ правильных границ качество основной зоны не ломает
    r2 = classify_sideways_v2(candles, 1.1, 1.9, alt_cfg)
    assert r2.ready and r2.sideways


def test_classifier_v2_rejects_drift(alt_cfg):
    # затухающий, но устойчивый дрейф вниз — не боковик (§8.6)
    candles: list[AltCandle] = []
    for t in range(150):
        v = 1.5 + 0.9 * math.exp(-t / 60) - 0.02 * math.sin(2 * math.pi * t / 20)
        candles.append(ac(T0 + t * DAY_MS, v, v, v, v))
    r = classify_sideways_v2(candles, 1.4, 2.5, alt_cfg)
    assert r.ready and not r.sideways
    assert {"center_shift", "efficiency"} & set(r.failed_conditions)


# ---------------------------------------------------------------------------
# Оркестратор: freeze по реакциям, тени отдельно, R-09/R-10
# ---------------------------------------------------------------------------


def test_engine_freeze_median_bounds_wicks_separate(alt_db, alt_cfg):
    eng = AltEngineV2(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=106, spike_at_abs=80, spike_high=2.0)
    s = eng.process_asset_history(1, 1, candles)
    assert s["state"] == "mature"
    assert s["rules_version"] == ALT_RULE_VERSION_V2
    rows = alt_db.list_alt_range_episodes(1)
    assert len(rows) == 1
    ep = rows[0]
    assert ep.state == AltEpisodeState.MATURE.value
    assert ep.rules_version == ALT_RULE_VERSION_V2
    # L/U — медианы кластеров реакций (~1.1/1.9), а не min/max участка;
    # одиночная тень 2.0 — отдельно в wick_high, U не образует (R-03)
    assert ep.lower == pytest.approx(1.1, abs=0.05)
    assert ep.upper == pytest.approx(1.9, abs=0.05)
    assert ep.wick_high == 2.0
    assert ep.width == pytest.approx(ep.upper - ep.lower)
    assert ep.mid == pytest.approx((ep.lower + ep.upper) / 2)
    assert ep.base_end_open_time is None and ep.base_end_reason is None
    q = json.loads(ep.quality_json)
    assert q["pair"]["reactions_lower"] >= alt_cfg.v2_min_reactions
    assert q["pair"]["reactions_upper"] >= alt_cfg.v2_min_reactions
    assert q["classifier"]["classifier_version"] == "v2"
    assert "width_atr_mult" in q["classifier"]["metrics"]
    assert ep.selection_rank_reason == "mature_quality_ok"
    assert ep.origin_key == f"v2:1:1:{ep.anchor_start_open_time}"


def test_engine_multi_candidate_after_impulse(alt_db, alt_cfg):
    """После импульсного выхода вторая консолидация — НОВЫЙ кандидат со
    своими якорями и возрастом (R-01/R-02, NEAR/HBAR-паттерн)."""
    eng = AltEngineV2(alt_db, alt_cfg)
    s = eng.process_asset_history(1, 1, build_two_bases())
    assert s["state"] == "mature"
    # старый цикл завершился подтверждённым выходом (ралли), новый — зрелый;
    # оба — замороженные эпизоды со своей геометрией (R-01/R-07)
    frozen = [e for e in alt_db.list_alt_range_episodes(1)
              if e.state in (AltEpisodeState.MATURE.value,
                             AltEpisodeState.ACCOMPANIMENT.value,
                             AltEpisodeState.TERMINAL.value)]
    assert len(frozen) == 2
    old, new = frozen  # ORDER BY anchor_start_open_time
    assert old.origin_key != new.origin_key
    assert new.anchor_start_open_time > old.anchor_start_open_time
    assert old.lower == pytest.approx(1.1, abs=0.05)
    assert old.upper == pytest.approx(1.9, abs=0.05)
    assert new.lower == pytest.approx(3.6, abs=0.05)
    assert new.upper == pytest.approx(4.4, abs=0.05)
    # выход/импульс не расширил замороженную старую базу (R-09)
    assert old.wick_high == pytest.approx(1.9, abs=0.05)
    # возраст ранней базы на позднюю не переносится (R-02)
    q_new = json.loads(new.quality_json)
    assert q_new["n_days"] == 101


def test_admission_window_expiry_and_rearm(alt_db):
    """R-01: после истечения окна допуска кандидат не создаётся; глубокая
    просадка НОВОГО ATH-эпизода перевзводит допуск без нового окна ожидания."""
    cfg = AltConfig(v2_admission_window_days=10)
    # боковик сразу после просадки #2 — допуск действует (перевзведён)
    eng = AltEngineV2(alt_db, cfg)
    s = eng.process_asset_history(1, 1, build_rearm(gap_after_drop2=0))
    rows = alt_db.list_alt_range_episodes(1)
    assert s["state"] == "mature" and len(rows) == 1
    # якорь — после второй просадки (abs 71), не после первой (abs 5)
    assert rows[0].anchor_start_open_time > T0 + 71 * DAY_MS

    # 30 плоских дней после просадки #2: окно (10 дней) истекло — база без
    # допуска, зрелого эпизода нет (отрицательный пример R-01)
    db2 = fresh_db()
    eng2 = AltEngineV2(db2, cfg)
    s2 = eng2.process_asset_history(1, 1, build_rearm(gap_after_drop2=30))
    assert s2["state"] == "searching"
    assert db2.list_alt_range_episodes(1) == []
    db2.close()

    # то же с широким окном по умолчанию — допуск ещё действует
    db3 = fresh_db()
    eng3 = AltEngineV2(db3, AltConfig())
    s3 = eng3.process_asset_history(1, 1, build_rearm(gap_after_drop2=30))
    assert s3["state"] == "mature"
    assert len(db3.list_alt_range_episodes(1)) == 1
    db3.close()


# ---------------------------------------------------------------------------
# §8.6: отрицательные случаи — зрелой базы нет
# ---------------------------------------------------------------------------


def build_monotonic_decline() -> list[AltCandle]:
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    for t in range(150):                                    # строго вниз
        v = 1.9 - t * 0.008
        candles.append(ac(T0 + (6 + t) * DAY_MS, v, v, v, v))
    return candles


def build_single_bounce() -> list[AltCandle]:
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    for t in range(150):  # единичный V-отскок и уход вверх без консолидации
        v = 1.6 + abs(t - 10) * 0.05
        candles.append(ac(T0 + (6 + t) * DAY_MS, v, v, v, v))
    return candles


def build_flat_with_one_shadow() -> list[AltCandle]:
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    for t in range(150):
        if t == 80:  # одна большая тень на плоском участке
            candles.append(ac(T0 + (6 + t) * DAY_MS, 1.5, 3.0, 1.5, 1.5))
        else:
            candles.append(ac(T0 + (6 + t) * DAY_MS, 1.5, 1.5, 1.5, 1.5))
    return candles


def build_unconfirmed_sweep() -> list[AltCandle]:
    """60 дней боковика, затем уход вниз без возврата и без новой
    консолидации: вынос без подтверждения зрелую базу не создаёт."""
    candles: list[AltCandle] = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(T0 + i * DAY_MS, p, p, p, p))
    candles.append(ac(T0 + 5 * DAY_MS, 9.9, 9.9, 1.9, 2.5))
    candles += osc_candles(60, 1.5, 0.4, 6)
    for t in range(200):                                    # затяжной уход вниз
        v = 1.3 - t * 0.005
        candles.append(ac(T0 + (66 + t) * DAY_MS, v, v, v, v))
    return candles


@pytest.mark.parametrize(
    "builder",
    [build_monotonic_decline, build_single_bounce,
     build_flat_with_one_shadow, build_unconfirmed_sweep],
    ids=["monotonic_decline", "single_bounce",
         "one_big_shadow", "unconfirmed_sweep"],
)
def test_negative_cases_no_mature_episode(alt_cfg, builder):
    db = fresh_db()
    eng = AltEngineV2(db, alt_cfg)
    s = eng.process_asset_history(1, 1, builder())
    mature = [e for e in db.list_alt_range_episodes(1)
              if e.state == AltEpisodeState.MATURE.value]
    assert mature == [], f"{builder.__name__}: {mature}"
    assert s["state"] != "mature"
    db.close()


# ---------------------------------------------------------------------------
# R-10: детерминизм, идемпотентность, префиксное свойство; R-09
# ---------------------------------------------------------------------------


def test_determinism_and_idempotent_rerun(alt_cfg):
    candles = build_two_bases()
    db1, db2 = fresh_db(), fresh_db()
    s1 = AltEngineV2(db1, alt_cfg).process_asset_history(1, 1, candles)
    s2 = AltEngineV2(db2, alt_cfg).process_asset_history(1, 1, candles)
    eps1 = [episode_tuple(e) for e in db1.list_alt_range_episodes(1)]
    eps2 = [episode_tuple(e) for e in db2.list_alt_range_episodes(1)]
    assert eps1 == eps2  # полный replay детерминирован

    # повторный прогон в ту же БД: ничего не создаётся (R-10)
    n_before = len(db1.list_alt_range_episodes(1))
    s3 = AltEngineV2(db1, alt_cfg).process_asset_history(1, 1, candles)
    assert len(db1.list_alt_range_episodes(1)) == n_before
    assert s3["episodes"], "ожидались freeze-переходы в журнале"
    assert all(not e["created"] for e in s3["episodes"])
    assert [episode_tuple(e) for e in db1.list_alt_range_episodes(1)] == eps1
    db1.close()
    db2.close()


FROZEN_EPISODE_STATES = (
    AltEpisodeState.MATURE.value,
    AltEpisodeState.ACCOMPANIMENT.value,
    AltEpisodeState.TERMINAL.value,
    AltEpisodeState.DECAYED.value,
)


def test_prefix_property(alt_cfg):
    """R-10: результат на префиксе == полный прогон, усечённый по дате
    доступности: замороженные эпизоды префикса — подмножество полного
    прогона с той же геометрией (включая факты жизненного цикла)."""
    candles = build_two_bases()
    full_db = fresh_db()
    AltEngineV2(full_db, alt_cfg).process_asset_history(1, 1, candles)
    full = {e.origin_key: episode_tuple(e) for e in
            full_db.list_alt_range_episodes(1)
            if e.state in FROZEN_EPISODE_STATES}
    assert len(full) == 2

    expected_frozen = {100: 0, 150: 1, 250: 1, len(candles): 2}
    for k, n_frozen in expected_frozen.items():
        db = fresh_db()
        AltEngineV2(db, alt_cfg).process_asset_history(1, 1, candles[:k])
        pref = {e.origin_key: episode_tuple(e) for e in
                db.list_alt_range_episodes(1)
                if e.state in FROZEN_EPISODE_STATES}
        assert len(pref) == n_frozen, f"prefix {k}: {pref}"
        for key, tup in pref.items():
            assert full[key] == tup, f"prefix {k}: геометрия {key} изменилась"
        db.close()
    full_db.close()


def test_frozen_geometry_immutable_on_rerun(alt_db, alt_cfg):
    """R-09: после freeze L/U/M/W и тени неизменны — поздний вынос на
    догоняющем прогоне замороженную геометрию не переписывает."""
    eng = AltEngineV2(alt_db, alt_cfg)
    candles = build_accumulation(seg_len=106)
    s1 = eng.process_asset_history(1, 1, candles)
    ep1 = alt_db.list_alt_range_episodes(1)[0]
    snap = episode_tuple(ep1)

    post = candles + [
        ac(T0 + 112 * DAY_MS, 1.6, 2.5, 1.5, 1.7),   # вынос-тень вверх после freeze
        ac(T0 + 113 * DAY_MS, 1.7, 1.8, 0.8, 1.6),   # глубокая тень вниз с возвратом
    ]
    eng.process_asset_history(1, 1, post)
    rows = alt_db.list_alt_range_episodes(1)
    assert len(rows) == 1
    assert episode_tuple(rows[0]) == snap
    assert rows[0].id == ep1.id


def test_forming_episode_decays_instead_of_maturing(alt_db, alt_cfg):
    """Распавшаяся до зрелости консолидация не зреет задним числом:
    формирующаяся строка переводится в decayed с причиной (R-02)."""
    eng = AltEngineV2(alt_db, alt_cfg)
    s = eng.process_asset_history(1, 1, build_unconfirmed_sweep())
    rows = alt_db.list_alt_range_episodes(1)
    assert all(e.state != AltEpisodeState.MATURE.value for e in rows)
    if rows:  # если формирующийся эпизод успел сохраниться — он распался
        assert all(e.state == AltEpisodeState.DECAYED.value for e in rows)
        q = json.loads(rows[0].quality_json)
        assert "rejected_reason" in q
    assert s["state"] != "mature"


# ---------------------------------------------------------------------------
# Смоук-тесты на эталонных фикстурах этапа 1 (полная приёмка — этап 6)
# ---------------------------------------------------------------------------


def _load_etalon(ticker: str) -> list[AltCandle]:
    rows = json.loads(
        (FIXTURES / "ohlc" / f"{ticker}.json").read_text(encoding="utf-8")
    )
    return [
        AltCandle(source_id=1, open_time=r["open_time"], open=r["open"],
                  high=r["high"], low=r["low"], close=r["close"])
        for r in rows
    ]


def _run_etalon(ticker: str) -> tuple[dict, list]:
    db = fresh_db()
    eng = AltEngineV2(db, AltConfig())
    s = eng.process_asset_history(1, 1, _load_etalon(ticker))
    rows = db.list_alt_range_episodes(1)
    db.close()
    return s, rows


def test_smoke_etalon_near_two_cycles():
    """СМОУК (этап 6 — полная калибровка): NEAR даёт отдельные эпизоды для
    старого и нового циклов (R-01): замороженные эпизоды с якорями и в
    2022–2024, и в 2025–2026, циклы не сливаются в одну рамку."""
    markup = json.loads((FIXTURES / "markup.json").read_text(encoding="utf-8"))
    assert len(markup["tickers"]["NEAR"]["episodes"]) >= 2
    s, rows = _run_etalon("NEAR")
    frozen = [e for e in rows if e.state in FROZEN_EPISODE_STATES]
    assert len(frozen) >= 2, [
        (e.state, e.anchor_start_open_time, e.lower, e.upper) for e in rows
    ]
    y2024 = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    y2025 = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    ms = lambda dt: int(dt.timestamp() * 1000)
    assert any(e.anchor_start_open_time < ms(y2024) for e in frozen)
    assert any(e.anchor_start_open_time >= ms(y2025) for e in frozen)
    anchors = {e.anchor_start_open_time for e in frozen}
    assert len(anchors) == len(frozen)  # отдельные якоря у отдельных эпизодов


def test_smoke_etalon_pump_lower_in_markup_interval():
    """СМОУК: у PUMP есть эпизод с L в интервале разметки 0.0016–0.0018
    (нижняя опорная зона по повторным реакциям, R-03), летний вынос вниз
    хранится отдельными эпизодами выноса (R-05), и ни один эпизод не берёт U
    по абсолютному историческому максимуму (ATH 0.00898). Точная калибровка
    уровня U (разметка 0.0033–0.0035) и длительности принятия ниже L —
    этап 6."""
    s, rows = _run_etalon("PUMP")
    frozen = [e for e in rows if e.state in FROZEN_EPISODE_STATES]
    assert frozen, "ожидался хотя бы один замороженный эпизод"
    assert any(0.0016 <= e.lower <= 0.0018 for e in frozen), [
        (e.state, round(e.lower, 6), round(e.upper, 6)) for e in rows
    ]
    assert all(e.upper < 0.00898 for e in frozen)
    # летний вынос 2026 — отдельный факт своего эпизода (alt_sweep_episode)
    db_rows_with_sweeps = 0
    db = fresh_db()
    eng = AltEngineV2(db, AltConfig())
    eng.process_asset_history(1, 1, _load_etalon("PUMP"))
    for e in db.list_alt_range_episodes(1):
        if db.list_alt_sweep_episodes(e.id):
            db_rows_with_sweeps += 1
    db.close()
    assert db_rows_with_sweeps >= 1
