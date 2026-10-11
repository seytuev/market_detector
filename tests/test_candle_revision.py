"""Этап 9: исправление закрытых свечей — ревизии (правила v1 §10.3).

Запись ревизии при изменении OHLC закрытой свечи (и её дедуп), причинный
пересчёт структуры с сохранением журнала, R46 (одно уведомление об
исправлении изменённого активного сценария; неизменённое не пересылается),
R47 (закрытая не переоткрывается, live не откатывается), ревизия плато.
"""
from __future__ import annotations

import json
from dataclasses import replace

from app.db import Database
from app.engine.ltf.engine import LtfEngine, LtfTickResult
from app.engine.scanner import Scanner
from app.models import Candle, Direction, EventKind, Zone, ZoneStatus, ZoneType
from tests.conftest import H1_MS, make_candle, make_h1_candles
from tests.test_audit_fixes import make_scenario

T = 1_780_012_800_000
H = 3_600_000
D = 24 * H


def _candle(iid, i, o, h, l, c, tf="H1", closed=True):
    tf_ms = H1_MS if tf == "H1" else D
    return Candle(
        instrument_id=iid, timeframe=tf, open_time=T + i * tf_ms,
        close_time=T + (i + 1) * tf_ms - 1, open=o, high=h, low=l, close=c,
        closed=closed, source="test",
    )


# --------------------------------------------------------------------- #
# Запись ревизии (upsert-путь) и дедуп
# --------------------------------------------------------------------- #

def test_revision_recorded_on_closed_ohlc_change(db, instrument_id):
    db.insert_candles([_candle(instrument_id, 0, 100, 102, 99, 101)])
    db.insert_candles([_candle(instrument_id, 0, 100, 110, 99, 108)])
    rows = db.list_candle_revisions(instrument_id, "H1")
    assert len(rows) == 1
    assert json.loads(rows[0]["old_ohlc_json"]) == [100.0, 102.0, 99.0, 101.0]
    assert json.loads(rows[0]["new_ohlc_json"]) == [100.0, 110.0, 99.0, 108.0]
    stored = db.candle_at(instrument_id, "H1", T)
    assert stored.revision == 1 and stored.high == 110.0
    # повторная доставка той же ревизии — дедуп
    db.insert_candles([_candle(instrument_id, 0, 100, 110, 99, 108)])
    assert len(db.list_candle_revisions(instrument_id, "H1")) == 1
    assert db.candle_at(instrument_id, "H1", T).revision == 1
    # новая ревизия к другому OHLC — новая строка
    db.insert_candles([_candle(instrument_id, 0, 100, 105, 99, 103)])
    assert len(db.list_candle_revisions(instrument_id, "H1")) == 2
    assert db.candle_at(instrument_id, "H1", T).revision == 2


def test_revision_recorded_for_d1(db, instrument_id):
    db.insert_candles([_candle(instrument_id, 0, 100, 102, 99, 101, tf="D1")])
    db.insert_candles([_candle(instrument_id, 0, 100, 95, 94, 96, tf="D1")])
    rows = db.list_candle_revisions(instrument_id, "D1")
    assert len(rows) == 1 and rows[0]["timeframe"] == "D1"


def test_r47_closed_not_reopened_and_no_revision(db, instrument_id):
    db.insert_candles([_candle(instrument_id, 0, 100, 102, 99, 101)])
    db.insert_candles([_candle(instrument_id, 0, 100, 90, 89, 91, closed=False)])
    stored = db.candle_at(instrument_id, "H1", T)
    assert stored.closed is True and stored.high == 102.0
    assert stored.revision == 0
    assert db.list_candle_revisions(instrument_id, "H1") == []


# --------------------------------------------------------------------- #
# Пересчёт структуры от ревизии (с сохранением журнала)
# --------------------------------------------------------------------- #

def test_engine_recounts_from_revision(db, cfg, instrument_id):
    bars = _h1_pivot_series(instrument_id)
    db.insert_candles(bars)
    engine = LtfEngine(db, cfg)
    first = engine.process_h1_close(instrument_id, now_ms=bars[-1].close_time)
    assert first.processed == 7
    pivots_before = [(p.kind, p.price) for p in db.list_ltf_pivots(instrument_id)]
    assert ("high", 12.0) in pivots_before
    events_before = len(db.list_ltf_events_for_instrument(instrument_id, limit=1000))
    # биржа переписала центральный high 12 → 9 (пивот исчезает из нового
    # расчёта) и дослала новую свечу
    revised = replace(bars[3], high=9.0)
    db.insert_candles([revised, _candle(instrument_id, 7, 5, 10, 1, 5)])
    rows = db.list_candle_revisions(instrument_id, "H1")
    assert len(rows) == 1 and rows[0]["open_time"] == bars[3].open_time
    second = engine.process_h1_close(instrument_id,
                                     now_ms=bars[-1].close_time + H1_MS)
    # причинный пересчёт с момента ревизии: idx3..idx7
    assert second.processed == 5
    # одна строка ревизии на обоих путях (upsert + детектор движка) — дедуп
    assert len(db.list_candle_revisions(instrument_id, "H1")) == 1
    # журнал сохранён: старые события не удалены
    assert len(db.list_ltf_events_for_instrument(instrument_id, limit=1000)) \
        >= events_before
    # повторный вызов без новых фактов — ничего не пересчитывает
    third = engine.process_h1_close(instrument_id,
                                    now_ms=bars[-1].close_time + 2 * H1_MS)
    assert third.processed == 0


def _h1_pivot_series(iid):
    return [_candle(iid, i, 5, hi, 1, 5)
            for i, hi in enumerate([10, 10, 10, 12, 10, 10, 10])]


