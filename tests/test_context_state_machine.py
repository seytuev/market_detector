"""Этап 3: машина состояний HTF-контекста (правила v1 §4, 10.10.2026).

Покрытие: R01–R08 (переходы и оба порядка фактов возврата), R03 (контакт B
не удаляет A), R05/R06 (waiting_for при половине условий), R28 (зеркально
long/short), R36 (ранее возникший обратный слом + поздний проход), R37
(A→B→C → awaiting_clarity), R02/R31 (BOS без HTF-основания не открывает
контекст), R34 (контакт и подтверждение на одной H1), R35 (причинность не
истекает по часам), R32 (причинный слом вне геометрии HTF).
"""
from __future__ import annotations

import json

from app.engine.ltf.engine import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.services.htf_context import (
    _maybe_resume,
    _save_episode_evidence,
    causally_linked,
    context_state_v2,
    current_context,
    episode_sources,
    load_episode,
    note_source_outcome,
    open_episode,
    plan_context_bar,
    record_interaction,
    _confirm_episode,
)
from tests.test_ltf_breaks import SERIES_A_CLOSES, SERIES_A_HL, SERIES_D_CLOSES, SERIES_D_HL, _series

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000
T0 = 1_780_000_000_000


def _zone(db, iid, direction, serial=0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def _confirm(db, ep, direction, at):
    _confirm_episode(db, ep, 0, at, [], [], None, direction)


def _pass_source(db, zone, at):
    db.update_zone(zone.id, market_validity="invalid",
                   evidence={"invalidated_at": at})
    note_source_outcome(db, db.get_zone(zone.id), at)


def _note_reverse(db, ep_id, at, available_at, direction):
    ep = load_episode(db, ep_id)
    evidence = json.loads(ep["evidence"] or "{}")
    evidence["reverse_break"] = {
        "occurred_at": at, "available_at": available_at,
        "kind": "BOS", "direction": direction,
    }
    _save_episode_evidence(db, ep_id, evidence)
    _maybe_resume(db, ep_id)


def _ab_chain(db, cfg, iid):
    """A bear подтверждён → контакт B bull → B подтверждён (opposing_confirmed)."""
    bear = _zone(db, iid, Direction.BEAR, serial=0)
    bull = _zone(db, iid, Direction.BULL, serial=1)
    ep_a = record_interaction(db, cfg, iid, bear, T)
    _confirm(db, ep_a, Direction.BEAR, T + H)
    ep_b = record_interaction(db, cfg, iid, bull, T + 2 * H)
    _confirm(db, ep_b, Direction.BULL, T + 3 * H)
    return bear, bull, load_episode(db, ep_a["id"]), load_episode(db, ep_b["id"])


# --------------------------------------------------------------------- #
# Переходы §4 (unit-уровень)
# --------------------------------------------------------------------- #

def test_r03_opposing_contact_keeps_previous_context(db, cfg, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    ep_a = record_interaction(db, cfg, instrument_id, bear, T)
    _confirm(db, ep_a, Direction.BEAR, T + H)
    ep_b = record_interaction(db, cfg, instrument_id, bull, T + 2 * H)
    # B — кандидат со ссылкой на A; A не стёрт и остаётся основным
    assert ep_b["state_v2"] == "opposing_pending"
    assert ep_b["predecessor_episode_id"] == ep_a["id"]
    assert ep_b["opposing_source_id"] is not None
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "opposing_pending"
    assert proj["main"] == {"episode_id": ep_a["id"], "direction": "bear"}
    assert proj["opposing"]["direction"] == "bull"


def test_r04_opposing_confirmation_stores_predecessor(db, cfg, instrument_id):
    _, _, ep_a, ep_b = _ab_chain(db, cfg, instrument_id)
    assert ep_b["state_v2"] == "opposing_confirmed"
    assert ep_b["predecessor_episode_id"] == ep_a["id"]
    # приоритет у B; A сохранён как предшественник
    assert current_context(db, instrument_id)["id"] == ep_b["id"]


def test_r05_pass_without_reverse_break_waits(db, cfg, instrument_id):
    _, bull, _, ep_b = _ab_chain(db, cfg, instrument_id)
    _pass_source(db, bull, T + 4 * H)
    ep_b = load_episode(db, ep_b["id"])
    assert ep_b["state_v2"] == "opposing_source_breached_waiting_return"
    assert ep_b["htf_pass_confirmed_at"] == T + 4 * H
    proj = context_state_v2(db, instrument_id)
    assert proj["waiting_for"] == "reverse_h1_break"


def test_r06_reverse_break_without_pass_waits(db, cfg, instrument_id):
    _, _, _, ep_b = _ab_chain(db, cfg, instrument_id)
    _note_reverse(db, ep_b["id"], T + 4 * H, T + 4 * H, "bear")
    ep_b = load_episode(db, ep_b["id"])
    # проход B ещё не подтверждён — состояние прежнее, ожидание явное
    assert ep_b["state_v2"] == "opposing_confirmed"
    proj = context_state_v2(db, instrument_id)
    assert proj["waiting_for"] == "htf_pass_close"


def test_r07_both_facts_resume_without_new_touch(db, cfg, instrument_id):
    _, bull, ep_a, ep_b = _ab_chain(db, cfg, instrument_id)
    _pass_source(db, bull, T + 4 * H)
    _note_reverse(db, ep_b["id"], T + 5 * H, T + 5 * H, "bear")
    ep_b = load_episode(db, ep_b["id"])
    assert ep_b["state_v2"] == "context_resumed"
    evidence = json.loads(ep_b["evidence"])
    # время возврата — доступность последнего факта
    assert evidence["resumed_at"] == T + 5 * H
    # A восстановлен без нового касания его источника
    assert load_episode(db, ep_a["id"])["state_v2"] == "context_resumed"
    assert current_context(db, instrument_id)["id"] == ep_a["id"]
    assert episode_sources(db, ep_a["id"])  # основание A сохранено


def test_r08_reverse_order_same_outcome(db, cfg, instrument_id):
    _, bull, ep_a, ep_b = _ab_chain(db, cfg, instrument_id)
    _note_reverse(db, ep_b["id"], T + 4 * H, T + 4 * H, "bear")
    _pass_source(db, bull, T + 5 * H)
    ep_b = load_episode(db, ep_b["id"])
    assert ep_b["state_v2"] == "context_resumed"
    evidence = json.loads(ep_b["evidence"])
    assert evidence["resumed_at"] == T + 5 * H
    assert current_context(db, instrument_id)["id"] == ep_a["id"]


def test_r36_earlier_reverse_break_suffices(db, cfg, instrument_id):
    # обратный BOS возник ДО подтверждения прохода B (правило 8):
    # новый BOS не требуется, переход — в момент доступности последнего факта
    _, bull, ep_a, ep_b = _ab_chain(db, cfg, instrument_id)
    _note_reverse(db, ep_b["id"], T + 4 * H, T + 4 * H, "bear")
    assert load_episode(db, ep_b["id"])["state_v2"] == "opposing_confirmed"
    _pass_source(db, bull, T + 7 * 24 * H)  # позднее закрытие W1
    ep_b = load_episode(db, ep_b["id"])
    assert ep_b["state_v2"] == "context_resumed"
    assert json.loads(ep_b["evidence"])["resumed_at"] == T + 7 * 24 * H
    assert current_context(db, instrument_id)["id"] == ep_a["id"]


def test_r37_deep_chain_goes_awaiting_clarity(db, cfg, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    bear2 = _zone(db, instrument_id, Direction.BEAR, serial=2)
    ep_a = record_interaction(db, cfg, instrument_id, bear, T)
    _confirm(db, ep_a, Direction.BEAR, T + H)
    ep_b = record_interaction(db, cfg, instrument_id, bull, T + 2 * H)
    _confirm(db, ep_b, Direction.BULL, T + 3 * H)
    # новая самостоятельная цепочка A→B→C
    ep_c = record_interaction(db, cfg, instrument_id, bear2, T + 4 * H)
    assert ep_c["predecessor_episode_id"] == ep_b["id"]
    _confirm(db, ep_c, Direction.BEAR, T + 5 * H)
    _pass_source(db, bear2, T + 6 * H)
    ep_c = load_episode(db, ep_c["id"])
    # неоднозначный возврат из длинной цепочки — ожидание ясности,
    # произвольный старый контекст автоматически не извлекается
    assert ep_c["state_v2"] == "awaiting_clarity"
    _note_reverse(db, ep_c["id"], T + 7 * H, T + 7 * H, "bull")
    assert load_episode(db, ep_c["id"])["state_v2"] == "awaiting_clarity"


def test_r28_mirror_bull_a_bear_b(db, cfg, instrument_id):
    bull = _zone(db, instrument_id, Direction.BULL, serial=0)
    bear = _zone(db, instrument_id, Direction.BEAR, serial=1)
    ep_a = record_interaction(db, cfg, instrument_id, bull, T)
    _confirm(db, ep_a, Direction.BULL, T + H)
    ep_b = record_interaction(db, cfg, instrument_id, bear, T + 2 * H)
    _confirm(db, ep_b, Direction.BEAR, T + 3 * H)
    _pass_source(db, bear, T + 4 * H)
    _note_reverse(db, ep_b["id"], T + 5 * H, T + 5 * H, "bull")
    assert load_episode(db, ep_b["id"])["state_v2"] == "context_resumed"
    ctx = current_context(db, instrument_id)
    assert ctx["id"] == ep_a["id"]
    proj = context_state_v2(db, instrument_id)
    assert proj["main"]["direction"] == "bull"


def test_first_context_pass_has_no_return(db, cfg, instrument_id):
    # нет предыдущего подтверждённого контекста — возврат неприменим:
    # проход источника просто снимает его (прежнее поведение drop_source)
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    record_interaction(db, cfg, instrument_id, bear, T)
    _pass_source(db, bear, T + H)
    assert open_episode(db, instrument_id) is None


# --------------------------------------------------------------------- #
# R02/R31/R32: структура без HTF-основания и причинность
# --------------------------------------------------------------------- #

def test_r02_r31_bos_without_htf_opens_nothing(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    for c in candles[:13]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)
    # бычий BOS состоялся как структурный факт, но эпизода/контекста нет
    assert open_episode(db, instrument_id) is None
    assert current_context(db, instrument_id) is None
    assert context_state_v2(db, instrument_id) is None


def test_r32_causal_break_outside_htf_geometry(db, instrument_id):
    # слом вне геометрии HTF-зоны остаётся причинным (контакт → движение →
    # H1-зона → слом); фильтра по часам и нахождению внутри зоны нет
    from app.engine.ltf.breaks import StructureEventDraft
    source = {"active": 1, "interaction_at": T, "zone_id": 1}
    event = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BEAR,
        break_level=999.0, break_candle_open_time=T + 30 * H,
        occurred_at=T + 31 * H, detected_at=T + 31 * H, level_key="k",
    )
    assert causally_linked(source, event) is True
    assert causally_linked({**source, "active": 0}, event) is False
    earlier = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BEAR,
        break_level=999.0, break_candle_open_time=T - 2 * H,
        occurred_at=T - H, detected_at=T - H, level_key="k2",
    )
    # слом ДО контакта причинно связан быть не может
    assert causally_linked(source, earlier) is False


# --------------------------------------------------------------------- #
# Интеграция с движком: R01, R34, R35, R07 (запись факта возврата движком)
# --------------------------------------------------------------------- #

def _feed(db, engine, iid, candles, upto=None):
    seq = candles if upto is None else candles[: upto + 1]
    for c in seq:
        db.insert_candles([c])
        engine.process_h1_close(iid, now_ms=c.close_time)


def test_r01_bearish_htf_bearish_bos_confirms_context(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    bear = _zone(db, instrument_id, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, bear, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    ep = open_episode(db, instrument_id)
    assert ep is None  # подтверждённый эпизод не «открытый»
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "context_active"
    assert proj["main"]["direction"] == "bear"


def test_r34_contact_and_bos_on_same_h1(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    bull = _zone(db, instrument_id, Direction.BULL)
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 11)
    # контакт по котировке в 12:40 внутри свечи idx12 (её закрытие — слом)
    inside = candles[12].open_time + 40 * 60_000
    engine.on_htf_zone_touched(
        instrument_id, bull, inside,
        evidence={"order_proven": True, "observed": True},
    )
    _feed(db, engine, instrument_id, candles[12:13])
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "context_active"
    assert proj["main"]["direction"] == "bull"


def test_r35_causality_does_not_expire_by_hours(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    bull = _zone(db, instrument_id, Direction.BULL)
    engine.on_htf_zone_touched(instrument_id, bull, T0)
    # слом через 48+ часов от контакта — причинная связь сохраняется
    candles = _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)
    from dataclasses import replace
    shift = 48 * H
    moved = [replace(c, open_time=c.open_time + shift,
                     close_time=c.close_time + shift) for c in candles]
    _feed(db, engine, instrument_id, moved, 12)
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "context_active"
    assert proj["main"]["direction"] == "bull"


def test_r07_engine_records_reverse_break_and_resumes(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    engine.on_htf_zone_touched(instrument_id, bear, T0)
    hl = SERIES_A_HL + SERIES_D_HL + SERIES_A_HL
    closes = {
        **SERIES_A_CLOSES,
        **{22 + k: v for k, v in SERIES_D_CLOSES.items() if k < len(SERIES_D_HL)},
        **{42 + k: v for k, v in SERIES_A_CLOSES.items()},
    }
    candles = _series(hl, closes, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)   # bear BOS → A подтверждён
    assert context_state_v2(db, instrument_id)["state_v2"] == "context_active"
    engine.on_htf_zone_touched(instrument_id, bull, candles[13].open_time)
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "opposing_pending"
    _feed(db, engine, instrument_id, candles[13:35])  # bull BOS idx34 → B
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "opposing_confirmed"
    assert proj["predecessor"]["direction"] == "bear"
    # проход B своим ТФ — ждём только обратный слом
    _pass_source(db, bull, candles[42].open_time)
    proj = context_state_v2(db, instrument_id)
    assert proj["waiting_for"] == "reverse_h1_break"
    _feed(db, engine, instrument_id, candles[42:55])  # bear BOS → возврат
    ep_b = load_episode(db, proj["episode_id"])
    assert ep_b["state_v2"] == "context_resumed"
    evidence = json.loads(ep_b["evidence"])
    rev = evidence["reverse_break"]
    # обратный слом записан рыночным фактом после подтверждения B; переход —
    # доступность последнего из двух фактов (здесь — сам слом, он позже
    # прохода B)
    assert rev["available_at"] >= candles[42].open_time
    assert evidence["resumed_at"] == rev["available_at"]
    ctx = current_context(db, instrument_id)
    assert ctx["id"] == proj["predecessor"]["episode_id"]
