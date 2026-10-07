"""Приёмка движка v2 по §8 ТЗ 07.10.2026 (пункты 1–7 и 12).

Пункты 8–11 (масштаб, «Авто», W1, PNG) проверяются
tests/test_alt_chart_js.py и tools/check_alt_ui.js: это поведение графика,
не расчёта диапазонов.

Границы эталонов — допустимые интервалы разметки, не константы тикера.
Где разметка и повторные реакции расходятся (длинная база, замороженная
на 101-й день раньше внешних кластеров), тест фиксирует структурное
требование ТЗ, а не подгонку цены. Числовой отчёт — tools/alt_replay_compare.py.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from app.alt.engine import AltEngine
from app.alt.engine_v2 import AltEngineV2
from app.config import AltConfig
from app.models_alt import AltEpisodeState
from app.services.alt_overview import select_current_episode
from tests.test_alt_engine_v2 import (
    FROZEN_EPISODE_STATES,
    _load_etalon,
    _run_etalon,
    build_monotonic_decline,
    build_single_bounce,
    build_two_bases,
    fresh_db,
)
from tests.test_alt_engine_v2_lifecycle import build_base, kinds_of

DAY_MS = 86_400_000
AS_OF = datetime.datetime(2026, 10, 6, tzinfo=datetime.timezone.utc)


def utc_ms(year: int, month: int, day: int) -> int:
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _frozen(rows):
    return [e for e in rows if e.state in FROZEN_EPISODE_STATES]


def _v1_frozen_tuples(db) -> list[tuple]:
    return [
        (round(r["lower"], 8), round(r["upper"], 8), r["start_anchor_open_time"])
        for r in db.conn.execute(
            "SELECT lower, upper, start_anchor_open_time "
            "FROM alt_frozen_range ORDER BY id"
        )
    ]


def test_acceptance_near_hbar_two_cycles_old_fill_ends():
    """§8.1: два цикла, у старого заливка не тянется до октября 2026."""
    as_of = utc_ms(2026, 10, 6)
    for ticker in ("NEAR", "HBAR"):
        _summary, rows = _run_etalon(ticker)
        frozen = _frozen(rows)
        assert len(frozen) >= 2, ticker
        assert any(e.anchor_start_open_time < utc_ms(2024, 1, 1) for e in frozen)
        assert any(e.anchor_start_open_time >= utc_ms(2025, 1, 1) for e in frozen)
        anchors = {e.anchor_start_open_time for e in frozen}
        assert len(anchors) == len(frozen)
        old = [e for e in frozen if e.anchor_start_open_time < utc_ms(2024, 1, 1)]
        assert old
        # Открытая заливка — зрелая или сопровождаемая база без конца
        # до даты снимка. Распавшийся до зрелости кандидат заливкой не является.
        open_states = (
            AltEpisodeState.MATURE.value,
            AltEpisodeState.ACCOMPANIMENT.value,
        )
        still_open = [
            e for e in old
            if e.state in open_states
            and (e.base_end_open_time is None or e.base_end_open_time >= as_of)
        ]
        assert still_open == [], [
            (ticker, e.origin_key, e.state, e.base_end_open_time) for e in still_open
        ]
        newest = max(frozen, key=lambda e: e.anchor_start_open_time)
        oldest = min(frozen, key=lambda e: e.anchor_start_open_time)
        assert newest.origin_key != oldest.origin_key
        assert newest.anchor_start_open_time - oldest.anchor_start_open_time > 180 * DAY_MS


def test_acceptance_sui_ena_local_base_not_wide_envelope():
    """§8.2: локальная база 2026, а не исторический конверт v1."""
    sui_v2 = _frozen(_run_etalon("SUI")[1])
    local = [
        e for e in sui_v2
        if e.anchor_start_open_time >= utc_ms(2026, 1, 1)
        and e.upper < 2.0 and e.lower > 0.5
    ]
    assert local, [(e.anchor_start_open_time, e.lower, e.upper) for e in sui_v2]
    db = fresh_db()
    AltEngine(db, AltConfig()).process_asset_history(1, 1, _load_etalon("SUI"))
    v1 = _v1_frozen_tuples(db)
    db.close()
    assert v1 and any(upper > 3.0 for _lo, upper, _a in v1)
    assert all(e.upper < 2.0 for e in local)

    ena = _frozen(_run_etalon("ENA")[1])
    hit = [
        e for e in ena
        if 0.07 <= e.lower <= 0.10 and 0.12 <= e.upper <= 0.16
        and e.base_end_reason == "breakout_confirmed"
    ]
    assert hit, [(e.lower, e.upper, e.base_end_reason, e.state) for e in ena]
    assert hit[0].base_end_open_time >= utc_ms(2026, 8, 1)


def test_acceptance_pump_bounds_sweep_and_frozen_geometry():
    """§8.3: U в зоне повторных реакций ~0.0034, летний низ отдельно,
    выход не переписывает замороженные L/U."""
    candles = _load_etalon("PUMP")
    june = utc_ms(2026, 6, 1)
    prefix = [c for c in candles if c.open_time < june]
    db = fresh_db()
    eng = AltEngineV2(db, AltConfig())
    eng.process_asset_history(1, 1, prefix)
    early = {
        e.origin_key: (e.lower, e.upper)
        for e in _frozen(db.list_alt_range_episodes(1))
        if 0.0016 <= e.lower <= 0.0018 and 0.0032 <= e.upper <= 0.0036
    }
    assert early, "до июня база 0.0016–0.0018 / ~0.0034 ещё не заморожена"
    eng.process_asset_history(1, 1, candles)
    rows = db.list_alt_range_episodes(1)
    matched = [
        e for e in _frozen(rows)
        if e.origin_key in early
    ]
    assert matched
    ep = matched[0]
    assert (ep.lower, ep.upper) == early[ep.origin_key]
    assert ep.base_end_reason == "breakout_confirmed"
    assert utc_ms(2026, 8, 6) <= ep.base_end_open_time <= utc_ms(2026, 8, 31)
    sweeps = db.list_alt_sweep_episodes(ep.id)
    assert sweeps
    assert min(s.min_price for s in sweeps) < ep.lower
    assert ep.lower > min(s.min_price for s in sweeps)
    db.close()


def test_acceptance_aave_historical_base_is_not_current():
    """§8.4: историческая база завершена и не выбирается текущей в октябре 2026."""
    candles = _load_etalon("AAVE")
    db = fresh_db()
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    rows = db.list_alt_range_episodes(1)
    db.close()
    historical = [
        e for e in _frozen(rows)
        if e.anchor_start_open_time < utc_ms(2024, 1, 1)
        and e.base_end_open_time is not None
        and e.base_end_open_time < utc_ms(2025, 1, 1)
    ]
    assert historical
    selected, reason, _alts = select_current_episode(
        rows, candles[-1].close, candles[-1].open_time + DAY_MS
    )
    historical_ids = {e.id for e in historical}
    assert selected is None or selected.id not in historical_ids, reason


def test_acceptance_ondo_later_range_does_not_widen_early():
    """§8.5: поздняя консолидация не расширяет раннюю базу задним числом."""
    _summary, rows = _run_etalon("ONDO")
    frozen = _frozen(rows)
    assert len(frozen) >= 2
    ordered = sorted(frozen, key=lambda e: e.anchor_start_open_time)
    early, late = ordered[0], ordered[-1]
    assert early.origin_key != late.origin_key
    assert early.base_end_open_time is not None
    assert early.upper < 0.32
    assert late.lower > early.upper or late.upper > early.upper + 0.02
    assert early.upper != late.upper


def test_acceptance_negatives_do_not_mature():
    """§8.6: монотонное падение и единичный отскок не создают зрелую базу."""
    for builder in (build_monotonic_decline, build_single_bounce):
        db = fresh_db()
        summary = AltEngineV2(db, AltConfig()).process_asset_history(
            1, 1, builder()
        )
        mature = [
            e for e in db.list_alt_range_episodes(1)
            if e.state == AltEpisodeState.MATURE.value
        ]
        db.close()
        assert mature == []
        assert summary["state"] != "mature"


def test_acceptance_prefix_does_not_rewrite_frozen_pair():
    """§8.7: префикс до выхода и полный прогон дают ту же замороженную пару."""
    candles = build_two_bases()
    full = fresh_db()
    AltEngineV2(full, AltConfig()).process_asset_history(1, 1, candles)
    full_rows = {
        e.origin_key: (e.lower, e.upper, e.state)
        for e in _frozen(full.list_alt_range_episodes(1))
    }
    full.close()
    assert len(full_rows) == 2
    pre = fresh_db()
    AltEngineV2(pre, AltConfig()).process_asset_history(1, 1, candles[:150])
    pre_rows = {
        e.origin_key: (e.lower, e.upper, e.state)
        for e in _frozen(pre.list_alt_range_episodes(1))
    }
    pre.close()
    assert len(pre_rows) == 1
    for key, tup in pre_rows.items():
        assert full_rows[key][:2] == tup[:2]


def test_acceptance_k_cancel_still_cancels():
    """§8.6: подтверждённое нарушение K отменяет сетап, а не маскируется выносом."""
    from tests.test_alt_engine import ac, T0
    candles = build_base() + [
        ac(T0 + 146 * DAY_MS, 1.4, 1.45, 0.95, 1.05),
        ac(T0 + 147 * DAY_MS, 1.05, 1.55, 0.25, 1.50),
    ]
    db = fresh_db()
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    ep = db.list_alt_range_episodes(1)[0]
    db.close()
    assert ep.state == AltEpisodeState.TERMINAL.value
    assert "cancelled" in kinds_of(ep)


def test_acceptance_v1_freeze_unchanged_beside_v2():
    """§8.12: прогон v2 в той же БД не меняет freeze v1 и не смешивает версии."""
    candles = build_two_bases()
    db = fresh_db()
    AltEngine(db, AltConfig()).process_asset_history(1, 1, candles)
    before = _v1_frozen_tuples(db)
    assert before
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    assert _v1_frozen_tuples(db) == before
    v2_rows = db.list_alt_range_episodes(1)
    assert v2_rows
    assert all(e.rules_version == "alt-0.2" for e in v2_rows)
    setups = db.list_alt_setups(1)
    assert setups
    db.close()


def test_acceptance_markup_file_present():
    """Эталон разметки, по которому собран отчёт сравнения, лежит в фикстурах."""
    path = Path(__file__).resolve().parent / "fixtures" / "alt_v2" / "markup.json"
    markup = json.loads(path.read_text(encoding="utf-8"))
    assert markup["as_of"] == AS_OF.date().isoformat()
    assert {"NEAR", "SUI", "HBAR", "TAO", "ENA", "PUMP", "AAVE", "ONDO"} <= set(
        markup["tickers"]
    )
