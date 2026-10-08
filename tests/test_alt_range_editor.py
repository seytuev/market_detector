from __future__ import annotations

import json

import pytest

from app.db import Database
from app.models_alt import (
    AltAsset, AltCandle, AltEvent, AltFrozenRange, AltInstrumentSource,
    AltRangeCandidate, AltSetup,
)
from app.services.alt_range_editor import preview_range_revision, save_range_revision

DAY = 86_400_000
T0 = 1_700_006_400_000


@pytest.fixture
def setup_db():
    db = Database(":memory:")
    asset = db.upsert_alt_asset(AltAsset(id=None, cmc_id=999, symbol="EDIT"))
    source = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="binance", symbol="EDITUSDT"))
    db.insert_alt_candles([
        AltCandle(source_id=source.id, open_time=T0 + i * DAY,
                  open=p, high=p + 1, low=p - 1, close=p)
        for i, p in enumerate([12, 8, 11, 22, 25])
    ])
    candidate = db.insert_alt_range_candidate(AltRangeCandidate(
        id=None, asset_id=asset.id, origin_key="edit", start_anchor_open_time=T0,
        rebound_anchor_open_time=T0 + DAY, lower=10, upper=20, width=10, mid=15,
        n_days=5,
    ))
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=candidate.id, lower=10, upper=20, width=10, mid=15,
        start_anchor_open_time=T0, rebound_anchor_open_time=T0 + DAY,
    ))
    setup, _ = db.insert_alt_setup(AltSetup(
        id=None, asset_id=asset.id, source_id=source.id, range_id=frozen.id,
    ))
    db.insert_alt_event(AltEvent(
        id=None, setup_id=setup.id, event_type="breakout", source_event_id="old",
        event_time_ms=T0 + 3 * DAY,
    ))
    yield db, setup, frozen
    db.close()


def test_preview_recomputes_geometry_and_historical_facts(setup_db):
    db, setup, _ = setup_db
    result = preview_range_revision(db, "setup", setup.id, {
        "lower": 9, "upper": 21, "base_start_open_time": T0,
    })
    assert result["range"]["mid"] == 15
    assert result["range"]["width"] == 12
    assert result["derived"]["targets"][0]["price"] == 33
    assert result["derived"]["cancel"] == {"price": -3, "reachable": False}
    assert result["derived"]["position"] == "above"
    assert len(result["derived"]["breakouts"]) == 2
    assert len(result["derived"]["excursions_below"]) == 1


def test_save_is_versioned_updates_effective_range_and_supersedes_pending(setup_db):
    db, setup, frozen = setup_db
    payload = {
        "lower": 9, "upper": 21, "base_start_open_time": T0,
        "expected_revision": 0, "idempotency_key": "same-request",
    }
    saved = save_range_revision(db, "setup", setup.id, payload)
    repeated = save_range_revision(db, "setup", setup.id, payload)
    assert saved["revision"] == 1 and saved["created"] is True
    assert repeated["id"] == saved["id"] and repeated["created"] is False
    effective = db.get_alt_frozen_range(frozen.id)
    assert (effective.lower, effective.upper, effective.mid, effective.width) == (9, 21, 15, 12)
    updated_setup = db.get_alt_setup(setup.id)
    assert updated_setup.cancel_price == -3
    assert json.loads(updated_setup.targets_json)[0]["price"] == 33
    old = db.list_alt_events(setup.id)[0]
    assert old.delivered is True
    assert json.loads(old.payload_json)["superseded_by_range_revision"] == 1


def test_save_rejects_stale_revision(setup_db):
    db, setup, _ = setup_db
    payload = {"lower": 9, "upper": 21, "base_start_open_time": T0,
               "expected_revision": 0, "idempotency_key": "first"}
    save_range_revision(db, "setup", setup.id, payload)
    payload.update({"upper": 22, "idempotency_key": "stale"})
    with pytest.raises(ValueError, match="revision_conflict:1"):
        save_range_revision(db, "setup", setup.id, payload)


def test_manual_candidate_geometry_survives_auto_update(setup_db):
    db, setup, frozen = setup_db
    candidate = db.get_alt_range_candidate(frozen.range_id)
    save_range_revision(db, "candidate", candidate.id, {
        "lower": 9, "upper": 21, "base_start_open_time": T0,
        "expected_revision": 0, "idempotency_key": "candidate-manual",
    })
    db.update_alt_range_candidate(
        candidate.id, lower=1, upper=2, mid=1.5, width=1,
        n_days=99, state="forming",
    )
    effective = db.get_alt_range_candidate(candidate.id)
    assert (effective.lower, effective.upper, effective.mid, effective.width) == (9, 21, 15, 12)
    assert effective.n_days == 99
