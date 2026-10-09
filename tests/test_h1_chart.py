"""Снимок H1: 20 точек, BOS/SMS только от машины пробоев, зоны без сценария."""
from __future__ import annotations

from app.config import DetectorConfig
from app.engine.ltf.breaks import scan_instrument_structure
from app.engine.ltf.pivots import PivotCandidate
from app.models import Direction, ZoneStatus, ZoneType
from app.models import Zone
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfPivot,
    LtfScenario,
    LtfScenarioEntry,
    LtfStructureEvent,
)
from app.services.h1_chart import (
    ADMISSION_UNRATED,
    LABEL_AMBIGUOUS,
    LABEL_BEAR_PAIR,
    LABEL_BEAR_WAIT,
    LABEL_BULL_WAIT,
    assemble_h1_layers,
    role_at,
    select_pivot_markers,
    structure_transition,
)
from app.services.overview import instrument_structure
from tests.conftest import H1_MS, make_candle

T0 = 1_700_000_000_000


class _Settings:
    detector = DetectorConfig()


def _t(i: int) -> int:
    return T0 + i * H1_MS


def _row(i: int, role: str, *, state="confirmed", kind=None, superseded=None,
         confirmed_at=None, price=None) -> dict:
    kind = kind or ("high" if role in ("HH", "LH", "internal_high") else "low")
    return {
        "id": i,
        "pivot_at": _t(i),
        "confirmed_at": _t(i) if confirmed_at is None else confirmed_at,
        "role": role,
        "state": state,
        "kind": kind,
        "price": 100 + i if price is None else price,
        "superseded_by": superseded,
        "calc_version_id": 1,
        "candle_open_time": _t(i),
    }


def _pivot(pid, price, kind, role, at, confirmed) -> PivotCandidate:
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=at,
        candle_open_time=at, confirmed_at=confirmed, left=3, right=3,
        state="confirmed", pivot_id=pid, role=role,
    )


def _candle(i, o, h, l, c, *, closed=True):
    return make_candle(_t(i), o, h, l, c, timeframe="H1", closed=closed)


def _store_pivot(db, instrument_id, pid_price, kind, role, at, confirmed) -> int:
    return db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=pid_price, kind=kind,
        pivot_at=at, candle_open_time=at, confirmed_at=confirmed, role=role,
        role_assigned_at=confirmed, left=3, right=3, state="confirmed",
    ))


def _counts(db):
    def n(table):
        return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return (
        n("ltf_structure_event"),
        n("ltf_entry_zone"),
        n("ltf_scenario_entry"),
        n("zone"),
    )


def test_t01_last_20_of_100_are_chronological():
    rows = [_row(i, "HH" if i % 2 == 0 else "HL") for i in range(100)]
    pack = select_pivot_markers(rows, as_of=_t(200))
    ids = [r["id"] for r in pack["points"]]
    assert ids == list(range(80, 100))
    assert ids == sorted(ids)


def test_t02_under_20_returns_all_and_limit_is_total_not_per_role():
    few = [_row(i, "LH") for i in range(7)]
    assert len(select_pivot_markers(few, as_of=_t(20))["points"]) == 7
    mixed = []
    for i, role in enumerate(["HH", "HL", "LH", "LL"] * 6):
        mixed.append(_row(i + 1, role))
    chosen = select_pivot_markers(mixed, as_of=_t(50))["points"]
    assert len(chosen) == 20
    assert len({r["role"] for r in chosen}) > 1
    assert sum(1 for r in chosen if r["role"] == "HH") < 20


def test_t03_diagnostic_roles_and_superseded_stay_out():
    rows = [
        _row(1, "HH"),
        _row(2, "internal_high", kind="high"),
        _row(3, "internal_low", kind="low"),
        _row(4, "none", kind="low"),
        _row(5, "LH", state="ambiguous"),
        _row(6, "LL", superseded=9),
        _row(7, "HL"),
    ]
    chosen = select_pivot_markers(rows, as_of=_t(20))["points"]
    assert [r["id"] for r in chosen] == [1, 7]
    diag = select_pivot_markers(rows, as_of=_t(20), include_diagnostic=True)["points"]
    assert {r["id"] for r in diag} >= {1, 2, 3, 4, 5, 7}
    assert 6 not in {r["id"] for r in diag}


