"""§8/§9: Entry Zones — происхождение, свежесть, overlap (приёмка п.8–11)."""
from __future__ import annotations

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import (
    LtfEngine,
    assign_roles,
    build_movement,
    classify_entry,
    confirmed_pivots,
    detect_entry_zones,
    find_h1_pivots,
)
from app.engine.ltf.breaks import StructureEventDraft
from app.engine.ltf.ranges import RangeDraft
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from tests.conftest import H1_MS, make_candle, make_h1_candles

T0 = 1_780_000_000_000
NOW = T0 + 500 * H1_MS

# Движение вниз: база idx3, импульс idx4 (пик 15.6), FVG-тройки, слом idx10.
# idx11 — откат, трогающий часть зон; idx12/idx13 — гэп через FVG [10.5;11.5].
ENTRIES_HL = [
    (13.0, 12.6), (13.6, 13.0), (14.2, 13.6), (15.0, 14.4), (15.6, 14.9),
    (14.8, 13.5), (13.8, 13.0), (13.2, 12.0), (12.8, 11.5), (11.8, 10.2),
    (10.5, 9.0), (13.4, 12.4), (10.4, 9.8), (11.8, 11.6),
]
ENTRIES_CLOSES = {4: 15.0, 10: 8.9}


def _candles():
    bars = []
    for i, (h, l) in enumerate(ENTRIES_HL):
        c = ENTRIES_CLOSES.get(i, (h + l) / 2)
        bars.append(((h + l) / 2, h, l, c))
    return make_h1_candles(bars, T0)


def _detection(cfg):
    candles = _candles()
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
    mv = build_movement(1, avail, candles, ev, Direction.BEAR, lookback_ms=17 * H1_MS)
    assert mv is not None
    return candles, avail, mv, cfg


def _by_bounds(det):
    return {(z.type, z.lower, z.upper): z for z in det.zones}


def test_detect_all_suitable_zones_no_ranking():
    # п.9: все подходящие зоны причинного движения, без выбора «лучшей»
    candles, avail, mv, cfg = _detection(DetectorConfig())
    det = detect_entry_zones(candles, mv, avail, Direction.BEAR, cfg)
    d = _by_bounds(det)
    assert len(det.zones) == 10
    # FVG-тройки движения
    assert ("FVG", 13.8, 14.9) in d
    assert ("FVG", 13.2, 13.5) in d
    assert ("FVG", 12.8, 13.0) in d
    assert ("FVG", 11.8, 12.0) in d
    assert ("FVG", 10.5, 11.5) in d
    # OB — только с подтверждающим внешним FVG (§8.3)
    assert ("OB", 14.4, 15.6) in d   # база idx3 + тень импульсной idx4 (§9.3 HTF)
    assert ("OB", 13.5, 14.8) in d
    assert ("OB", 13.0, 13.8) in d
    assert ("OB", 12.0, 13.2) in d
    # BSL — экстремум по тени, относящийся к движению (§8.4)
    assert ("BSL", 15.6, 15.6) in d
    # неподходящие исключены с объяснением (п.9)
    assert any(r["type"] == "OB" and "внешнего FVG" in r["reason"]
               for r in det.rejected)
    assert any(r["type"] == "FVG" and "вне причинного движения" in r["reason"]
               and r["formed_at"] == T0 + 13 * H1_MS for r in det.rejected)


def test_wrong_direction_excluded():
    # п.9: зона не по направлению сценария исключается с объяснением
    candles, avail, mv, cfg = _detection(DetectorConfig())
    det = detect_entry_zones(candles, mv, avail, Direction.BULL, cfg)
    assert [z for z in det.zones if z.type in ("FVG", "OB")] == []
    assert any("направление" in r["reason"] for r in det.rejected)


