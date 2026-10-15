"""Reuse historical source samples without hiding failures or window closes."""
from types import SimpleNamespace

import pytest

from app.adapters import coinglass
from app.adapters.coinglass import AuthError, CoinglassClient, CoinglassError
from app.events import sources


def test_cache_reuses_detached_sample_and_expires_at_new_closed_window(monkeypatch):
    clock = [0, 14_399]
    monkeypatch.setattr(coinglass, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[1]))
    client = CoinglassClient("test-key", cache_seconds=300)
    calls = []

    def fetch(path, params):
        calls.append((path, params))
        return {"data": [{"close": len(calls)}]}

    monkeypatch.setattr(client, "_get", fetch)
    first = client.get("/history", {"symbol": "BTC", "interval": "4h"})
    first["data"][0]["close"] = 999
    assert client.get("/history", {"interval": "4h", "symbol": "BTC"})["data"][0]["close"] == 1
    assert len(calls) == 1
    clock[:] = [1, 14_400]
    assert client.get("/history", {"symbol": "BTC", "interval": "4h"})["data"][0]["close"] == 2
    clock[:] = [301, 14_700]
    assert client.get("/history", {"symbol": "BTC", "interval": "4h"})["data"][0]["close"] == 3


def test_expired_success_does_not_hide_or_cache_rate_limit_failure(monkeypatch):
    clock = [0]
    monkeypatch.setattr(coinglass, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 1000 + clock[0]))
    client = CoinglassClient("test-key", cache_seconds=300)
    calls = []

    def fetch(path, params):
        calls.append(path)
        if len(calls) == 2:
            raise CoinglassError("Too Many Requests", code="429", http=429)
        return {"data": [len(calls)]}

    monkeypatch.setattr(client, "_get", fetch)
    assert client.get("/history") == {"data": [1]}
    clock[0] = 300
    with pytest.raises(CoinglassError, match="Too Many Requests"):
        client.get("/history")
    assert client.get("/history") == {"data": [3]}


def test_source_client_shared_between_cycles_and_isolated_by_account_and_host():
    sources._coinglass_client.cache_clear()
    try:
        settings = SimpleNamespace(coinglass_api_key="account-a", coinglass_base_url="https://source.test")
        first = sources.make_coinglass(settings)
        assert sources.make_coinglass(settings) is first
        settings.coinglass_api_key = "account-b"
        assert sources.make_coinglass(settings) is not first
        settings.coinglass_api_key = "account-a"
        settings.coinglass_base_url = "https://other.test"
        assert sources.make_coinglass(settings) is not first
        settings.coinglass_api_key = ""
        with pytest.raises(AuthError):
            sources.make_coinglass(settings)
        assert "account-a" not in repr(first)
    finally:
        sources._coinglass_client.cache_clear()


def test_concurrent_identical_reads_share_one_request(monkeypatch):
    import concurrent.futures
    import time
    client = CoinglassClient("test-key", cache_seconds=300)
    calls = []

    def fetch(path, params):
        calls.append(path)
        time.sleep(.02)
        return {"data": []}

    monkeypatch.setattr(client, "_get", fetch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.get("/history"), range(4)))
    assert results == [{"data": []}] * 4
    assert calls == ["/history"]
