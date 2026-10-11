"""Этап 11: приёмочное покрытие критериев R01–R48 (правила v1 §13).

Карта покрытия (id → тест):

  R01 test_context_state_machine::test_r01_bearish_htf_bearish_bos_confirms_context
  R02 ЭТОТ ФАЙЛ :: test_r02_bull_bos_without_opposing_keeps_main_context
  R03 test_context_state_machine::test_r03_opposing_contact_keeps_previous_context
  R04 test_context_state_machine::test_r04_opposing_confirmation_stores_predecessor
  R05 test_context_state_machine::test_r05_pass_without_reverse_break_waits
  R06 test_context_state_machine::test_r06_reverse_break_without_pass_waits
      + test_htf_sources_levels::test_r21_h1_close_does_not_confirm_w1_pass
  R07 test_context_state_machine::test_r07_both_facts_resume_without_new_touch
      + test_r07_engine_records_reverse_break_and_resumes
  R08 test_context_state_machine::test_r08_reverse_order_same_outcome
  R09 test_htf_sources_levels (test_r09_*)
  R10 ЭТОТ ФАЙЛ :: test_r10_d1_and_w1_equivalent_no_tf_bonus
  R11 test_bos_sms_parity (test_sms_confirms_episode_without_bos и др.)
  R12 test_pd_rules::test_r12_bare_bos_does_not_move_pd_anchor
  R13 test_h1_local_pd (provisional-пометки), test_ltf_provisional_range
  R14 test_pd_rules::test_r14_zone_outside_pd_admitted / test_r14_contact_full_range_is_contact
  R15 test_pd_rules::test_r15_legacy_outside_pd_row_not_blocked
  R16 test_activation_visit (test_r16_*)
  R17 test_activation_visit (test_r17_*)
  R18 test_activation_visit::test_r18_fvg_filled_before_activation_not_fresh
  R19 test_activation_visit::test_r19_fvg_filled_within_activation_keeps_basis
      + test_notify_rules::test_fvg_filled_current_activation_message
  R20 test_htf_sources_levels (test_r20_*)
  R21 test_htf_sources_levels::test_r21_h1_close_does_not_confirm_w1_pass
  R22 test_quote_contract::test_r22_r23_quote_touch_shared_activation
  R23 test_quote_contract::test_r22_r23_quote_touch_shared_activation
  R24 test_context_lifetime (test_r24_*)
  R25 test_schema_migrations::test_r25_history_visits_not_converted_to_activations
      + test_activation_visit::test_r25_unknown_history_not_fresh_but_not_blocked
  R26 test_notify_rules::test_r26_current_activation_not_suppressed_by_depth
  R27 test_pd_rules::test_r27_out_of_range_rows_pass_admission
  R28 ЭТОТ ФАЙЛ :: test_r28_mirror_rules_parametrized
      (+ test_context_state_machine::test_r28_mirror_bull_a_bear_b)
  R29 ЭТОТ ФАЙЛ :: test_r29_replay_and_restart_no_duplicates
  R30 test_htf_sources_levels::test_r30_manual_source_known_later_does_not_backdate
      + ЭТОТ ФАЙЛ :: test_r30_historical_bos_not_confirmed_for_late_source
  R31 test_cancellation_rules (test_r31_*)
  R32 test_cancellation_rules::test_r32_break_from_context_structure_is_qualified
      + test_context_state_machine::test_r32_causal_break_outside_htf_geometry
  R33 test_cancellation_rules (test_r33_*)
  R34 test_context_state_machine::test_r34_contact_and_bos_on_same_h1
  R35 test_context_state_machine::test_r35_causality_does_not_expire_by_hours
  R36 test_context_state_machine::test_r36_earlier_reverse_break_suffices
  R37 test_context_state_machine::test_r37_deep_chain_goes_awaiting_clarity
  R38 test_activation_visit::test_r38_tick_out_and_return_continues_activation
  R39 test_context_lifetime (test_r39_*)
  R40 test_pd_rules::test_r40_late_causal_break_updates_anchor
  R41 ЭТОТ ФАЙЛ :: test_r41_sms_without_anchors_pd_preliminary_zones_not_blocked
      (+ test_ltf_provisional_range)
  R42 test_htf_sources_levels (test_r42_*) + test_quote_contract
  R43 test_pivot_plateau (test_r43_*)
  R44 test_pivot_plateau::test_r44_both_sides_excluded_ohlc_kept
  R45 test_adapters (hyperliquid mid), test_adapters_bybit (ticker fallback),
      test_quote_contract
  R46 test_candle_revision (test_r46_*) + test_notify_rules::test_r46_correction_text_has_reason_and_fact
  R47 test_quote_contract::test_r47_stale_tick_keeps_forming_bar_and_zones
      + test_candle_revision::test_r47_closed_not_reopened_and_no_revision
  R48 ЭТОТ ФАЙЛ :: test_r48_same_contacts_any_order_same_outcome
      (+ test_audit_fixes::test_f12_aggregate_order_independent,
       test_htf_sources_levels::test_r48_multiple_sources_one_interval_keep_uncertainty)
"""
from __future__ import annotations

