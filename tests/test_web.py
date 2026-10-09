"""Тесты веб-слоя HTF Zones: авторизация, ручные зоны, версионирование
границ, review кандидатов, визуальное объединение, health (§10, §11)."""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import (
    Candle,
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.web.api import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    # settings.json должен писаться во временный каталог, а не в проект
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    app = create_app(db, settings)
    return TestClient(app)


@pytest.fixture()
def seeded(db, client):
    """Пара инструментов, свечи, активные/кандидат-зоны, событие."""
    ins1 = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    ins2 = db.upsert_instrument(Instrument(
        id=None, asset="ETH", venue="binance", market_type="spot",
        symbol="ETHUSDT", quote_asset="USDT",
    ))
    # свечи D1 для health и /api/candles (сканируются только HTF, §1)
    base = now_ms() - 10 * 86400_000
    candles = [
        Candle(instrument_id=ins1, timeframe="D1",
               open_time=base + i * 86400_000, close_time=base + (i + 1) * 86400_000 - 1,
               open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, source="test")
        for i in range(10)
    ]
    db.insert_candles(candles)

    def zone(ztype, lower, upper, status, tf="D1", formed=base, source="auto"):
        zid = db.insert_zone(Zone(
            id=None, instrument_id=ins1, type=ztype, direction=Direction.BULL,
            timeframe=tf, lower=lower, upper=upper, formed_at=formed,
            confirmed_at=formed, status=status, source=source, created_at=formed,
            evidence={"reason": "test"},
        ))
        return zid

    # пересекающаяся пара ACTIVE + отдельная ACTIVE + кандидат
    z_a = zone(ZoneType.FVG, 100.0, 110.0, ZoneStatus.ACTIVE)
    z_b = zone(ZoneType.OB, 105.0, 115.0, ZoneStatus.ACTIVE, formed=base + 1)
    z_c = zone(ZoneType.SSL, 200.0, 200.0, ZoneStatus.ACTIVE, formed=base + 2)
    # кандидат PRB ниже текущих цен: исторически нетронутый и непробитый
    z_cand = zone(ZoneType.PRB, 50.0, 55.0, ZoneStatus.CANDIDATE, formed=base + 3)
    now = now_ms()
    db.insert_event(Event(
        id=None, zone_id=z_a, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=now, detected_at=now, price=110.0,
    ))
    return {"ins1": ins1, "ins2": ins2, "z_a": z_a, "z_b": z_b,
            "z_c": z_c, "z_cand": z_cand, "base": base}


# ---------------------------------------------------------------------------
# Авторизация (§11 п.8)
# ---------------------------------------------------------------------------

def test_auth_required(client):
    assert client.get("/api/instruments").status_code == 401
    assert client.get("/api/instruments", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_auth_ok_bearer_and_query(client):
    assert client.get("/api/instruments", headers=AUTH).status_code == 200
    assert client.get(f"/api/instruments?token={TOKEN}").status_code == 200


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------

def test_instruments_crud(seeded, client):
    resp = client.get("/api/instruments", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = client.post("/api/instruments", headers=AUTH, json={
        "asset": "SOL", "venue": "binance", "symbol": "SOLUSDT",
    })
    assert resp.status_code == 201
    assert resp.json()["enabled"] is True

    ins_id = resp.json()["id"]
    resp = client.post(f"/api/instruments/{ins_id}/toggle", headers=AUTH)
    assert resp.json()["enabled"] is False
    resp = client.post(f"/api/instruments/{ins_id}/toggle", headers=AUTH)
    assert resp.json()["enabled"] is True


def test_instrument_active_stops_procedures_and_overview(seeded, client):
    """Одна команда гасит опрос и расчёт. Обзор перестаёт считать актив."""
    ins_id = seeded["ins1"]
    other = seeded["ins2"]
    resp = client.post(
        f"/api/instruments/{ins_id}/active", headers=AUTH, json={"active": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["ltf_analyze"] is False

    rows = client.get("/api/ltf/instruments", headers=AUTH).json()["instruments"]
    ids = [row["instrument"]["id"] for row in rows]
    assert ins_id not in ids
    assert other in ids

    listed = client.get("/api/instruments", headers=AUTH).json()
    saved = next(i for i in listed if i["id"] == ins_id)
    assert saved["enabled"] is False and saved["ltf_analyze"] is False

    back = client.post(
        f"/api/instruments/{ins_id}/active", headers=AUTH, json={"active": True},
    )
    assert back.json()["enabled"] is True
    assert back.json()["ltf_analyze"] is True
    rows = client.get("/api/ltf/instruments", headers=AUTH).json()["instruments"]
    assert ins_id in [row["instrument"]["id"] for row in rows]

    missing = client.post(
        "/api/instruments/999999/active", headers=AUTH, json={"active": False},
    )
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# Ручные зоны (§10)
# ---------------------------------------------------------------------------

def test_manual_zone_create(seeded, client):
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins1"], "direction": "bull",
        "lower": 95.0, "upper": 98.0, "timeframe": "D1",
        "name": "Моя зона", "comment": "тест",
    })
    assert resp.status_code == 201
    z = resp.json()
    assert z["type"] == "manual"
    assert z["source"] == "manual"
    assert z["status"] == "active"
    assert z["name"] == "Моя зона"
    assert z["mid"] == pytest.approx(96.5)


def test_manual_zone_level(seeded, client):
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins1"], "level": 123.45, "timeframe": "H4",
    })
    assert resp.status_code == 201
    z = resp.json()
    assert z["lower"] == z["upper"] == 123.45
    assert z["is_level"] is True


def test_patch_boundaries_versioning_and_history(seeded, client, db):
    """Правка границ: версия растёт, старые границы в evidence.history,
    история событий не сбрасывается (§10, §13.15)."""
    zid = seeded["z_a"]
    before = client.get(f"/api/zones/{zid}", headers=AUTH).json()
    assert len(before["events"]) == 1  # TOUCH был до правки

    resp = client.patch(f"/api/zones/{zid}", headers=AUTH, json={
        "lower": 101.0, "upper": 112.0, "name": "уточнённая",
    })
    assert resp.status_code == 200
    z = resp.json()
    assert z["lower"] == 101.0 and z["upper"] == 112.0
    assert z["boundary_version"] == 2
    assert z["name"] == "уточнённая"
    history = z["evidence"]["history"]
    assert history[-1]["lower"] == 100.0 and history[-1]["upper"] == 110.0

    after = client.get(f"/api/zones/{zid}", headers=AUTH).json()
    assert len(after["events"]) == 1  # событие TOUCH на месте
    assert after["reviews"][-1]["decision"] == "corrected"
    assert after["reviews"][-1]["boundary_version"] == 2

    # в БД событие тоже не тронуто
    assert len(db.get_events(zone_id=zid)) == 1


# ---------------------------------------------------------------------------
# Review кандидатов (§10)
# ---------------------------------------------------------------------------

def test_review_confirmed(seeded, client):
    zid = seeded["z_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "confirmed", "text": "проверено",
    })
    assert resp.status_code == 200
    assert resp.json()["zone"]["status"] == "active"
    detail = client.get(f"/api/zones/{zid}", headers=AUTH).json()
    kinds = [e["kind"] for e in detail["events"]]
    assert "zone_confirmed_by_user" in kinds
    assert detail["reviews"][-1]["decision"] == "confirmed"


