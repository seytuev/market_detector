"""Long-lived theses survive corrections; only lifecycle consumes their zones."""
from dataclasses import replace

import pytest

from app.config import DetectorConfig
from app.engine.ltf.engine import LtfEngine
from app.engine.ltf.relevance import zone_at
from app.models import Direction, Zone, ZoneType, ZoneStatus
from app.models_ltf import LtfObservation, LtfScenario, LtfStructureEvent, LtfMovement, LtfEntryZone, LtfScenarioEntry, LtfRange
from app.services.htf_ideas import project_ideas, reconcile_ideas
from app.services.h1_chart import assemble_h1_layers
from tests.conftest import make_candle, H1_MS

T = 1_800_000_000_000 // H1_MS * H1_MS


def seed(db, iid, direction=Direction.BEAR, kind="OB"):
    parent = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB if direction == Direction.BEAR else ZoneType.FVG,
        direction=direction, timeframe="W1", lower=80, upper=140,
        formed_at=T - 100 * H1_MS, confirmed_at=T - H1_MS, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=parent, zone_version=1, cycle_id=1,
        direction=direction, activated_at=T, state="active", created_at=T, updated_at=T,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction, trigger="BOS", stage="primary",
        state="monitoring_entries", created_at=T + 2 * H1_MS, updated_at=T + 2 * H1_MS,
    ))
    ev = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary", direction=direction,
        break_level=100, break_candle_open_time=T + H1_MS, occurred_at=T + 2 * H1_MS,
        detected_at=T + 2 * H1_MS, level_key="origin",
    ))
    mv = db.insert_ltf_movement(LtfMovement(
        id=None, scenario_id=sc.id, start_pivot_id=1, end_pivot_id=2,
        start_at=T, end_at=T + 2 * H1_MS, break_event_id=ev.id, confirmed_at=T + 2 * H1_MS,
    ))
    db.update_ltf_scenario(sc.id, trigger_event_id=ev.id, origin_break_event_id=ev.id, origin_movement_id=mv)
    lower, upper = (110, 120) if direction == Direction.BEAR else (80, 90)
    z = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=iid, type=kind, direction=direction, lower=lower, upper=upper,
        formed_at=T + H1_MS, confirmed_at=T + 2 * H1_MS, movement_id=mv,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=z.id, range_version=1,
        eligible=True, overlap="full", reason="ok", added_at=T + 2 * H1_MS, updated_at=T + 2 * H1_MS,
    ))
    db.insert_ltf_range(LtfRange(id=None, scenario_id=sc.id, version=1, lower=80, upper=120,
                               mid=100, available_at=T + 2 * H1_MS))
    return parent, obs, sc, z


def bars(db, iid, n=400):
    rows = [make_candle(T + i * H1_MS, 100, 102, 98, 100, timeframe="H1", instrument_id=iid)
            for i in range(n)]
    db.insert_candles(rows)
    return rows