def test_freshness_and_forming_candles():
    candles, avail, mv, cfg = _detection(DetectorConfig())
    det = detect_entry_zones(candles, mv, avail, Direction.BEAR, cfg)
    d = _by_bounds(det)
    # п.10: протестированная до готовности (откатом idx11) — не свежая
    assert d[("FVG", 13.2, 13.5)].validity == "tested"
    assert d[("FVG", 13.2, 13.5)].first_test_at == T0 + 11 * H1_MS
    assert d[("OB", 12.0, 13.2)].validity == "tested"
    # п.11: формирующие свечи FVG — не её тест; idx12/idx13 — гэп через зону
    # без цен внутри — тоже не касание (§8.5): зона остаётся свежей
    fvg2 = d[("FVG", 10.5, 11.5)]
    assert fvg2.validity == "fresh" and fvg2.first_test_at is None
    # уровень: рождение экстремума — не его снятие (§8.4)
    bsl = d[("BSL", 15.6, 15.6)]
    assert bsl.validity == "fresh"
    assert bsl.confirmed_at == T0 + 8 * H1_MS - 1  # pivot idx4 + 3 правые свечи


def test_classify_overlap_partial_no_cut():
    rng = RangeDraft(direction=Direction.BEAR, lower=90, upper=110, mid=100,
                     anchor_low_ref=None, anchor_high_ref=None, available_at=0)
    # §8.5/п.8: Premium от 100, OB=[98;102] — подходит частично, не обрезается
    assert classify_entry(98, 102, False, rng, Direction.BEAR) == (True, "partial")
    assert classify_entry(101, 109, False, rng, Direction.BEAR) == (True, "full")
    assert classify_entry(80, 90, False, rng, Direction.BEAR) == (False, "none")
    assert classify_entry(95, 100, False, rng, Direction.BEAR) == (True, "partial")
    assert classify_entry(100, 100, True, rng, Direction.BEAR) == (True, "full")
    assert classify_entry(99, 99, True, rng, Direction.BEAR) == (False, "none")
    # range_pending: фильтр не применяется
    assert classify_entry(80, 90, False, None, Direction.BEAR) == (True, "pending")


def _setup_scenario(db: Database, instrument_id: int):
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
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, available_at=T0,
    ))
    return obs, sc


def _add_zone(db: Database, instrument_id: int, sc: LtfScenario,
              type_: str, lower: float, upper: float, state: str,
              eligible: bool, overlap: str) -> LtfEntryZone:
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type=type_, direction=Direction.BEAR,
        lower=lower, upper=upper, formed_at=T0, confirmed_at=T0 + 1000,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
        eligible=eligible, overlap=overlap, state=state,
    ))
    return ez


def test_touch_at_full_zone_edge_not_cut(db: Database, cfg, instrument_id: int):
    """п.8: Premium от 100, OB=[98;102]; движение снизу — касание на 98,
    зона не обрезана до [100;102]."""
    obs, sc = _setup_scenario(db, instrument_id)
    ez = _add_zone(db, instrument_id, sc, "OB", 98.0, 102.0, "fresh", True, "partial")
    outside = _add_zone(db, instrument_id, sc, "OB", 80.0, 90.0,
                        "out_of_range", False, "none")
    engine = LtfEngine(db, cfg)
    t1 = T0 + 500 * H1_MS
    c1 = make_candle(t1, 99.5, 99.0, 97.5, 98.2, timeframe="H1",
                     instrument_id=instrument_id)
    db.insert_candles([c1])
    engine.process_h1_close(instrument_id, now_ms=c1.close_time)
    touch_events = [e for e in db.list_ltf_events(observation_id=obs.id)
                    if e.kind == "touch"]
    assert len(touch_events) == 1
    assert touch_events[0].payload["entry_zone_id"] == ez.id
    # границы зоны не обрезаны (§8.5)
    got = db.get_ltf_entry_zone(ez.id)
    assert (got.lower, got.upper) == (98.0, 102.0)
    assert got.validity == "tested"
    # неподходящая зона: даже проход цены не даёт события (п.8)
    c2 = make_candle(t1 + H1_MS, 91.0, 91.0, 85.0, 88.0, timeframe="H1",
                     instrument_id=instrument_id)
    db.insert_candles([c2])
    engine.process_h1_close(instrument_id, now_ms=c2.close_time)
    assert [e for e in db.list_ltf_events(observation_id=obs.id)
            if e.kind == "touch" and e.payload["entry_zone_id"] == outside.id] == []