import json

import pytest

from app.config import DetectorConfig
from app.engine.ltf.engine import LtfEngine
from app.models import Candle, Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfScenario,
    LtfScenarioEntry,
)
from app.services.htf_context import (
    _confirm_episode,
    context_state_v2,
    episode_sources,
    open_episode,
)
from tests.test_ltf_breaks import SERIES_A_CLOSES, SERIES_A_HL, SERIES_D_CLOSES, SERIES_D_HL, _series

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000
T0 = 1_780_000_000_000


def _zone(db, iid, direction, serial=0, timeframe="D1", source="auto",
          created_at=0, ztype=ZoneType.OB):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ztype, direction=direction,
        timeframe=timeframe, lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE, source=source,
        created_at=created_at,
    ))
    return db.get_zone(zid)


def _confirm(db, ep, direction, at):
    _confirm_episode(db, ep, 0, at, [], [], None, direction)


def _feed(db, engine, iid, candles, upto=None):
    seq = candles if upto is None else candles[: upto + 1]
    for c in seq:
        db.insert_candles([c])
        engine.process_h1_close(iid, now_ms=c.close_time)


# --------------------------------------------------------------------- #
# R02: bullish BOS без встречного HTF — коррекция, основной bear сохранён
# --------------------------------------------------------------------- #

