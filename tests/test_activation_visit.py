"""Этап 4: активации, визиты и свежесть (правила v1 §7, 10.10.2026).

Покрытие: R16 (OB ≥90% до активации — не свежее основание, текущая активация
не отменена), R17 (90% в текущем визите — сценарий жив), R18/R19 (FVG 100%
до/в активации), R38 (выход на тик и возврат — активация продолжается), R25
(unknown истории не превращается в live-сигнал и не блокирует наблюдение),
одновременная истинность filled + current_activation_valid + not fresh,
связь событий с визитом/активацией, завершение активации проходом своего ТФ.
"""
from __future__ import annotations

from app.engine.ltf.eligibility import (
    REASON_HISTORICALLY_WORKED,
    REASON_OK,
    admitted_scenario_entries,
    evaluate_entry,
    evaluate_final,
)
from app.engine.ltf.engine import LtfEngine
from app.engine.ltf.relevance import ActivationCut, activation_cut
from app.engine.scanner import Scanner
from app.models import Candle, Direction, EventKind, Zone, ZoneStatus, ZoneType
from app.services.htf_context import note_source_outcome
from tests.conftest import H1_MS, make_h1_candles
from tests.test_audit_fixes import make_entry, make_scenario

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000


def _htf_zone(db, iid, ztype=ZoneType.OB, direction=Direction.BULL,
              lower=100.0, upper=110.0, **fields):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ztype, direction=direction,
        timeframe="D1", lower=lower, upper=upper, formed_at=T - 2 * D,
        confirmed_at=T - D, status=ZoneStatus.ACTIVE,
    ))
    if fields:
        db.update_zone(zid, **fields)
    return db.get_zone(zid)


# --------------------------------------------------------------------- #
# R16/R18: отработанность ДО новой активации
# --------------------------------------------------------------------- #

def test_r16_ob_worked_before_activation_blocks_new_observation(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id, max_test_depth=0.95, has_tests=True)
    obs = LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    assert obs is None
    acts = db.list_zone_activations(zone.id)
    assert len(acts) == 1
    assert acts[0]["depth_before"] == 0.95
    assert acts[0]["fresh_for_new_activation"] == 0
    assert acts[0]["observation_id"] is None
    assert acts[0]["ended_at"] is None


def test_r16_current_activation_continues_despite_later_depth(db, cfg, instrument_id):
    # активация открыта на свежей зоне; глубина 90% накоплена уже В ней —
    # наблюдение и активация живут (R17 на уровне HTF-зоны)
    zone = _htf_zone(db, instrument_id, lower=100.0, upper=104.0,
                     direction=Direction.BEAR)
    engine = LtfEngine(db, cfg)
    obs = engine.on_htf_zone_touched(instrument_id, zone, T)
    assert obs is not None
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 103.8, T + H)   # глубина 0.95 в визите
    fresh = db.get_zone(zone.id)
    assert engine.check_parent_validity(fresh) is False
    assert db.get_ltf_observation(obs.id).state not in ("closed_by_parent",)
    act = db.get_open_zone_activation(zone.id, zone.cycle_id)
    assert act is not None and act["ended_at"] is None
    assert act["max_depth_current"] >= 0.9
    # WORKED/не-eligible по глубине — механика зоны, не отмена наблюдения
    db.update_zone(zone.id, entry_eligible=False, status=ZoneStatus.WORKED)
    assert engine.check_parent_validity(db.get_zone(zone.id)) is False


def test_r18_fvg_filled_before_activation_not_fresh(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id, ztype=ZoneType.FVG)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 105.0, T - 2 * H)
    scanner.on_price(instrument_id, 99.0, T - H)    # полное заполнение ДО
    obs = LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    assert obs is None
    act = db.list_zone_activations(zone.id)[0]
    assert act["fill_before"] == 1.0
    assert act["fresh_for_new_activation"] == 0


# --------------------------------------------------------------------- #
# R17/R19: eligibility в текущей активации (отсечка §7)
# --------------------------------------------------------------------- #

