"""§6: BOS/SMS — фиксация слома, этапы, самостоятельный SMS, отмена (п.2–4)."""
from __future__ import annotations

import pytest

from app.engine.ltf import (
    assign_roles,
    confirmed_pivots,
    detect_breaks,
    find_h1_pivots,
)
from app.engine.ltf.breaks import bear_break, bull_break
from app.models import Direction
from app.models_ltf import ScanTrace
from tests.conftest import H1_MS, make_candle, make_h1_candles

T0 = 1_780_000_000_000
NOW = T0 + 200 * H1_MS


def _series(
    hl: list[tuple[float, float]],
    closes: dict[int, float] | None = None,
    instrument_id: int = 1,
):
    """(h, l) ряды → свечи H1; close по умолчанию — середина."""
    bars = []
    for i, (h, l) in enumerate(hl):
        c = closes.get(i, (h + l) / 2) if closes else (h + l) / 2
        bars.append(((h + l) / 2, h, l, c))
    return make_h1_candles(bars, T0, instrument_id)


def _pivots_with_roles(candles):
    pivots = find_h1_pivots(candles, 3, 3)
    avail = confirmed_pivots(pivots, NOW)
    res = assign_roles(avail)
    for i, p in enumerate(avail):
        p.role = res.roles[i]
    return avail


# Серия A: восходящая структура (H1=13, HL L1=9, HH H2=15), слом вниз,
# L_first=8 (idx15), откат H3=12 (idx18), вторичный слом
SERIES_A_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 10.5), (14, 10), (13, 9.5), (12, 8.8), (11, 8.7),
    (10.5, 8.5), (10.8, 8.0), (11, 8.2), (11.5, 8.4), (12, 8.6), (11.5, 8.4),
    (11, 8.2), (10.8, 7.8),
]
SERIES_A_CLOSES = {12: 8.9, 21: 7.9}   # idx12 — первичный BOS, idx21 — вторичный

# Серия B: H1=13, L_ref=9, H_peak=15, внутренний L_internal=11 (idx12),
# откат H_pullback=14 (idx15), закрытие ниже L_internal при целом L_ref
SERIES_B_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11.2), (14, 11.5), (13, 11.3), (13, 11),
    (13, 11.2), (13.5, 11.4), (14, 11.6), (13.5, 11.4), (13, 11.2), (12.5, 10.8),
]

# Серия C (зеркало B): нисходящая структура, bull SMS по §6.4
SERIES_C_HL = [
    (14, 13), (13.5, 12.5), (13, 12), (12.5, 11), (13, 11.5), (14, 12),
    (15, 12.5), (14, 12), (13, 11), (12, 9), (12.5, 9.5), (12.8, 10),
    (13, 11.2), (12.8, 11.3), (12.6, 11.4), (12.4, 11), (12.6, 11.2),
    (12.8, 11.4), (13.5, 11.6),
]

# Серия D (зеркало A): bull BOS первичный + вторичный
SERIES_D_HL = [
    (14, 13), (13.5, 12.5), (13, 12), (13.5, 11), (14, 11.5), (14.5, 12),
    (15, 12.5), (14.5, 12), (14, 11), (13.5, 9), (14, 9.5), (14.5, 10),
    (15.2, 10.5), (14.5, 14.2), (14.8, 14.5), (16, 15), (15.8, 15.3),
    (15.6, 15.0), (15.2, 14.5), (15.5, 14.8), (15.8, 15.0), (16.2, 15.2),
]
SERIES_D_CLOSES = {12: 15.1, 21: 16.1}

# Серия E: после первичного слома — непрерывное падение без отката
SERIES_E_HL = SERIES_A_HL[:13] + [
    (11, 8.7), (10.8, 8.5), (10.6, 8.0), (10.5, 8.2), (10.4, 8.3),
    (10.3, 8.1), (10.35, 8.2), (10.2, 8.15), (10.1, 7.9),
]
SERIES_E_CLOSES = {12: 8.9, 21: 7.95}

