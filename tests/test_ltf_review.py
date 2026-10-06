"""Тесты разметки LTF Entry Zones: ltf_review / ltf_review_assessment,
экспорт ltf_labels.jsonl (v1), поля reviews/latest_assessment в таблице
entries. Оценка только фиксируется — зона и привязки не меняются."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfRange,
    LtfReview,
    LtfReviewAssessment,
    LtfScenario,
    LtfScenarioEntry,
)
from app.web.api import create_app

from .conftest import make_h1_candles

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
    # ltf_labels.jsonl пишется рядом с БД — во временный каталог, не в проект
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


@pytest.fixture()
def seeded(db, client, instrument_id):
    """Наблюдение active (D1 bear) со сценарием/диапазоном и двумя зонами:
    FVG (с исходными свечами в evidence) и OB. Плюс закрытые H1-свечи."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=z1, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, available_at=T0 + 60,
    ))
    # закрытые H1-свечи: open_time — опора assessed_as_of и OHLC экспорта
    candles = make_h1_candles(
        [(99.0, 103.0, 98.5, 102.5), (102.5, 103.0, 101.0, 101.5),
         (101.5, 102.0, 97.0, 97.5), (97.5, 99.0, 97.0, 98.8),
         (98.8, 100.5, 98.0, 100.2)],
        start_ms=T0,
        instrument_id=instrument_id,
    )
    db.insert_candles(candles)
    fvg_candles = [c.open_time for c in candles[1:4]]

    def entry_zone(type_, lower, upper, evidence=None):
        ez = db.insert_ltf_entry_zone(LtfEntryZone(
            id=None, instrument_id=instrument_id, type=type_,
            direction=Direction.BEAR, lower=lower, upper=upper,
            formed_at=T0 + 10, confirmed_at=T0 + 20,
            evidence=evidence or {},
        ))
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
            eligible=True, overlap="full", state="fresh",
            added_at=T0 + 60, updated_at=T0 + 60,
        ))
        return ez

    ez_fvg = entry_zone("FVG", 98.0, 102.0, evidence={"fvg_candles": fvg_candles})
    ez_ob = entry_zone("OB", 100.0, 101.0)
    return {"obs": obs, "sc": sc, "z1": z1, "ez_fvg": ez_fvg, "ez_ob": ez_ob,
            "candles": candles, "fvg_candles": fvg_candles,
            "instrument_id": instrument_id}


def read_ltf_labels(settings: Settings) -> list[dict]:
    path = Path(settings.db_path).parent / "ltf_labels.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------------------
# Репозиторий: ltf_review / ltf_review_assessment
# ---------------------------------------------------------------------------

def test_ltf_review_repo_roundtrip(db, seeded):
    ez = seeded["ez_fvg"]
    rid = db.add_ltf_review(LtfReview(
        id=None, entry_zone_id=ez.id, scenario_id=seeded["sc"].id,
        decision="correct", text="проверено", created_at=1000,
    ))
    aid = db.add_ltf_assessment(LtfReviewAssessment(
        id=None, entry_zone_id=ez.id, review_id=rid, review_decision="correct",
        geometry_verdict="valid", lifecycle_verdict=None, reason_code="correct",
        assessed_as_of=1000, reviewed_at=1000,
    ))
    reviews = db.get_ltf_reviews(ez.id)
    assert len(reviews) == 1 and reviews[0].id == rid
    assert reviews[0].scenario_id == seeded["sc"].id
    assert reviews[0].text == "проверено"
    got = db.get_ltf_assessments(ez.id)
    assert len(got) == 1 and got[0].id == aid
    assert got[0].geometry_verdict == "valid"
    assert got[0].requires_clarification is False
    assert got[0].corrected_lower is None and got[0].corrected_upper is None


# ---------------------------------------------------------------------------
# Решения ревью: вердикты фиксируются, зона не меняется
# ---------------------------------------------------------------------------

def test_correct_valid_zone_untouched(db, client, seeded):
    """correct → geometry=valid; validity, границы и привязка зоны не тронуты."""
    ez = seeded["ez_fvg"]
    before = db.get_ltf_entry_zone(ez.id)
    resp = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                       json={"decision": "correct", "text": "форма верна",
                             "scenario_id": seeded["sc"].id})
    assert resp.status_code == 200
    a = resp.json()["assessment"]
    assert a["review_decision"] == "correct"
    assert a["geometry_verdict"] == "valid"
    assert a["lifecycle_verdict"] is None      # касания не было
    # assessed_as_of — open_time последней закрытой H1-свечи
    assert a["assessed_as_of"] == seeded["candles"][-1].open_time
    after = db.get_ltf_entry_zone(ez.id)
    assert after.validity == before.validity == "fresh"
    assert (after.lower, after.upper) == (before.lower, before.upper)
    assert after.first_test_at == before.first_test_at
    entry = db.list_ltf_scenario_entries(seeded["sc"].id)[0]
    assert entry.state == "fresh"              # привязка не изменилась
    # несуществующая зона — 404
    assert client.post("/api/ltf/entry-zones/999/review", headers=AUTH,
                       json={"decision": "correct"}).status_code == 404


