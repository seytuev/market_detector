"""Тесты жизненного цикла движка v2 post-freeze (этап 5 ТЗ от 07.10.2026).

Покрытие: выносы вниз R-05 (open → return_pending → returned |
accepted_below; возврат — закрытием; K = 2L − U приоритетна и не скрывается
выносом; K ≤ 0 недостижима; замороженный L не расширяется), выход и ретест
R-06 (тень ≠ выход; Close > U·(1+tol) — выход; ретест [M, U] — факт своего
эпизода), конец заливки отделён от сопровождения R-07 (base_end_* у свечи
выхода/распада, accompaniment_end при терминальных условиях, повторный
вход не открывает старую заливку), цели TP_n = U + n·W и K по собственной
геометрии эпизода (R-09), независимость параллельных эпизодов (R-01),
префиксное свойство и идемпотентность replay (R-10).
"""
from __future__ import annotations

import json
import math

import pytest

from app.alt.engine_v2 import AltEngineV2
from app.config import AltConfig
from app.models import close_boundary_ms
from app.models_alt import AltEpisodeState, AltSweepState
from tests.test_alt_engine import T0, ac
from tests.test_alt_engine_v2 import (
    FROZEN_EPISODE_STATES,
    build_two_bases,
    episode_tuple,
    fresh_db,
    osc_candles,
)

DAY_MS = 86_400_000


def osc_d(t: int, mid: float, amp: float, per: int = 20) -> float:
    """Синус с ЗАТУХАЮЩИМ джиттером: поздние экстремумы мягче ранних —
    после freeze трены не пересекают замороженные L/U (нет микро-выносов
    и ложных выходов на тестовых данных)."""
    return mid - amp * math.sin(2 * math.pi * t / per) * (1 - 1e-9 * (t + 1))


def osc_candles_d(n: int, mid: float, amp: float, start_abs: int,
                  per: int = 20) -> list:
    return [
        ac(T0 + (start_abs + t) * DAY_MS, (v := osc_d(t, mid, amp, per)), v, v, v)
        for t in range(n)
    ]


def ot(abs_day: int) -> int:
    return T0 + abs_day * DAY_MS


def boundary(abs_day: int) -> int:
    return close_boundary_ms(ot(abs_day), "D1")


def build_base(n: int = 140, mid: float = 1.5, amp: float = 0.4):
    """ATH=10 → −81% → боковик n дней (freeze ≈ abs 111, L≈1.1, U≈1.9)."""
    candles = []
    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        candles.append(ac(ot(i), p, p, p, p))
    candles.append(ac(ot(5), 9.9, 9.9, 1.9, 2.5))   # deep drop abs 5
    candles += osc_candles_d(n, mid, amp, 6)
    return candles


def build_sweep_return():
    """База, затем вынос на 3 свечи с возвратом закрытием (abs 146-148)."""
    return build_base() + [
        ac(ot(146), 1.4, 1.45, 0.95, 1.05),   # low < L, close < L → вынос
        ac(ot(147), 1.05, 1.1, 0.90, 1.02),   # новый минимум 0.90
        ac(ot(148), 1.02, 1.35, 1.00, 1.30),  # close > L → возврат подтверждён
    ]


def build_accepted_below():
    """База, затем 35 дней ниже L без возврата → accepted_below (abs 175)."""
    return build_base() + [
        ac(ot(146 + k), 0.95, 1.0, 0.90, 0.95) for k in range(35)
    ]


def build_breakout():
    """База → тень выше U (abs 146) → выход закрытием (abs 147) → цели,
    ретест, завершение сопровождения (abs 150)."""
    return build_base() + [
        ac(ot(146), 1.7, 2.5, 1.6, 1.7),    # тень выше U, close внутри — экскурсия
        ac(ot(147), 1.8, 2.25, 1.75, 2.2),  # close 2.2 > U=1.9 → выход
        ac(ot(148), 2.3, 3.1, 2.25, 2.9),   # high ≥ TP1=2.7
        ac(ot(149), 2.0, 2.05, 1.85, 1.95),  # ретест [M, U]: low ≤ U, high ≥ M
        ac(ot(150), 3.0, 5.5, 2.95, 5.2),   # high ≥ TP2..TP4 → targets_completed
    ]


def lifecycle_of(ep) -> list[dict]:
    return json.loads(ep.quality_json).get("lifecycle", [])


def kinds_of(ep) -> list[str]:
    return [e["kind"] for e in lifecycle_of(ep)]


