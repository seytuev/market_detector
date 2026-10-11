"""Регрессионные тесты этапа 0: исправления аудита F1–F13 (10.10.2026).

Каждый тест фиксирует ЖЕЛАЕМОЕ поведение после исправления; исходные
воспроизведения ошибок — tools/repro_htf_ltf_audit_2026_10_10.py и
tools/repro_htf_ltf_followup_2026_10_10.py.
"""
from __future__ import annotations

import json
from dataclasses import replace

from app.config import DetectorConfig
from app.engine.ltf.eligibility import admitted_scenario_entries, evaluate_final
from app.engine.ltf.engine import LtfEngine, LtfTickResult
from app.engine.ltf.pivots import find_h1_pivots
from app.engine.ltf.structure import StructureBatch
from app.engine.scanner import Scanner
from app.models import Candle, Direction, Event, EventKind, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from app.services.h1_setup import _trade_context
from app.services.htf_context import (
    context_supports_direction,
    drop_source,
    episode_sources,
    open_episode,
    record_interaction,
)
from app.services.htf_parent import parent_decision

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000


def make_scenario(db, iid, at=T, direction=Direction.BEAR):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - D,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zid, zone_version=1,
        cycle_id=1, direction=direction, state="active", activated_at=at,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction, trigger="BOS",
        stage="primary", state="monitoring_entries", created_at=at, updated_at=at,
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90, upper=110, mid=100,
        anchor_low_pivot_id=None, anchor_high_pivot_id=None, available_at=at,
    ))
    return obs, sc


def make_entry(db, iid, sc, validity="fresh", depth=0):
    z = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=iid, type="OB", direction=Direction.BEAR,
        lower=100, upper=104, formed_at=T - 2 * H, confirmed_at=T - H,
        validity=validity, max_test_depth=depth,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=1,
        eligible=True, overlap="full", state="fresh", reason="ok",
        added_at=T, updated_at=T,
    ))
    return z, db.list_ltf_scenario_entries(sc.id)[0]


def make_source_zone(db, iid, direction, serial=0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


# --------------------------------------------------------------------- #
# F1: catchup LTF-события не получает HTF-подтверждение из будущего
# --------------------------------------------------------------------- #

def _fvg_context_setup(db, iid, confirmed_at, event_at):
    obs, sc = make_scenario(db, iid)
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.FVG, direction=Direction.BULL,
        timeframe="D1", lower=90, upper=110, formed_at=T - 2 * H,
        confirmed_at=confirmed_at, status=ZoneStatus.ACTIVE,
    ))
    db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.DEPTH_50,
        occurred_at=event_at, detected_at=event_at, price=100,
    ))
    candle = Candle(instrument_id=iid, timeframe="H1", open_time=T,
                    close_time=T + H - 1, open=101, high=105, low=100,
                    close=102, closed=True)
    return obs, sc, candle


def test_f1_future_htf_fact_does_not_confirm_old_h1(db, cfg, instrument_id):
    # зона и её тест 50% появились ПОСЛЕ закрытия обрабатываемой H1 —
    # catchup-обработка этой H1 не видит их, даже если они уже в БД
    obs, sc, candle = _fvg_context_setup(db, instrument_id, T + 2 * H, T + 3 * H)
    eng = LtfEngine(db, cfg)
    result = LtfTickResult()
    eng._update_scenario_context(obs, sc, [], [candle], T + 4 * H, "catchup",
                                 3 * H, result)
    assert [e for e in result.events if e.kind == "context_update"] == []


def test_f1_htf_fact_known_by_close_does_confirm(db, cfg, instrument_id):
    # контроль: факт, доступный к закрытию свечи, подтверждает контекст
    obs, sc, candle = _fvg_context_setup(db, instrument_id, T - 2 * H, T + H - 1)
    eng = LtfEngine(db, cfg)
    result = LtfTickResult()
    eng._update_scenario_context(obs, sc, [], [candle], T + 4 * H, "catchup",
                                 3 * H, result)
    updates = [e for e in result.events if e.kind == "context_update"]
    assert len(updates) == 1
    assert updates[0].payload["fact"] == "htf_fvg50"
    assert updates[0].occurred_at == candle.close_time