def test_revision_detected_without_new_closes(db, cfg, instrument_id):
    bars = _h1_pivot_series(instrument_id)
    db.insert_candles(bars)
    engine = LtfEngine(db, cfg)
    engine.process_h1_close(instrument_id, now_ms=bars[-1].close_time)
    # ревизия без новых свечей: дешёвый сигнал по candle_revision будит
    # пересчёт даже при last.close_time <= курсора
    db.insert_candles([replace(bars[3], high=9.0)])
    second = engine.process_h1_close(instrument_id,
                                     now_ms=bars[-1].close_time + 1)
    assert second.processed > 0


def test_plateau_revision_recomputes(db, cfg, instrument_id):
    highs = [10, 11, 12, 12, 12, 10, 9, 8, 7]
    candles = make_h1_candles(
        [(h - 1.0, h, h - 2.5, h - 0.5) for h in highs], T,
        instrument_id=instrument_id,
    )
    db.insert_candles(candles)
    engine = LtfEngine(db, cfg)
    engine.process_h1_close(instrument_id, now_ms=candles[-1].close_time)
    assert len(db.list_ltf_plateau(instrument_id)) == 1
    # ревизия разрывает плато: участник уходит с уровня
    db.insert_candles([replace(candles[3], high=11.0),
                       _candle(instrument_id, len(highs), 6, 6.5, 5.5, 6)])
    assert len(db.list_candle_revisions(instrument_id, "H1")) == 1
    second = engine.process_h1_close(
        instrument_id, now_ms=candles[-1].close_time + H1_MS)
    assert second.processed > 0


# --------------------------------------------------------------------- #
# R46: уведомление об исправлении изменённого активного сигнала
# --------------------------------------------------------------------- #

def test_r46_correction_event_for_changed_scenario(db, cfg, instrument_id):
    obs, sc = make_scenario(db, instrument_id)
    engine = LtfEngine(db, cfg)
    revisions = [{
        "open_time": T + H, "previous": [1, 2, 3, 4], "current": [1, 2, 3, 5],
        "detected_at": T + 2 * H,
    }]
    db.update_ltf_scenario(sc.id, state="cancelled",
                           cancellation_reason="reverse_bos",
                           cancelled_at=T + 2 * H, updated_at=T + 2 * H)
    result = LtfTickResult()
    engine._emit_correction_events(
        instrument_id, revisions, {sc.id: "monitoring_entries"},
        T + H + 1_000, cfg.ltf_live_grace_seconds * 1000, result,
    )
    events = [e for e in db.list_ltf_events(observation_id=obs.id)
              if e.kind == "correction"]
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["reason"] == "candle_revision"
    assert ev.payload["scenario_id"] == sc.id
    assert ev.payload["previous_state"] == "monitoring_entries"
    assert ev.payload["new_state"] == "cancelled"
    assert ev.payload["revision_open_times"] == [T + H]
    # в пределах grace — live (доставляемое), это дельта, не рассылка истории
    assert ev.processing_mode == "live"
    assert ev.delayed is False
    # повторный вызов — дедуп, второго уведомления нет
    engine._emit_correction_events(
        instrument_id, revisions, {sc.id: "monitoring_entries"},
        T + 3 * H, cfg.ltf_live_grace_seconds * 1000, LtfTickResult(),
    )
    assert len([e for e in db.list_ltf_events(observation_id=obs.id)
                if e.kind == "correction"]) == 1


def test_r46_no_event_when_scenario_unchanged(db, cfg, instrument_id):
    obs, sc = make_scenario(db, instrument_id)
    engine = LtfEngine(db, cfg)
    engine._emit_correction_events(
        instrument_id,
        [{"open_time": T + H, "previous": [1, 2, 3, 4],
          "current": [1, 2, 3, 5], "detected_at": T + 2 * H}],
        {sc.id: "monitoring_entries"}, T + 2 * H,
        cfg.ltf_live_grace_seconds * 1000, LtfTickResult(),
    )
    assert [e for e in db.list_ltf_events(observation_id=obs.id)
            if e.kind == "correction"] == []


# --------------------------------------------------------------------- #
# HTF-сторона: ревизия D1 — переоценка с дедупом, журнал сохранён
# --------------------------------------------------------------------- #

def test_htf_d1_revision_reprocessing_keeps_journal(db, cfg, instrument_id):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=T - 2 * D, confirmed_at=T - D, status=ZoneStatus.ACTIVE,
    ))
    scanner = Scanner(db, cfg)
    # закрытие D1 строго ниже нижней границы — проход подтверждён
    scanner.on_closed_candle(_candle(instrument_id, 0, 105, 106, 98, 99, tf="D1"))
    assert len([e for e in db.get_events(zid)
                if e.kind == EventKind.SOURCE_PASSED]) == 1
    # ревизия: закрытие обратно внутри зоны — записана; переоценка не
    # дублирует события, старое остаётся в журнале
    db.insert_candles([_candle(instrument_id, 0, 105, 106, 98, 105, tf="D1")])
    assert len(db.list_candle_revisions(instrument_id, "D1")) == 1
    created = scanner.process_candle_revisions(instrument_id, now_ms := T + D)
    # переоценка детерминирована: дубля SOURCE_PASSED нет (UNIQUE), но
    # переход «закрытие снова внутри» — новый факт d1_close_inside
    assert [e.kind for e in created] == [EventKind.D1_CLOSE_INSIDE]
    assert len([e for e in db.get_events(zid)
                if e.kind == EventKind.SOURCE_PASSED]) == 1
    # курсор обработки ревизий — повторный вызов ничего не делает
    assert scanner.process_candle_revisions(instrument_id, now_ms + 1) == []