def test_t04_new_point_drops_oldest_open_candle_does_not():
    rows = [_row(i, "HH" if i % 2 == 0 else "HL") for i in range(1, 21)]
    base = [r["id"] for r in select_pivot_markers(rows, as_of=_t(30))["points"]]
    assert base[0] == 1 and len(base) == 20
    grown = rows + [_row(21, "LL")]
    nxt = [r["id"] for r in select_pivot_markers(grown, as_of=_t(30))["points"]]
    assert nxt[0] == 2 and nxt[-1] == 21
    opened = grown + [_row(22, "HH", state="candidate", confirmed_at=None)]
    still = [r["id"] for r in select_pivot_markers(opened, as_of=_t(30))["points"]]
    assert 22 not in still
    assert still == nxt


def test_t05_scroll_window_does_not_change_recent_20():
    rows = [_row(i, "HL" if i % 2 else "HH") for i in range(30)]
    recent = select_pivot_markers(rows, as_of=_t(40))
    scrolled = select_pivot_markers(
        rows, as_of=_t(40), window_from=_t(1), window_to=_t(5),
    )
    assert [r["id"] for r in recent["points"]] == [r["id"] for r in scrolled["points"]]
    history = select_pivot_markers(
        rows, as_of=_t(40), mode="history", window_from=_t(2), window_to=_t(6),
    )
    assert history["selection"]["mode"] == "history"
    assert [r["id"] for r in history["points"]] != [r["id"] for r in recent["points"]]
    assert history["points"]


def test_t08_role_text_without_strict_close_is_not_a_break():
    pivots = [
        _pivot(1, 10, "low", "HL", _t(2), _t(5)),
        _pivot(2, 15, "high", "HH", _t(6), _t(9)),
    ]
    quiet = [_candle(12, 12, 14, 10.2, 11)]
    assert scan_instrument_structure(pivots, quiet, _t(20)) == []
    relabeled = [
        _pivot(1, 10, "low", "LL", _t(2), _t(5)),
        _pivot(2, 15, "high", "LH", _t(6), _t(9)),
    ]
    assert scan_instrument_structure(relabeled, quiet, _t(20)) == []
    transition = structure_transition([], relabeled, _t(20))
    assert transition["label"] == LABEL_AMBIGUOUS
    assert transition["causal_event_id"] is None


def test_t09_wick_open_candle_and_equal_close_do_not_confirm():
    pivots = [
        _pivot(1, 10, "low", "HL", _t(2), _t(5)),
        _pivot(2, 15, "high", "HH", _t(6), _t(9)),
    ]
    wick = [_candle(12, 12, 13, 8, 10.5)]
    equal = [_candle(12, 12, 13, 9, 10)]
    opened = [_candle(12, 12, 13, 8, 9, closed=False)]
    assert scan_instrument_structure(pivots, wick, _t(20)) == []
    assert scan_instrument_structure(pivots, equal, _t(20)) == []
    assert scan_instrument_structure(pivots, opened, _t(20)) == []


def _bear_break_inputs():
    pivots = [
        _pivot(1, 10, "low", "HL", _t(2), _t(5)),
        _pivot(2, 15, "high", "HH", _t(6), _t(9)),
        _pivot(3, 14, "high", "LH", _t(14), _t(17)),
        _pivot(4, 8, "low", "LL", _t(18), _t(21)),
    ]
    candles = [_candle(12, 11, 12, 9, 9.5)]
    return pivots, candles


def test_t07_break_then_linked_pair_does_not_move_break_time():
    pivots, candles = _bear_break_inputs()
    events = scan_instrument_structure(pivots, candles, _t(30))
    bos = [e for e in events if e.kind == "BOS" and e.stage == "primary"]
    assert len(bos) == 1
    assert bos[0].occurred_at == candles[0].close_time
    assert bos[0].break_level == 10
    waiting = structure_transition(events, pivots, _t(16))
    assert waiting["label"] == LABEL_BEAR_WAIT
    assert waiting["break_at"] == bos[0].occurred_at
    assert waiting["pair"] is None
    ready = structure_transition(events, pivots, _t(21))
    assert ready["label"] == LABEL_BEAR_PAIR
    assert ready["break_at"] == bos[0].occurred_at
    assert ready["pair"]["confirmed_at"] == _t(21)
    assert ready["pair"]["confirmed_at"] != ready["break_at"]
    assert ready["pair"]["kind"] == "LH_LL"


