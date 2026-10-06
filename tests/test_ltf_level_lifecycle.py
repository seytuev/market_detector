"""§9 (Этап 5): жизненный цикл уровня BSL/SSL — терминальные исходы.

- Close за уровнем на закрытой H1 (касанием или разрывом) — уровень пройден
  без возврата (broken): терминально, не воскресает от версий диапазона,
  повторной обработки и возврата цены за уровень позже.
- High>K и Close<K — снятие с возвратом (прежнее confirmed).
- High==K / Close==K — equal_close: исход неопределён, но не терминален;
  позднее закрытие за уровнем финализирует broken.
- Уровень, пройденный до активации зоны (после рождения экстремума),
  рождается пройденным — «свежим» не становится.
"""
from __future__ import annotations

from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.eligibility import REASON_LEVEL_BROKEN, evaluate_entry
from app.models import Direction
from app.services.overview import _entry_row
from tests.conftest import H1_MS
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _events,
    _feed,
    _setup,
)

HEAD = 15  # idx0..14 серии H: открытие сценария на BOS (idx14)


def _head_candles(instrument_id: int):
    return _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)[:HEAD]


def _mk(db: Database, instrument_id: int, bars):
    """Свечи head серии H + произвольные (o, h, l, c) бары дальше."""
    from tests.conftest import make_h1_candles

    head = _head_candles(instrument_id)
    tail = make_h1_candles(bars, head[-1].open_time + H1_MS, instrument_id)
    return head + tail


def _bsl15(db: Database, instrument_id: int):
    return [
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if z.type == "BSL" and z.lower == 15.0
    ][0]


def test_close_beyond_level_is_terminal_broken(db: Database, cfg,
                                               instrument_id: int):
    """Касание с закрытием за уровнем → broken (failed): зона tested,
    привязка level_broken, возврат цены за уровень исход не меняет."""
    engine = LtfEngine(db, cfg)
    candles = _mk(db, instrument_id, [
        (15.0, 16.0, 14.0, 15.5),   # idx15: касание K=15, Close 15.5 > K
        (14.0, 15.4, 13.0, 13.5),   # idx16: возврат ниже — исход не меняется
    ])
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, HEAD - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state != "cancelled"

    _feed(db, engine, instrument_id, candles, HEAD)      # свеча пробоя
    zone = _bsl15(db, instrument_id)
    assert zone.validity == "tested"
    assert zone.first_test_at == candles[HEAD].close_time
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert len(tests) == 1
    t = tests[0]
    assert t.state == "failed"                          # broken_without_reclaim
    assert t.close_price == 15.5
    assert t.candle_open_time == candles[HEAD].open_time
    assert t.resolved_at == candles[HEAD].close_time
    entries = [e for e in db.list_ltf_scenario_entries(sc.id)
               if e.entry_zone_id == zone.id]
    assert entries and all(e.reason == REASON_LEVEL_BROKEN for e in entries)
    kinds = [e.kind for e in _events(db, obs.id)]
    assert kinds == ["bos", "touch", "sweep_failed"]
    fail = _events(db, obs.id)[-1]
    assert fail.payload["outcome"] == "failed"

    # возврат ниже K: записанный исход пробойной свечи не переписывается
    _feed(db, engine, instrument_id, candles, HEAD + 1)
    t2 = db.list_ltf_liquidity_tests(scenario_id=sc.id)[0]
    assert (t2.state, t2.close_price) == ("failed", 15.5)
    assert [e.kind for e in _events(db, obs.id)] == kinds
    # не воскресает в пригодности (та же конъюнкция, что у rebinding версий)
    ev = evaluate_entry(db.get_ltf_entry_zone(zone.id), Direction.BEAR, cfg,
                        None, liquidity_tests=db.list_ltf_liquidity_tests(
                            scenario_id=sc.id))
    assert ev.reason == REASON_LEVEL_BROKEN and ev.state == "tested"
    # replay не дублирует тест/события
    engine.replay_observation(obs.id)
    assert len(db.list_ltf_liquidity_tests(scenario_id=sc.id)) == 1
    assert [e.kind for e in _events(db, obs.id)] == kinds


def test_gap_close_beyond_without_touch(db: Database, cfg, instrument_id: int):
    """Разрыв через уровень (Low > K, касания нет): Close > K — тоже broken."""
    engine = LtfEngine(db, cfg)
    candles = _mk(db, instrument_id, [
        (15.4, 16.0, 15.2, 15.5),   # idx15: вся свеча выше K=15 — касания нет
    ])
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, HEAD - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]

    _feed(db, engine, instrument_id, candles, HEAD)
    zone = _bsl15(db, instrument_id)
    assert zone.validity == "tested"        # не «свежая ликвидность»
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert len(tests) == 1 and tests[0].state == "failed"
    kinds = [e.kind for e in _events(db, obs.id)]
    assert kinds == ["bos", "sweep_failed"]  # touch не было