# --------------------------------------------------------------------- #
# F2: историческая проекция торгового контекста не зависит от будущего
# --------------------------------------------------------------------- #

def test_f2_future_scenario_not_in_past_context(db, instrument_id):
    _, sc = make_scenario(db, instrument_id, at=T + 10 * H)
    past = T + H
    assert _trade_context(db, instrument_id, Direction.BEAR.value, past)[0] is False
    # отмена сценария в будущем не меняет прошлую проекцию
    db.update_ltf_scenario(sc.id, state="cancelled", cancelled_at=T + 12 * H)
    assert _trade_context(db, instrument_id, Direction.BEAR.value, past)[0] is False


def test_f2_existing_scenario_visible_at_as_of(db, instrument_id):
    _, sc = make_scenario(db, instrument_id, at=T + 10 * H)
    assert _trade_context(db, instrument_id, Direction.BEAR.value, T + 11 * H)[0] is True
    # отмена позже as_of — контекст на as_of был; отмена до as_of — нет
    db.update_ltf_scenario(sc.id, state="cancelled", cancelled_at=T + 12 * H)
    assert _trade_context(db, instrument_id, Direction.BEAR.value, T + 11 * H)[0] is True
    assert _trade_context(db, instrument_id, Direction.BEAR.value, T + 13 * H)[0] is False


# --------------------------------------------------------------------- #
# F3: общий отбор и переклассификация используют актуальный cfg
# --------------------------------------------------------------------- #

def test_f3_shared_helper_respects_configured_depth(db, instrument_id):
    _, sc = make_scenario(db, instrument_id)
    z, e = make_entry(db, instrument_id, sc, validity="tested", depth=0.95)
    cfg = DetectorConfig(entry_reuse_max_depth=0.98)
    assert evaluate_final(e, z, allow_outside=False, cfg=cfg).eligible_now
    # тот же отбор с тем же cfg обязан совпадать с evaluate_final
    admitted = admitted_scenario_entries(db, sc.id, cfg=cfg)
    assert [zone.id for _, zone, _ in admitted] == [z.id]
    # default-порог 0.90 зону не допускает — и это тоже согласованно
    assert admitted_scenario_entries(db, sc.id) == []


def test_f3_reclassify_applies_new_depth_threshold(db, instrument_id):
    # смена entry_reuse_max_depth через /api/settings вызывает
    # reclassify_active_entries — строка пересчитывается новым порогом
    _, sc = make_scenario(db, instrument_id)
    z, e = make_entry(db, instrument_id, sc, validity="tested", depth=0.95)
    engine = LtfEngine(db, DetectorConfig(entry_reuse_max_depth=0.90))
    changed = engine.reclassify_active_entries(now_ms=T + H)
    assert changed == {sc.id: 1}
    stored = db.list_ltf_scenario_entries(sc.id)[0]
    assert stored.reason == "tested_too_deep"


# --------------------------------------------------------------------- #
# F7: поздно пришедший старый контакт не откатывает окно эпизода
# --------------------------------------------------------------------- #

def test_f7_out_of_order_interaction_keeps_expiry(db, cfg, instrument_id):
    zone = make_source_zone(db, instrument_id, Direction.BEAR)
    newer = record_interaction(db, cfg, instrument_id, zone, T + 10 * H)
    older = record_interaction(db, cfg, instrument_id, zone, T + 2 * H)
    assert older["last_distinct_interaction_at"] == newer["last_distinct_interaction_at"]
    assert older["expires_at"] == newer["expires_at"]


# --------------------------------------------------------------------- #
# F8: монотонность котировок
# --------------------------------------------------------------------- #