# Серия G: A + новая якорная нога (HH 12.5 idx30, HL 8.4 idx27): первичный
# слом idx34, вторичный idx42 — вторичный возможен в КАЖДОЙ якорной ноге
SERIES_G_HL = SERIES_A_HL + [
    (11, 8.3), (11.4, 8.8), (11.8, 9.2), (11.4, 8.9), (11.0, 8.5),
    (10.8, 8.4), (11.2, 8.8), (11.8, 9.3), (12.5, 9.7), (12, 9.4),
    (11.5, 9.1), (11, 8.9), (10.6, 8.3), (10.2, 7.9), (9.9, 7.6),
    (9.6, 7.5), (9.8, 7.7), (10.4, 8.1), (10.1, 7.9), (9.8, 7.4),
    (9.5, 7.3),
]
SERIES_G_CLOSES = {**SERIES_A_CLOSES, 34: 8.35, 42: 7.4}

# Серия F: A + разворот вверх (LL 7.8 на idx21, закрытие выше LH=12 на idx25)
SERIES_F_HL = SERIES_A_HL + [(11.5, 7.9), (11.8, 8.0), (12.0, 7.95), (12.3, 8.2)]
SERIES_F_CLOSES = {**SERIES_A_CLOSES, 25: 12.1}

# Серия N: бычий слом (bull BOS 13.2 idx15, secondary 13.5 idx21) ДО
# формирования медвежьей структуры и её слома (bear BOS 11.5 на idx25)
SERIES_N_HL = [
    (12, 11), (12.5, 11.5), (13, 12), (13.5, 12.5), (13, 12.2), (12.5, 11.8),
    (12, 11.4), (12.4, 11.7), (12.8, 12.1), (13.2, 12.4), (12.8, 12.0),
    (12.4, 11.6), (12, 11.2), (12.4, 11.5), (12.8, 11.9), (13.5, 12.2),
    (13.2, 12.0), (12.8, 11.7), (12.6, 11.5), (13.0, 11.9), (13.6, 12.4),
    (14.2, 12.9), (13.8, 12.6), (13.4, 12.2), (13.0, 11.8), (12.6, 11.2),
    (12.0, 10.8), (11.6, 10.4), (11.2, 10.0),
]
SERIES_N_CLOSES = {15: 13.4, 25: 11.3}

# Серия N2: N до idx25 + откат (LH 12.9 idx28), новый LL (9.4 idx34) и
# бычий слом 12.9 на idx39 — ПОСЛЕ медвежьего триггера (§6.5)
SERIES_N2_HL = SERIES_N_HL[:26] + [
    (12.2, 11.0), (12.6, 11.3), (12.9, 11.6), (12.5, 11.2), (12.1, 10.8),
    (11.7, 10.4), (11.3, 10.0), (11.0, 9.7), (10.7, 9.4), (11.1, 9.9),
    (11.5, 10.3), (11.9, 10.7), (12.4, 11.1), (13.0, 11.6),
]
SERIES_N2_CLOSES = {**SERIES_N_CLOSES, 39: 13.05}


def test_break_primitives():
    c = make_candle(T0, 100, 101, 95, 99, timeframe="H1")   # тень ниже, закрытие выше
    assert not bear_break(98, c)                            # тень за уровнем — не слом
    assert not bull_break(100, c)
    assert bear_break(98, make_candle(T0, 100, 101, 95, 97, timeframe="H1"))
    assert bull_break(100, make_candle(T0, 100, 101, 95, 100.5, timeframe="H1"))
    # равенство — не слом
    assert not bear_break(99, c)
    assert not bull_break(99, make_candle(T0, 100, 101, 95, 99, timeframe="H1"))
    # незакрытая свеча — не слом
    assert not bear_break(
        98, make_candle(T0, 100, 101, 95, 97, timeframe="H1", closed=False)
    )