@pytest.mark.parametrize("direction", [Direction.BEAR, Direction.BULL])
def test_opposite_theses_survive_reverse_bos_stale_and_new_range(db, instrument_id, direction):
    cfg = DetectorConfig()
    _, obs, sc, z = seed(db, instrument_id, direction)
    other = Direction.BULL if direction == Direction.BEAR else Direction.BEAR
    _, _, _, opposite = seed(db, instrument_id, other)
    candles = bars(db, instrument_id)
    now = candles[-1].close_time
    db.update_ltf_scenario(sc.id, state="cancelled", cancellation_reason="reverse_bos", cancelled_at=T + 10 * H1_MS)
    db.update_ltf_observation(obs.id, state="closed_stale")
    db.insert_ltf_range(LtfRange(id=None, scenario_id=sc.id, version=2, lower=98, upper=102,
                               mid=100, available_at=T + 12 * H1_MS))
    reconcile_ideas(db, instrument_id, cfg, now)
    ideas, links, _ = project_ideas(db, instrument_id, cfg, now)
    assert len(ideas) == 2 and all(i["state"] == "active" for i in ideas)
    assert links[z.id][0]["eligible_now"] is False
    assert links[z.id][0]["entry_reason"] == "structure_pending"
    assert links[opposite.id][0]["eligible_now"] is True
    assert all(i["id"] for i in ideas)
    before = db.list_htf_ideas(instrument_id)
    events = db.conn.execute("SELECT COUNT(*) FROM ltf_event").fetchone()[0]
    reconcile_ideas(db, instrument_id, cfg, now)
    assert db.list_htf_ideas(instrument_id) == before
    assert db.conn.execute("SELECT COUNT(*) FROM ltf_event").fetchone()[0] == events
    settings = type("Settings", (), {"detector": cfg})()
    snap = assemble_h1_layers(db, settings, instrument_id, as_of=now)
    shown = {v["id"]: v for v in snap["detected_zones"]}
    assert shown[z.id]["idea_links"] and shown[opposite.id]["idea_links"]


@pytest.mark.parametrize("direction", [Direction.BEAR, Direction.BULL])
def test_tests_after_attempt_cancel_partial_then_threshold_and_historical_causality(db, instrument_id, direction):
    cfg = DetectorConfig()
    _, _, sc, z = seed(db, instrument_id, direction)
    initial = bars(db, instrument_id, 5)
    db.update_ltf_scenario(sc.id, state="cancelled", cancellation_reason="reverse_sms", cancelled_at=initial[3].close_time)
    partial = make_candle(T + 5 * H1_MS, 100, 115 if direction == Direction.BEAR else 102,
                          98 if direction == Direction.BEAR else 85, 100, timeframe="H1", instrument_id=instrument_id)
    deep = replace(partial, open_time=T + 6 * H1_MS, close_time=T + 7 * H1_MS - 1,
                   high=119 if direction == Direction.BEAR else 102,
                   low=98 if direction == Direction.BEAR else 81)
    db.insert_candles([partial, deep])
    reconcile_ideas(db, instrument_id, cfg, partial.close_time)
    assert db.list_htf_ideas(instrument_id)[0]["state"] == "active"
    assert db.get_ltf_entry_zone(z.id).max_test_depth == .5
    reconcile_ideas(db, instrument_id, cfg, deep.close_time)
    idea = db.list_htf_ideas(instrument_id)[0]
    assert idea["reason"] == "zones_exhausted"
    assert db.get_ltf_entry_zone(z.id).max_test_depth == .9
    past, _, facts = project_ideas(db, instrument_id, cfg, partial.close_time)
    assert past[0]["state"] == "active" and facts[z.id][0].max_test_depth == .5


def test_gap_then_backfill_and_manual_close_survive_repair(db, instrument_id):
    cfg = DetectorConfig()
    _, _, _, z = seed(db, instrument_id)
    rows = bars(db, instrument_id, 10)
    db.conn.execute("DELETE FROM candle WHERE open_time=?", (rows[5].open_time,))
    reconcile_ideas(db, instrument_id, cfg, rows[-1].close_time)
    assert db.list_htf_ideas(instrument_id)[0]["state"] == "paused_data"
    db.insert_candles([rows[5]])
    reconcile_ideas(db, instrument_id, cfg, rows[-1].close_time)
    idea = db.list_htf_ideas(instrument_id)[0]
    assert idea["state"] == "active"
    db.close_htf_idea(idea["id"], rows[-1].close_time)
    reconcile_ideas(db, instrument_id, cfg, rows[-1].close_time)
    assert db.list_htf_ideas(instrument_id)[0]["reason"] == "manual"