def test_review_rejected(seeded, client):
    zid = seeded["z_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "rejected",
    })
    assert resp.json()["zone"]["status"] == "rejected"


def test_review_corrected(seeded, client):
    zid = seeded["z_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "corrected", "lower": 49.0, "upper": 56.0, "text": "сдвиг",
    })
    z = resp.json()["zone"]
    assert z["lower"] == 49.0 and z["upper"] == 56.0
    assert z["evidence"]["history"][-1]["lower"] == 50.0
    assert resp.json()["reviews"][-1]["decision"] == "corrected"


def test_candidates_list(seeded, client):
    resp = client.get("/api/candidates", headers=AUTH)
    assert resp.status_code == 200
    cands = resp.json()
    assert len(cands) == 1
    assert cands[0]["id"] == seeded["z_cand"]
    assert cands[0]["explanation"]["reason"] == "test"
    assert cands[0]["instrument"]["symbol"] == "BTCUSDT"


def test_reviewed_candidate_leaves_queue(seeded, client, db):
    """Решения вроде now_irrelevant статуса не меняют (§15.1.1), но проверенный
    кандидат не должен возвращаться в очередь /api/candidates и в счётчик
    «Требует проверки»."""
    zid = seeded["z_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "now_irrelevant",
    })
    assert resp.status_code == 200
    assert resp.json()["zone"]["status"] == "candidate"  # статус не тронут
    assert client.get("/api/candidates", headers=AUTH).json() == []
    assert db.count_candidate_zones() == {}