def test_bear_bos_primary_without_secondary():
    candles = _series(SERIES_A_HL[:15], SERIES_A_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert len(res.events) == 1
    ev = res.events[0]
    assert (ev.kind, ev.stage) == ("BOS", "primary")
    assert ev.direction == Direction.BEAR
    assert ev.break_level == 9                                # опорный HL
    assert ev.break_candle_open_time == T0 + 12 * H1_MS
    assert res.cancellation is None


def test_bear_bos_secondary_after_pullback():
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert [(e.kind, e.stage) for e in res.events] == [
        ("BOS", "primary"), ("BOS", "secondary"),
    ]
    assert res.events[1].break_level == 8                     # L_first
    assert res.events[1].break_candle_open_time == T0 + 21 * H1_MS
    assert len({e.level_key for e in res.events}) == 2        # разные уровни


def test_continuous_fall_without_pullback_is_not_two_stages():
    # §6.1/приёмка п.4: L_first сформирован, но отката не было — один этап
    candles = _series(SERIES_E_HL, SERIES_E_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert [(e.kind, e.stage) for e in res.events] == [("BOS", "primary")]


def test_secondary_bos_allowed_in_each_anchor_regime():
    # два якоря (HH 15 idx9 и HH 12.5 idx30) — вторичный слом в каждой ноге;
    # защёлка secondary_done сбрасывается новым якорем, дубли режет level_key
    candles = _series(SERIES_G_HL, SERIES_G_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert [(e.kind, e.stage) for e in res.events] == [
        ("BOS", "primary"), ("BOS", "secondary"),
        ("BOS", "primary"), ("BOS", "secondary"),
    ]
    assert [e.break_level for e in res.events] == [9, 8, 8.4, 7.5]
    assert res.events[2].break_candle_open_time == T0 + 34 * H1_MS
    assert res.events[3].break_candle_open_time == T0 + 42 * H1_MS
    assert len({e.level_key for e in res.events}) == 4        # разные уровни


def test_bear_sms_standalone():
    # §6.3: SMS без BOS — L_ref=9 не пробит (закрытие 10.9 > 9)
    candles = _series(SERIES_B_HL, {18: 10.9})
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert len(res.events) == 1
    ev = res.events[0]
    assert ev.kind == "SMS" and ev.stage == "primary"
    assert ev.break_level == 11                               # L_internal
    assert ev.accompanying is False


def test_bull_sms_standalone():
    # §6.4: зеркальный SMS — H_ref=15 не пробит (закрытие 13.2 < 15)
    candles = _series(SERIES_C_HL, {18: 13.2})
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BULL, NOW)
    assert len(res.events) == 1
    ev = res.events[0]
    assert ev.kind == "SMS"
    assert ev.direction == Direction.BULL
    assert ev.break_level == 13                               # H_internal


def test_one_candle_bos_with_accompanying_sms():
    # §6.5: одна свеча пересекает внутренний (11) и внешний (9) уровни —
    # основное событие BOS + сопутствующий SMS
    candles = _series(SERIES_B_HL[:-1] + [(12.5, 8.8)], {18: 8.9})
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert [(e.kind, e.accompanying) for e in res.events] == [
        ("BOS", False), ("SMS", True),
    ]
    assert res.events[0].break_level == 9
    assert res.events[1].break_level == 11
    same_candle = res.events[0].break_candle_open_time
    assert res.events[1].break_candle_open_time == same_candle == T0 + 18 * H1_MS


def test_bull_bos_primary_and_secondary():
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BULL, NOW)
    assert [(e.kind, e.stage) for e in res.events] == [
        ("BOS", "primary"), ("BOS", "secondary"),
    ]
    assert res.events[0].break_level == 15                    # опорный LH
    assert res.events[1].break_level == 16                    # H_first


def test_reverse_bos_cancels_scenario():
    # §6.5: после двух этапов bear-сценария закрытие выше последнего LH —
    # обратный слом; события до свечи отмены сохраняются
    candles = _series(SERIES_F_HL, SERIES_F_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert [(e.kind, e.stage) for e in res.events] == [
        ("BOS", "primary"), ("BOS", "secondary"),
    ]
    assert res.cancellation is not None
    assert res.cancellation.pattern == "reverse_bos"
    assert res.cancellation.event.direction == Direction.BULL
    assert res.cancellation.event.break_level == 12           # последний LH
    assert res.cancellation.event.break_candle_open_time == T0 + 25 * H1_MS


def test_cancellation_only_from_reverse_break_after_trigger():
    # §6.5: обратные сломы 13.2 (idx15) и 13.5 (idx21) раньше медвежьего
    # триггера (idx25) отмену не дают и скан не обрезают (нога дополняется
    # вторичным 11.0 на idx32); отменяет bull BOS 12.9 на idx39 — после триггера
    candles = _series(SERIES_N2_HL, SERIES_N2_CLOSES)
    pivots = _pivots_with_roles(candles)
    trigger_at = candles[25].close_time
    trace = ScanTrace()
    res = detect_breaks(pivots, candles, Direction.BEAR, NOW,
                        cancel_not_before_ms=trigger_at, trace=trace)
    assert [(e.kind, e.stage, e.break_level) for e in res.events] == [
        ("BOS", "primary", 11.5), ("BOS", "secondary", 11.0),
    ]
    assert res.cancellation is not None
    assert res.cancellation.pattern == "reverse_bos"
    assert res.cancellation.event.break_level == 12.9
    assert res.cancellation.event.break_candle_open_time == T0 + 39 * H1_MS
    skipped = [e for e in trace.entries if e.reason == "reverse_before_trigger"]
    assert [(e.level_kind, e.level_price) for e in skipped] == [
        ("BOS", 13.2), ("BOS", 13.5),
    ]
    # без порога — прежнее поведение: отмена первым обратным сломом окна
    res0 = detect_breaks(pivots, candles, Direction.BEAR, NOW)
    assert res0.events == []
    assert res0.cancellation.event.break_level == 13.2


# ---------- трассировка (диагностика, off by default) ----------

def test_trace_off_by_default():
    candles = _series(SERIES_A_HL[:15], SERIES_A_CLOSES)
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR, NOW)
    assert res.trace is None
    assert len(res.events) == 1                      # поведение без trace не меняется


def test_trace_accept_bear_primary_bos():
    candles = _series(SERIES_A_HL[:15], SERIES_A_CLOSES)
    trace = ScanTrace()
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW, trace=trace)
    assert res.trace is trace
    acc = [e for e in trace.accepts()
           if e.check == "primary_bos" and e.direction == "bear"]
    assert len(acc) == 1
    e = acc[0]
    assert e.level_kind == "BOS" and e.level_stage == "primary"
    assert e.level_price == 9                              # опорный HL
    assert e.candle_open_time == T0 + 12 * H1_MS
    # refs: якорь HH (idx9) + опорный HL (idx6); до материализации ref=pivot_at
    assert e.ref_pivot_ids == [T0 + 9 * H1_MS, T0 + 6 * H1_MS]
    assert e.anchor_pivot_id == T0 + 9 * H1_MS
    assert e.anchor_price == 15
    assert e.ref_pivot_id == T0 + 6 * H1_MS
    # роль пика видна в момент поглощения
    hh = [a for a in trace.absorptions
          if a.pivot_ref == T0 + 9 * H1_MS and a.direction == "bear"]
    assert hh and hh[0].role == "HH" and hh[0].kind == "high"


