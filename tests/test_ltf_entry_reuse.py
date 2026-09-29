"""ТЗ «Единый движок HTF/LTF» §2–§4: повторное использование протестированной
Entry Zone (приёмка B/C/D).

- допуск к повторному выбору по максимальной глубине тестов СТРОГО < 0.90
  (ровно 0.90 недопустимо, точное сравнение без epsilon);
- глубина накапливается за всю историю: поздний мелкий тест не стирает
  более глубокий прежний;
- validity остаётся фактом истории: зона на 90%+ рыночно актуальна
  (не invalid), недоступен только новый вход;
- touch-событие не дублируется при повторных касаниях (§11.5).
"""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import (
    LtfEngine,
    assign_roles,
    build_movement,
    confirmed_pivots,
    detect_entry_zones,
    entry_reusable,
    find_h1_pivots,
)
from app.engine.ltf.breaks import StructureEventDraft
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfScenario,
    LtfScenarioEntry,
)
from tests.conftest import H1_MS, make_h1_candles

T0 = 1_780_000_000_000
NOW = T0 + 1000 * H1_MS


def _zone(direction: Direction, lower: float, upper: float,
          depth: float = 0.0, extreme=None) -> LtfEntryZone:
    return LtfEntryZone(
        id=None, instrument_id=1, type="OB", direction=direction,
        lower=lower, upper=upper, formed_at=T0, validity="tested",
        max_test_depth=depth, test_extreme=extreme,
    )


# --------------------------------------------------------------------- #
# Приёмка C: пороги допуска 89% / 90% / 95% (юнит-уровень)
# --------------------------------------------------------------------- #

def test_reuse_thresholds_bear():
    cfg = DetectorConfig()
    # медвежий OB [98;102]: W=4, P90 = 98 + 0.9·4 = 101.6
    assert entry_reusable(_zone(Direction.BEAR, 98, 102, 0.89, 101.56), cfg)
    assert not entry_reusable(_zone(Direction.BEAR, 98, 102, 0.90, 101.6), cfg)
    assert not entry_reusable(_zone(Direction.BEAR, 98, 102, 0.95, 101.8), cfg)


def test_reuse_thresholds_bull():
    cfg = DetectorConfig()
    # бычий OB [98;102]: P90 = 102 − 3.6 = 98.4
    assert entry_reusable(_zone(Direction.BULL, 98, 102, 0.89, 98.44), cfg)
    assert not entry_reusable(_zone(Direction.BULL, 98, 102, 0.90, 98.4), cfg)
    assert not entry_reusable(_zone(Direction.BULL, 98, 102, 0.95, 98.2), cfg)


def test_exact_p90_no_epsilon():
    """Точность: экстремум ровно на P90 недопустим (без epsilon); шагом
    мельче порога внутрь зоны — допустим."""
    cfg = DetectorConfig()
    # бычий [10;20]: P90 = 11.0; медвежий: P90 = 19.0
    assert not entry_reusable(_zone(Direction.BULL, 10.0, 20.0, 0.9, 11.0), cfg)
    assert entry_reusable(_zone(Direction.BULL, 10.0, 20.0, 0.89, 11.0001), cfg)
    assert not entry_reusable(_zone(Direction.BEAR, 10.0, 20.0, 0.9, 19.0), cfg)
    assert entry_reusable(_zone(Direction.BEAR, 10.0, 20.0, 0.89, 18.9999), cfg)


def test_reuse_fallback_without_extreme():
    """Записи без сохранённого экстремума (старые строки) — допуск по
    накопленной колонке max_test_depth."""
    cfg = DetectorConfig()
    assert entry_reusable(_zone(Direction.BEAR, 98, 102, depth=0.5), cfg)
    assert not entry_reusable(_zone(Direction.BEAR, 98, 102, depth=0.9), cfg)


# --------------------------------------------------------------------- #
# Приёмка D: история глубины не стирается поздним мелким тестом
# --------------------------------------------------------------------- #

# Медвежья нога: база idx3, импульс idx4 (пик 15.6), слом idx10; затем
# глубокий возврат в OB [14.4;15.6] на 95% (idx11) и мелкий на 20% (idx12)
REUSE_HL = [
    (13.0, 12.6), (13.6, 13.0), (14.2, 13.6), (15.0, 14.4), (15.6, 14.9),
    (14.8, 13.5), (13.8, 13.0), (13.2, 12.0), (12.8, 11.5), (11.8, 10.2),
    (10.5, 9.0),
    (15.54, 14.5),   # тест OB на 95%: (15.54−14.4)/1.2 = 0.95
    (14.64, 13.9),   # поздний тест на 20%: (14.64−14.4)/1.2 = 0.2
]
REUSE_CLOSES = {4: 15.0, 10: 8.9}