def test_f8_stale_quote_does_not_overwrite(db, instrument_id):
    db.set_quote(instrument_id, 110, T + H)
    db.set_quote(instrument_id, 90, T)
    assert db.get_quote(instrument_id) == (110.0, T + H)
    # тик того же момента обновляет цену
    db.set_quote(instrument_id, 95, T + H)
    assert db.get_quote(instrument_id) == (95.0, T + H)


# --------------------------------------------------------------------- #
# F9: закрытая свеча не становится открытой от старого ответа
# --------------------------------------------------------------------- #

def test_f9_closed_candle_not_downgraded(db, instrument_id):
    c = Candle(instrument_id=instrument_id, timeframe="H1", open_time=T,
               close_time=T + H - 1, open=100, high=102, low=99, close=101,
               closed=True)
    db.insert_candles([c])
    db.insert_candles([replace(c, closed=False, high=99)])
    stored = db.last_candle(instrument_id, "H1", closed_only=False)
    assert stored.closed is True
    assert stored.high == 102
    assert db.last_candle(instrument_id, "H1") is not None


def test_f9_closed_revision_with_same_flag_applies(db, instrument_id):
    c = Candle(instrument_id=instrument_id, timeframe="H1", open_time=T,
               close_time=T + H - 1, open=100, high=102, low=99, close=101,
               closed=True)
    db.insert_candles([c])
    db.insert_candles([replace(c, closed=True, high=120)])
    assert db.last_candle(instrument_id, "H1").high == 120


def test_f9_open_candle_finalizes(db, instrument_id):
    c = Candle(instrument_id=instrument_id, timeframe="H1", open_time=T,
               close_time=T + H - 1, open=100, high=102, low=99, close=101,
               closed=False)
    db.insert_candles([c])
    db.insert_candles([replace(c, closed=True)])
    assert db.last_candle(instrument_id, "H1") is not None


# --------------------------------------------------------------------- #
# F10: ревизия уже обработанной H1 фиксируется (детекция)
# --------------------------------------------------------------------- #

def _h1_bars(iid):
    return [Candle(instrument_id=iid, timeframe="H1", open_time=T + i * H,
                   close_time=T + (i + 1) * H - 1, open=5, high=hi, low=1,
                   close=5, closed=True)
            for i, hi in enumerate([10, 10, 10, 12, 10, 10, 10])]


def test_f10_engine_detects_candle_revision(db, cfg, instrument_id):
    original = _h1_bars(instrument_id)
    db.insert_candles(original)
    engine = LtfEngine(db, cfg)
    first = engine.process_h1_close(instrument_id, now_ms=original[-1].close_time)
    assert first.processed == 7
    # биржа переписала уже обработанную свечу; пришла и новая
    corrected = replace(original[3], high=9)
    db.insert_candles([corrected])
    new_bar = Candle(instrument_id=instrument_id, timeframe="H1",
                     open_time=T + 7 * H, close_time=T + 8 * H - 1,
                     open=5, high=10, low=1, close=5, closed=True)
    db.insert_candles([new_bar])
    second = engine.process_h1_close(instrument_id, now_ms=new_bar.close_time)
    # §10.3: причинный пересчёт с момента самой ранней ревизии (idx3..idx7),
    # а не только новая свеча
    assert second.processed == 5
    raw = db.get_meta(f"ltf:h1:revision:{instrument_id}")
    assert raw is not None
    revision = json.loads(raw)
    assert revision["open_time"] == corrected.open_time
    assert revision["previous"][1] == 12
    assert revision["current"][1] == 9
    # факт записан и в журнал candle_revision (дедуп с upsert-путём)
    rows = db.list_candle_revisions(instrument_id, "H1")
    assert len(rows) == 1 and rows[0]["open_time"] == corrected.open_time