def test_parent_invalidated_closes_idea_but_not_its_past(db, instrument_id):
    cfg = DetectorConfig()
    parent, _, _, _ = seed(db, instrument_id)
    rows = bars(db, instrument_id, 10)
    db.update_zone(parent, market_validity="invalid", display_until=rows[7].close_time)
    ideas, _, _ = project_ideas(db, instrument_id, cfg, rows[-1].close_time)
    assert ideas[0]["reason"] == "parent_invalid"
    past, _, _ = project_ideas(db, instrument_id, cfg, rows[6].close_time)
    assert past[0]["state"] == "active"


def test_no_provenance_is_not_price_matching(db, instrument_id):
    cfg = DetectorConfig()
    _, _, sc, z = seed(db, instrument_id)
    rows = bars(db, instrument_id, 8)
    db.update_ltf_entry_zone(z.id, movement_id=0)
    ideas, links, _ = project_ideas(db, instrument_id, cfg, rows[-1].close_time)
    assert not links and ideas[0]["state"] == "waiting_zones"


def test_level_not_reached_is_not_failed_and_fvg_filled_is_terminal(db, instrument_id):
    cfg = DetectorConfig()
    _, _, _, z = seed(db, instrument_id, kind="FVG")
    rows = bars(db, instrument_id, 5)
    level = replace(z, type="BSL", lower=120, upper=120)
    _, fact = zone_at(level, rows, rows[-1].close_time, cfg)
    assert fact["relevant"]
    hit = replace(rows[-1], high=121)
    _, fact = zone_at(z, rows[:-1] + [hit], hit.close_time, cfg)
    assert fact["reason"] == "fvg_filled"
    _, fact = zone_at(level, rows[:-1] + [hit], hit.close_time, cfg)
    assert fact["reason"] == "swept_level"


def test_stale_archiver_keeps_live_idea_and_repair_restores_old_observation(db, instrument_id):
    cfg = DetectorConfig(ltf_observation_stale_days=14)
    _, obs, sc, _ = seed(db, instrument_id)
    rows = bars(db, instrument_id, 24 * 20)
    engine = LtfEngine(db, cfg)
    assert engine.archive_stale_observations(rows[-1].close_time) == []
    db.update_ltf_scenario(sc.id, state="cancelled", cancellation_reason="stale", cancelled_at=rows[300].close_time)
    db.update_ltf_observation(obs.id, state="closed_stale")
    reconcile_ideas(db, instrument_id, cfg, rows[-1].close_time)
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"
    assert db.get_ltf_scenario(sc.id).state == "cancelled"


def test_persisted_restart_and_authenticated_close(tmp_path):
    from app.db import Database
    from app.models import Instrument
    from app.config import Settings
    from app.web.api import create_app
    from fastapi.testclient import TestClient
    path = str(tmp_path / "ideas.sqlite")
    db = Database(path)
    iid = db.upsert_instrument(Instrument(id=None, asset="ETH", venue="binance", market_type="spot", symbol="ETHUSDT", quote_asset="USDT"))
    _, _, sc, _ = seed(db, iid)
    rows = bars(db, iid, 10)
    now = rows[-1].close_time
    settings = Settings()
    settings.auth_token = "idea-test"
    reconcile_ideas(db, iid, settings.detector, now)
    before = db.list_htf_ideas(iid)
    db.close()
    db = Database(path)
    assert db.list_htf_ideas(iid) == before
    client = TestClient(create_app(db, settings))
    url = f'/api/ltf/ideas/{before[0]["id"]}/close'
    assert client.post(url).status_code in (401, 403)
    response = client.post(url, headers={"Authorization": "Bearer idea-test"})
    assert response.status_code == 200 and response.json()["reason"] == "manual"
    assert db.get_ltf_scenario(sc.id).cancellation_reason == "manual"
    closed_at = db.list_htf_ideas(iid)[0]["manual_closed_at"]
    reconcile_ideas(db, iid, settings.detector, max(now, closed_at))
    assert db.list_htf_ideas(iid)[0]["reason"] == "manual"
    db.close()