def test_zone_dict_display_fields(seeded, client, db):
    """§15.1.3/§15.1.7: zone_to_dict несёт display_from/display_until/
    end_reason и breaker-флаги; без display_from — fallback на formed_at."""
    resp = client.get(f"/api/zones?instrument_id={seeded['ins1']}", headers=AUTH)
    assert resp.status_code == 200
    z = next(x for x in resp.json() if x["id"] == seeded["z_a"])
    assert z["display_from"] == seeded["base"]
    assert z["display_until"] is None
    assert z["end_reason"] is None
    assert z["breaker_pending"] is False
    assert z["breaker_forbidden"] is False

    # завершённая зона: display_until/end_reason — типизированные колонки
    db.update_zone(
        seeded["z_a"],
        display_from=seeded["base"] + 1000,
        display_until=seeded["base"] + 2000,
        end_reason="worked_90 (§6)",
    )
    resp = client.get(f"/api/zones?instrument_id={seeded['ins1']}", headers=AUTH)
    z = next(x for x in resp.json() if x["id"] == seeded["z_a"])
    assert z["display_from"] == seeded["base"] + 1000
    assert z["display_until"] == seeded["base"] + 2000
    assert z["end_reason"] == "worked_90 (§6)"


# ---------------------------------------------------------------------------
# Визуальное объединение (§10)
# ---------------------------------------------------------------------------

def test_grouped_does_not_modify_zones(seeded, client, db):
    resp = client.get(f"/api/zones/grouped?instrument_id={seeded['ins1']}", headers=AUTH)
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    sizes = sorted(len(g["zones"]) for g in groups)
    # §10: сливаются только ОДНОТИПНЫЕ зоны — разнотипные A(FVG)+B(OB)
    # пересекаются, но не группируются
    assert sizes == [1, 1, 1]

    # однотипная пересекающаяся пара сливается; исходные зоны не пересчитаны
    base = seeded["base"]
    z_d = db.insert_zone(Zone(
        id=None, instrument_id=seeded["ins1"], type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=108.0, upper=120.0,
        formed_at=base + 4, confirmed_at=base + 4, status=ZoneStatus.ACTIVE,
        source="auto", created_at=base + 4, evidence={"reason": "test"},
    ))
    resp = client.get(f"/api/zones/grouped?instrument_id={seeded['ins1']}", headers=AUTH)
    groups = resp.json()["groups"]
    big = next(g for g in groups if len(g["zones"]) == 2)
    assert set(big["zone_ids"]) == {seeded["z_a"], z_d}
    assert big["lower"] == 100.0 and big["upper"] == 120.0

    a = db.get_zone(seeded["z_a"])
    d = db.get_zone(z_d)
    assert (a.lower, a.upper) == (100.0, 110.0)
    assert (d.lower, d.upper) == (108.0, 120.0)


# ---------------------------------------------------------------------------
# Свечи / события / health
# ---------------------------------------------------------------------------