# ---------------------------------------------------------------------------
# R-05: выносы вниз
# ---------------------------------------------------------------------------


def test_sweep_open_return_pending_returned():
    db = fresh_db()
    eng = AltEngineV2(db, AltConfig())
    candles = build_sweep_return()

    # префикс на свече ухода: вынос открыт, возврат не подтверждён
    eng.process_asset_history(1, 1, candles[:147])
    ep = db.list_alt_range_episodes(1)[0]
    sw = db.list_alt_sweep_episodes(ep.id)[0]
    assert sw.state == AltSweepState.OPEN.value
    assert sw.start_open_time == ot(146)
    assert sw.min_price == 0.95 and sw.min_open_time == ot(146)
    assert not sw.return_confirmed and sw.end_open_time is None
    assert ep.state == AltEpisodeState.MATURE.value

    # префикс на второй свече: «выход ниже, возврат не подтверждён»
    db2 = fresh_db()
    AltEngineV2(db2, AltConfig()).process_asset_history(1, 1, candles[:148])
    ep2 = db2.list_alt_range_episodes(1)[0]
    sw2 = db2.list_alt_sweep_episodes(ep2.id)[0]
    assert sw2.state == AltSweepState.RETURN_PENDING.value
    assert sw2.min_price == 0.90 and sw2.min_open_time == ot(147)
    assert not sw2.return_confirmed
    db2.close()

    # полный прогон: возврат подтверждён ЗАКРЫТИЕМ abs 148
    s = eng.process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    sw = db.list_alt_sweep_episodes(ep.id)[0]
    assert sw.state == AltSweepState.RETURNED.value
    assert sw.end_open_time == ot(148)
    assert sw.return_confirmed is True
    assert sw.return_confirmed_at_ms == boundary(148)
    # замороженная геометрия не расширена выносом (R-05/R-09)
    assert ep.lower == pytest.approx(1.1, abs=0.05)
    assert ep.upper == pytest.approx(1.9, abs=0.05)
    assert ep.state == AltEpisodeState.MATURE.value
    assert ep.base_end_open_time is None
    kinds = kinds_of(ep)
    assert kinds == ["sweep_started", "sweep_returned"]
    ret = lifecycle_of(ep)[1]
    assert ret["min_price"] == 0.90 and ret["days_below"] == 3
    db.close()


def test_sweep_return_requires_close_above_L():
    """Равенство Close == L — не возврат (конвенция v1); тень выше L при
    Close ниже — тоже не подтверждение."""
    db = fresh_db()
    candles = build_base() + [
        ac(ot(146), 1.4, 1.45, 0.95, 1.05),
        ac(ot(147), 1.05, 1.5, 1.0, 1.05),   # high > L, но close < L — внизу
        ac(ot(148), 1.05, 1.15, 1.0, 1.05),  # close < L
    ]
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    sw = db.list_alt_sweep_episodes(ep.id)[0]
    assert sw.state == AltSweepState.RETURN_PENDING.value
    assert not sw.return_confirmed
    assert ep.state == AltEpisodeState.MATURE.value
    db.close()


def test_accepted_below_decays_base_and_grounds_new_candidate():
    """Принятие ниже L (≥ v2_sweep_max_days) → распад старой базы без
    расширения геометрии; новая консолидация ниже — НОВЫЙ эпизод
    (PUMP/ENA-паттерн летнего выноса, R-01/R-05/R-07)."""
    # Фикстура построена на пороге 30 дней (35 дней ниже L, новая база с abs 181).
    # Дефолт AltConfig длиннее — он калибруется на возвратных выносах этапа 6
    # и этот сценарий не должен от него зависеть.
    cfg = AltConfig(v2_sweep_max_days=30)
    db = fresh_db()
    candles = build_accepted_below()
    # консолидация на новом уровне после принятия (abs 181..320)
    candles += osc_candles(140, 0.9, 0.08, 181)
    AltEngineV2(db, cfg).process_asset_history(1, 1, candles)
    rows = db.list_alt_range_episodes(1)
    old = rows[0]
    assert old.state == AltEpisodeState.DECAYED.value
    assert old.base_end_reason == "decay"
    # заливка завершается у свечи ухода, подтверждение — у свечи принятия
    assert old.base_end_open_time == ot(146)
    assert old.base_end_confirmed_at_ms == boundary(146 + 30 - 1)
    assert old.accompaniment_end_open_time == ot(146 + 30 - 1)
    # геометрия старой базы не расширена вниз (R-05/R-09)
    assert old.lower == pytest.approx(1.1, abs=0.05)
    assert old.wick_low == pytest.approx(1.1, abs=0.05)
    sw = db.list_alt_sweep_episodes(old.id)[0]
    assert sw.state == AltSweepState.ACCEPTED_BELOW.value
    assert sw.end_open_time == ot(146 + 30 - 1)
    assert sw.min_price == 0.90 and not sw.return_confirmed
    kinds = kinds_of(old)
    assert kinds == ["sweep_started", "sweep_accepted_below"]

    # новая база на уровне принятия — отдельный зрелый эпизод со своим якорем
    new = [e for e in rows[1:] if e.state in FROZEN_EPISODE_STATES]
    assert len(new) == 1, [(e.state, e.lower, e.upper) for e in rows]
    new = new[0]
    assert new.id != old.id and new.origin_key != old.origin_key
    assert new.anchor_start_open_time > old.anchor_start_open_time
    assert new.lower == pytest.approx(0.82, abs=0.02)
    assert new.upper == pytest.approx(0.98, abs=0.02)
    db.close()