def test_f10_structure_batch_recomputes_on_junction_ohlc_change():
    original = _h1_bars(1)
    corrected = list(original)
    corrected[-1] = replace(original[-1], high=13)
    batch = StructureBatch()
    before = batch.pivots_for(original, 3, 3)
    after = batch.pivots_for(corrected, 3, 3)
    assert [(p.kind, p.price) for p in after] == [
        (p.kind, p.price) for p in find_h1_pivots(
            [c for c in corrected if c.closed], 3, 3)
    ]
    assert [(p.kind, p.price) for p in before] == [("high", 12)]


# --------------------------------------------------------------------- #
# F11: после удаления источника агрегат пересчитывается
# --------------------------------------------------------------------- #

def test_f11_drop_source_recomputes_aggregate(db, cfg, instrument_id):
    bear = make_source_zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = make_source_zone(db, instrument_id, Direction.BULL, serial=1)
    record_interaction(db, cfg, instrument_id, bear, T)
    record_interaction(db, cfg, instrument_id, bull, T + H)
    assert open_episode(db, instrument_id)["candidate_direction"] == "neutral"
    drop_source(db, instrument_id, bull.id, T + 2 * H, "market_invalid")
    ep = open_episode(db, instrument_id)
    assert ep["candidate_direction"] == "bear"
    sources = episode_sources(db, ep["id"])
    assert context_supports_direction(db, ep, sources, "bear") is True
    assert context_supports_direction(db, ep, sources, "bull") is False


# --------------------------------------------------------------------- #
# F12: агрегат одновременных контактов не зависит от порядка вставки
# --------------------------------------------------------------------- #

def test_f12_aggregate_order_independent(db, cfg, instrument_id):
    results = []
    for round_no, order in enumerate(
        [(Direction.BEAR, Direction.BULL, Direction.BULL),
         (Direction.BULL, Direction.BULL, Direction.BEAR)]
    ):
        for i, direction in enumerate(order):
            record_interaction(
                db, cfg, instrument_id,
                make_source_zone(db, instrument_id, direction,
                                 serial=round_no * 10 + i), T,
            )
        results.append(open_episode(db, instrument_id)["candidate_direction"])
        drop_all = db.conn.execute(
            "SELECT id FROM htf_context_episode WHERE instrument_id=?",
            (instrument_id,),
        ).fetchall()
        for row in drop_all:
            db.conn.execute(
                "UPDATE htf_context_episode SET state='closed_by_user' WHERE id=?",
                (row["id"],),
            )
        db.conn.commit()
    assert results[0] == results[1] == "neutral"


# --------------------------------------------------------------------- #
# F13: историческая инвалидация HTF пишется рыночным временем
# --------------------------------------------------------------------- #

def test_f13_historical_invalidation_keeps_market_time(db, cfg, instrument_id):
    # R33 (правила v1 §6.1): инвалидация родителя — информационный факт
    # (context_update), не отмена; рыночное время и режим сохранены (F13)
    obs, sc = make_scenario(db, instrument_id)
    z = db.get_zone(obs.zone_id)
    db.update_zone(z.id, market_validity="invalid",
                   evidence={**z.evidence, "invalidated_at": T + H},
                   end_reason="ob_invalidated")
    assert LtfEngine(db, cfg).check_parent_validity(db.get_zone(z.id)) is True
    event = next(e for e in db.list_ltf_events(observation_id=obs.id)
                 if e.kind == "context_update")
    assert event.occurred_at == T + H
    assert event.processing_mode == "catchup"
    assert event.delayed is True
    stored = db.get_ltf_scenario(sc.id)
    assert stored.state == "monitoring_entries"
    assert stored.cancelled_at is None


def test_f13_fresh_invalidation_stays_live(db, cfg, instrument_id):
    from app.models import now_ms
    obs, sc = make_scenario(db, instrument_id)
    z = db.get_zone(obs.zone_id)
    now = now_ms()
    db.update_zone(z.id, market_validity="invalid",
                   evidence={**z.evidence, "invalidated_at": now},
                   end_reason="ob_invalidated")
    assert LtfEngine(db, cfg).check_parent_validity(db.get_zone(z.id)) is True
    event = next(e for e in db.list_ltf_events(observation_id=obs.id)
                 if e.kind == "context_update")
    assert event.occurred_at == now
    assert event.processing_mode == "live"
    assert event.delayed is False
    assert db.get_ltf_scenario(sc.id).state == "monitoring_entries"