def test_wrong_invalid_zone_untouched(db, client, seeded):
    """wrong (и wrong_*): geometry=invalid, зона остаётся fresh с теми же
    границами — разметка не влияет на движок."""
    ez = seeded["ez_ob"]
    resp = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                       json={"decision": "wrong_type",
                             "reason_code": "wrong_fvg_base"})
    assert resp.status_code == 200
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "invalid"
    assert a["reason_code"] == "wrong_fvg_base"
    zone = db.get_ltf_entry_zone(ez.id)
    assert zone.validity == "fresh"
    assert (zone.lower, zone.upper) == (100.0, 101.0)


def test_no_context_requires_clarification(client, seeded):
    resp = client.post(f"/api/ltf/entry-zones/{seeded['ez_fvg'].id}/review",
                       headers=AUTH, json={"decision": "no_context"})
    a = resp.json()["assessment"]
    assert a["requires_clarification"] is True
    assert a["geometry_verdict"] == "unknown"


def test_fix_boundaries_requires_bounds(client, seeded):
    resp = client.post(f"/api/ltf/entry-zones/{seeded['ez_fvg'].id}/review",
                       headers=AUTH, json={"decision": "fix_boundaries"})
    assert resp.status_code == 422


def test_fix_boundaries_records_correction(db, client, settings, seeded):
    """fix_boundaries: corrected bounds в assessment и в jsonl, а границы
    самой зоны НЕ изменились (в отличие от HTF-ревью)."""
    ez = seeded["ez_fvg"]
    resp = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                       json={"decision": "fix_boundaries",
                             "lower": 97.5, "upper": 102.5,
                             "text": "включить тень"})
    assert resp.status_code == 200
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "needs_correction"
    assert a["corrected_lower"] == 97.5 and a["corrected_upper"] == 102.5
    zone = db.get_ltf_entry_zone(ez.id)
    assert (zone.lower, zone.upper) == (98.0, 102.0)  # зона не тронута
    rec = read_ltf_labels(settings)[0]
    assert rec["corrected_lower"] == 97.5 and rec["corrected_upper"] == 102.5
    assert rec["entry_zone"]["lower"] == 98.0          # снимок — исходный


# ---------------------------------------------------------------------------
# Обратная совместимость legacy-решений (как на HTF, R13)
# ---------------------------------------------------------------------------

def test_legacy_decisions_mapping(db, client, seeded):
    ez = seeded["ez_fvg"]
    r = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                    json={"decision": "confirmed"})
    assert r.json()["assessment"]["review_decision"] == "correct"
    r = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                    json={"decision": "rejected"})
    a = r.json()["assessment"]
    assert a["review_decision"] == "wrong"
    assert a["reason_code"] == "unknown"       # не выдумываем причину
    assert a["geometry_verdict"] == "invalid"
    r = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                    json={"decision": "corrected", "lower": 97.0, "upper": 103.0})
    assert r.json()["assessment"]["review_decision"] == "fix_boundaries"
    # в истории review решения сохранены как нажаты
    pressed = [rev.decision for rev in db.get_ltf_reviews(ez.id)]
    assert pressed == ["confirmed", "rejected", "corrected"]
    # неизвестное решение — 400
    assert client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                       json={"decision": "maybe"}).status_code == 400


# ---------------------------------------------------------------------------
# Жизненный цикл: tested по first_test_at
# ---------------------------------------------------------------------------

def test_lifecycle_tested_when_first_test_at(db, client, seeded):
    ez = seeded["ez_fvg"]
    db.update_ltf_entry_zone(ez.id, first_test_at=T0 + 70)
    resp = client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                       json={"decision": "correct"})
    assert resp.json()["assessment"]["lifecycle_verdict"] == "tested"
    # оценка не меняет validity/first_test_at
    zone = db.get_ltf_entry_zone(ez.id)
    assert zone.first_test_at == T0 + 70
    assert zone.validity == "fresh"


# ---------------------------------------------------------------------------
# Экспорт ltf_labels.jsonl (v1)
# ---------------------------------------------------------------------------

