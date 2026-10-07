"""§10/§11 (Этап 6): жизненные циклы OB/FVG и пригодность.

- OB-основание у начала движения (свечи до pivot_at/BOS) не отбрасывается:
  критерий — принадлежность подтверждающему движению, а не formed_at.
- Глубина OB: ровно 90% — рыночно актуален, но не переизбирается; 89% — может.
- FVG: полное перекрытие (100%) — терминальный fvg_filled по собственному
  правилу; связанный OB оценивается независимо (своим правилом 90%).
"""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine, LtfTickResult
from app.engine.ltf.eligibility import (
    REASON_FVG_FILLED,
    REASON_OK,
    REASON_TESTED_TOO_DEEP,
    evaluate_entry,
)
from app.engine.ltf.entries import fvg_fill_status
from app.engine.ltf.ranges import RangeDraft
from app.models import Direction
from app.models_ltf import LtfEntryZone, LtfScenarioEntry
from tests.conftest import H1_MS, make_h1_candles
from tests.test_ltf_breaks import _series
from tests.test_ltf_entries import _by_bounds, _detection
from tests.test_ltf_engine import (
    T0,
    _events,
    _feed,
    _mk_scenario,
    _mkp,
    _PAIR,
    _setup,
)

_CFG = DetectorConfig()


def _rng() -> RangeDraft:
    return RangeDraft(
        direction=Direction.BEAR, lower=90.0, upper=110.0, mid=100.0,
        anchor_low_ref=None, anchor_high_ref=None, available_at=0,
    )


def _zone(type_: str, lower: float, upper: float, **kw) -> LtfEntryZone:
    base = dict(
        id=1, instrument_id=1, type=type_, direction=Direction.BEAR,
        lower=lower, upper=upper, formed_at=0, confirmed_at=1,
    )
    base.update(kw)
    return LtfEntryZone(**base)


# --------------------------------------------------------------------- #
# §10.1: OB-основание у источника движения не отбрасывается
# --------------------------------------------------------------------- #

def test_ob_base_at_origin_kept():
    """База OB у начальной опоры (свечи ДО start_pivot/BOS) — кандидат:
    отброс по formed_at >= break_time запрещён, критерий — движение."""
    candles, avail, mv, cfg = _detection(_CFG)
    det = _by_bounds(__import__(
        "app.engine.ltf.entries", fromlist=["detect_entry_zones"]
    ).detect_entry_zones(candles, mv, avail, Direction.BEAR, cfg))
    ob = det[("OB", 14.4, 15.6)]
    # база idx3 — раньше start-pivot движения (пик 15.6, idx4) и раньше BOS
    assert ob.formed_at < mv.start_at
    assert ob.evidence["base_candles"]               # исходные свечи хранятся
    fvg = det[("FVG", 13.8, 14.9)]
    assert fvg.evidence["fvg_candles"]               # тройка — в evidence


# --------------------------------------------------------------------- #
# §10.5: граница правила глубины OB (ровно 90% — недопустим к перевыбору)
# --------------------------------------------------------------------- #

def test_ob_depth_boundary_89_90():
    """89% → повторный выбор допустим; ровно 90% → рыночно актуален
    (validity не invalid), но новый вход запрещён (tested_too_deep)."""
    rng = _rng()  # Premium [100;110]
    ob89 = _zone("OB", 100.0, 110.0, validity="tested",
                 max_test_depth=0.89, test_extreme=108.9)
    ev = evaluate_entry(ob89, Direction.BEAR, _CFG, rng)
    assert (ev.reason, ev.state) == (REASON_OK, "fresh")
    ob90 = _zone("OB", 100.0, 110.0, validity="tested",
                 max_test_depth=0.9, test_extreme=109.0)
    ev = evaluate_entry(ob90, Direction.BEAR, _CFG, rng)
    assert (ev.reason, ev.state) == (REASON_TESTED_TOO_DEEP, "tested")
    assert ob90.validity == "tested"        # не invalid — рыночно актуален


# --------------------------------------------------------------------- #
# §10.4: полное перекрытие FVG — терминально и независимо от OB
# --------------------------------------------------------------------- #

def test_fvg_filled_terminal_ob_independent():
    rng = _rng()
    fvg = _zone("FVG", 100.0, 110.0, validity="tested",
                max_test_depth=1.0, test_extreme=110.0)
    ev = evaluate_entry(fvg, Direction.BEAR, _CFG, rng)
    assert (ev.reason, ev.state) == (REASON_FVG_FILLED, "tested")
    assert fvg_fill_status(fvg) == "filled"
    # OB той же геометрии и глубины — НЕ fvg_filled: своё правило 90%
    ob = _zone("OB", 100.0, 110.0, validity="tested",
               max_test_depth=1.0, test_extreme=110.0)
    ev = evaluate_entry(ob, Direction.BEAR, _CFG, rng)
    assert ev.reason == REASON_TESTED_TOO_DEEP
    assert fvg_fill_status(ob) is None
    # статусы перекрытия
    assert fvg_fill_status(_zone("FVG", 100.0, 110.0)) == "open"
    assert fvg_fill_status(_zone("FVG", 100.0, 110.0, validity="tested",
                                 max_test_depth=0.5,
                                 test_extreme=105.0)) == "partially_filled"


# --------------------------------------------------------------------- #
# Движок: FVG, перекрытый до создания зоны, рождается filled
# --------------------------------------------------------------------- #