def test_equal_close_not_terminal_then_broken(db: Database, cfg,
                                              instrument_id: int):
    """High == K (касание без превышения) — equal_close: не снятие и не
    пробой, уровень остаётся кандидатом; позднее закрытие за K — broken."""
    engine = LtfEngine(db, cfg)
    candles = _mk(db, instrument_id, [
        (14.6, 15.0, 14.0, 14.5),   # idx15: High ровно K=15, Close ниже
        (15.0, 16.0, 14.5, 15.6),   # idx16: Close 15.6 > K — пробой
    ])
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, HEAD - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]

    _feed(db, engine, instrument_id, candles, HEAD)
    zone = _bsl15(db, instrument_id)
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert len(tests) == 1 and tests[0].state == "equal_close"
    entry = [e for e in db.list_ltf_scenario_entries(sc.id)
             if e.entry_zone_id == zone.id][0]
    assert entry.reason != REASON_LEVEL_BROKEN     # уровень жив
    assert [e.kind for e in _events(db, obs.id)] == ["bos", "touch",
                                                     "sweep_failed"]

    _feed(db, engine, instrument_id, candles, HEAD + 1)
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert [t.state for t in tests] == ["equal_close", "failed"]
    entry = [e for e in db.list_ltf_scenario_entries(sc.id)
             if e.entry_zone_id == zone.id][0]
    assert entry.reason == REASON_LEVEL_BROKEN


def test_sweep_reclaimed_preserved(db: Database, cfg, instrument_id: int):
    """High > K и Close < K на одной свече — снятие с возвратом (confirmed):
    прежнее поведение сохранено, reason swept_level."""
    engine = LtfEngine(db, cfg)
    candles = _mk(db, instrument_id, [
        (14.4, 15.6, 14.0, 14.5),   # High 15.6 > K=15, Close 14.5 < K
    ])
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, HEAD - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]

    _feed(db, engine, instrument_id, candles, HEAD)
    zone = _bsl15(db, instrument_id)
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert len(tests) == 1 and tests[0].state == "confirmed"
    entry = [e for e in db.list_ltf_scenario_entries(sc.id)
             if e.entry_zone_id == zone.id][0]
    assert entry.reason == "swept_level"
    assert [e.kind for e in _events(db, obs.id)] == [
        "bos", "touch", "sweep_confirmed",
    ]


# Серия «пробой до активации»: откатный хай 10.9 (idx21) — BSL внутри
# медвежьего движения; свеча idx25 закрывается выше 10.9 (уровень пройден)
# ДО слома структуры и до создания зоны; далее снижение до BOS (idx29)
PASSED_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11),                    # HH 15.0 (idx9), HL 9.0
    (14.0, 12.5), (12.6, 11.2), (11.4, 10.0), (10.8, 9.8),
    (10.6, 10.0), (11.0, 10.2), (11.6, 10.4),         # откатный хай 11.6
    (11.2, 10.6), (10.7, 10.2), (10.5, 10.0), (10.3, 9.8),
    (10.9, 10.1),                                     # idx21: BSL 10.9
    (10.5, 9.9), (10.2, 9.7), (10.0, 9.5),
    (11.3, 10.4),                                     # idx25: Close 11.2 > 10.9
    (10.8, 9.9), (11.4, 9.2),                         # фитиль 11.4: idx25 — не pivot
    (9.8, 8.6), (9.2, 7.8),                           # idx29: Close 7.9 — BOS
]
PASSED_CLOSES = {
    10: 13.0, 11: 11.5, 12: 10.5, 13: 10.0, 14: 10.4, 15: 10.6, 16: 11.0,
    17: 10.8, 18: 10.4, 19: 10.2, 20: 10.0, 21: 10.7, 22: 10.1, 23: 9.9,
    24: 9.7, 25: 11.2, 26: 10.1, 27: 9.4, 28: 8.8, 29: 7.9,
}


def test_level_passed_before_activation_born_broken(db: Database, cfg,
                                                    instrument_id: int):
    """§9: уровень, пройденный закрытием после рождения экстремума, но до
    создания/активации зоны, рождается пройденным — fresh не становится."""
    engine = LtfEngine(db, cfg)
    candles = _series(PASSED_HL, PASSED_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 29)

    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 1
    sc = scenarios[0]
    assert sc.state != "cancelled"
    zone = [
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if z.type == "BSL" and z.lower == 10.9
    ][0]
    assert zone.validity == "tested"              # НЕ fresh
    assert zone.first_test_at == candles[25].close_time
    tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
             if t.entry_zone_id == zone.id]
    assert len(tests) == 1
    assert tests[0].state == "failed"
    assert tests[0].close_price == 11.2
    assert tests[0].candle_open_time == candles[25].open_time
    entries = [e for e in db.list_ltf_scenario_entries(sc.id)
               if e.entry_zone_id == zone.id]
    assert entries and all(e.reason == REASON_LEVEL_BROKEN for e in entries)
    fail = [e for e in _events(db, obs.id) if e.kind == "sweep_failed"]
    assert len(fail) == 1
    assert fail[0].occurred_at == candles[25].close_time
    # replay не дублирует ни тест, ни событие
    engine.replay_observation(obs.id)
    assert len(db.list_ltf_liquidity_tests(scenario_id=sc.id)) == 1
    assert len([e for e in _events(db, obs.id)
                if e.kind == "sweep_failed"]) == 1


def test_point_level_depth_is_null_in_api(db: Database, cfg, instrument_id: int):
    """§9: точечный уровень (W=0) глубины не имеет — в API null (UI «—»);
    у зон с шириной глубина — число."""
    engine = LtfEngine(db, cfg)
    candles = _head_candles(instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, HEAD - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    entries = db.list_ltf_scenario_entries(sc.id)
    rows = {
        db.get_ltf_entry_zone(e.entry_zone_id).type:
            _entry_row(db, e, db.get_ltf_entry_zone(e.entry_zone_id), sc, None)
        for e in entries
    }
    assert rows["BSL"]["is_level"] is True
    assert rows["BSL"]["max_test_depth"] is None
    assert rows["FVG"]["max_test_depth"] is not None