def test_max_depth_history_not_erased_by_late_shallow_test():
    bars = [((h + l) / 2, h, l, REUSE_CLOSES.get(i, (h + l) / 2))
            for i, (h, l) in enumerate(REUSE_HL)]
    candles = make_h1_candles(bars, T0)
    pivots = find_h1_pivots(candles, 3, 3)
    avail = confirmed_pivots(pivots, NOW)
    res = assign_roles(avail)
    for i, p in enumerate(avail):
        p.role = res.roles[i]
    ev = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BEAR, break_level=9.0,
        break_candle_open_time=T0 + 10 * H1_MS,
        occurred_at=T0 + 11 * H1_MS - 1, detected_at=NOW,
        level_key="bos:primary:test",
    )
    mv = build_movement(1, avail, candles, ev, Direction.BEAR,
                        lookback_ms=17 * H1_MS)
    assert mv is not None
    det = detect_entry_zones(candles, mv, avail, Direction.BEAR,
                             DetectorConfig())
    ob = [z for z in det.zones
          if (z.type, z.lower, z.upper) == ("OB", 14.4, 15.6)][0]
    assert ob.validity == "tested"
    # D: максимум за всю историю — 95%; поздний тест 20% его не стирает
    assert ob.max_test_depth == pytest.approx(0.95)
    assert ob.test_extreme == 15.54


# --------------------------------------------------------------------- #
# Приёмка B/C: сквозной поток движка — мелкий тест допускает повторный
# выбор на новой версии диапазона, глубокий (>= 90%) — нет
# --------------------------------------------------------------------- #

# Медвежья структура для новой пары LH→LL: low1 idx3 (92.0), high1 idx6
# (99.0), low2 idx9 (90.5) — пара подтверждается на закрытии idx12 и даёт
# диапазон [90.5; 99.0], Premium [94.75; 99.0] накрывает OB [98;102]
REUSE_FLOW = [
    (96.5, 97.0, 96.0, 96.5),
    (95.5, 96.5, 95.0, 95.8),
    (94.0, 96.0, 93.5, 94.2),
    (92.5, 95.0, 92.0, 92.6),   # low1 = 92.0
    (96.0, 96.5, 92.5, 96.2),
    (98.0, 98.8, 96.0, 98.4),   # тест OB [98;102] на 20% (high 98.8)
    (98.5, 99.0, 97.5, 98.6),   # high1 = 99.0 (касание 25%, зона уже tested)
    (95.0, 98.0, 94.5, 95.2),
    (92.0, 96.0, 91.5, 92.1),
    (91.0, 93.0, 90.5, 91.2),   # low2 = 90.5
    (92.0, 92.5, 91.0, 92.2),
    (93.0, 93.5, 92.0, 93.2),
    (94.0, 94.5, 93.0, 94.1),   # low2 подтверждён (3 правые) → диапазон v1
]
T_FLOW = T0 + 500 * H1_MS


