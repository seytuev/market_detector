"""Этап 6a (§13): доменные версии состояния и WS-конверт.

- счётчики seq:<domain> растут только при реальной записи домена
  (no-op upsert, отклонённый F9-снимок, запоздавший тик версию не двигают);
- снимки несут вектор versions, согласованный с данными (read_tx);
- WS-сообщения несут epoch/seq/domain_versions поверх прежнего state_seq;
  seq монотонен на соединение и независим между соединениями;
- state_seq и прежние поля ответов не тронуты (обратная совместимость).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import (
    Direction,
    Event,
    EventKind,
    Review,
    Zone,
    ZoneStatus,
    ZoneType,
)
from app.models_ltf import LtfObservation, LtfScenario
from app.services.data_sync import DataSync
from app.services.runtime import Readiness
from app.web.api import create_app
from tests.conftest import make_candle

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T0 = 1_780_000_000_000
H1_MS = 3_600_000


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


def _observation(db: Database, instrument_id: int, zone_id: int) -> LtfObservation:
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))


# ---------------------------------------------------------------------------
# Доменные счётчики: bump только при реальной записи
# ---------------------------------------------------------------------------

def test_quote_bump_only_on_accepted_write(db: Database, instrument_id: int):
    key = f"seq:quote:{instrument_id}"
    assert db.get_domain(key) == 0
    assert db.set_quote_if_fresh(
        instrument_id, 100.0, T0, source="test", event_at=T0) is True
    assert db.get_domain(key) == 1
    # запоздавший тик (старше сохранённого) не принят — версия не растёт
    assert db.set_quote_if_fresh(
        instrument_id, 99.0, T0 - 1000, source="test",
        event_at=T0 - 1000) is False
    assert db.get_domain(key) == 1
    # более свежий принят
    assert db.set_quote_if_fresh(
        instrument_id, 101.0, T0 + 1000, source="test", event_at=T0 + 1000)
    assert db.get_domain(key) == 2


def test_repeated_instrument_flags_preserve_config_and_cancel_disabled_quotes(db, instrument_id):
    start = db.get_domain('seq:config')
    state = db.get_state_seq()
    db.set_instrument_enabled(instrument_id, True)
    assert db.get_domain('seq:config') == start and db.get_state_seq() == state
    db.set_instrument_active(instrument_id, True)
    changed = db.get_domain('seq:config')
    state = db.get_state_seq()
    db.set_instrument_active(instrument_id, True)
    assert db.get_domain('seq:config') == changed and db.get_state_seq() == state
    db.set_instrument_enabled(instrument_id, False)
    disabled = db.get_domain('seq:config')
    db.spool_quote(instrument_id, source='test', event_at=T0, received_at=T0,
                   price=100, price_kind='trade', source_seq='disabled-recovery')
    db.set_instrument_enabled(instrument_id, False)
    assert db.get_domain('seq:config') == disabled
    assert not db.spool_counts().get(instrument_id)
    db.set_instrument_active(instrument_id, False)
    disabled = db.get_domain('seq:config'); state = db.get_state_seq()
    db.set_instrument_active(instrument_id, False)
    assert db.get_domain('seq:config') == disabled and db.get_state_seq() == state


def test_insert_candles_bumps_only_changed_rows(db: Database, instrument_id: int):
    key = f"seq:candles:{instrument_id}:H1"
    db.insert_candles([make_candle(
        T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    assert db.get_domain(key) == 1
    # повтор той же свечи — no-op upsert: доменная версия НЕ растёт
    # (глобальный state_seq при этом растёт — прежнее поведение не тронуто)
    state_before = db.get_state_seq()
    db.insert_candles([make_candle(
        T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    assert db.get_domain(key) == 1
    assert db.get_state_seq() > state_before
    # ревизия закрытой свечи — bump и candles, и history
    db.insert_candles([make_candle(
        T0, 100, 102, 99, 101.5, timeframe="H1", instrument_id=instrument_id)])
    assert db.get_domain(key) == 2
    assert db.get_domain(f"seq:history:{instrument_id}") == 1
    # незакрытый снимок поверх закрытой свечи отклонён (F9) — версия не растёт
    db.insert_candles([make_candle(
        T0, 100, 105, 98, 103.0, timeframe="H1", instrument_id=instrument_id,
        closed=False)])
    assert db.get_domain(key) == 2
    # другой таймфрейм — свой счётчик
    db.insert_candles([make_candle(
        T0, 100, 101, 99, 100.5, timeframe="D1", instrument_id=instrument_id)])
    assert db.get_domain(f"seq:candles:{instrument_id}:D1") == 1
    assert db.get_domain(key) == 2
    # две НОВЫЕ свечи одного канала в одном вызове — один инкремент
    db.insert_candles([
        make_candle(T0 + H1_MS, 100.5, 101, 99.5, 100.7,
                    timeframe="H1", instrument_id=instrument_id),
        make_candle(T0 + 2 * H1_MS, 100.7, 101.2, 100.1, 100.9,
                    timeframe="H1", instrument_id=instrument_id),
    ])
    assert db.get_domain(key) == 3


def test_batch_writes_single_bump_per_key(db: Database, instrument_id: int):
    with db.batch_writes():
        db.set_quote_if_fresh(instrument_id, 100.0, T0, source="t", event_at=T0)
        db.set_quote_if_fresh(
            instrument_id, 101.0, T0 + 1, source="t", event_at=T0 + 1)
        db.insert_candles([make_candle(
            T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
        db.insert_candles([make_candle(
            T0 + H1_MS, 100.5, 101, 99.5, 100.7,
            timeframe="H1", instrument_id=instrument_id)])
    assert db.get_domain(f"seq:quote:{instrument_id}") == 1
    assert db.get_domain(f"seq:candles:{instrument_id}:H1") == 1


def test_batch_rollback_restores_domain_counter(db: Database, instrument_id: int):
    db.set_quote_if_fresh(instrument_id, 100.0, T0, source="t", event_at=T0)
    with pytest.raises(RuntimeError):
        with db.batch_writes():
            db.set_quote_if_fresh(
                instrument_id, 101.0, T0 + 1, source="t", event_at=T0 + 1)
            raise RuntimeError("boom")
    assert db.get_domain(f"seq:quote:{instrument_id}") == 1


def test_zone_scenario_config_bumps(
    db: Database, client: TestClient, instrument_id: int
):
    # zones: вставка, смена статуса, ревью
    zid = _zone(db, instrument_id)
    assert db.get_domain("seq:zones") == 1
    db.update_zone(zid, status=ZoneStatus.REJECTED)
    assert db.get_domain("seq:zones") == 2
    db.add_review(Review(
        id=None, zone_id=zid, decision="note", text="ок", created_at=T0))
    assert db.get_domain("seq:zones") == 3
    # событие зоны — тоже домен зон (лента событий — представление зон)
    db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=T0, detected_at=T0, price=100.0, depth=0.0))
    assert db.get_domain("seq:zones") == 4

    # scenario: наблюдение, сценарий, апдейты состояния
    obs = _observation(db, instrument_id, zid)
    assert db.get_domain(f"seq:scenario:{instrument_id}") == 1
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    assert db.get_domain(f"seq:scenario:{instrument_id}") == 2
    db.update_ltf_observation(obs.id, state="paused_data")
    assert db.get_domain(f"seq:scenario:{instrument_id}") == 3
    db.update_ltf_scenario(sc.id, state="cancelled", cancelled_at=T0 + 200)
    assert db.get_domain(f"seq:scenario:{instrument_id}") == 4

    # config: включение/выключение инструмента и POST /api/settings
    before = db.get_domain("seq:config")
    resp = client.post(f"/api/instruments/{instrument_id}/toggle", headers=AUTH)
    assert resp.status_code == 200
    assert db.get_domain("seq:config") == before + 1
    resp = client.post("/api/settings", headers=AUTH, json={"approach_pct": 0.03})
    assert resp.status_code == 200, resp.text
    assert db.get_domain("seq:config") == before + 2


# ---------------------------------------------------------------------------
# Снимки: вектор версий, согласованный с данными
# ---------------------------------------------------------------------------

def test_candles_bundle_versions(
    db: Database, client: TestClient, instrument_id: int
):
    db.insert_candles([make_candle(
        T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    url = f"/api/candles?instrument_id={instrument_id}&timeframe=H1"
    plain = client.get(url, headers=AUTH).json()
    assert isinstance(plain, list) and len(plain) == 1  # прежний контракт

    body1 = client.get(url + "&with_version=true", headers=AUTH).json()
    assert len(body1["candles"]) == 1
    assert body1["state_version"] == db.get_state_seq()  # совместимость (D01)
    assert body1["versions"] == {
        "candles": db.get_domain(f"seq:candles:{instrument_id}:H1"),
        "quote": 0,
    }
    assert body1["versions"]["candles"] == 1

    # запись между двумя последовательными запросами видна по смене версии
    db.set_quote_if_fresh(instrument_id, 100.7, T0 + 1, source="t", event_at=T0 + 1)
    db.insert_candles([make_candle(
        T0 + H1_MS, 100.5, 101, 99.5, 100.7,
        timeframe="H1", instrument_id=instrument_id)])
    body2 = client.get(url + "&with_version=true", headers=AUTH).json()
    assert body2["versions"]["candles"] == body1["versions"]["candles"] + 1
    assert body2["versions"]["quote"] == 1
    assert len(body2["candles"]) == 2


def test_zones_and_grouped_versions(
    db: Database, client: TestClient, instrument_id: int
):
    zid = _zone(db, instrument_id)
    # прежний контракт — голый список
    plain = client.get(f"/api/zones?instrument_id={instrument_id}",
                       headers=AUTH).json()
    assert isinstance(plain, list) and [z["id"] for z in plain] == [zid]
    body = client.get(
        f"/api/zones?instrument_id={instrument_id}&with_version=true",
        headers=AUTH).json()
    assert [z["id"] for z in body["zones"]] == [zid]
    assert body["versions"] == {"zones": db.get_domain("seq:zones")}
    # смена статуса двигает версию следующего снимка
    db.update_zone(zid, status=ZoneStatus.WEAKENED)
    body2 = client.get(
        f"/api/zones?instrument_id={instrument_id}&with_version=true",
        headers=AUTH).json()
    assert body2["versions"]["zones"] == body["versions"]["zones"] + 1

    grouped = client.get(f"/api/zones/grouped?instrument_id={instrument_id}",
                         headers=AUTH).json()
    assert grouped["versions"]["zones"] == db.get_domain("seq:zones")
    assert "groups" in grouped  # прежние поля на месте


def test_events_versions_opt_in(
    db: Database, client: TestClient, instrument_id: int
):
    zid = _zone(db, instrument_id)
    event_id = db.insert_event(Event(
        id=None, zone_id=zid, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=T0, detected_at=T0, price=100.0, depth=0.0))
    plain = client.get("/api/events?limit=5", headers=AUTH).json()
    assert isinstance(plain, list) and [e["id"] for e in plain] == [event_id]
    body = client.get("/api/events?limit=5&with_version=true",
                      headers=AUTH).json()
    assert [e["id"] for e in body["events"]] == [event_id]
    assert body["versions"]["zones"] == db.get_domain("seq:zones")
    assert "history" in body["versions"]


def test_ltf_read_models_versions(
    db: Database, client: TestClient, instrument_id: int
):
    zid = _zone(db, instrument_id)
    obs = _observation(db, instrument_id, zid)
    db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    listing = client.get("/api/ltf/instruments", headers=AUTH).json()
    assert listing["state_version"] == db.get_state_seq()  # совместимость
    assert listing["versions"]["zones"] == db.get_domain("seq:zones")
    assert listing["versions"]["scenario"] == {
        str(instrument_id): db.get_domain(f"seq:scenario:{instrument_id}")
    }
    assert "quote" in listing["versions"]

    current = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                         headers=AUTH).json()
    assert current["state_version"] == db.get_state_seq()
    assert current["versions"] == {
        "scenario": db.get_domain(f"seq:scenario:{instrument_id}"),
        "zones": db.get_domain("seq:zones"),
        "quote": db.get_domain(f"seq:quote:{instrument_id}"),
    }


def test_data_sync_status_versions_and_readiness(
    db: Database, settings: Settings, client: TestClient, instrument_id: int
):
    # без координатора — версии доступны, readiness пуст
    body = client.get("/api/data-sync/status", headers=AUTH).json()
    assert body["available"] is False
    assert "versions" in body and body["readiness"] is None

    worker = SimpleNamespace(
        db=db, scan_tfs=("H1",), _stop=asyncio.Event(),
        settings=settings, cfg=settings.detector, readiness=Readiness(),
        _restoring_instruments=set(),
    )
    sync = DataSync(worker)
    sync.ready = True
    client.app.state.data_sync = sync
    try:
        db.set_quote_if_fresh(instrument_id, 100.0, T0, source="t", event_at=T0)
        body = client.get("/api/data-sync/status", headers=AUTH).json()
        assert body["available"] is True
        assert body["versions"]["quote"] == {str(instrument_id): 1}
        assert body["state_version"] == db.get_state_seq()  # совместимость
        readiness = body["readiness"]
        assert readiness["ready"] is False and readiness["phase"] == "starting"
        worker.readiness.reach("realtime")
        body = client.get("/api/data-sync/status", headers=AUTH).json()
        assert body["readiness"]["ready"] is True
        assert body["readiness"]["phase"] == "realtime"
    finally:
        client.app.state.data_sync = None
        sync._write_executor.shutdown(wait=True)


def test_diagnostics_pipeline_section(
    db: Database, client: TestClient, instrument_id: int
):
    db.insert_candles([make_candle(
        T0, 100, 101, 99, 100.5, timeframe="H1", instrument_id=instrument_id)])
    db.set_meta(f"data_sync:processed:{instrument_id}:H1", str(T0))
    db.set_quote_if_fresh(instrument_id, 100.0, T0, source="t", event_at=T0)
    resp = client.get("/api/diagnostics", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    # прежние секции на месте
    for section in ("market_queue", "calc_queue", "readiness", "backfill",
                    "state_version"):
        assert section in body
    pipeline = body["pipeline"]
    channel = next(
        c for c in pipeline["channels"]
        if c["instrument_id"] == instrument_id and c["timeframe"] == "H1"
    )
    assert channel["received"] == T0 + H1_MS - 1
    assert channel["processed"] == T0
    assert channel["calc_pending_wait_s"] is None
    quote = next(
        q for q in pipeline["quotes"] if q["instrument_id"] == instrument_id)
    assert quote["quote_at"] == T0
    assert quote["quote_age_ms"] is not None and quote["quote_age_ms"] >= 0
    # transport отсутствует, пока realtime выключен; секретов нет
    assert "transport" not in pipeline
    assert "token" not in resp.text.lower()


# ---------------------------------------------------------------------------
# WS-конверт: epoch/seq/domain_versions
# ---------------------------------------------------------------------------

def test_ws_envelope_epoch_seq_domains(
    db: Database, client: TestClient, instrument_id: int
):
    hub = client.app.state.ws_hub
    with client.websocket_connect("/ws", headers=AUTH) as ws:
        hub.broadcast({"type": "zone", "zone_id": 1})
        first = ws.receive_json()
        hub.broadcast({"type": "price", "instrument_id": instrument_id,
                       "price": 1.0})
        second = ws.receive_json()
    # прежнее поле state_seq на месте (совместимость)
    assert first["state_seq"] == db.get_state_seq()
    assert first["epoch"] == hub.epoch
    assert second["epoch"] == hub.epoch
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["domain_versions"] == {"zones": db.get_domain("seq:zones")}
    assert second["domain_versions"] == {
        "quote": db.get_domain(f"seq:quote:{instrument_id}")
    }
    # второе соединение — независимый seq с единицы
    with client.websocket_connect("/ws", headers=AUTH) as other:
        hub.broadcast({"type": "zone", "zone_id": 2})
        msg = other.receive_json()
    assert msg["seq"] == 1


def test_ws_domain_versions_stamped_after_bump(
    db: Database, client: TestClient, instrument_id: int
):
    """Порядок bump → broadcast: версия в конверте включает изменение,
    refetch клиента не читает состояние до bump (эндпоинт мутирует зону
    внутри unit of work — рассылка откладывается на post-commit)."""
    zid = _zone(db, instrument_id)
    before = db.get_domain("seq:zones")
    with client.websocket_connect("/ws", headers=AUTH) as ws:
        resp = client.patch(f"/api/zones/{zid}", headers=AUTH,
                            json={"name": "A"})
        assert resp.status_code == 200
        msg = ws.receive_json()
    assert msg["type"] == "zone"
    bumped = db.get_domain("seq:zones")
    assert bumped > before
    assert msg["domain_versions"]["zones"] == bumped
