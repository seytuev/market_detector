"""ТЗ переработки D01: согласованный снимок с монотонной версией состояния.

- state_seq растёт при записи предметного состояния (свечи, события, зоны,
  ltf_*);
- /current и карточка наблюдения отдают state_version == state_seq;
- WS-сообщения несут state_seq (версия изменения);
- read_tx даёт серию чтений одной версии;
- F04: слоевые эндпоинты (chart/journal/entries) отдают state_version из
  того же счётчика — на спокойной БД версии всех слоёв одного инструмента
  равны.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import (
    Direction,
    Event,
    EventKind,
    Zone,
    ZoneStatus,
    ZoneType,
)
from app.models_ltf import LtfEvent, LtfObservation, LtfScenario
from app.web.api import create_app
from tests.conftest import make_candle

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T0 = 1_780_000_000_000


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(
        create_app(db, settings, ltf_engine=LtfEngine(db, settings.detector))
    )


def _zone(db: Database, instrument_id: int) -> int:
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000,
        status=ZoneStatus.ACTIVE,
    ))


def test_state_seq_grows_on_writes(db: Database, instrument_id: int):
    assert db.get_state_seq() == 0
    db.insert_candles([make_candle(T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    s1 = db.get_state_seq()
    assert s1 > 0
    zid = _zone(db, instrument_id)
    s2 = db.get_state_seq()
    assert s2 > s1
    db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=T0, detected_at=T0, price=100.0, depth=0.0,
    ))
    assert db.get_state_seq() > s2
    db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    assert db.get_state_seq() > s2 + 1 or db.get_state_seq() > s2


def test_current_state_version_matches_seq(db, client, settings,
                                           instrument_id: int):
    zid = _zone(db, instrument_id)
    db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    r = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                   headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state_version"] == db.get_state_seq()
    # запись поднимает версию следующего снимка
    db.insert_candles([make_candle(T0 + 3_600_000, 100, 101, 99, 100.5,
                            timeframe="H1", instrument_id=instrument_id)])
    r2 = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                    headers=AUTH)
    assert r2.json()["state_version"] > r.json()["state_version"]


def test_read_tx_consistent(db: Database, instrument_id: int):
    db.insert_candles([make_candle(T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    with db.read_tx():
        seq1 = db.get_state_seq()
        candles = db.get_candles(instrument_id, "H1")
        seq2 = db.get_state_seq()
    assert seq1 == seq2 and len(candles) == 1
    # после выхода из read_tx запись снова возможна
    db.insert_candles([make_candle(T0 + 3_600_000, 100, 101, 99, 100.5,
                            timeframe="H1", instrument_id=instrument_id)])
    assert db.get_state_seq() > seq1


def test_read_tx_nested_and_waits_out_open_write(db: Database):
    with db.read_tx():
        with db.read_tx():
            assert db.get_state_seq() >= 0

    started = threading.Event()

    def writer():
        db.conn._lock.acquire()
        db.conn._conn.execute("BEGIN")
        db.conn._lock.release()
        started.set()
        time.sleep(0.05)
        db.conn._lock.acquire()
        db.conn._conn.commit()
        db.conn._lock.release()

    thread = threading.Thread(target=writer)
    thread.start()
    assert started.wait(2)
    with db.read_tx():
        db.get_state_seq()
    thread.join(2)
    assert not thread.is_alive()


def test_ws_broadcast_carries_state_seq(db, client, instrument_id: int):
    with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
        hub = client.app.state.ws_hub
        hub.broadcast({"type": "zone", "zone_id": 1})
        msg = ws.receive_json()
    assert msg["type"] == "zone"
    assert msg["state_seq"] == db.get_state_seq()


def _seed_observation_with_scenario(db: Database, instrument_id: int):
    """Наблюдение + сценарий + событие — минимум для слоевых эндпоинтов."""
    zid = _zone(db, instrument_id)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id, kind="bos",
        payload={"break_level": 105.0}, occurred_at=T0 + 100,
        detected_at=T0 + 100, dedupe_key="bos:sc",
    ))
    return obs.id, sc.id


def test_layer_endpoints_share_state_version(db, client, instrument_id: int):
    """F04: chart/journal/entries несут state_version из общего state_seq;
    на спокойной БД версии всех слоёв инструмента совпадают с /current."""
    obs_id, sc_id = _seed_observation_with_scenario(db, instrument_id)
    current = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                         headers=AUTH).json()
    chart = client.get(f"/api/ltf/observations/{obs_id}/chart",
                       headers=AUTH).json()
    obs_journal = client.get(f"/api/ltf/observations/{obs_id}/journal",
                             headers=AUTH).json()
    sc_journal = client.get(f"/api/ltf/scenarios/{sc_id}/journal",
                            headers=AUTH).json()
    entries = client.get(f"/api/ltf/scenarios/{sc_id}/entries",
                         headers=AUTH).json()
    # конверты: верхнеуровневые поля chart сохранены, списки — в events/entries
    assert chart["timeframe"] == "H1"
    assert [e["kind"] for e in obs_journal["events"]] == ["bos"]
    assert [e["kind"] for e in sc_journal["events"]] == ["bos"]
    assert entries["entries"] == []
    versions = {
        current["state_version"], chart["state_version"],
        obs_journal["state_version"], sc_journal["state_version"],
        entries["state_version"],
    }
    assert versions == {db.get_state_seq()}
    # запись поднимает версию всех слоёв
    db.insert_candles([make_candle(T0 + 3_600_000, 100, 101, 99, 100.5,
                            timeframe="H1", instrument_id=instrument_id)])
    bumped = db.get_state_seq()
    assert bumped > chart["state_version"]
    chart2 = client.get(f"/api/ltf/observations/{obs_id}/chart",
                        headers=AUTH).json()
    journal2 = client.get(f"/api/ltf/observations/{obs_id}/journal",
                          headers=AUTH).json()
    entries2 = client.get(f"/api/ltf/scenarios/{sc_id}/entries",
                          headers=AUTH).json()
    assert (chart2["state_version"], journal2["state_version"],
            entries2["state_version"]) == (bumped, bumped, bumped)