def test_r02_bull_bos_without_opposing_keeps_main_context(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    bear = _zone(db, instrument_id, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, bear, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    assert context_state_v2(db, instrument_id)["main"]["direction"] == "bear"
    # бычий BOS без встречного HTF-основания — новый контекст не открывается,
    # основной медвежий сохраняется (коррекция)
    up = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    from dataclasses import replace
    shift = 30 * H
    moved = [replace(c, open_time=c.open_time + shift,
                     close_time=c.close_time + shift) for c in up]
    _feed(db, engine, instrument_id, moved, 12)
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "context_active"
    assert proj["main"]["direction"] == "bear"
    assert proj["opposing"] is None


# --------------------------------------------------------------------- #
# R10: D1 и W1 равноправны — эквивалентные факты дают эквивалентный исход
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("timeframe", ["D1", "W1"])
def test_r10_d1_and_w1_equivalent_no_tf_bonus(db, cfg, instrument_id, timeframe):
    engine = LtfEngine(db, cfg)
    zone = _zone(db, instrument_id, Direction.BEAR, timeframe=timeframe)
    engine.on_htf_zone_touched(instrument_id, zone, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    proj = context_state_v2(db, instrument_id)
    # одинаковый сценарий на D1 и на W1: тот же исход (контекст bear
    # подтверждён тем же сломом), бонуса по ТФ нет
    assert proj["state_v2"] == "context_active"
    assert proj["main"]["direction"] == "bear"


# --------------------------------------------------------------------- #
# R28: зеркальные случаи long/short для ключевых правил
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("a_dir,b_dir", [
    (Direction.BEAR, Direction.BULL),
    (Direction.BULL, Direction.BEAR),
])
def test_r28_mirror_rules_parametrized(db, cfg, instrument_id, a_dir, b_dir):
    """Зеркало A↔B в обоих направлениях: контакт, подтверждение, проход,
    возврат — симметричные состояния."""
    engine = LtfEngine(db, cfg)
    za = _zone(db, instrument_id, a_dir, serial=0)
    zb = _zone(db, instrument_id, b_dir, serial=1)
    engine.on_htf_zone_touched(instrument_id, za, T)
    ep_a = open_episode(db, instrument_id)
    _confirm(db, ep_a, a_dir, T + H)
    engine.on_htf_zone_touched(instrument_id, zb, T + 2 * H)
    ep_b = open_episode(db, instrument_id)
    assert ep_b["predecessor_episode_id"] == ep_a["id"]
    _confirm(db, ep_b, b_dir, T + 3 * H)
    # проход B своим ТФ — ожидание обратного слома
    db.update_zone(zb.id, market_validity="invalid",
                   evidence={"invalidated_at": T + 4 * H})
    from app.services.htf_context import note_source_outcome
    note_source_outcome(db, db.get_zone(zb.id), T + 4 * H)
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "opposing_source_breached_waiting_return"
    assert proj["waiting_for"] == "reverse_h1_break"
    assert proj["predecessor"]["direction"] == a_dir.value
    # обратный слом в направлении A — возврат
    from app.services.htf_context import _maybe_resume, _save_episode_evidence, load_episode
    ep = load_episode(db, ep_b["id"])
    evidence = json.loads(ep["evidence"] or "{}")
    evidence["reverse_break"] = {"occurred_at": T + 5 * H,
                                 "available_at": T + 5 * H,
                                 "kind": "BOS", "direction": a_dir.value}
    _save_episode_evidence(db, ep_b["id"], evidence)
    _maybe_resume(db, ep_b["id"])
    assert context_state_v2(db, instrument_id)["state_v2"] == "context_resumed"


@pytest.mark.parametrize("direction", [Direction.BULL, Direction.BEAR])
def test_r28_mirror_source_pass_strict_close(db, cfg, instrument_id, direction):
    """Зеркало §5: проход источника — строгое закрытие своего ТФ за дальней
    границей, в обоих направлениях."""
    from app.engine.scanner import Scanner
    if direction == Direction.BULL:
        lower, upper, close_in, close_out = 100.0, 110.0, 100.0, 99.0
    else:
        lower, upper, close_in, close_out = 100.0, 110.0, 110.0, 111.0
    zone = _zone(db, instrument_id, direction, ztype=ZoneType.FVG,
                 timeframe="D1")
    db.update_zone(zone.id, lower=lower, upper=upper)
    scanner = Scanner(db, cfg)
    far = Candle(instrument_id=instrument_id, timeframe="D1", open_time=T,
                 close_time=T + D - 1, open=close_in, high=close_in + 1,
                 low=close_in - 1, close=close_in, closed=True)
    scanner.on_closed_candle(far)   # равенство границе — не проход
    assert db.get_zone(zone.id).market_validity == "active"
    beyond = Candle(instrument_id=instrument_id, timeframe="D1",
                    open_time=T + D, close_time=T + 2 * D - 1,
                    open=close_in, high=close_out + 1, low=close_out - 1,
                    close=close_out, closed=True)
    scanner.on_closed_candle(beyond)  # строго за границей — проход
    assert db.get_zone(zone.id).market_validity == "invalid"


@pytest.mark.parametrize("direction", [Direction.BULL, Direction.BEAR])
def test_r28_mirror_ob90_freshness(db, cfg, instrument_id, direction):
    """Зеркало §7: OB 90% до активации — не свежее основание; в текущей
    активации — живёт, в обоих направлениях."""
    zone = _zone(db, instrument_id, direction)
    db.update_zone(zone.id, max_test_depth=0.95, has_tests=True)
    zone = db.get_zone(zone.id)
    obs = LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    assert obs is None
    act = db.list_zone_activations(zone.id)[0]
    assert act["fresh_for_new_activation"] == 0


# --------------------------------------------------------------------- #
# R29: повторная обработка/рестарт — нет дублей активации, перехода, Telegram
# --------------------------------------------------------------------- #

def test_r29_replay_and_restart_no_duplicates(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    zone = _zone(db, instrument_id, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, zone, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)

    def counts():
        return {
            "activations": len(db.list_zone_activations(zone.id)),
            "episodes": db.conn.execute(
                "SELECT COUNT(*) AS n FROM htf_context_episode"
            ).fetchone()["n"],
            "events": len(db.list_ltf_events_for_instrument(instrument_id,
                                                            limit=1000)),
            "scenarios": len(db.list_ltf_scenarios_for_instrument(instrument_id)),
            "transitions": len([
                e for e in db.list_ltf_events_for_instrument(instrument_id)
                if e.kind == "context_transition"
            ]),
        }

    before = counts()
    obs = db.list_ltf_observations(instrument_id=instrument_id)[0]
    # повторный replay тем же движком
    assert engine.replay_observation(obs.id).events == []
    # «рестарт»: новый экземпляр движка на той же БД — replay и обработка
    fresh = LtfEngine(db, cfg)
    assert fresh.replay_observation(obs.id).events == []
    assert fresh.process_h1_close(instrument_id,
                                  now_ms=candles[-1].close_time).processed == 0
    assert counts() == before


# --------------------------------------------------------------------- #
# R30: ручная зона позже исторического BOS — BOS не live-реакция
# --------------------------------------------------------------------- #

def test_r30_historical_bos_not_confirmed_for_late_source(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    bos_close = candles[12].close_time
    # ручная зона создана ПОСЛЕ слома (idx12): исторический BOS задним числом
    # не становится реакцией от ещё неизвестного источника
    zone = _zone(db, instrument_id, Direction.BEAR, source="manual",
                 created_at=bos_close + H, ztype=ZoneType.MANUAL)
    engine.on_htf_zone_touched(instrument_id, zone, T0 - H)
    ep = open_episode(db, instrument_id)
    assert ep["started_at"] == bos_close + H
    _feed(db, engine, instrument_id, candles, 12)
    ep = open_episode(db, instrument_id)
    assert ep is not None
    assert ep["state"] == "awaiting_h1"
    assert context_state_v2(db, instrument_id)["state_v2"] == \
        "awaiting_initial_confirmation"


# --------------------------------------------------------------------- #
# R41: после SMS ещё нет опор PD — preliminary/absent, зоны не блокируются
# --------------------------------------------------------------------- #

def test_r41_sms_without_anchors_pd_preliminary_zones_not_blocked(db, cfg, instrument_id):
    # SMS-сценарий без пары опорных пивотов: диапазона нет (range_pending)
    zid = _zone(db, instrument_id, Direction.BEAR).id
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="SMS", stage="primary", state="range_pending",
        created_at=T, updated_at=T,
    ))
    assert db.get_ltf_scenario(sc.id).state == "range_pending"
    assert db.get_current_ltf_range(sc.id) is None
    z = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB",
        direction=Direction.BEAR, lower=100, upper=104, formed_at=T - 2 * H,
        confirmed_at=T - H, validity="fresh",
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=0,
        eligible=True, overlap="pending", state="fresh", reason="range_pending",
        added_at=T, updated_at=T,
    ))
    # пространственный фильтр не применяется: зона — кандидат в ожидании
    # диапазона (range_pending), а не исключённая outside_pd
    from app.engine.ltf.eligibility import admitted_scenario_entries, evaluate_final
    entry = db.list_ltf_scenario_entries(sc.id)[0]
    fe = evaluate_final(entry, db.get_ltf_entry_zone(z.id),
                        allow_outside=False, cfg=cfg)
    assert fe.primary_reason == "range_pending"
    assert fe.state == "fresh"
    assert "outside_pd" not in fe.blocking_reasons
    # в допуск не попадает лишь до появления диапазона, строка жива
    assert admitted_scenario_entries(db, sc.id, cfg=cfg) == []
    assert db.list_ltf_scenario_entries(sc.id)[0].state == "fresh"


# --------------------------------------------------------------------- #
# R48: одно множество контактов в разном порядке — детерминированный итог
# --------------------------------------------------------------------- #

def test_r48_same_contacts_any_order_same_outcome(cfg):
    outcomes = []
    orders = [
        [Direction.BEAR, Direction.BULL, Direction.BULL],
        [Direction.BULL, Direction.BULL, Direction.BEAR],
        [Direction.BULL, Direction.BEAR, Direction.BULL],
    ]
    from app.db import Database
    from app.models import Instrument
    for order in orders:
        db = Database(":memory:")
        try:
            iid = db.upsert_instrument(Instrument(
                id=None, asset="BTC", venue="binance", market_type="spot",
                symbol="BTCUSDT", quote_asset="USDT",
            ))
            engine = LtfEngine(db, cfg)
            for serial, direction in enumerate(order):
                zone = _zone(db, iid, direction, serial=serial)
                engine.on_htf_zone_touched(iid, zone, T)
            ep = open_episode(db, iid)
            sources = episode_sources(db, ep["id"])
            events = db.list_ltf_events_for_instrument(iid, limit=1000)
            outcomes.append({
                "candidate_direction": ep["candidate_direction"],
                "state_v2": ep["state_v2"],
                "sources": sorted(
                    json.loads(s["evidence"])["direction"] for s in sources
                ),
                "event_kinds": sorted(e.kind for e in events),
            })
        finally:
            db.close()
    assert outcomes[0] == outcomes[1] == outcomes[2]
    # конфликт направлений — детерминированный neutral, не голосование порядком
    assert outcomes[0]["candidate_direction"] == "neutral"