def test_export_label_file(db, client, settings, seeded):
    ez = seeded["ez_fvg"]
    client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                json={"decision": "correct", "text": "проверено",
                      "scenario_id": seeded["sc"].id})
    labels = read_ltf_labels(settings)
    assert len(labels) == 1
    rec = labels[0]
    assert rec["labels_version"] == 1
    assert rec["review_id"] is not None
    assert rec["reviewed_at"] > 0 and rec["assessed_as_of"] > 0
    assert rec["review_decision"] == "correct"
    assert rec["geometry_verdict"] == "valid"
    # полный снимок зоны и инструмент
    assert rec["entry_zone"]["id"] == ez.id
    assert rec["entry_zone"]["type"] == "FVG"
    assert rec["instrument"]["symbol"] == "BTCUSDT"
    # контекст: сценарий, наблюдение, родительская HTF-зона
    assert rec["scenario_id"] == seeded["sc"].id
    assert rec["scenario"]["id"] == seeded["sc"].id
    assert rec["observation"]["id"] == seeded["obs"].id
    assert rec["parent_zone"]["id"] == seeded["z1"]
    # все привязки зоны
    assert len(rec["scenario_entries"]) == 1
    assert rec["scenario_entries"][0]["entry_zone_id"] == ez.id
    # OHLC исходных свечей из evidence.fvg_candles
    assert [c["open_time"] for c in rec["source_candles_ohlc"]] == seeded["fvg_candles"]
    first = rec["source_candles_ohlc"][0]
    assert (first["open"], first["high"], first["low"], first["close"]) == \
        (102.5, 103.0, 101.0, 101.5)


# ---------------------------------------------------------------------------
# Миграция существующей БД
# ---------------------------------------------------------------------------

def test_migrate_existing_db_adds_ltf_review_tables(tmp_path):
    """БД старой схемы дополняется таблицами разметки LTF без потерь."""
    import sqlite3

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version VALUES (1)")
    raw.execute(
        """CREATE TABLE zone (
            id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id INTEGER NOT NULL,
            type TEXT NOT NULL, direction TEXT NOT NULL, timeframe TEXT NOT NULL,
            lower REAL NOT NULL, upper REAL NOT NULL, formed_at INTEGER NOT NULL,
            confirmed_at INTEGER, status TEXT NOT NULL,
            cycle_id INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'auto',
            rule_version TEXT NOT NULL DEFAULT '0.1',
            evidence TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL DEFAULT 0)"""
    )
    raw.execute(
        """INSERT INTO zone (instrument_id, type, direction, timeframe, lower, upper,
                             formed_at, status)
           VALUES (1, 'ob', 'bull', 'D1', 100.0, 110.0, 42, 'active')"""
    )
    # migrate() проверяет PRAGMA table_info(visit) — таблица должна быть
    raw.execute(
        """CREATE TABLE visit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zone_id INTEGER NOT NULL,
            cycle_id INTEGER NOT NULL, entered_at INTEGER NOT NULL,
            exited_at INTEGER, max_depth REAL NOT NULL DEFAULT 0,
            observed INTEGER NOT NULL DEFAULT 1)"""
    )
    raw.commit()
    raw.close()

    db = Database(str(path))
    tables = {
        r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "ltf_review" in tables
    assert "ltf_review_assessment" in tables
    # данные не потеряны
    row = db.conn.execute("SELECT * FROM zone").fetchone()
    assert row["lower"] == 100.0 and row["status"] == "active"
    # повторный запуск миграции идемпотентен
    db.close()
    db2 = Database(str(path))
    assert "ltf_review_assessment" in {
        r["name"] for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    db2.close()


# ---------------------------------------------------------------------------
# Таблица entries: reviews / latest_assessment
# ---------------------------------------------------------------------------

def test_entries_include_reviews_and_latest_assessment(client, seeded):
    sc = seeded["sc"]
    ez = seeded["ez_fvg"]
    client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                json={"decision": "correct", "text": "первая",
                      "scenario_id": sc.id})
    client.post(f"/api/ltf/entry-zones/{ez.id}/review", headers=AUTH,
                json={"decision": "no_context", "text": "вторая",
                      "scenario_id": sc.id})
    rows = client.get(f"/api/ltf/scenarios/{sc.id}/entries", headers=AUTH).json()["entries"]
    fvg = next(r for r in rows if r["entry_zone_id"] == ez.id)
    assert [r["decision"] for r in fvg["reviews"]] == ["correct", "no_context"]
    assert fvg["reviews"][0]["text"] == "первая"
    last = fvg["latest_assessment"]
    assert last["review_decision"] == "no_context"
    assert last["requires_clarification"] is True
    # зона без оценок: пустая история, latest_assessment = null
    ob = next(r for r in rows if r["entry_zone_id"] == seeded["ez_ob"].id)
    assert ob["reviews"] == []
    assert ob["latest_assessment"] is None
