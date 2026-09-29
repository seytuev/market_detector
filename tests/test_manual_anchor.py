"""ТЗ «Единый движок HTF/LTF» (22.09.2026): §7 — якорь ручной зоны на
графике (приёмка J), §10 — исправление экспорта labels: lifecycle-замечания
не портят вердикт геометрии (приёмка M), слой нормализации старого паттерна.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import (
    Direction,
    Instrument,
    ReviewAssessment,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.web.api import create_app, export_label

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    # labels.jsonl пишется рядом с БД — во временный каталог, а не в проект
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


@pytest.fixture()
def seeded(db, client):
    """Инструмент и живой OB-кандидат для ревью."""
    ins = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    base = now_ms() - 10 * 86_400_000
    ob = db.insert_zone(Zone(
        id=None, instrument_id=ins, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=100.0, upper=110.0, formed_at=base,
        confirmed_at=base, status=ZoneStatus.CANDIDATE, created_at=base,
        evidence={"reason": "test"},
    ))
    return {"ins": ins, "base": base, "ob": ob}


def read_labels(settings: Settings) -> list[dict]:
    path = Path(settings.db_path).parent / "labels.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------------------
# Приёмка J (§7): ручная зона с якорем на графике
# ---------------------------------------------------------------------------

def test_manual_zone_historical_anchor(seeded, client):
    """POST /api/zones/manual с историческим anchor_time: display_from =
    anchor_time, confirmed_at IS NULL, zone_type сохраняется, повторный GET
    возвращает те же значения."""
    anchor = seeded["base"]  # историческое начало, выбранное пользователем
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins"], "direction": "bull",
        "lower": 95.0, "upper": 98.0, "timeframe": "D1",
        "name": "историческая", "anchor_time": anchor, "zone_type": "ob",
    })
    assert resp.status_code == 201
    z = resp.json()
    assert z["source"] == "manual" and z["type"] == "manual"
    assert z["status"] == "active"
    assert z["anchor_time"] == anchor
    assert z["display_from"] == anchor
    assert z["confirmed_at"] is None  # ручное создание ≠ алгоритмическое подтверждение
    assert z["zone_type"] == "ob"
    assert z["created_at"] >= anchor

    # после «перезагрузки» (повторные GET) начало сохраняется — данные из БД
    zid = z["id"]
    again = client.get(f"/api/zones?instrument_id={seeded['ins']}", headers=AUTH).json()
    z2 = next(x for x in again if x["id"] == zid)
    assert z2["anchor_time"] == anchor
    assert z2["display_from"] == anchor
    assert z2["confirmed_at"] is None
    assert z2["zone_type"] == "ob"

    detail = client.get(f"/api/zones/{zid}", headers=AUTH).json()
    assert detail["zone"]["anchor_time"] == anchor
    assert detail["zone"]["zone_type"] == "ob"


def test_manual_zone_default_anchor_now(seeded, client):
    """Без anchor_time якорь — момент создания; zone_type по умолчанию None."""
    before = now_ms()
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins"], "lower": 95.0, "upper": 98.0,
    })
    assert resp.status_code == 201
    z = resp.json()
    assert before <= z["anchor_time"] <= now_ms()
    assert z["display_from"] == z["anchor_time"]
    assert z["confirmed_at"] is None
    assert z["zone_type"] is None


def test_manual_zone_level_zone_type_nullable(seeded, client):
    """Level-зона (L == U): zone_type допустимо null (ТЗ §7)."""
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins"], "level": 123.45,
        "anchor_time": seeded["base"],
    })
    assert resp.status_code == 201
    z = resp.json()
    assert z["is_level"] is True
    assert z["zone_type"] is None
    assert z["anchor_time"] == seeded["base"]


def test_manual_zone_bad_zone_type_rejected(seeded, client):
    resp = client.post("/api/zones/manual", headers=AUTH, json={
        "instrument_id": seeded["ins"], "lower": 95.0, "upper": 98.0,
        "zone_type": "breaker",
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Приёмка M (§10): lifecycle-замечание не портит вердикт геометрии
# ---------------------------------------------------------------------------

def test_lifecycle_comment_not_geometry_invalid(seeded, client, settings, db):
    """Ревью корректного OB с lifecycle-замечанием («не отработан»): зона НЕ
    отклоняется, geometry_verdict != invalid, выставлены lifecycle_verdict и
    reason_code; в labels.jsonl комментарий сохранён без изменений, запись
    содержит фактический rule_version зоны."""
    text = "Он не отработан полностью. Касание создало BSL внутри этого OB."
    resp = client.post(f"/api/zones/{seeded['ob']}/review", headers=AUTH,
                       json={"decision": "wrong_type", "text": text})
    assert resp.status_code == 200
    body = resp.json()
    assert body["zone"]["status"] != "rejected"
    a = body["assessment"]
    assert a["geometry_verdict"] != "invalid"
    assert a["lifecycle_verdict"] == "relevant"
    assert a["reason_code"] == "not_worked_out"
    assert a["requires_clarification"] is False

    rec = read_labels(settings)[-1]
    assert rec["geometry_verdict"] != "invalid"
    assert rec["lifecycle_verdict"] == "relevant"
    assert rec["reason_code"] == "not_worked_out"
    assert rec["comment"] == text  # исходный комментарий без изменений
    assert rec["rule_version"] == db.get_zone(seeded["ob"]).rule_version
    assert rec["rule_version"] == "0.2"  # DetectorConfig.rule_version
    # вердикты уже корректны в маппинге — слой нормализации не нужен
    assert "normalized" not in rec


def test_already_tested_comment(seeded, client):
    """«Уже тестирован» — тоже lifecycle-замечание: tested, а не invalid."""
    resp = client.post(f"/api/zones/{seeded['ob']}/review", headers=AUTH,
                       json={"decision": "wrong_type",
                             "text": "Но уже тестировался и был прошит"})
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] != "invalid"
    assert a["lifecycle_verdict"] == "tested"
    assert a["reason_code"] == "already_tested"
    assert resp.json()["zone"]["status"] != "rejected"


def test_wrong_type_without_lifecycle_comment_still_rejects(seeded, client):
    """Обычный wrong_type без lifecycle-комментария — по-прежнему invalid +
    REJECTED (старое поведение для реальных ошибок геометрии)."""
    resp = client.post(f"/api/zones/{seeded['ob']}/review", headers=AUTH,
                       json={"decision": "wrong_type", "text": "это не OB"})
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "invalid"
    assert a["lifecycle_verdict"] is None
    assert resp.json()["zone"]["status"] == "rejected"


# ---------------------------------------------------------------------------
# §10: слой нормализации старого ошибочного паттерна в export_label
# ---------------------------------------------------------------------------

def test_export_normalized_layer_for_legacy_pattern(seeded, db, settings):
    """Запись со старым слепком (wrong_type + geometry invalid + lifecycle-
    комментарий) получает поле normalized с исправленными вердиктами; сырые
    поля и комментарий не переписываются (R13)."""
    zone = db.get_zone(seeded["ob"])
    text = "Он не является отработанным, а является актуальным."
    legacy = ReviewAssessment(
        id=None, zone_id=zone.id, review_id=131, review_decision="wrong_type",
        geometry_verdict="invalid", lifecycle_verdict=None,
        reason_code="wrong_type", evidence_source="manual_ui",
        assessed_as_of=now_ms(), reviewed_at=now_ms(),
    )
    export_label(db, zone, "wrong_type", text, settings, assessment=legacy)
    rec = read_labels(settings)[-1]
    # сырые поля — как в старом паттерне
    assert rec["geometry_verdict"] == "invalid"
    assert rec["lifecycle_verdict"] is None
    assert rec["comment"] == text
    # нормализованный слой рядом
    n = rec["normalized"]
    assert n["geometry_verdict"] == "unknown"
    assert n["lifecycle_verdict"] == "relevant"
    assert n["reason_code"] == "not_worked_out"
    assert n["explanation"]


def test_export_no_normalized_for_clean_record(seeded, db, settings):
    """Обычная запись (нет старого паттерна) — без поля normalized."""
    zone = db.get_zone(seeded["ob"])
    ok = ReviewAssessment(
        id=None, zone_id=zone.id, review_id=1, review_decision="correct",
        geometry_verdict="valid", lifecycle_verdict=None,
        reason_code="correct", evidence_source="manual_ui",
        assessed_as_of=now_ms(), reviewed_at=now_ms(),
    )
    export_label(db, zone, "correct", "отлично", settings, assessment=ok)
    rec = read_labels(settings)[-1]
    assert "normalized" not in rec
    assert rec["rule_version"] == "0.2"
