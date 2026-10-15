"""Regression coverage for the stale sync receipt and shared REST exhaustion."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx

from app.adapters.base import AdapterError
from app.adapters.binance import BinanceSpotAdapter
from app.alt.runner import AltRunner
from tests.test_data_sync import market
from tests.test_alt_v3_integration import market as alt_market
from tests.test_alt_engine_v3 import stamp


async def test_binance_all_callers_respect_ban_without_retry_header():
    adapter = BinanceSpotAdapter("https://test.binance")
    from app.models import now_ms
    with respx.mock:
        banned = respx.get("https://test.binance/api/v3/trades").respond(
            418, json={"code": -1003, "msg": f"IP banned until {now_ms() + 120000}."})
        history = respx.get("https://test.binance/api/v3/klines").respond(200, json=[])
        with pytest.raises(AdapterError) as error:
            await adapter.last_price("BTCUSDT")
        assert error.value.retry_after > 110
        with pytest.raises(AdapterError) as error:
            await adapter.klines("ETHUSDT", "H1", 0, 1)
        assert error.value.retry_after > 110
        assert banned.call_count == 1 and history.call_count == 0
    await adapter.aclose()


async def test_binance_budget_includes_direct_concurrent_requests(monkeypatch):
    adapter = BinanceSpotAdapter("https://test.binance")
    adapter._weight_budget = 50
    with respx.mock:
        trades = respx.get("https://test.binance/api/v3/trades").respond(
            200, json=[{"id": 1, "price": "10", "time": 100}])
        results = await asyncio.gather(*(adapter.last_price("BTCUSDT") for _ in range(3)),
                                       return_exceptions=True)
        assert trades.call_count == 2
        assert sum(isinstance(r, AdapterError) for r in results) == 1
        assert next(r for r in results if isinstance(r, AdapterError)).retry_after > 0
    await adapter.aclose()


async def test_binance_budget_uses_source_weight_and_resets(monkeypatch):
    adapter = BinanceSpotAdapter("https://test.binance")
    wall = [120.0]
    monkeypatch.setattr("app.adapters.binance.time.time", lambda: wall[0])
    with respx.mock:
        trades = respx.get("https://test.binance/api/v3/trades").respond(
            200, headers={"X-MBX-USED-WEIGHT-1M": "4799"},
            json=[{"id": 1, "price": "10", "time": 100}])
        await adapter.last_price("BTCUSDT")
        with pytest.raises(AdapterError):
            await adapter.last_price("ETHUSDT")
        assert trades.call_count == 1
        wall[0] = 182.0
        adapter._cooldown_until = 0
        trades.respond(200, json=[{"id": 2, "price": "11", "time": 101}])
        await adapter.last_price("ETHUSDT")
        assert adapter._weight_used == 25
    await adapter.aclose()


async def test_restoring_asset_does_not_block_other_periodic_channels(market, monkeypatch):
    _, worker, _, _, instruments, _ = market
    restoring, live = instruments
    worker._restoring_instruments.add(restoring.id)
    htf, ltf = AsyncMock(), AsyncMock()
    monkeypatch.setattr(worker, "_poll_instrument", htf)
    monkeypatch.setattr(worker, "_poll_ltf_instrument", ltf)
    monkeypatch.setattr(worker, "_calculate", AsyncMock(return_value=0))
    async with worker.data_sync.instrument(restoring.id):
        await asyncio.wait_for(worker.poll_once(), 1)
        await asyncio.wait_for(worker.ltf_poll_once(), 1)
    htf.assert_awaited_once_with(live)
    ltf.assert_awaited_once_with(live)
    worker._restoring_instruments.clear()
    await worker.poll_once()
    assert htf.await_count == 3


async def test_alt_healthy_stream_suppresses_rest_and_recovers_when_stale(alt_market, monkeypatch):
    db, settings, src, _ = alt_market
    clock = [stamp("2026-08-18")]
    monkeypatch.setattr("app.alt.runner.now_ms", lambda: clock[0])
    db.set_meta(f"alt:engine:{src.asset_id}:{src.id}", "v3")
    venue = SimpleNamespace(last_price=AsyncMock(return_value=SimpleNamespace(
        price=1.8, event_at=clock[0], received_at=clock[0], price_kind="trade", source_seq="1")))
    runner = AltRunner(db, settings, {"binance": venue}, None)
    runner._stream_received[src.id] = clock[0]
    await runner.poll_live()
    venue.last_price.assert_not_awaited()
    assert src.id in runner._live_sources
    clock[0] += 31_000
    await runner.poll_live()
    venue.last_price.assert_awaited_once()
    clock[0] += 5_000
    await runner.poll_live()
    venue.last_price.assert_awaited_once()
    clock[0] += 60_000
    await runner.poll_live()
    assert venue.last_price.await_count == 2


def test_sync_display_distinguishes_current_freshness_from_old_job():
    if not shutil.which("node"):
        pytest.skip("Node is required for the frontend regression")
    subprocess.run(["node", str(Path(__file__).with_name("sync_incident_display.cjs"))],
                   check=True, capture_output=True, text=True)
