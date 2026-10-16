"""Добавление, удаление и восстановление пар без потери истории."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import AdapterError
from app.adapters.binance import BinanceSpotAdapter
from app.config import Settings
from app.db import Database
from app.models import Instrument
from app.web.api import create_app
from .conftest import make_candle
from .test_worker import FakeAdapter, _make_worker

AUTH = {"Authorization": "Bearer instrument-test"}


@pytest.fixture
def client(db, tmp_path):
    settings = Settings()
    settings.auth_token = "instrument-test"
    settings.db_path = str(tmp_path / "settings.db")
    return TestClient(create_app(db, settings))


def test_remove_and_restore_history(client, db, instrument_id):
    other = db.upsert_instrument(Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT"))
    db.insert_candles([make_candle(0, 100, 110, 90, 105, instrument_id=instrument_id)])
    db.spool_quote(instrument_id, source="test", event_at=1, received_at=1,
                   price=105, price_kind="trade", source_seq="1")
    version = db.get_domain("seq:config")
    assert client.delete(f"/api/instruments/{instrument_id}").status_code == 401
    assert client.delete(f"/api/instruments/{instrument_id}", headers=AUTH).status_code == 204
    assert [i["id"] for i in client.get("/api/instruments", headers=AUTH).json()] == [other]
    saved = db.get_instrument(instrument_id)
    assert saved.removed and not saved.enabled and not saved.ltf_analyze
    assert db.get_domain("seq:config") > version
    assert not db.load_spool(instrument_id)
    assert len(db.get_candles(instrument_id, "D1")) == 1
    for route, payload in [("active", {"active": True}), ("toggle", {}), ("ltf-analyze", {"analyze": True})]:
        assert client.post(f"/api/instruments/{instrument_id}/{route}", headers=AUTH, json=payload).status_code == 404
    # DELETE is repeatable, and restoring the pair keeps the same identity.
    assert client.delete(f"/api/instruments/{instrument_id}", headers=AUTH).status_code == 204
    restored = client.post("/api/instruments", headers=AUTH, json={
        "asset": "BTC", "venue": "binance", "symbol": "BTCUSDT",
    })
    assert restored.status_code == 201
    assert restored.json()["id"] == instrument_id
    assert restored.json()["enabled"] and restored.json()["ltf_analyze"]
    assert not db.get_instrument(instrument_id).removed
    assert len(db.get_candles(instrument_id, "D1")) == 1
    assert len(db.get_instruments()) == 2
    assert client.delete("/api/instruments/999999", headers=AUTH).status_code == 404


@pytest.mark.parametrize("symbol", ["solusdc", " SOL/USDC ", "SOL-USDC"])
def test_add_from_catalog_uses_exchange_metadata(client, db, monkeypatch, symbol):
    catalog = AsyncMock(return_value=[Instrument(None, "SOL", "binance", "spot", "SOLUSDC", "USDC", precision=3)])
    monkeypatch.setattr(BinanceSpotAdapter, "catalog", catalog)
    payload = {"venue": "binance", "symbol": symbol}
    assert client.post("/api/instruments/from-catalog", json=payload).status_code == 401
    response = client.post("/api/instruments/from-catalog", headers=AUTH, json=payload)
    assert response.status_code == 201
    ins = response.json()
    assert (ins["symbol"], ins["quote_asset"], ins["precision"]) == ("SOLUSDC", "USDC", 3)
    db.remove_instrument(ins["id"])
    restored = client.post("/api/instruments/from-catalog", headers=AUTH, json=payload).json()
    assert restored["id"] == ins["id"] and restored["enabled"] and restored["ltf_analyze"]
    assert len(db.get_instruments()) == 1


def test_catalog_errors_do_not_create_pair(client, db, monkeypatch):
    monkeypatch.setattr(BinanceSpotAdapter, "catalog", AsyncMock(return_value=[]))
    payload = {"venue": "binance", "symbol": "UNKNOWNUSDT"}
    assert client.post("/api/instruments/from-catalog", headers=AUTH, json=payload).status_code == 422
    monkeypatch.setattr(BinanceSpotAdapter, "catalog", AsyncMock(side_effect=AdapterError("offline")))
    assert client.post("/api/instruments/from-catalog", headers=AUTH, json=payload).status_code == 503
    assert not db.get_instruments()


async def test_removed_seed_stays_removed_after_restart(tmp_path):
    path = str(tmp_path / "instruments.db")
    db = Database(path)
    iid = db.upsert_instrument(Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT"))
    db.remove_instrument(iid)
    db.close()
    db = Database(path)
    try:
        worker, _ = _make_worker(db, FakeAdapter())
        await worker.seed_instruments()
        assert db.get_instrument(iid).removed
        assert not db.get_instrument(iid).ltf_analyze
        assert iid not in [i.id for i in db.get_instruments()]
        assert iid not in [i.id for i in db.get_instruments(enabled_only=True)]
    finally:
        db.close()


def test_sqlite_upgrade_preserves_existing_pairs(tmp_path):
    path = str(tmp_path / "upgrade.db")
    db = Database(path)
    iid = db.upsert_instrument(Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT"))
    db.conn.execute("ALTER TABLE instrument DROP COLUMN removed")
    db.conn.commit()
    db.close()
    db = Database(path)
    try:
        assert db.get_instrument(iid).enabled
        assert not db.get_instrument(iid).removed
        db.remove_instrument(iid)
        assert not db.get_instruments()
    finally:
        db.close()