# ---------------------------------------------------------------------------
# R-05: отмена по K = 2L − U (формула v1 по геометрии эпизода)
# ---------------------------------------------------------------------------


def test_k_cancel_by_wick_priority_over_same_candle_return():
    """Открытый вынос + свеча с Low ≤ K и Close > L: отмена приоритетна —
    это НЕ подтверждённый возврат (R-05: нарушение K нельзя скрыть)."""
    db = fresh_db()
    candles = build_base() + [
        ac(ot(146), 1.4, 1.45, 0.95, 1.05),  # вынос открыт (K ≈ 0.3)
        ac(ot(147), 1.05, 1.55, 0.25, 1.50),  # low ≤ K И close > L
    ]
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    assert ep.state == AltEpisodeState.TERMINAL.value
    assert ep.base_end_reason == "decay"
    assert ep.base_end_open_time == ot(147)
    assert ep.base_end_confirmed_at_ms == boundary(147)
    assert ep.accompaniment_end_open_time == ot(147)
    kinds = kinds_of(ep)
    assert kinds == ["sweep_started", "cancelled"]
    sw = db.list_alt_sweep_episodes(ep.id)[0]
    assert sw.state == AltSweepState.ACCEPTED_BELOW.value  # не «возврат»
    assert not sw.return_confirmed
    q = json.loads(ep.quality_json)
    assert q["lifecycle_state"]["k_price"] == pytest.approx(
        2 * ep.lower - ep.upper
    )
    db.close()


def test_k_cancel_by_close_mode_only():
    """cancel_mode=close_on_closed_d1: тень за K не отменяет, отменяет
    только Close за K."""
    cfg = AltConfig(cancel_mode="close_on_closed_d1")
    db = fresh_db()
    candles = build_base() + [
        ac(ot(146), 1.4, 1.5, 0.25, 1.50),   # low ≤ K, close > K — не отмена
        ac(ot(147), 1.5, 1.55, 1.15, 1.55),  # внутри базы, без выноса
        ac(ot(148), 0.3, 1.0, 0.20, 0.25),   # close ≤ K → отмена
    ]
    AltEngineV2(db, cfg).process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    assert ep.state == AltEpisodeState.TERMINAL.value
    assert ep.base_end_open_time == ot(148)
    kinds = kinds_of(ep)
    # первая свеча — вынос с возвратом в том же закрытии (close > L)
    assert kinds == ["sweep_started", "sweep_returned", "cancelled"]
    db.close()


def test_k_unreachable_when_nonpositive():
    """K = 2L − U ≤ 0 → отмена недостижима (конвенция v1): глубокая тень —
    вынос с возвратом, эпизод остаётся зрелым."""
    db = fresh_db()
    candles = build_base(mid=2.0, amp=1.0) + [  # L≈1.0, U≈3.0 → K≈−1
        ac(ot(146), 1.9, 2.0, 0.01, 1.95),   # глубокая тень, close > L
    ]
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    q = json.loads(ep.quality_json)
    assert q["lifecycle_state"]["k_price"] <= 0
    assert q["lifecycle_state"]["cancel_reachable"] is False
    assert ep.state == AltEpisodeState.MATURE.value
    assert "cancelled" not in kinds_of(ep)
    assert kinds_of(ep) == ["sweep_started", "sweep_returned"]
    db.close()


# ---------------------------------------------------------------------------
# R-06/R-07: выход, ретест, цели, конец заливки и сопровождения
# ---------------------------------------------------------------------------