def _deep_entry_setup(db, iid, deep_candle_index):
    """Сценарий + OB-зона [100;104] с lifetime-глубиной 0.95; свечи H1 с
    глубоким тестом на указанном индексе относительно активации (T)."""
    obs, sc = make_scenario(db, iid)
    z, e = make_entry(db, iid, sc, validity="tested", depth=0.95)
    db.update_ltf_entry_zone(z.id, test_extreme=103.8)
    # история H1 вокруг активации (obs.activated_at = T); базовые свечи
    # ниже медвежьей зоны [100;104] — касаний нет; глубокий тест — high 103.8
    bars = [(98.0, 99.0, 97.5, 98.5)] * 6
    bars[deep_candle_index] = (98.5, 103.8, 98.0, 103.0)
    candles = make_h1_candles(bars, T - 4 * H1_MS, instrument_id=iid)
    db.insert_candles(candles)
    return obs, sc, z, e


def test_r16_eligibility_historically_worked(db, cfg, instrument_id):
    # глубокий тест был ДО активации (индекс 3: T-H..T) — не свежее основание
    obs, sc, z, e = _deep_entry_setup(db, instrument_id, 3)
    admitted = admitted_scenario_entries(db, sc.id, cfg=cfg)
    assert admitted == []
    fe = evaluate_final(
        e, db.get_ltf_entry_zone(z.id), allow_outside=False, cfg=cfg,
        cut=activation_cut(db, db.get_ltf_entry_zone(z.id), obs.activated_at, cfg),
    )
    assert fe.primary_reason == REASON_HISTORICALLY_WORKED


def test_r17_deep_test_within_activation_not_blocking(db, cfg, instrument_id):
    # глубокий тест — в текущем активирующем визите (индекс 5 > 4):
    # зона остаётся допустимой, сценарий не отменяется
    obs, sc, z, e = _deep_entry_setup(db, instrument_id, 5)
    admitted = admitted_scenario_entries(db, sc.id, cfg=cfg)
    assert [zone.id for _, zone, _ in admitted] == [z.id]
    cut = activation_cut(db, db.get_ltf_entry_zone(z.id), obs.activated_at, cfg)
    assert cut.depth_before == 0.0
    fe = evaluate_final(e, db.get_ltf_entry_zone(z.id), allow_outside=False,
                        cfg=cfg, cut=cut)
    assert fe.eligible_now is True


def test_r17_unknown_history_keeps_lifetime_semantics(db, cfg, instrument_id):
    # нет свечей до активации — unknown (R25): не «свежий сигнал», применяется
    # консервативный максимум за жизнь (прежнее поведение)
    obs, sc = make_scenario(db, instrument_id)
    z, e = make_entry(db, instrument_id, sc, validity="tested", depth=0.95)
    assert admitted_scenario_entries(db, sc.id, cfg=cfg) == []
    assert admitted_scenario_entries(
        db, sc.id, cfg=type(cfg)(entry_reuse_max_depth=0.98)
    )


def test_r19_fvg_filled_within_activation_keeps_basis(db, cfg, instrument_id):
    obs, sc = make_scenario(db, instrument_id)
    z = db.insert_ltf_entry_zone(__import__("app.models_ltf", fromlist=["LtfEntryZone"]).LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BEAR,
        lower=100, upper=104, formed_at=T - 2 * H, confirmed_at=T - H,
        validity="tested", max_test_depth=1.0, test_extreme=100.0,
    ))
    from app.models_ltf import LtfScenarioEntry
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=1,
        eligible=True, overlap="full", state="fresh", reason="ok",
        added_at=T, updated_at=T,
    ))
    entry = db.list_ltf_scenario_entries(sc.id)[0]
    zone = db.get_ltf_entry_zone(z.id)
    # заполнение ДО активации — не свежий FVG
    filled = ActivationCut(1.0, True, 100.0)
    assert evaluate_final(entry, zone, allow_outside=False, cfg=cfg,
                          cut=filled).primary_reason == "fvg_filled"
    # заполнение В текущей активации — основание сохранено
    fresh_cut = ActivationCut(0.4, False, 101.6)
    fe = evaluate_final(entry, zone, allow_outside=False, cfg=cfg, cut=fresh_cut)
    assert fe.eligible_now is True


# --------------------------------------------------------------------- #
# R38/R25: активация ≠ визит; unknown-история
# --------------------------------------------------------------------- #

def test_r38_tick_out_and_return_continues_activation(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 105.0, T + H)        # заход
    scanner.on_price(instrument_id, 99.0, T + 2 * H)     # выход на тик
    scanner.on_price(instrument_id, 108.0, T + 3 * H)    # возврат — тот же…
    scanner.on_price(instrument_id, 99.5, T + 4 * H)     # …и снова выход
    acts = db.list_zone_activations(zone.id)
    assert len(acts) == 1                       # новой активации нет
    assert acts[0]["ended_at"] is None          # активация продолжается
    visits = db.get_visits(zone.id, zone.cycle_id)
    assert len(visits) == 2                     # визиты — разные сущности