# --------------------------------------------------------------------- #
# F4: HTF-скан одной свечи не читает будущие свечи из БД
# --------------------------------------------------------------------- #

def test_f4_single_candle_scan_ignores_future_rows(db, cfg, instrument_id):
    values = [(100, 102, 99, 101), (101, 109, 100, 108), (110, 112, 109, 111)]
    candles = [Candle(
        instrument_id=instrument_id, timeframe="D1", open_time=T + i * D,
        close_time=T + (i + 1) * D - 1, open=o, high=h, low=l, close=c,
        closed=True,
    ) for i, (o, h, l, c) in enumerate(values)]
    db.insert_candles(candles)
    Scanner(db, cfg).on_closed_candle(candles[0])
    future = [
        z for z in db.get_zones(instrument_id)
        if z.confirmed_at and z.confirmed_at > candles[0].close_time + 1
    ]
    assert future == []


# --------------------------------------------------------------------- #
# F5: первый глубокий тест сразу получает tested_too_deep
# --------------------------------------------------------------------- #

def test_f5_first_deep_touch_persists_correct_reason(db, cfg, instrument_id):
    _, sc = make_scenario(db, instrument_id)
    z, e = make_entry(db, instrument_id, sc)
    candle = Candle(instrument_id=instrument_id, timeframe="H1", open_time=T,
                    close_time=T + H - 1, open=101, high=103.8, low=100.5,
                    close=101, closed=True)
    LtfEngine(db, cfg)._on_entry_touched(
        make_observation(db, sc), sc, e, z, candle, [], candle.close_time,
        "live", 0, LtfTickResult())
    stored = db.list_ltf_scenario_entries(sc.id)[0]
    actual = db.get_ltf_entry_zone(z.id)
    assert stored.state == "tested"
    assert stored.reason == "tested_too_deep"
    assert evaluate_final(stored, actual, allow_outside=False).primary_reason == stored.reason


def make_observation(db, sc):
    return db.get_ltf_observation(sc.observation_id)


# --------------------------------------------------------------------- #
# F6: допуск родителя на прошлую дату — причинный, не текущий lifecycle
# --------------------------------------------------------------------- #

def test_f6_parent_as_of_before_later_invalidation(db, cfg, instrument_id):
    zone = Zone(id=1, instrument_id=instrument_id, type=ZoneType.OB,
                direction=Direction.BEAR, timeframe="D1", lower=100, upper=104,
                formed_at=T, confirmed_at=T + H, status=ZoneStatus.ACTIVE)
    assert parent_decision(zone, cfg, T + 2 * H).eligible is True
    invalidated = replace(zone, status=ZoneStatus.ARCHIVED,
                          market_validity="invalid", display_until=T + 5 * H)
    # зона завершилась позже as_of — на отсечку она ещё была рабочей
    assert parent_decision(invalidated, cfg, T + 2 * H).eligible is True
    # live-путь (as_of=None) не изменился
    assert parent_decision(invalidated, cfg).eligible is False
    # конец до отсечки — неактуальна и тогда
    assert parent_decision(invalidated, cfg, T + 6 * H).eligible is False


def test_f6_unknown_history_not_substituted_by_present(db, cfg, instrument_id):
    zone = Zone(id=1, instrument_id=instrument_id, type=ZoneType.OB,
                direction=Direction.BEAR, timeframe="D1", lower=100, upper=104,
                formed_at=T, confirmed_at=T + H, status=ZoneStatus.ARCHIVED,
                market_validity="invalid")
    decision = parent_decision(zone, cfg, T + 2 * H)
    assert decision.eligible is False
    assert decision.reason == "unknown_history"
    assert decision.history_trusted is False