def test_breakout_confirmation_sequence_and_base_end():
    db = fresh_db()
    eng = AltEngineV2(db, AltConfig())
    candles = build_breakout()

    # префикс на тени выше U: экскурсия — не выход, base_end не записан
    eng.process_asset_history(1, 1, candles[:147])
    ep = db.list_alt_range_episodes(1)[0]
    assert ep.state == AltEpisodeState.MATURE.value
    assert ep.base_end_open_time is None and ep.base_end_reason is None
    assert kinds_of(ep) == ["upper_excursion"]

    # префикс на свече выхода: конец заливки — ровно у этой свечи (R-07)
    db2 = fresh_db()
    AltEngineV2(db2, AltConfig()).process_asset_history(1, 1, candles[:148])
    ep2 = db2.list_alt_range_episodes(1)[0]
    assert ep2.state == AltEpisodeState.ACCOMPANIMENT.value
    assert ep2.base_end_open_time == ot(147)
    assert ep2.base_end_reason == "breakout_confirmed"
    assert ep2.base_end_confirmed_at_ms == boundary(147)
    assert ep2.accompaniment_end_open_time is None
    db2.close()

    # полный прогон: цели/ретест/завершение сопровождения (R-06/R-07/R-09)
    eng.process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    assert ep.state == AltEpisodeState.TERMINAL.value
    assert ep.accompaniment_end_open_time == ot(150)
    assert ep.base_end_open_time == ot(147)  # не переезжает
    kinds = kinds_of(ep)
    assert kinds == [
        "upper_excursion", "breakout", "target_hit",
        "retest", "target_hit", "targets_completed",
    ]
    life = lifecycle_of(ep)
    brk = life[1]
    assert brk["close"] == 2.2 and brk["upper"] == pytest.approx(ep.upper)
    # TP_n = U + n·W по СОБСТВЕННОЙ замороженной геометрии (R-09)
    w = ep.upper - ep.lower
    assert brk["targets"] == pytest.approx(
        [ep.upper + n * w for n in range(1, 5)]
    )
    ret = life[3]
    assert ret["zone"] == {
        "lower": pytest.approx((ep.lower + ep.upper) / 2),
        "upper": pytest.approx(ep.upper),
    }
    # ретест не расширил U до вершины импульса (R-06/R-09)
    assert ep.upper == pytest.approx(1.9, abs=0.05)
    assert ep.wick_high == pytest.approx(1.9, abs=0.05)
    q = json.loads(ep.quality_json)
    hits = {t["tp"] for t in q["lifecycle_state"]["targets"] if t["hit"]}
    assert hits == {1, 2, 3, 4}
    db.close()


def test_breakout_tolerance_config():
    """v2_breakout_tol: Close выше U, но в пределах допуска — не выход."""
    base = build_base()
    wick_close = [ac(ot(146), 1.8, 1.96, 1.75, 1.95)]  # close 1.95 > U≈1.9
    db = fresh_db()
    AltEngineV2(db, AltConfig(v2_breakout_tol=0.03)).process_asset_history(
        1, 1, base + wick_close
    )
    ep = db.list_alt_range_episodes(1)[0]
    assert ep.state == AltEpisodeState.MATURE.value
    assert ep.base_end_open_time is None
    assert kinds_of(ep) == ["upper_excursion"]  # 1.95 < 1.9·1.03
    db.close()
    # без допуска тот же Close — подтверждённый выход (строго, как v1)
    db2 = fresh_db()
    AltEngineV2(db2, AltConfig(v2_breakout_tol=0.0)).process_asset_history(
        1, 1, base + wick_close
    )
    ep2 = db2.list_alt_range_episodes(1)[0]
    assert ep2.state == AltEpisodeState.ACCOMPANIMENT.value
    assert ep2.base_end_open_time == ot(146)
    db2.close()


def test_reentry_after_completed_base_opens_new_episode():
    """Повторный вход в диапазон завершённой базы старую заливку НЕ
    открывает: новая консолидация у тех же цен — новый эпизод (R-01/R-07)."""
    db = fresh_db()
    candles = build_breakout()
    candles += osc_candles(140, 1.5, 0.39, 151)   # консолидация у старых цен
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    rows = db.list_alt_range_episodes(1)
    old = rows[0]
    assert old.state == AltEpisodeState.TERMINAL.value
    assert old.accompaniment_end_open_time == ot(150)  # не переоткрыта
    new = [e for e in rows[1:] if e.state in FROZEN_EPISODE_STATES]
    assert len(new) == 1, [(e.state, e.lower, e.upper) for e in rows]
    new = new[0]
    assert new.origin_key != old.origin_key
    assert new.anchor_start_open_time >= ot(151)
    assert new.lower == pytest.approx(1.11, abs=0.05)
    assert new.upper == pytest.approx(1.89, abs=0.05)
    db.close()