def test_trace_reject_pullback_missing_on_continuous_fall():
    # §6.1/приёмка п.4: L_first сформирован, но отката нет — вторичный слом
    # отклоняется с pullback_missing
    candles = _series(SERIES_E_HL, SERIES_E_CLOSES)
    trace = ScanTrace()
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW, trace=trace)
    assert [(e.kind, e.stage) for e in res.events] == [("BOS", "primary")]
    sec_rej = [e for e in trace.rejects()
               if e.check == "secondary_bos" and e.direction == "bear"]
    assert "pullback_missing" in {e.reason for e in sec_rej}
    assert not [e for e in trace.accepts() if e.check == "secondary_bos"]


def test_trace_standalone_sms_records_internal_level():
    candles = _series(SERIES_B_HL, {18: 10.9})
    trace = ScanTrace()
    detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                  NOW, trace=trace)
    acc = [e for e in trace.accepts() if e.check == "sms"]
    assert len(acc) == 1
    e = acc[0]
    assert e.level_kind == "SMS" and e.level_stage == "primary"
    assert e.level_price == 11                             # L_internal
    assert e.internal_price == 11
    assert e.internal_pivot_id == T0 + 12 * H1_MS
    assert e.candle_open_time == T0 + 18 * H1_MS


def test_trace_context_candles_before_since():
    candles = _series(SERIES_B_HL, {18: 10.9})
    since = T0 + 10 * H1_MS
    trace = ScanTrace()
    res = detect_breaks(_pivots_with_roles(candles), candles, Direction.BEAR,
                        NOW, since_ms=since, trace=trace)
    # свечи раньше since_ms — только контекст (обе стороны), без accept
    ctx = [e for e in trace.entries if e.decision == "context_only"]
    assert {e.candle_open_time for e in ctx} == {
        T0 + i * H1_MS for i in range(10)
    }
    assert {e.direction for e in ctx} == {"bear", "bull"}
    assert not [e for e in trace.entries
                if e.decision in ("accept", "reject")
                and e.candle_open_time < since]
    # SMS на idx18 всё ещё находится — контекст учитывается
    assert [e.kind for e in res.events] == ["SMS"]
    # записи по конкретной свече доступны через helper
    assert trace.entries_for(T0 + 18 * H1_MS)