def test_t07_bull_break_waits_for_its_own_pair():
    pivots = [
        _pivot(1, 12, "high", "LH", _t(2), _t(5)),
        _pivot(2, 8, "low", "LL", _t(6), _t(9)),
    ]
    candles = [_candle(12, 10, 13, 9.5, 12.5)]
    events = scan_instrument_structure(pivots, candles, _t(20))
    bos = [e for e in events if e.kind == "BOS"]
    assert bos and bos[0].direction == Direction.BULL
    waiting = structure_transition(events, pivots, _t(20))
    assert waiting["label"] == LABEL_BULL_WAIT
    assert waiting["break_at"] == bos[0].occurred_at


def _arm(db, instrument_id):
    hl = _store_pivot(db, instrument_id, 10, "low", "HL", _t(2), _t(5))
    hh = _store_pivot(db, instrument_id, 15, "high", "HH", _t(6), _t(9))
    db.insert_candles([_candle(12, 11, 12, 9, 9.5)])
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(30)))
    return hl, hh


def test_t06_old_anchor_survives_the_label_cap(db, instrument_id):
    ids = []
    for i in range(25):
        role = "HH" if i % 2 == 0 else "HL"
        kind = "high" if role == "HH" else "low"
        ids.append(_store_pivot(
            db, instrument_id, 50 + i, kind, role, _t(i), _t(i + 3),
        ))
    # пробой строится отдельной опорой раньше окна последних 20:
    # она уже есть среди 25. Берём самую раннюю как level и проверяем,
    # что она не в подписях, но остаётся опорой, если событие на неё есть.
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(40))
    marker_ids = [p["id"] for p in pack["pivot_markers"]]
    assert len([p for p in pack["pivot_markers"] if p["role"] in {"HH", "HL", "LH", "LL"}]) == 20
    assert ids[0] not in marker_ids
    assert pack["snapshot"]["timeframe"] == "H1"


def test_t06_break_anchor_is_outside_the_20(db, instrument_id):
    early = []
    for i in range(3):
        role = "HL" if i == 0 else "HH"
        kind = "low" if role == "HL" else "high"
        early.append(_store_pivot(
            db, instrument_id, 10 if role == "HL" else 20 + i, kind, role,
            _t(i + 1), _t(i + 4),
        ))
    db.insert_candles([_candle(8, 12, 13, 9, 9)])
    for i in range(20, 45):
        role = "LH" if i % 2 == 0 else "LL"
        kind = "high" if role == "LH" else "low"
        _store_pivot(db, instrument_id, 30 + i, kind, role, _t(i), _t(i + 3))
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(60)))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(60))
    marker_ids = {p["id"] for p in pack["pivot_markers"]}
    assert len(marker_ids) == 20
    events = pack["structural_events"]
    assert events, "слом ранней опоры должен быть в снимке"
    anchor = events[0]["level_pivot_id"]
    assert anchor not in marker_ids
    assert anchor in {p["id"] for p in pack["anchor_refs"]}


def test_t10_events_exist_without_context_and_survive_scenario_close(db, instrument_id):
    _arm(db, instrument_id)
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(30))
    assert pack["structural_events"]
    assert pack["snapshot"]["scenario_open"] is False
    body = instrument_structure(db, _Settings(), instrument_id, as_of=_t(30))
    assert body["context_id"] is None
    assert body["structure_events"] == []
    assert body["entries"] == []
    assert body["structural_events"]
    before = [e["id"] for e in body["structural_events"]]
    again = instrument_structure(db, _Settings(), instrument_id, as_of=_t(30))
    assert [e["id"] for e in again["structural_events"]] == before