# ---------------------------------------------------------------------------
# R-01: параллельные эпизоды не смешиваются
# ---------------------------------------------------------------------------


def test_two_episodes_evolve_independently():
    """Старая база в сопровождении + новая формирующаяся/зрелая: факты
    жизненного цикла ссылаются на свой эпизод (NEAR/HBAR-паттерн)."""
    db = fresh_db()
    candles = build_two_bases()
    # вынос ниже L второй базы (3.6) с возвратом (abs 301-303)
    candles += [
        ac(ot(301), 3.5, 3.55, 3.30, 3.45),
        ac(ot(302), 3.45, 3.50, 3.25, 3.40),
        ac(ot(303), 3.40, 3.70, 3.35, 3.65),  # close > 3.6? close=3.65 > L
    ]
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    rows = [e for e in db.list_alt_range_episodes(1)
            if e.state in FROZEN_EPISODE_STATES]
    assert len(rows) == 2
    old, new = rows
    # у каждого эпизода — свои факты: выход старого при ралли, вынос abs 301
    # — только у нового и только под её L (минимумы разного масштаба)
    old_sweeps = db.list_alt_sweep_episodes(old.id)
    new_sweeps = db.list_alt_sweep_episodes(new.id)
    assert all(s.min_price < 1.5 for s in old_sweeps)
    assert all(s.min_price > 3.0 for s in new_sweeps)
    target = [s for s in new_sweeps if s.start_open_time == ot(301)]
    assert len(target) == 1, [(s.start_open_time, s.min_price)
                              for s in new_sweeps]
    sw = target[0]
    assert sw.episode_id == new.id
    assert sw.min_price == 3.25 and sw.min_open_time == ot(302)
    assert sw.state == AltSweepState.RETURNED.value
    assert sw.return_confirmed_at_ms == boundary(303)
    # геометрия и конец заливки старого эпизода выносом нового не затронуты
    assert old.lower == pytest.approx(1.1, abs=0.05)
    assert old.upper == pytest.approx(1.9, abs=0.05)
    assert old.base_end_reason == "breakout_confirmed"
    assert new.lower == pytest.approx(3.6, abs=0.05)  # вынос не расширил L
    # цели старой базы посчитаны по ЕЁ ширине, новой — по её собственной
    q_old = json.loads(old.quality_json)["lifecycle_state"]
    q_new = json.loads(new.quality_json)["lifecycle_state"]
    assert q_old["k_price"] == pytest.approx(2 * old.lower - old.upper)
    assert q_new["k_price"] == pytest.approx(2 * new.lower - new.upper)
    db.close()


# ---------------------------------------------------------------------------
# R-10: идемпотентность replay жизненного цикла
# ---------------------------------------------------------------------------


def test_lifecycle_replay_idempotent():
    """Повторный прогон не дублирует строки выносов и факты; два независимых
    replay дают идентичные эпизоды и quality_json."""
    for scenario in (build_accepted_below(), build_breakout()):
        db1, db2 = fresh_db(), fresh_db()
        AltEngineV2(db1, AltConfig()).process_asset_history(1, 1, scenario)
        AltEngineV2(db2, AltConfig()).process_asset_history(1, 1, scenario)
        eps1 = db1.list_alt_range_episodes(1)
        eps2 = db2.list_alt_range_episodes(1)
        assert [episode_tuple(e) for e in eps1] == [episode_tuple(e) for e in eps2]
        assert [e.quality_json for e in eps1] == [e.quality_json for e in eps2]
        # второй прогон в ту же БД: ни новых эпизодов, ни новых выносов
        sweeps_before = sum(
            len(db1.list_alt_sweep_episodes(e.id)) for e in eps1
        )
        s2 = AltEngineV2(db1, AltConfig()).process_asset_history(1, 1, scenario)
        eps3 = db1.list_alt_range_episodes(1)
        assert len(eps3) == len(eps1)
        sweeps_after = sum(
            len(db1.list_alt_sweep_episodes(e.id)) for e in eps3
        )
        assert sweeps_after == sweeps_before
        assert all(not e["created"] for e in s2["episodes"])
        assert [e.quality_json for e in eps3] == [e.quality_json for e in eps1]
        db1.close()
        db2.close()