# ---------- sync_structure: материализация в БД (идемпотентность, дедуп) ----------

def test_sync_structure_materializes_and_dedups(db, cfg, instrument_id):
    from app.db import Database
    from app.engine.ltf import sync_structure
    from app.models import Zone, ZoneStatus, ZoneType
    from app.models_ltf import LtfObservation, LtfScenario, LtfStructureEvent

    assert isinstance(db, Database)
    candles = _series(SERIES_B_HL, {18: 10.9}, instrument_id)
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary",
    ))

    r1 = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert r1.pivots_new == 5
    roles = {p.pivot_at: p.role for p in db.list_ltf_pivots(instrument_id)}
    assert roles[T0 + 9 * H1_MS] == "HH"               # H_peak
    assert roles[T0 + 6 * H1_MS] == "HL"               # опорный HL
    assert roles[T0 + 12 * H1_MS] == "internal_low"    # участник SMS (§6.3)
    # пересмотры ролей записаны в историю
    hl_id = [p.id for p in db.list_ltf_pivots(instrument_id)
             if p.pivot_at == T0 + 12 * H1_MS][0]
    assert db.list_ltf_pivot_role_log(hl_id)
    # событие SMS найдено для активного сценария
    evs = r1.events[sc.id]
    assert [e.kind for e in evs] == ["SMS"]

    # повторный sync: pivots не дублируются
    r2 = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert r2.pivots_new == 0
    assert len(db.list_ltf_pivots(instrument_id)) == 5

    # событие записано в БД → следующий sync его не возвращает (дедуп)
    e = evs[0]
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind=e.kind, stage=e.stage,
        direction=e.direction, break_level=e.break_level,
        break_candle_open_time=e.break_candle_open_time,
        occurred_at=e.occurred_at, detected_at=e.detected_at,
        level_key=e.level_key, ref_pivot_ids=e.ref_pivot_ids,
        accompanying=e.accompanying, evidence=e.evidence,
    ))
    r3 = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert sc.id not in r3.events


def test_sync_structure_role_log_idempotent(db, cfg, instrument_id):
    """Повторный sync по тем же данным не пишет новых строк в role_log."""
    from app.engine.ltf import sync_structure

    def log_rows() -> int:
        return db.conn.execute(
            "SELECT COUNT(*) FROM ltf_pivot_role_log").fetchone()[0]

    candles = _series(SERIES_B_HL, {18: 10.9}, instrument_id)
    r1 = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert r1.role_changes
    assert log_rows() == len(r1.role_changes)

    r2 = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert r2.role_changes == []
    assert log_rows() == len(r1.role_changes)


def test_sync_structure_genuine_role_revision_logged_once(db, cfg, instrument_id):
    """Реальный пересмотр роли между sync'ами — ровно одна строка лога."""
    from app.engine.ltf import sync_structure

    candles = _series(SERIES_B_HL, {18: 10.9}, instrument_id)
    # укороченная история: LH (idx15) ещё не сформирован, минимум idx12 — HL
    sync_structure(db, cfg, instrument_id, candles[:16], NOW)
    # полная история: LH пересматривает idx12 в internal_low (§6.3),
    # сам idx15 — новый pivot с первой ролью
    r = sync_structure(db, cfg, instrument_id, candles, NOW)
    assert [(c.old_role, c.new_role) for c in r.role_changes] == [
        ("HL", "internal_low"), ("none", "LH"),
    ]
    il_id = [p.id for p in db.list_ltf_pivots(instrument_id)
             if p.pivot_at == T0 + 12 * H1_MS][0]
    # полная история genuine-пересмотров, без дублей от повторных пересчётов
    assert [
        (e["old_role"], e["new_role"]) for e in db.list_ltf_pivot_role_log(il_id)
    ] == [("none", "HL"), ("HL", "internal_low")]