def test_t11_same_candle_keeps_both_facts_with_distinct_ids():
    # Сопутствующий SMS машина отдаёт вместе с BOS, когда вооружены оба
    # уровня. Здесь проверяем контракт идентификаторов на двух черновиках
    # одной свечи: разные kind не склеиваются в один id.
    from app.engine.ltf.breaks import StructureEventDraft
    from app.services.h1_chart import _event_id
    candle_at = _t(12)
    bos = StructureEventDraft(
        kind="BOS", stage="primary", direction=Direction.BEAR, break_level=10,
        break_candle_open_time=candle_at, occurred_at=candle_at + H1_MS - 1,
        detected_at=candle_at, level_key="bos:primary:HL:1:10",
        ref_pivot_ids=[2, 1],
    )
    sms = StructureEventDraft(
        kind="SMS", stage="primary", direction=Direction.BEAR, break_level=11,
        break_candle_open_time=candle_at, occurred_at=candle_at + H1_MS - 1,
        detected_at=candle_at, level_key="sms:primary:internal_low:3:11",
        ref_pivot_ids=[2, 3, 4], accompanying=True,
    )
    assert _event_id(bos) != _event_id(sms)
    assert bos.break_candle_open_time == sms.break_candle_open_time


def test_t14_t15_admission_is_not_invented(db, instrument_id):
    db.insert_candles([_candle(i, 100, 101, 99, 100) for i in range(4)])
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(10)))
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=98, upper=102, formed_at=_t(1), confirmed_at=_t(3),
    ))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(10))
    assert any(z["id"] == zone.id for z in pack["detected_zones"])
    assert pack["scenario_admission"]
    assert all(a["eligibility"] == "not_evaluated" for a in pack["scenario_admission"])
    assert all(a["reason"] == ADMISSION_UNRATED for a in pack["scenario_admission"])

    parent = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=90, upper=110,
        formed_at=_t(0), confirmed_at=_t(1), status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent, zone_version=1,
        cycle_id=1, direction=Direction.BULL, activated_at=_t(1),
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=zone.id, range_version=1,
        eligible=False, overlap="none", state="out_of_range",
        reason="outside_pd", added_at=_t(4), updated_at=_t(4),
    ))
    rated = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=_t(10), context_id=obs.id,
    )
    row = next(z for z in rated["detected_zones"] if z["id"] == zone.id)
    adm = next(a for a in rated["scenario_admission"] if a["zone_id"] == zone.id)
    assert row["lower"] == 98 and row["upper"] == 102
    assert adm["eligibility"] == "excluded"
    assert adm["reason"] == "data_gap"
    assert row["relevance"]["data_quality"] == "gap"


def test_t16_partial_fvg_stays_filled_fvg_ends_deep_ob_stays(db, instrument_id):
    fill_at = _t(6)
    candles = [
        _candle(1, 100, 101, 99, 100),
        _candle(2, 102, 103, 101.5, 102),
        _candle(3, 103, 104, 102.5, 103),
        _candle(6, 100, 101, 97, 98),
        _candle(8, 110, 112, 109, 111),
    ]
    db.insert_candles(candles)
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(12)))
    partial = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=90, upper=110, formed_at=_t(1), confirmed_at=_t(3),
        first_test_at=_t(4), max_test_depth=0.4, test_extreme=102,
        validity="tested",
    ))
    filled = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG", direction=Direction.BULL,
        lower=98, upper=102, formed_at=_t(2), confirmed_at=_t(4),
        validity="tested",
    ))
    ob = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB", direction=Direction.BULL,
        lower=95, upper=100, formed_at=_t(1), confirmed_at=_t(3),
        first_test_at=_t(5), max_test_depth=0.95, test_extreme=95.2,
        validity="tested",
    ))
    bsl = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="BSL", direction=Direction.BEAR,
        lower=140, upper=140, formed_at=_t(1), confirmed_at=_t(3),
    ))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(12))
    history = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=_t(12), zone_history=True,
    )
    assert filled.id not in {z["id"] for z in pack["detected_zones"]}
    assert {partial.id, ob.id, bsl.id} <= {z["id"] for z in pack["detected_zones"]}
    by_id = {z["id"]: z for z in history["detected_zones"]}
    assert by_id[partial.id]["fill"] == "partially_filled"
    assert by_id[partial.id]["lifecycle"] == "active"
    assert by_id[partial.id]["display_until"] is None
    assert by_id[filled.id]["lifecycle"] == "ended"
    assert by_id[filled.id]["display_until"] == candles[3].close_time
    assert by_id[filled.id]["display_until"] != candles[-1].close_time
    assert by_id[ob.id]["lifecycle"] == "active"
    assert by_id[ob.id]["display_until"] is None
    assert by_id[bsl.id]["is_level"] is True
    assert by_id[bsl.id]["lower"] == by_id[bsl.id]["upper"]


