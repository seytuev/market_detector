"""Excluded assets, unsupported pairs, and genuine source failures stay distinct."""
import json
from datetime import datetime, timezone

import pytest

from app.alt.runner import AltRunner
from app.config import Settings
from app.models_alt import AltAsset, AltRun
from app.services.alt_overview import alt_setups_table
from tests.test_alt_runner import FakeVenue


def asset(db, cmc_id, symbol, **kwargs):
    return db.upsert_alt_asset(AltAsset(None, cmc_id, symbol, cmc_rank=20, **kwargs))


@pytest.mark.parametrize("version", ["v1", "v3"])
def test_exclusions_and_unsupported_pairs_are_not_broken_setups(db, version):
    settings = Settings()
    settings.alt_config.engine_version = version
    asset(db, 1, "USDe", exclusion_category="stable")
    asset(db, 2, "USDG", exclusion_category="stable")
    asset(db, 3, "WETH", exclusion_category="wrapped")
    asset(db, 4, "DAI")  # older row without the persisted classification
    asset(db, 5, "OFF", enabled=False)
    unsupported = asset(db, 6, "XMR")
    pending = asset(db, 7, "WAIT")
    failed = asset(db, 8, "FAIL")
    db.insert_alt_run(AltRun(None, started_ms=1, as_of_ms=1, finished_ms=2, status="ok", errors=1,
        summary_json=json.dumps({"per_asset": [
            {"asset_id": unsupported.id, "status": "skipped", "reason": "no_spot_pair"},
            {"asset_id": failed.id, "status": "skipped", "reason": "catalog_unavailable:binance"}]})))
    result = alt_setups_table(db, settings, bucket="review")
    assert {r["asset"]["symbol"] for r in result["rows"]} == {"WAIT", "FAIL"}
    assert result["buckets"]["review"] == 2
    assert result["coverage"] == {"excluded": 5, "unsupported_spot": 1}
    assert "каталог" in next(r for r in result["rows"] if r["asset"]["symbol"] == "FAIL")["reason"]


async def test_runner_never_targets_excluded_assets(db):
    stable = asset(db, 1, "USDG", exclusion_category="stable")
    wrapped = asset(db, 2, "WETH")
    disabled = asset(db, 3, "OFF", enabled=False)
    allowed = asset(db, 4, "NEAR")
    runner = AltRunner(db, Settings(), {}, None)
    assert [a.symbol for a, _ in runner._target_assets([1, 2, 3, 4])] == ["NEAR"]


async def test_catalog_failure_is_not_reported_as_no_pair(db):
    row = asset(db, 1, "COIN")
    runner = AltRunner(db, Settings(), {
        "binance": FakeVenue("binance", catalog_error="offline"),
        "bybit": FakeVenue("bybit")}, None)
    src, reason = await runner._ensure_source(row, {}, {})
    assert src is None and reason == "catalog_unavailable:binance"


async def test_catalog_failure_can_still_use_verified_spot_fallback(db):
    row = asset(db, 1, "COIN")
    runner = AltRunner(db, Settings(), {
        "binance": FakeVenue("binance", catalog_error="offline"),
        "bybit": FakeVenue("bybit", {"COIN": ["USDT"]})}, None)
    src, reason = await runner._ensure_source(row, {}, {})
    assert reason is None and src.venue == "bybit" and src.symbol == "COINUSDT"


async def test_unresolved_identity_does_not_pick_pair_by_symbol(db):
    row = asset(db, 1, "COIN", mapping_status="mapping_pending")
    runner = AltRunner(db, Settings(), {"binance": FakeVenue("binance", {"COIN": ["USDT"]})}, None)
    assert await runner._ensure_source(row, {}, {}) == (None, "mapping_pending")


def test_storage_upgrade_resets_one_time_replay_even_after_failed_run(db):
    now = datetime(2026, 10, 15, 23, tzinfo=timezone.utc)
    db.insert_alt_run(AltRun(None, started_ms=int(now.timestamp()*1000), as_of_ms=0, status="ok", errors=184))
    db.set_meta("alt:v3:storage_upgrade_pending", "1")
    runner = AltRunner(db, Settings(), {}, None)
    assert runner.should_run(now)
    db.set_meta("alt:v3:initialized", "replayed")
    assert not runner.should_run(now)
