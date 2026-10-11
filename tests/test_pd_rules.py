"""Этап 5: PD — якорь, ревизии, не-фильтр (правила v1 §6.2–6.3).

R12: BOS/SMS без HTF-реакции не заменяет якорь рабочего PD. R14/R15/R27:
зона вне PD допускается, контакт полного диапазона действителен, исключение
counter-sweep+FVG50 не требуется. R40: позднейший причинный слом обновляет
якорь на равных. R41/R13: preliminary/absent PD не блокирует зоны.
"""
from __future__ import annotations

from app.engine.ltf.eligibility import (
    REASON_OK,
    REASON_OUTSIDE_PD,
    admitted_scenario_entries,
    evaluate_entry,
    evaluate_final,
)
from app.engine.ltf.ranges import RangeDraft
from app.models import Direction
from app.models_ltf import LtfEntryZone, LtfScenarioEntry
from tests.test_audit_fixes import make_scenario
from tests.test_ltf_breaks import SERIES_A_CLOSES, SERIES_A_HL, _series
from app.engine.ltf.engine import LtfEngine
from app.models import Zone, ZoneStatus, ZoneType

H = 3_600_000
T = 1_780_012_800_000
T0 = 1_780_000_000_000


def _entry_zone(iid, lower, upper, ztype="OB"):
    return LtfEntryZone(
        id=1, instrument_id=iid, type=ztype, direction=Direction.BEAR,
        lower=lower, upper=upper, formed_at=T - 2 * H, confirmed_at=T - H,
    )


# --------------------------------------------------------------------- #
# §6.3: PD — не фильтр (R14, R15, R27)
# --------------------------------------------------------------------- #

def test_r14_zone_outside_pd_admitted(cfg, instrument_id):
    # bear-диапазон [90;110], premium [100;110]; зона полностью в discount —
    # прежний блок outside_pd снят: допуск по остальным правилам
    rng = RangeDraft(
        direction=Direction.BEAR, lower=90.0, upper=110.0, mid=100.0,
        anchor_low_ref=None, anchor_high_ref=None, available_at=T,
    )
    zone = _entry_zone(instrument_id, 90.0, 95.0)
    ev = evaluate_entry(zone, Direction.BEAR, cfg, rng)
    assert ev.reason == REASON_OK
    assert ev.eligible is True
    # положение в PD — описательная характеристика
    assert ev.overlap in ("none", "partial", "full")


def test_r14_contact_full_range_is_contact(cfg, instrument_id):
    # контакт на 104 при EQ=100 остаётся контактом — касание полного диапазона
    rng = RangeDraft(
        direction=Direction.BEAR, lower=90.0, upper=110.0, mid=100.0,
        anchor_low_ref=None, anchor_high_ref=None, available_at=T,
    )
    zone = _entry_zone(instrument_id, 102.0, 106.0)  # целиком выше EQ
    ev = evaluate_entry(zone, Direction.BEAR, cfg, rng)
    assert ev.reason == REASON_OK


def test_r15_legacy_outside_pd_row_not_blocked(cfg, instrument_id):
    # старая строка с reason=outside_pd (до §6.3) — не блокирует
    zone = _entry_zone(instrument_id, 90.0, 95.0)
    entry = LtfScenarioEntry(
        id=1, scenario_id=1, entry_zone_id=1, range_version=1,
        eligible=False, overlap="none", state="fresh", reason=REASON_OUTSIDE_PD,
        added_at=T, updated_at=T,
    )
    fe = evaluate_final(entry, zone, allow_outside=False, cfg=cfg)
    assert fe.eligible_now is True
    assert fe.admission_basis == "rule"
    # контекстное исключение как условие допуска не требуется
    assert fe.admission_basis != "context_exception"


def test_r27_out_of_range_rows_pass_admission(db, cfg, instrument_id):
    _, sc = make_scenario(db, instrument_id)
    z = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB",
        direction=Direction.BEAR, lower=90, upper=95, formed_at=T - 2 * H,
        confirmed_at=T - H, validity="fresh",
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=1,
        eligible=False, overlap="none", state="out_of_range", reason="outside_pd",
        added_at=T, updated_at=T,
    ))
    admitted = admitted_scenario_entries(db, sc.id, cfg=cfg)
    assert [zone.id for _, zone, _ in admitted] == [z.id]


# --------------------------------------------------------------------- #
# §6.2: якорь PD и ревизии (R40), R12
# --------------------------------------------------------------------- #

def _bear_zone(db, iid):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=9.0, upper=10.0, formed_at=T0 - H,
        confirmed_at=T0 - 1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def _feed(db, engine, iid, candles, upto=None):
    seq = candles if upto is None else candles[: upto + 1]
    for c in seq:
        db.insert_candles([c])
        engine.process_h1_close(iid, now_ms=c.close_time)


def test_pd_anchor_from_trigger_and_revisions(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    zone = _bear_zone(db, instrument_id)
    engine.on_htf_zone_touched(instrument_id, zone, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    sc = db.list_ltf_scenarios_for_instrument(instrument_id)[0]
    trigger = db.list_ltf_structure_events(sc.id)[0]
    sc = db.get_ltf_scenario(sc.id)
    # якорь рабочего PD — триггерное причинное событие; тип сохраняется
    assert sc.pd_anchor_structure_key == trigger.level_key
    assert sc.pd_anchor_kind == trigger.kind
    assert sc.htf_interaction_id is not None
    revisions = db.list_pd_revisions(sc.id)
    assert len(revisions) == 1
    assert revisions[0]["anchor_structure_key"] == trigger.level_key


def test_r40_late_causal_break_updates_anchor(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    zone = _bear_zone(db, instrument_id)
    engine.on_htf_zone_touched(instrument_id, zone, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    sc = db.list_ltf_scenarios_for_instrument(instrument_id)[0]
    first_key = db.get_ltf_scenario(sc.id).pd_anchor_structure_key
    _feed(db, engine, instrument_id, candles[13:22])   # secondary слом idx21
    sc = db.get_ltf_scenario(sc.id)
    events = db.list_ltf_structure_events(sc.id)
    assert len(events) >= 2
    # позднейший причинный слом обновил якорь (R40); прежний сохранён ревизией
    assert sc.pd_anchor_structure_key != first_key
    keys = [r["anchor_structure_key"] for r in db.list_pd_revisions(sc.id)]
    assert first_key in keys and sc.pd_anchor_structure_key in keys
    assert len(keys) >= 2


def test_r12_bare_bos_does_not_move_pd_anchor(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    zone = _bear_zone(db, instrument_id)
    engine.on_htf_zone_touched(instrument_id, zone, T0)
    candles = _series(SERIES_A_HL, SERIES_A_CLOSES, instrument_id)
    _feed(db, engine, instrument_id, candles, 12)
    sc = db.list_ltf_scenarios_for_instrument(instrument_id)[0]
    anchor = db.get_ltf_scenario(sc.id).pd_anchor_structure_key
    # BOS/SMS в другом направлении без HTF-основания — не якорь PD сценария
    from tests.test_ltf_breaks import SERIES_D_HL, SERIES_D_CLOSES
    from dataclasses import replace
    shift = 30 * H
    up = [replace(c, open_time=c.open_time + shift,
                  close_time=c.close_time + shift) for c in
          _series(SERIES_D_HL, SERIES_D_CLOSES, instrument_id)]
    _feed(db, engine, instrument_id, up, 12)
    assert db.get_ltf_scenario(sc.id).pd_anchor_structure_key == anchor
