"""§10: двухэтапное снятие BSL/SSL (приёмка п.13–14)."""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine, resolve_sweep
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfLiquidityTest,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from tests.conftest import H1_MS, make_candle

T0 = 1_780_000_000_000


def test_resolve_sweep_truth_table():
    # BSL: High>K и Close<K → confirmed (п.13)
    assert resolve_sweep("BSL", 100, make_candle(T0, 99, 100.5, 98, 99.8, timeframe="H1")) == "confirmed"
    # High>K, Close>K — возврата нет → failed
    assert resolve_sweep("BSL", 100, make_candle(T0, 99, 100.5, 98, 100.4, timeframe="H1")) == "failed"
    # High=K — равное касание без снятия; Close=K — не строгий возврат
    assert resolve_sweep("BSL", 100, make_candle(T0, 99, 100.0, 98, 99.0, timeframe="H1")) == "equal_close"
    assert resolve_sweep("BSL", 100, make_candle(T0, 99, 100.5, 98, 100.0, timeframe="H1")) == "equal_close"
    # незакрытая свеча — исхода нет
    assert resolve_sweep("BSL", 100, make_candle(T0, 99, 100.5, 98, 99.8, timeframe="H1", closed=False)) is None
    # SSL зеркально: Low<K и Close>K → confirmed
    assert resolve_sweep("SSL", 100, make_candle(T0, 101, 101.5, 99.5, 100.4, timeframe="H1")) == "confirmed"
    assert resolve_sweep("SSL", 100, make_candle(T0, 101, 101.5, 99.5, 99.6, timeframe="H1")) == "failed"
    assert resolve_sweep("SSL", 100, make_candle(T0, 101, 101.5, 100.0, 100.5, timeframe="H1")) == "equal_close"


def _setup(db: Database, instrument_id: int, direction: Direction,
           range_bounds=(90.0, 110.0)):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=direction, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=direction, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=range_bounds[0],
        upper=range_bounds[1], mid=(range_bounds[0] + range_bounds[1]) / 2,
        available_at=T0,
    ))
    return obs, sc


def _add_level(db: Database, instrument_id: int, sc, type_: str, level: float,
               direction: Direction) -> LtfEntryZone:
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type=type_, direction=direction,
        lower=level, upper=level, formed_at=T0, confirmed_at=T0 + 1000,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
        eligible=True, overlap="full", state="fresh",
    ))
    return ez


def _feed(db: Database, engine: LtfEngine, instrument_id: int, candle):
    db.insert_candles([candle])
    engine.process_h1_close(instrument_id, now_ms=candle.close_time)


def test_bsl_sweep_confirmed_and_no_revival(db: Database, cfg, instrument_id: int):
    """п.13–14: касание создаёт тест, закрытие той же свечи — исход;
    уровень не оживает для нового входа."""
    obs, sc = _setup(db, instrument_id, Direction.BEAR)
    ez = _add_level(db, instrument_id, sc, "BSL", 100.0, Direction.BEAR)
    engine = LtfEngine(db, cfg)
    t1 = T0 + 500 * H1_MS

    # High>K, Close<K → снятие подтверждено на закрытии той же свечи
    c1 = make_candle(t1, 99.0, 100.5, 98.5, 99.8, timeframe="H1",
                     instrument_id=instrument_id)
    _feed(db, engine, instrument_id, c1)
    tests = db.list_ltf_liquidity_tests(scenario_id=sc.id)
    assert len(tests) == 1
    t = tests[0]
    assert t.state == "confirmed"          # не остался awaiting_close (п.14)
    assert t.candle_open_time == t1
    assert t.close_price == 99.8 and t.sweep_at == c1.close_time
    kinds = [e.kind for e in sorted(db.list_ltf_events(observation_id=obs.id),
                                    key=lambda e: e.id)]
    assert kinds == ["touch", "sweep_confirmed"]
    assert db.list_ltf_events(observation_id=obs.id)[0].payload["entry_zone_id"] == ez.id
    # зона выведена из свежих
    entries = db.list_ltf_scenario_entries(sc.id, state="fresh")
    assert entries == []

    # повторное касание уровня — НЕ оживляет его: ни теста, ни событий (п.14)
    c2 = make_candle(t1 + H1_MS, 99.5, 100.3, 99.0, 99.6, timeframe="H1",
                     instrument_id=instrument_id)
    _feed(db, engine, instrument_id, c2)
    assert len(db.list_ltf_liquidity_tests(scenario_id=sc.id)) == 1
    assert len(db.list_ltf_events(observation_id=obs.id)) == 2