def test_level_taken_outside_structure_horizon_stays_ended(db, instrument_id):
    """Снятие старше окна структуры не оставляет уровень действующим."""
    day = 86_400_000
    as_of = T0 + 40 * day
    cross_open = T0 + 2 * day
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(as_of))
    cross = make_candle(cross_open, 100, 151, 99, 150, timeframe="H1")
    db.insert_candles([
        cross,
        make_candle(T0 + 35 * day, 100, 110, 90, 105, timeframe="H1"),
    ])
    taken = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="BSL", direction=Direction.BEAR,
        lower=140, upper=140, formed_at=T0, confirmed_at=T0 + H1_MS,
    ))
    equal_close = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="BSL", direction=Direction.BEAR,
        lower=150, upper=150, formed_at=T0, confirmed_at=T0 + H1_MS,
    ))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=as_of)
    ids = {z["id"] for z in pack["detected_zones"]}
    assert taken.id not in ids
    assert equal_close.id in ids
    history = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=as_of, zone_history=True,
    )
    row = next(z for z in history["detected_zones"] if z["id"] == taken.id)
    assert row["lifecycle"] == "ended"
    assert row["display_until"] == cross.close_time
    kept = next(z for z in history["detected_zones"] if z["id"] == equal_close.id)
    assert kept["lifecycle"] == "active"


def test_t17_empty_reasons_stay_distinct(db, instrument_id):
    empty = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(5))
    assert empty["detected_zones"] == []
    assert empty["layer_status"]["zones"]["state"] == "no_data"
    assert empty["layer_status"]["zones"]["reason"]
    db.insert_candles([_candle(1, 1, 2, 0.5, 1.5)])
    pending = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(5))
    assert pending["layer_status"]["zones"]["state"] == "no_data"
    assert pending["layer_status"]["zones"]["reason"] != empty["layer_status"]["zones"]["reason"]
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(5)))
    done = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(5))
    assert done["layer_status"]["zones"]["state"] == "calculated"
    assert done["detected_zones"] == []
    assert done["layer_status"]["zones"]["reason"] is None


def test_t18_h1_prb_is_not_renamed_to_ob(db, instrument_id):
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(5)))
    db.insert_candles([_candle(1, 10, 11, 9, 10)])
    prb = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.PRB,
        direction=Direction.BULL, timeframe="H1", lower=12345.67, upper=12350.0,
        formed_at=_t(1), confirmed_at=_t(2), status=ZoneStatus.ACTIVE,
    ))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(5))
    assert all(z["type"] in {"OB", "FVG", "BSL", "SSL"} for z in pack["detected_zones"])
    assert all(z["type"] != "PRB" for z in pack["detected_zones"])
    assert all(not (z["lower"] == 12345.67 and z["type"] == "OB") for z in pack["detected_zones"])
    stored = db.get_zone(prb)
    assert stored.type == ZoneType.PRB
    assert stored.timeframe == "H1"


