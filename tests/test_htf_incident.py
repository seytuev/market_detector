"""Приёмка инцидента SOL HTF/H1 (ТЗ 07.10.2026, A01–A14, A19–A22)."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.eligibility import (
    REASON_LEVEL_BROKEN,
    evaluate_final,
)
from app.models import (
    Direction,
    Event,
    EventKind,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import (
    LtfEntryZone,
    LtfLiquidityTest,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from app.services.entry_reason_repair import repair_terminal_entry_reasons
from app.services.htf_parent import eligible_htf_parent
from app.services.htf_profile import migrate_saved_htf_profile
from app.services.overview import instrument_current, instrument_structure
from app.services.runtime import CALC_OWNER, build_diagnostics, claim_owner
from app.web.api import create_app
from tests.conftest import H1_MS, make_candle
from tests.test_ltf_worker import FakeAdapter, _make_worker, _recent_h1

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(tmp_path, types: str = "OB") -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    s.detector = DetectorConfig()
    s.detector.htf_context_types = types
    return s


def _live(db, instrument_id, price: float):
    now = now_ms()
    candle = make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )
    db.insert_candles([candle])
    db.set_quote(instrument_id, price, now)
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(candle.close_time))
    return now


def _zone(db, instrument_id, *, type_=ZoneType.OB, tf="D1",
          lower=90.0, upper=100.0, status=ZoneStatus.ACTIVE,
          confirmed_at=1, direction=Direction.BULL, display_until=None):
    now = now_ms()
    return db.get_zone(db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=type_,
        direction=direction, timeframe=tf, lower=lower, upper=upper,
        formed_at=now - 10 * 86_400_000,
        confirmed_at=None if confirmed_at is None else now - 9 * 86_400_000,
        status=status, display_until=display_until,
    )))


@pytest.mark.asyncio
async def test_a01_a03_candidate_parent_and_no_duplicate():
    db = Database(":memory:")
    adapter = FakeAdapter()
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    confirmed = _zone(
        db, ins.id, status=ZoneStatus.CANDIDATE, lower=90, upper=100,
    )
    bare = _zone(
        db, ins.id, status=ZoneStatus.CANDIDATE, confirmed_at=None,
        lower=10, upper=20,
    )
    assert eligible_htf_parent(confirmed, worker.cfg) is True
    assert eligible_htf_parent(bare, worker.cfg) is False
    await worker._ltf_open_marked_zones(ins)
    assert db.get_ltf_observation_by_zone(confirmed.id, confirmed.cycle_id) is not None
    assert db.get_ltf_observation_by_zone(bare.id, bare.cycle_id) is None
    await worker._ltf_open_marked_zones(ins)
    assert len(db.list_ltf_observations(instrument_id=ins.id)) == 1
    closed = _zone(db, ins.id, lower=30, upper=40)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=ins.id, zone_id=closed.id, zone_version=1,
        cycle_id=closed.cycle_id, direction=Direction.BULL,
        state="closed_by_user", activated_at=now_ms(),
    ))
    await worker._ltf_open_marked_zones(ins)
    assert db.get_ltf_observation(obs.id).state == "closed_by_user"


@pytest.mark.asyncio
async def test_a02_touch_opens_candidate_once():
    db = Database(":memory:")
    adapter = FakeAdapter()
    now = now_ms()
    adapter.candles = [
        make_candle(now - 86_400_000, 109, 111, 108, 110, "D1"),
        make_candle(now - 7 * 86_400_000, 109, 111, 108, 110, "W1"),
        *_recent_h1([(10, 9), (11, 9), (12, 9)]),
    ]
    adapter.price = (99.0, now)
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone = _zone(
        db, ins.id, status=ZoneStatus.CANDIDATE, direction=Direction.BULL,
        lower=90, upper=100,
    )
    await worker.poll_once()
    assert db.get_ltf_observation_by_zone(zone.id, zone.cycle_id) is not None
    await worker.poll_once()
    assert len(db.list_ltf_observations(instrument_id=ins.id)) == 1


def test_manual_ob_is_parent_plain_manual_and_closed_are_not(db, instrument_id):
    """Ручной OB D1 — родитель лонга. Зона без правила и уже закрытая — нет."""
    cfg = DetectorConfig()
    bull = db.get_zone(db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.MANUAL,
        direction=Direction.BULL, timeframe="D1", lower=80.0, upper=81.5,
        formed_at=now_ms(), confirmed_at=None, status=ZoneStatus.ACTIVE,
        source="manual", zone_type="ob",
    )))
    plain = db.get_zone(db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.MANUAL,
        direction=Direction.BULL, timeframe="D1", lower=70.0, upper=71.0,
        formed_at=now_ms(), confirmed_at=None, status=ZoneStatus.ACTIVE,
        source="manual",
    )))
    closed = db.get_zone(db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.MANUAL,
        direction=Direction.BEAR, timeframe="D1", lower=80.0, upper=82.0,
        formed_at=now_ms(), confirmed_at=None, status=ZoneStatus.ARCHIVED,
        source="manual", zone_type="ob", display_until=now_ms(),
    )))
    assert eligible_htf_parent(bull, cfg) is True
    assert eligible_htf_parent(plain, cfg) is False
    assert eligible_htf_parent(closed, cfg) is False


def test_a04_a05_a06_wait_codes(tmp_path, db, instrument_id):
    settings = _settings(tmp_path, "OB,FVG")
    _live(db, instrument_id, 115.83)
    fvg = _zone(
        db, instrument_id, type_=ZoneType.FVG, tf="W1",
        lower=114.32, upper=116.32, direction=Direction.BULL,
    )
    ssl = _zone(
        db, instrument_id, type_=ZoneType.SSL, tf="D1",
        lower=116.32, upper=116.32, status=ZoneStatus.TAKEN,
        direction=Direction.BULL,
    )
    db.insert_event(Event(
        id=None, zone_id=ssl.id, cycle_id=ssl.cycle_id,
        kind=EventKind.LEVEL_TAKEN, occurred_at=now_ms(),
        detected_at=now_ms(), price=116.32,
    ))
    cur = instrument_current(db, settings, instrument_id)
    assert cur["stage"] == "HTF-зона достигнута, рассчитываем H1"
    assert "Ждём HTF-зону" not in cur["stage"]
    assert cur["wait"]["code"] == "observation_pending"
    assert cur["wait"]["params"]["zone_id"] == fvg.id
    facts = cur["liquidity_facts"]
    assert facts and facts[0]["kind"] == "level_taken"
    assert facts[0]["creates_scenario"] is False

    settings.detector.htf_context_types = "OB"
    cur_ob = instrument_current(db, settings, instrument_id)
    assert cur_ob["wait"]["code"] == "context_type_disabled"
    assert "FVG" in cur_ob["wait"]["message"]
    assert "отключён" in cur_ob["wait"]["message"]
    assert cur_ob["reached_disabled"]


@pytest.mark.asyncio
async def test_a07_h1_without_observation():
    db = Database(":memory:")
    adapter = FakeAdapter()
    adapter.candles = _recent_h1(
        [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)]
    )
    worker, _ = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    assert not db.list_ltf_observations(instrument_id=ins.id)
    await worker.ltf_poll_once()
    assert not db.list_ltf_observations(instrument_id=ins.id)
    pivots = db.list_ltf_pivots(ins.id)
    assert len(pivots) == 1 and pivots[0].price == 13
    assert db.get_meta(f"ltf:h1:last_close:{ins.id}")
    assert db.get_meta(f"ltf:h1:scenario_last_close:{ins.id}") is None


def test_a08_structure_without_context(tmp_path, db, instrument_id):
    settings = _settings(tmp_path)
    app = create_app(db, settings, ltf_engine=LtfEngine(db, settings.detector))
    client = TestClient(app)
    _live(db, instrument_id, 100.0)
    res = client.get(
        f"/api/ltf/instruments/{instrument_id}/structure", headers=AUTH,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["context_id"] is None
    assert body["ranges"] == []
    assert body["entries"] == []
    assert body["entries_excluded"] == []
    assert body["expected"] == {"bos": None, "sms": None}
    assert body["timeframe"] == "H1"
    direct = instrument_structure(db, settings, instrument_id)
    assert direct["ranges"] == []


def test_a11_finished_parent_not_selected(tmp_path, db, instrument_id):
    settings = _settings(tmp_path)
    now = _live(db, instrument_id, 100.0)
    zone = _zone(
        db, instrument_id, lower=71.90, upper=75.80,
        display_until=now - 1000,
    )
    db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone.id, zone_version=1,
        cycle_id=zone.cycle_id, direction=Direction.BULL, state="active",
        activated_at=now - 50_000,
    ))
    cur = instrument_current(db, settings, instrument_id)
    assert cur["selected_context_id"] is None
    assert db.list_ltf_observations(instrument_id=instrument_id)
    assert cur["wait"]["code"] == "parent_no_longer_relevant"
    assert zone.display_until is not None


def test_a12_deep_ob_stays_parent(db, instrument_id):
    zone = _zone(db, instrument_id)
    zone.max_test_depth = 0.95
    zone.validity = "tested"
    assert zone.market_validity == "active"
    assert eligible_htf_parent(zone, DetectorConfig()) is True


def test_a13_a14_terminal_level_and_repair(tmp_path, db, instrument_id):
    settings = _settings(tmp_path)
    now = _live(db, instrument_id, 100.0)
    parent = _zone(db, instrument_id, lower=90, upper=110)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=parent.id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, state="active",
        activated_at=now - 10_000,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=now, updated_at=now,
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90, upper=120, mid=105,
        available_at=now,
    ))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="SSL",
        direction=Direction.BULL, lower=120.64, upper=120.64,
        formed_at=now, confirmed_at=now, validity="tested",
    ))
    entry = db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
        eligible=True, overlap="none", state="tested", reason="ok",
        added_at=now, updated_at=now,
    ))
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=ez.id, scenario_id=sc.id, level=120.64,
        touch_at=now, candle_open_time=now, state="failed",
        close_price=121.0, resolved_at=now,
    ))
    fe = evaluate_final(
        entry, ez, allow_outside=False,
        liquidity_tests=db.list_ltf_liquidity_tests(scenario_id=sc.id),
    )
    assert fe.eligible_now is False
    assert fe.primary_reason == REASON_LEVEL_BROKEN
    cur = instrument_current(db, settings, instrument_id)
    assert cur["counts"]["eligible"] == 0
    kept = db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=2,
        eligible=False, overlap="none", state="tested", reason="outside_pd",
        added_at=now, updated_at=now,
    ))
    deep = db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=3,
        eligible=False, overlap="none", state="tested", reason="tested_too_deep",
        added_at=now, updated_at=now,
    ))
    preview = repair_terminal_entry_reasons(db, dry_run=True)
    assert preview and preview[0]["old"] == "ok" and preview[0]["new"] == "level_broken"
    assert {row["id"] for row in preview} == {entry.id}
    assert db.get_ltf_scenario_entry(entry.id).reason == "ok"
    assert db.get_ltf_scenario_entry(kept.id).reason == "outside_pd"
    assert db.get_ltf_scenario_entry(deep.id).reason == "tested_too_deep"
    applied = repair_terminal_entry_reasons(db, dry_run=False)
    assert applied[0]["new"] == "level_broken"
    assert db.get_ltf_scenario_entry(entry.id).reason == "level_broken"


def test_a19_forming_bar_does_not_confirm_pivot(db, instrument_id):
    bars = _recent_h1(
        [(10, 9), (11, 9), (12, 9), (13, 9), (12, 9), (11, 9), (10, 9)],
        instrument_id=instrument_id,
    )
    forming = make_candle(
        bars[-1].open_time + H1_MS, 12, 20, 8, 12,
        timeframe="H1", instrument_id=instrument_id, closed=False,
    )
    db.insert_candles([*bars, forming])
    LtfEngine(db, DetectorConfig()).process_h1_close(instrument_id)
    pivots = db.list_ltf_pivots(instrument_id)
    assert len(pivots) == 1
    assert all(p.candle_open_time != forming.open_time for p in pivots)
    assert pivots[0].state == "confirmed"


def test_a21_diagnostics_and_single_owner(tmp_path, db, instrument_id):
    settings = _settings(tmp_path)
    app = create_app(db, settings)
    client = TestClient(app)
    res = client.get("/api/diagnostics", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    blob = json.dumps(body)
    assert "telegram" not in blob.lower() or "telegram_owner" in blob
    assert "dev-token" not in blob
    assert settings.auth_token not in blob
    assert body["schema_version"] == 1
    assert body["profile_fingerprint"]
    assert body["process_role"] == "web"
    own = claim_owner(db, CALC_OWNER, {"pid": os.getpid(), "role": "calc"})
    assert own["owned"] is True
    other = claim_owner(db, CALC_OWNER, {"pid": os.getpid() + 100000, "role": "calc"})
    assert other["owned"] is False
    diag = build_diagnostics(
        db, settings, role="web", instance_id="t", started_at=1, build_id="test",
    )
    assert diag["calc_conflict"]["owned"] is False


def test_profile_migration_exact_ob_only(tmp_path):
    db = Database(":memory:")
    settings = Settings()
    settings.db_path = str(tmp_path / "x.db")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"detector": {"htf_context_types": "OB", "ltf_enabled": True}}),
                    encoding="utf-8")
    report = migrate_saved_htf_profile(db, settings)
    assert report["action"] == "migrated"
    assert report["added"] == ["FVG"]
    assert settings.detector.htf_context_types == "OB,FVG"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["detector"]["htf_context_types"] == "OB,FVG"
    assert saved["detector"]["ltf_enabled"] is True
    saved["detector"]["htf_context_types"] = "FVG"
    path.write_text(json.dumps(saved), encoding="utf-8")
    again = migrate_saved_htf_profile(db, settings)
    assert again.get("repeated") is True
    assert json.loads(path.read_text(encoding="utf-8"))["detector"]["htf_context_types"] == "FVG"

    db2 = Database(":memory:")
    path.write_text(json.dumps({"detector": {"htf_context_types": "FVG"}}), encoding="utf-8")
    skipped = migrate_saved_htf_profile(db2, settings)
    assert skipped["action"] == "skipped"
    path.write_text(json.dumps({"detector": {"htf_context_types": "OB"}}), encoding="utf-8")
    still = migrate_saved_htf_profile(db2, settings)
    assert still.get("repeated") is True
    assert json.loads(path.read_text(encoding="utf-8"))["detector"]["htf_context_types"] == "OB"


def test_cross_process_cache_sees_other_writer(tmp_path):
    path = str(tmp_path / "shared.db")
    writer = Database(path)
    reader = Database(path)
    assert reader.get_zones() == []
    writer.upsert_instrument(__import__("app.models", fromlist=["Instrument"]).Instrument(
        id=None, asset="SOL", venue="binance", market_type="spot",
        symbol="SOLUSDT", quote_asset="USDT",
    ))
    iid = writer.get_instruments()[0].id
    writer.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=1, upper=2, formed_at=1, confirmed_at=2,
        status=ZoneStatus.ACTIVE,
    ))
    reader._epoch_checked_at = 0
    assert len(reader.get_zones(instrument_id=iid)) == 1
    writer.close()
    reader.close()