def test_candles_lightweight_format(seeded, client):
    resp = client.get(
        f"/api/candles?instrument_id={seeded['ins1']}&timeframe=D1&limit=5", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 5
    c = data[0]
    assert set(c) == {"time", "open", "high", "low", "close", "closed"}
    assert c["time"] == seeded["base"] // 1000 + 5 * 86400  # последние 5 свечей


def test_candles_include_forming_bar(seeded, client, db):
    """График получает текущую незакрытую свечу; детектор её не видит."""
    ins1 = seeded["ins1"]
    open_time = seeded["base"] + 10 * 86400_000
    db.insert_candles([Candle(
        instrument_id=ins1, timeframe="D1",
        open_time=open_time, close_time=open_time + 86400_000 - 1,
        open=110, high=111, low=109, close=110.5, closed=False, source="test",
    )])
    data = client.get(
        f"/api/candles?instrument_id={ins1}&timeframe=D1", headers=AUTH).json()
    assert data[-1]["time"] == open_time // 1000
    assert data[-1]["close"] == 110.5
    assert db.last_candle(ins1, "D1").closed is True
    forming = db.last_candle(ins1, "D1", closed_only=False)
    assert forming is not None and forming.closed is False


def test_events_with_zone_and_instrument(seeded, client):
    resp = client.get("/api/events?limit=10", headers=AUTH)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["kind"] == "touch"
    assert events[0]["zone"]["type"] == "fvg"
    assert events[0]["instrument"]["symbol"] == "BTCUSDT"


def test_health(seeded, client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    h = resp.json()
    assert h["time"] > 0
    assert h["version"]
    assert h["active_zones"] == 3
    fresh = [f for f in h["candle_freshness"] if f["symbol"] == "BTCUSDT"]
    assert fresh and fresh[0]["timeframe"] == "D1"
    assert fresh[0]["stale"] is False


# ---------------------------------------------------------------------------
# Настройки (§10)
# ---------------------------------------------------------------------------

def test_settings_roundtrip(seeded, client, settings, tmp_path):
    resp = client.get("/api/settings", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "approach_pct" in data["detector"]
    assert "uncalibrated_cluster_denominator" in data["uncalibrated"]
    # секреты не отдаются (§11 п.8)
    assert "telegram_token" not in json.dumps(data)
    assert "auth_token" not in json.dumps(data)
    assert isinstance(data["telegram_configured"], bool)

    resp = client.post("/api/settings", headers=AUTH, json={
        "approach_pct": 0.03, "notify_only_reviewed": True,
    })
    assert resp.status_code == 200
    assert set(resp.json()["applied"]) == {"approach_pct", "notify_only_reviewed"}
    assert settings.detector.approach_pct == pytest.approx(0.03)
    assert settings.detector.notify_only_reviewed is True
    # сохранено в файл
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["detector"]["approach_pct"] == pytest.approx(0.03)


def test_settings_strict_schema(seeded, client, settings, tmp_path):
    """L04: неизвестные/устаревшие поля и невалидные значения отклоняются
    422 с ошибками по полям; память и файл не меняются (атомарность)."""
    before = asdict(settings.detector)
    resp = client.post("/api/settings", headers=AUTH, json={
        "approach_pct": 0.05,          # валидное, но применено не будет
        "unknown_field": 1,            # неизвестное — отклонить
        "pivot_left": -5,              # вне диапазона
        "ltf_range_right": 4,          # устаревшее — не редактируется
        "ltf_entry_types": "FVG,XXX",  # недопустимое перечисление
        "depth_mid": 0.95,             # ломает зависимость depth_mid < depth_worked
    })
    assert resp.status_code == 422
    fields_err = resp.json()["detail"]["fields"]
    assert "unknown_field" in fields_err
    assert "pivot_left" in fields_err
    assert "ltf_range_right" in fields_err
    assert "ltf_entry_types" in fields_err
    # ничего не применилось: ни память, ни файл
    assert asdict(settings.detector) == before
    assert not (tmp_path / "settings.json").exists()
    # зависимость порогов — на итоговом конфиге чистого патча
    resp = client.post("/api/settings", headers=AUTH,
                       json={"depth_mid": 0.95})
    assert resp.status_code == 422
    assert "depth_mid" in resp.json()["detail"]["fields"]
    assert asdict(settings.detector) == before
    assert not (tmp_path / "settings.json").exists()
    # GET отдаёт группы и устаревшие поля
    data = client.get("/api/settings", headers=AUTH).json()
    assert data["groups"]["suppress_hours"] == "delivery"
    assert data["groups"]["approach_pct"] == "analysis"
    assert data["groups"]["ltf_range_right"] == "deprecated"
    assert data["groups"]["ltf_provisional_range_enabled"] == "experimental"
    assert data["deprecated"] == ["ltf_range_right"]
    # валидные CSV-перечисления проходят (в т.ч. lowercase ltf_notify_kinds)
    resp = client.post("/api/settings", headers=AUTH, json={
        "ltf_notify_kinds": "bos_sms,touch",
        "ltf_entry_types": "FVG,OB",
    })
    assert resp.status_code == 200
    assert settings.detector.ltf_notify_kinds == "bos_sms,touch"
    assert settings.detector.ltf_entry_types == "FVG,OB"


def test_settings_int_rejects_fractional(seeded, client, settings):
    """A12: дробный float для int-поля отклоняется, а не усекается молча
    (3.9 → 3); целый 3.0 допустим и приводится к int."""
    before = asdict(settings.detector)
    resp = client.post("/api/settings", headers=AUTH,
                       json={"ltf_structure_left": 3.9})
    assert resp.status_code == 422
    assert "ltf_structure_left" in resp.json()["detail"]["fields"]
    assert asdict(settings.detector) == before
    resp = client.post("/api/settings", headers=AUTH,
                       json={"ltf_structure_left": 3.0})
    assert resp.status_code == 200
    assert settings.detector.ltf_structure_left == 3
    assert isinstance(settings.detector.ltf_structure_left, int)


def test_settings_save_failure_keeps_memory(
    seeded, client, settings, tmp_path, monkeypatch,
):
    """A05: сбой записи файла — 500, память и прежний файл не тронуты."""
    before = asdict(settings.detector)

    def boom(*args, **kwargs):
        raise OSError("диск переполнен")

    monkeypatch.setattr("os.replace", boom)
    resp = client.post("/api/settings", headers=AUTH,
                       json={"approach_pct": 0.04})
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "settings_save_failed"
    assert asdict(settings.detector) == before
    assert not (tmp_path / "settings.json").exists()


def test_settings_recalc_interrupted_by_restart(db, settings):
    """A05: задание пересчёта в статусе running при старте — процесс умер
    посреди пересчёта; помечается failed/interrupted_by_restart."""
    db.set_meta("settings:recalc", json.dumps({
        "status": "running", "started_at": 123, "finished_at": None,
        "error": None, "result": None,
    }))
    create_app(db, settings)
    recalc = json.loads(db.get_meta("settings:recalc"))
    assert recalc["status"] == "failed"
    assert recalc["error"] == "interrupted_by_restart"
    assert recalc["started_at"] == 123
    assert recalc["finished_at"] is not None


def test_labels_endpoint(client):
    """/api/labels отдаёт формулировки из единого источника (app/texts_ru.py)."""
    resp = client.get("/api/labels", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_kinds"]["touch"] == "первое касание"
    assert data["event_kinds"]["data_recovered"]
    assert data["statuses"]["active"] == "активна"
    assert data["types"]["ob"] == "Orderblock"
    assert data["directions"] == {"bull": "бычий", "bear": "медвежий"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def test_ws_auth_rejected(db, settings):
    from starlette.websockets import WebSocketDisconnect

    app = create_app(db, settings)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?token=wrong"):
                pass


def test_ws_connect_and_broadcast(db, settings):
    """broadcast() из hub рассылает JSON подключённым клиентам (§11 п.6)."""
    app = create_app(db, settings)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            app.state.ws_hub.broadcast({"type": "price", "price": 1.0})
            data = ws.receive_json()
            assert data["type"] == "price"
            assert data["price"] == 1.0
        assert not app.state.ws_hub.connections  # отключение очищает hub