def test_t19_instruments_and_as_of_do_not_mix(db):
    a = db.upsert_instrument(__import__("app.models", fromlist=["Instrument"]).Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    b = db.upsert_instrument(__import__("app.models", fromlist=["Instrument"]).Instrument(
        id=None, asset="ETH", venue="binance", market_type="spot",
        symbol="ETHUSDT", quote_asset="USDT",
    ))
    _arm(db, a)
    _store_pivot(db, b, 3, "low", "HL", _t(2), _t(5))
    left = assemble_h1_layers(db, _Settings(), a, as_of=_t(30))
    right = assemble_h1_layers(db, _Settings(), b, as_of=_t(30))
    assert left["snapshot"]["instrument_id"] == a
    assert right["snapshot"]["instrument_id"] == b
    assert left["snapshot"]["snapshot_id"] != right["snapshot"]["snapshot_id"]
    assert left["structural_events"]
    assert {e["id"] for e in left["structural_events"]}.isdisjoint(
        {e["id"] for e in right["structural_events"]}
    )
    hh = next(p for p in db.list_ltf_pivots(a) if p.role == "HH")
    db.update_ltf_pivot_role(hh.id, "LH", _t(40))
    past = assemble_h1_layers(db, _Settings(), a, as_of=_t(30))
    future = assemble_h1_layers(db, _Settings(), a, as_of=_t(50))
    past_role = next(p["role"] for p in past["anchor_refs"] if p["id"] == hh.id) if any(
        p["id"] == hh.id for p in past["anchor_refs"]
    ) else past["roles_as_of"][hh.id]
    assert past["roles_as_of"][hh.id] == "HH"
    assert future["roles_as_of"][hh.id] == "LH"
    assert past_role == "HH"


def test_t20_future_confirmation_is_absent(db, instrument_id):
    _store_pivot(db, instrument_id, 10, "low", "HL", _t(2), _t(5))
    future = _store_pivot(db, instrument_id, 15, "high", "HH", _t(40), _t(43))
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(_t(50)))
    pack = assemble_h1_layers(db, _Settings(), instrument_id, as_of=_t(10))
    assert future not in {p["id"] for p in pack["pivot_markers"]}
    assert future not in pack["roles_as_of"]
    pivot = LtfPivot(
        id=future, instrument_id=instrument_id, price=15, kind="high",
        pivot_at=_t(40), candle_open_time=_t(40), confirmed_at=_t(43),
        role="LH", role_assigned_at=_t(48),
    )
    assert role_at(pivot, [
        {"old_role": "HH", "new_role": "LH", "changed_at": _t(48)},
    ], _t(10)) == "HH"


def test_t23_replay_does_not_insert_or_change_admission(db, instrument_id):
    _arm(db, instrument_id)
    zone = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB", direction=Direction.BEAR,
        lower=9, upper=11, formed_at=_t(1), confirmed_at=_t(4),
    ))
    parent = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=1, upper=20,
        formed_at=_t(0), confirmed_at=_t(1), status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, activated_at=_t(1),
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=10,
        break_candle_open_time=_t(12), occurred_at=_t(12) + H1_MS - 1,
        detected_at=_t(13), level_key="legacy-different-key",
        ref_pivot_ids=[1],
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=zone.id, range_version=1,
        eligible=True, overlap="full", reason="ok", added_at=_t(4), updated_at=_t(4),
    ))
    before = _counts(db)
    first = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=_t(30), context_id=obs.id, zone_history=True,
    )
    second = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=_t(30), context_id=obs.id, zone_history=True,
    )
    assert _counts(db) == before
    assert [e["id"] for e in first["structural_events"]] == [
        e["id"] for e in second["structural_events"]
    ]
    adm = next(a for a in second["scenario_admission"] if a["zone_id"] == zone.id)
    # A stale saved admission cannot resurrect an OB consumed by later bars.
    assert adm["eligibility"] == "excluded"
    assert adm["reason"] == "tested_too_deep"
    stored = db.list_ltf_scenario_entries(sc.id)[0]
    assert stored.eligible is True and stored.reason == "ok"
    assert len(first["pivot_markers"]) <= 20


def test_links_do_not_merge_different_events_on_the_same_price(db, instrument_id):
    hl, hh = _arm(db, instrument_id)
    parent = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=1, upper=30,
        formed_at=_t(0), confirmed_at=_t(1), status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, activated_at=_t(1),
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary",
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=10,
        break_candle_open_time=_t(3), occurred_at=_t(4),
        detected_at=_t(4), level_key="other-event",
        ref_pivot_ids=[hl],
    ))
    pack = assemble_h1_layers(
        db, _Settings(), instrument_id, as_of=_t(30), context_id=obs.id,
    )
    linked = [e for e in pack["structural_events"] if sc.id in e["scenario_ids"]]
    assert linked == []
    assert pack["structural_events"]