def test_r25_unknown_history_not_fresh_but_not_blocked(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id, needs_replay=True)
    obs = LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    # unknown не блокирует наблюдение, но и не считается свежим
    assert obs is not None
    act = db.list_zone_activations(zone.id)[0]
    assert act["depth_before"] is None
    assert act["fill_before"] is None
    assert act["fresh_for_new_activation"] == 0


# --------------------------------------------------------------------- #
# Факты текущего визита: глубина в активации, связи событий, завершение
# --------------------------------------------------------------------- #

def test_activation_tracks_current_visit_and_links_events(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id, ztype=ZoneType.FVG,
                     lower=100.0, upper=101.0)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    scanner = Scanner(db, cfg)
    ev = scanner.on_price(instrument_id, 100.2, T + H)
    act = db.get_open_zone_activation(zone.id, zone.cycle_id)
    assert act is not None
    visit = db.open_visit_for(zone.id, zone.cycle_id)
    assert ev and ev[0].activation_id == act["id"]
    assert visit is not None and ev[0].visit_id == visit.id
    assert act["max_depth_current"] == visit.max_depth
    # заполнение в текущем визите — активация жива, filled+valid одновременно
    scanner.on_price(instrument_id, 99.0, T + 2 * H)
    act = db.get_open_zone_activation(zone.id, zone.cycle_id)
    assert act["fill_current"] == 1.0
    assert act["current_activation_valid"] == 1
    assert act["ended_at"] is None


def test_simultaneous_truth_flags(db, instrument_id):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=100, upper=101,
        formed_at=T - D, confirmed_at=T - H, status=ZoneStatus.ACTIVE,
    ))
    aid = db.create_zone_activation(
        zid, 1, T, depth_before=1.0, fill_before=True,
        fresh_for_new_activation=False, created_at=T,
    )
    db.update_zone_activation(aid, fill_current=1.0, max_depth_current=1.0)
    act = db.get_zone_activation(aid)
    assert act["fill_current"] == 1.0
    assert act["current_activation_valid"] == 1
    assert act["fresh_for_new_activation"] == 0


def test_activation_ends_on_confirmed_pass(db, cfg, instrument_id):
    zone = _htf_zone(db, instrument_id)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, T)
    db.update_zone(zone.id, market_validity="invalid",
                   evidence={"invalidated_at": T + D})
    note_source_outcome(db, db.get_zone(zone.id), T + D)
    act = db.list_zone_activations(zone.id)[0]
    assert act["ended_at"] == T + D
    assert act["end_reason"] == "source_passed"


# --------------------------------------------------------------------- #
# Причинное восстановление на отсечку (relevance)
# --------------------------------------------------------------------- #

def test_activation_cut_reconstructs_two_timelines(db, cfg, instrument_id):
    obs, sc = make_scenario(db, instrument_id)
    z, _ = make_entry(db, instrument_id, sc, validity="tested", depth=0.95)
    bars = [(98.0, 99.0, 97.5, 98.5)] * 6
    bars[5] = (98.5, 103.8, 98.0, 103.0)     # глубокий тест на T+H..T+2H
    db.insert_candles(make_h1_candles(bars, T - 4 * H1_MS, instrument_id=instrument_id))
    zone = db.get_ltf_entry_zone(z.id)
    before = activation_cut(db, zone, T - 2 * H1_MS - 1, cfg)
    after = activation_cut(db, zone, T + 2 * H1_MS, cfg)
    assert before.depth_before == 0.0
    assert after.depth_before == 0.95
    assert after.extreme_before == 103.8


def test_list_activations_at(db, instrument_id):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=100, upper=110,
        formed_at=T - D, confirmed_at=T - H, status=ZoneStatus.ACTIVE,
    ))
    aid = db.create_zone_activation(zid, 1, T, created_at=T)
    assert [a["id"] for a in db.list_zone_activations_at(zid, T + H)] == [aid]
    assert db.list_zone_activations_at(zid, T - 1) == []
    db.update_zone_activation(aid, ended_at=T + 2 * H, end_reason="source_passed")
    assert db.list_zone_activations_at(zid, T + H) != []
    assert db.list_zone_activations_at(zid, T + 2 * H) == []