def test_bsl_failed_and_equal_close(db: Database, cfg, instrument_id: int):
    obs, sc = _setup(db, instrument_id, Direction.BEAR)
    ez_fail = _add_level(db, instrument_id, sc, "BSL", 102.0, Direction.BEAR)
    ez_eq = _add_level(db, instrument_id, sc, "BSL", 104.0, Direction.BEAR)
    engine = LtfEngine(db, cfg)
    t1 = T0 + 500 * H1_MS
    # High>K, Close>K — возврата нет → failed (не ждём ещё свечей, §10)
    _feed(db, engine, instrument_id,
          make_candle(t1, 101.0, 102.5, 100.5, 102.3, timeframe="H1",
                      instrument_id=instrument_id))
    # Close=K — неподтверждённый исход → equal_close
    _feed(db, engine, instrument_id,
          make_candle(t1 + H1_MS, 103.0, 104.3, 101.5, 104.0, timeframe="H1",
                      instrument_id=instrument_id))
    tests = {t.entry_zone_id: t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)}
    assert tests[ez_fail.id].state == "failed"
    assert tests[ez_fail.id].sweep_at is None
    assert tests[ez_eq.id].state == "equal_close"
    events = db.list_ltf_events(observation_id=obs.id)
    failed = [e for e in events if e.kind == "sweep_failed"]
    assert len(failed) == 2
    assert {e.payload["outcome"] for e in failed} == {"failed", "equal_close"}


def test_ssl_sweep_confirmed_mirror(db: Database, cfg, instrument_id: int):
    obs, sc = _setup(db, instrument_id, Direction.BULL)
    ez = _add_level(db, instrument_id, sc, "SSL", 98.0, Direction.BULL)
    engine = LtfEngine(db, cfg)
    t1 = T0 + 500 * H1_MS
    # SSL: Low<K и Close>K → confirmed (зеркало §10)
    _feed(db, engine, instrument_id,
          make_candle(t1, 99.0, 99.5, 97.5, 98.6, timeframe="H1",
                      instrument_id=instrument_id))
    t = db.list_ltf_liquidity_tests(scenario_id=sc.id)[0]
    assert t.state == "confirmed"
    events = sorted(db.list_ltf_events(observation_id=obs.id), key=lambda e: e.id)
    assert [e.kind for e in events] == ["touch", "sweep_confirmed"]


def test_awaiting_test_resolved_on_candle_close(db: Database, cfg, instrument_id: int):
    """п.14: тест, начатый внутри свечи (live-путь), завершается её закрытием,
    даже если касание было раньше обработки."""
    obs, sc = _setup(db, instrument_id, Direction.BEAR)
    ez = _add_level(db, instrument_id, sc, "BSL", 100.0, Direction.BEAR)
    # зона уже выведена из свежих, тест ждёт закрытия — как при live-тике
    db.update_ltf_entry_zone(ez.id, validity="tested", first_test_at=T0 + 1)
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
        eligible=True, overlap="full", state="tested",
    ))
    t1 = T0 + 500 * H1_MS
    tid = db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=ez.id, scenario_id=sc.id, level=100.0,
        touch_at=t1, candle_open_time=t1,
    ))
    c1 = make_candle(t1, 99.0, 100.5, 98.5, 99.7, timeframe="H1",
                     instrument_id=instrument_id)
    db.insert_candles([c1])
    engine = LtfEngine(db, cfg)
    engine.process_h1_close(instrument_id, now_ms=c1.close_time)
    t = [x for x in db.list_ltf_liquidity_tests(scenario_id=sc.id) if x.id == tid][0]
    assert t.state == "confirmed"
    # дублей теста/касания не создано
    assert len(db.list_ltf_liquidity_tests(scenario_id=sc.id)) == 1
    events = sorted(db.list_ltf_events(observation_id=obs.id), key=lambda e: e.id)
    assert [e.kind for e in events] == ["sweep_confirmed"]