# Как PASSED (Этап 5), но в хвосте idx22-24 формируется FVG [9.8;9.9] и
# [10.0;10.1]. Сценарий открывается SMS на idx24 (зоны рождаются fresh),
# свеча idx26 касает оба FVG, полностью их перекрывая (High 10.8) —
# перекрытие фиксируется на живом касании (first_test_at — close_time, §9)
FILL_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11),
    (14.0, 12.5), (12.6, 11.2), (11.4, 10.0), (10.8, 9.8),
    (10.6, 10.0), (11.0, 10.2), (11.6, 10.4),
    (11.2, 10.6), (10.7, 10.2), (10.5, 10.0), (10.3, 9.8),
    (10.9, 10.1),                                     # idx21: хай 10.9
    (10.3, 9.9), (10.0, 9.7), (9.8, 9.5),             # idx22-24: FVG-тройки
    (11.3, 10.4),                                     # idx25: Close 11.2
    (10.8, 9.9),                                      # idx26: перекрытие FVG
    (11.4, 9.2),                                      # idx27: фитиль (idx25 не pivot)
    (9.8, 8.6), (9.2, 7.8),                           # idx29: Close 7.9 — BOS
]
FILL_CLOSES = {
    10: 13.0, 11: 11.5, 12: 10.5, 13: 10.0, 14: 10.4, 15: 10.6, 16: 11.0,
    17: 10.8, 18: 10.4, 19: 10.2, 20: 10.0, 21: 10.7, 22: 10.1, 23: 9.9,
    24: 9.7, 25: 11.2, 26: 10.1, 27: 9.4, 28: 8.8, 29: 7.9,
}


def test_fvg_filled_before_activation_born_filled(db: Database, cfg,
                                                  instrument_id: int):
    engine = LtfEngine(db, cfg)
    candles = _series(FILL_HL, FILL_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 29)

    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    zones = {
        (z.lower, z.upper, z.formed_at): z
        for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if z.type == "FVG"
    }
    for bounds in ((10.0, 10.1), (9.8, 9.9)):
        formed = candles[23].open_time if bounds == (10.0, 10.1) \
            else candles[24].open_time
        zone = zones[(bounds[0], bounds[1], formed)]
        assert zone.validity == "tested"                 # не fresh-void
        assert zone.max_test_depth == 1.0
        # живое касание idx26 (§9: first_test_at — close_time свечи)
        assert zone.first_test_at == candles[26].close_time
        entries = [e for e in db.list_ltf_scenario_entries(sc.id)
                   if e.entry_zone_id == zone.id]
        assert entries and all(e.reason == REASON_FVG_FILLED for e in entries)
    # replay не меняет итог
    engine.replay_observation(obs.id)
    for e in db.list_ltf_scenario_entries(sc.id):
        z = db.get_ltf_entry_zone(e.entry_zone_id)
        if z.type == "FVG" and z.max_test_depth == 1.0:
            assert e.reason == REASON_FVG_FILLED


def test_fvg_filled_while_out_of_range_caught_at_rebind(db: Database, cfg,
                                                        instrument_id: int):
    """FVG перекрыт, пока был вне половины диапазона: на новой версии
    диапазона не возвращается в fresh — fvg_filled; OB рядом — своё правило."""
    engine = LtfEngine(db, cfg)
    obs, sc = _mk_scenario(db, instrument_id)
    res = LtfTickResult()
    now = T0 + 10 * H1_MS
    engine._update_range(sc, [], now, res, avail=_PAIR())   # v1 [9;15]
    fvg = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG",
        direction=Direction.BEAR, lower=10.0, upper=11.0, formed_at=T0,
        confirmed_at=T0 + 100, evidence={"fvg_candles": [T0]},
    ))
    ob = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB",
        direction=Direction.BEAR, lower=9.5, upper=10.5, formed_at=T0,
        confirmed_at=T0 + 100,
    ))
    for ez in (fvg, ob):
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
            eligible=False, overlap="none", state="out_of_range",
            reason="outside_pd", added_at=now, updated_at=now,
        ))
    # свечи после формирования: High 11.5 перекрывает FVG [10;11] полностью,
    # OB [9.5;10.5] задет лишь частично (глубина (11.5-9.5)/1.0 > 0.9 — но это
    # его правило, не fvg_filled)
    up_to = make_h1_candles(
        [(10.2, 11.5, 9.9, 10.0)], T0 + H1_MS, instrument_id
    )
    ps2 = _PAIR() + [_mkp("low", 8.0, "LL", 6, 13, confirmed_idx=9)]
    r2 = engine._update_range(sc, up_to, T0 + 12 * H1_MS, res, avail=ps2)
    assert r2 is not None and r2.version == 2

    zone = db.get_ltf_entry_zone(fvg.id)
    assert zone.validity == "tested" and zone.max_test_depth == 1.0
    assert zone.first_test_at == up_to[0].open_time
    fe = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == fvg.id and e.range_version == 2][0]
    assert fe.reason == REASON_FVG_FILLED
    oe = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == ob.id and e.range_version == 2][0]
    assert oe.reason != REASON_FVG_FILLED
    # повторный вызов (идемпотентность) — ничего не меняет
    engine._update_range(sc, up_to, T0 + 12 * H1_MS, res, avail=ps2)
    zone2 = db.get_ltf_entry_zone(fvg.id)
    assert (zone2.max_test_depth, zone2.test_extreme) == (1.0, 11.5)