def _setup_pending(db: Database, instrument_id: int):
    """HTF-родитель + наблюдение + сценарий в range_pending (диапазона нет)."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="range_pending",
    ))
    return obs, sc


def _add_ob(db: Database, instrument_id: int, sc: LtfScenario,
            validity: str, state: str, depth: float = 0.0,
            extreme=None, first_test_at=None) -> LtfEntryZone:
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB",
        direction=Direction.BEAR, lower=98.0, upper=102.0, formed_at=T0,
        confirmed_at=T0 + 1000, validity=validity,
        max_test_depth=depth, test_extreme=extreme,
        first_test_at=first_test_at,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=0,
        eligible=True, overlap="pending", state=state,
    ))
    return ez


def _feed(db: Database, engine: LtfEngine, instrument_id: int, candles,
          upto: int):
    for c in candles[: upto + 1]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)


def _entry_row(db: Database, sc_id: int, zone_id: int, ver: int):
    rows = [e for e in db.list_ltf_scenario_entries(sc_id)
            if e.entry_zone_id == zone_id and e.range_version == ver]
    return rows[0] if rows else None


def test_shallow_tested_ob_reselected_on_new_range(db: Database, cfg,
                                                   instrument_id: int):
    """B: OB после первого теста 20% остаётся актуальным и снова становится
    Entry Zone на новой версии диапазона; touch-событие не дублируется."""
    engine = LtfEngine(db, cfg)
    candles = make_h1_candles(REUSE_FLOW, T_FLOW, instrument_id)
    obs, sc = _setup_pending(db, instrument_id)
    ez = _add_ob(db, instrument_id, sc, "fresh", "fresh")

    # idx5: первое касание 20% — событие, зона потреблена для текущего поиска
    _feed(db, engine, instrument_id, candles, 5)
    got = db.get_ltf_entry_zone(ez.id)
    assert got.validity == "tested"
    assert got.first_test_at == candles[5].close_time
    assert got.max_test_depth == pytest.approx(0.2)
    assert got.test_extreme == 98.8
    row0 = _entry_row(db, sc.id, ez.id, 0)
    assert row0.state == "tested"
    # диапазона ещё нет: кандидат потреблён, пригодность к перевыбору ждёт пары
    assert row0.reason == "range_pending"
    touches = [e for e in db.list_ltf_events(observation_id=obs.id)
               if e.kind == "touch"]
    assert len(touches) == 1

    # idx12: новая пара LH→LL → диапазон v1; глубина 20% < 90% →
    # повторный выбор (приёмка B), validity остаётся фактом истории
    _feed(db, engine, instrument_id, candles, 12)
    rng = db.get_current_ltf_range(sc.id)
    assert (rng.version, rng.lower, rng.upper) == (1, 90.5, 99.0)
    row1 = _entry_row(db, sc.id, ez.id, 1)
    assert (row1.state, row1.reason) == ("fresh", "ok")
    got = db.get_ltf_entry_zone(ez.id)
    assert got.validity == "tested"          # не invalid и не «обнулена»
    ready = [e for e in db.list_ltf_events(observation_id=obs.id)
             if e.kind == "entries_ready"]
    assert len(ready) == 1
    assert ez.id in [en["entry_zone_id"] for en in ready[0].payload["entries"]]

    # повторное касание после перевыбора: глубина накапливается (25% → max
    # остаётся от более глубокого захода), touch-событие не дублируется
    deeper = make_h1_candles([(98.5, 99.2, 97.0, 98.0)],
                             T_FLOW + len(REUSE_FLOW) * H1_MS, instrument_id)
    db.insert_candles(deeper)
    engine.process_h1_close(instrument_id, now_ms=deeper[0].close_time)
    got = db.get_ltf_entry_zone(ez.id)
    assert got.max_test_depth == pytest.approx(0.3)   # (99.2−98)/4
    row1b = _entry_row(db, sc.id, ez.id, 1)
    # мелкий тест (< 90%): зона потреблена, но допуск к перевыбору сохранён
    assert (row1b.state, row1b.reason) == ("tested", "ok")
    touches = [e for e in db.list_ltf_events(observation_id=obs.id)
               if e.kind == "touch" and e.payload["entry_zone_id"] == ez.id]
    assert len(touches) == 1                            # дедуп §11.5 сохранён


@pytest.mark.parametrize("depth,extreme,reselected", [
    (0.89, 101.56, True),     # 89% < 90% — повторный выбор допустим
    (0.90, 101.6, False),     # ровно 90% (P90 = 101.6) — недопустимо
    (0.95, 101.8, False),     # 95% — недопустимо
])
def test_deep_test_blocks_reselection(db: Database, cfg, instrument_id: int,
                                      depth, extreme, reselected):
    """C: прежний тест >= 90% запрещает повторный выбор, но зона остаётся
    рыночно актуальной (validity='tested', не invalid)."""
    engine = LtfEngine(db, cfg)
    candles = make_h1_candles(REUSE_FLOW, T_FLOW, instrument_id)
    obs, sc = _setup_pending(db, instrument_id)
    ez = _add_ob(db, instrument_id, sc, "tested", "tested",
                 depth=depth, extreme=extreme, first_test_at=T0 + H1_MS)

    _feed(db, engine, instrument_id, candles, 12)
    assert db.get_current_ltf_range(sc.id).version == 1
    row = _entry_row(db, sc.id, ez.id, 1)
    if reselected:
        assert (row.state, row.reason) == ("fresh", "ok")
    else:
        assert (row.state, row.reason) == ("tested", "tested_too_deep")
    got = db.get_ltf_entry_zone(ez.id)
    assert got.validity == "tested"          # глубина не инвалидирует зону
    assert got.max_test_depth == pytest.approx(depth)
    ready = [e for e in db.list_ltf_events(observation_id=obs.id)
             if e.kind == "entries_ready"]
    announced = {en["entry_zone_id"] for e in ready
                 for en in e.payload["entries"]}
    assert (ez.id in announced) is reselected
