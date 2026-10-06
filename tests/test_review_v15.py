"""Тесты ревью §15.3 (R01/R13): раздельная оценка геометрии и актуальности,
review_assessment, boundary_correction, labels v2, display-поля зоны."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import (
    TIMEFRAME_MINUTES,
    BoundaryCorrection,
    Direction,
    ReviewAssessment,
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
    # labels.jsonl пишется рядом с БД — во временный каталог, а не в проект
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


@pytest.fixture()
def zones(db, client):
    """Инструмент и зоны: живой кандидат, завершённый кандидат (display_until),
    отработанная зона, кандидат с breaker_pending/breaker_forbidden."""
    from app.models import Instrument

    ins = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    base = now_ms() - 10 * 86_400_000

    def zone(ztype, lower, upper, status, formed, evidence=None):
        return db.insert_zone(Zone(
            id=None, instrument_id=ins, type=ztype, direction=Direction.BULL,
            timeframe="D1", lower=lower, upper=upper, formed_at=formed,
            confirmed_at=formed, status=status, created_at=formed,
            evidence=evidence or {},
        ))

    live_cand = zone(ZoneType.OB, 100.0, 110.0, ZoneStatus.CANDIDATE, base)
    # завершённый кандидат: рисунок закрыт, но статус ещё candidate
    done_cand = zone(ZoneType.FVG, 120.0, 130.0, ZoneStatus.CANDIDATE, base + 1)
    db.update_zone(done_cand, display_until=base + 5 * 86_400_000,
                   end_reason="fvg_filled (§3)", display_from=base + 86_400_000)
    worked = zone(ZoneType.OB, 140.0, 150.0, ZoneStatus.WORKED, base + 2)
    brk_cand = zone(ZoneType.OB, 160.0, 170.0, ZoneStatus.CANDIDATE, base + 3)
    db.update_zone(brk_cand, breakout_close_at=base + 3 * 86_400_000,
                   breaker_forbidden=True)
    return {"ins": ins, "base": base, "live_cand": live_cand,
            "done_cand": done_cand, "worked": worked, "brk_cand": brk_cand}


def read_labels(settings: Settings) -> list[dict]:
    path = Path(settings.db_path).parent / "labels.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------------------
# Репозиторий: review_assessment / boundary_correction (§12)
# ---------------------------------------------------------------------------

def test_assessment_repo_roundtrip(db, zones):
    from app.models import Review

    zid = zones["live_cand"]
    rid = db.add_review(Review(id=None, zone_id=zid, decision="correct",
                               created_at=1000))
    aid = db.add_assessment(ReviewAssessment(
        id=None, zone_id=zid, review_id=rid, review_decision="correct",
        geometry_verdict="valid", lifecycle_verdict=None, reason_code="correct",
        assessed_as_of=1000, reviewed_at=1000,
    ))
    got = db.get_assessments(zid)
    assert len(got) == 1 and got[0].id == aid
    assert got[0].geometry_verdict == "valid"
    assert got[0].requires_clarification is False


def test_boundary_correction_repo_roundtrip(db, zones):
    zid = zones["live_cand"]
    cid = db.add_boundary_correction(BoundaryCorrection(
        id=None, zone_id=zid, boundary_version=2,
        original_lower=100.0, original_upper=110.0,
        corrected_lower=98.5, corrected_upper=110.0,
        anchor_candle_open_time=zones["base"], reason="тень импульсной свечи",
        created_at=now_ms(),
    ))
    got = db.get_boundary_corrections(zid)
    assert len(got) == 1 and got[0].id == cid
    assert got[0].anchor_candle_open_time == zones["base"]
    assert got[0].corrected_lower == 98.5


# ---------------------------------------------------------------------------
# zone_to_dict: display-поля (§15.1.3, §15.1.7)
# ---------------------------------------------------------------------------

def test_zone_to_dict_display_fields(zones, client):
    resp = client.get(f"/api/zones?instrument_id={zones['ins']}", headers=AUTH)
    by_id = {z["id"]: z for z in resp.json()}

    live = by_id[zones["live_cand"]]
    assert live["display_from"] == zones["base"]  # fallback на formed_at
    assert live["display_until"] is None
    assert live["end_reason"] is None
    assert live["breaker_pending"] is False
    assert live["breaker_forbidden"] is False

    done = by_id[zones["done_cand"]]
    assert done["display_from"] == zones["base"] + 86_400_000  # средняя свеча
    assert done["display_until"] == zones["base"] + 5 * 86_400_000
    assert done["end_reason"] == "fvg_filled (§3)"

    brk = by_id[zones["brk_cand"]]
    assert brk["breaker_pending"] is True
    assert brk["breaker_forbidden"] is True


# ---------------------------------------------------------------------------
# Решения ревью §15.3
# ---------------------------------------------------------------------------

def test_correct_live_candidate_activates(zones, client, db):
    """correct на живом кандидате: ACTIVE + ZONE_CONFIRMED_BY_USER +
    assessment geometry_verdict=valid (R01)."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "correct", "text": "проверено"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["zone"]["status"] == "active"
    a = body["assessment"]
    assert a["review_decision"] == "correct"
    assert a["geometry_verdict"] == "valid"
    assert a["lifecycle_verdict"] is None
    kinds = [e.kind.value for e in db.get_events(zone_id=zid)]
    assert "zone_confirmed_by_user" in kinds


def test_correct_finished_zone_does_not_revive(zones, client, db):
    """§15.5: correct на завершённой зоне не воскрешает её —
    статус не меняется, lifecycle_verdict=completed, события нет."""
    zid = zones["done_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "correct"})
    body = resp.json()
    assert body["zone"]["status"] == "candidate"  # статус не тронут
    a = body["assessment"]
    assert a["geometry_verdict"] == "valid"
    assert a["lifecycle_verdict"] == "completed"
    assert db.get_events(zone_id=zid) == []  # ZONE_CONFIRMED_BY_USER не создано


def test_correct_worked_zone_keeps_status(zones, client):
    """Отработанная (worked) зона: correct сохраняет положительную геометрию,
    но не возвращает в активные (§15.1.1, §15.5)."""
    zid = zones["worked"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "correct"})
    assert resp.json()["zone"]["status"] == "worked"
    assert resp.json()["assessment"]["lifecycle_verdict"] == "completed"


def test_now_irrelevant_keeps_status(zones, client):
    """now_irrelevant: верная форма, но сейчас неактуально — статус не меняем."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "now_irrelevant", "text": "уже отработал"})
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "valid"
    assert a["lifecycle_verdict"] == "completed"
    assert resp.json()["zone"]["status"] == "candidate"


@pytest.mark.parametrize("decision", ["wrong_type", "wrong_base"])
def test_wrong_decisions_reject(zones, client, decision):
    """wrong_type/wrong_base: геометрия неверна → REJECTED."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": decision})
    assert resp.json()["zone"]["status"] == "rejected"
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "invalid"
    assert a["reason_code"] == decision


def test_no_context_requires_clarification(zones, client):
    """no_context: нет контекста — не ошибка геометрии, статус не меняется."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "no_context"})
    assert resp.json()["zone"]["status"] == "candidate"
    a = resp.json()["assessment"]
    assert a["requires_clarification"] is True
    assert a["geometry_verdict"] == "unknown"


def test_already_breaker(zones, client):
    """already_breaker: верная форма, цикл converted, статус не меняем."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "already_breaker"})
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "valid"
    assert a["lifecycle_verdict"] == "converted"
    assert resp.json()["zone"]["status"] == "candidate"


def test_fix_boundaries_writes_correction(zones, client, db):
    """fix_boundaries: needs_correction + boundary_correction с якорем +
    прежнее версионирование границ (§15.2)."""
    zid = zones["live_cand"]
    anchor = zones["base"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "fix_boundaries", "lower": 98.5, "upper": 111.0,
        "text": "включить тень", "anchor_candle_open_time": anchor,
    })
    body = resp.json()
    assert body["zone"]["lower"] == 98.5 and body["zone"]["upper"] == 111.0
    assert body["zone"]["boundary_version"] == 2
    a = body["assessment"]
    assert a["geometry_verdict"] == "needs_correction"
    c = body["boundary_correction"]
    assert c["original_lower"] == 100.0 and c["original_upper"] == 110.0
    assert c["corrected_lower"] == 98.5 and c["corrected_upper"] == 111.0
    assert c["anchor_candle_open_time"] == anchor
    # в БД запись тоже есть
    stored = db.get_boundary_corrections(zid)
    assert len(stored) == 1 and stored[0].boundary_version == 2


def test_fix_boundaries_requires_bounds(zones, client):
    resp = client.post(f"/api/zones/{zones['live_cand']}/review", headers=AUTH,
                       json={"decision": "fix_boundaries"})
    assert resp.status_code == 400


def test_unknown_decision_rejected(zones, client):
    resp = client.post(f"/api/zones/{zones['live_cand']}/review", headers=AUTH,
                       json={"decision": "maybe"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Обратная совместимость (R13)
# ---------------------------------------------------------------------------

def test_legacy_confirmed_maps_to_correct(zones, client):
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "confirmed"})
    assert resp.json()["zone"]["status"] == "active"
    assert resp.json()["assessment"]["review_decision"] == "correct"
    # в истории review сохраняется исходное нажатие
    assert resp.json()["reviews"][-1]["decision"] == "confirmed"


def test_legacy_rejected_reason_unknown(zones, client):
    """rejected без reason_code → 'unknown', а не выдуманная причина (R13)."""
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "rejected"})
    assert resp.json()["zone"]["status"] == "rejected"
    a = resp.json()["assessment"]
    assert a["review_decision"] == "wrong"
    assert a["reason_code"] == "unknown"
    assert a["geometry_verdict"] == "invalid"


def test_legacy_corrected_maps_to_fix_boundaries(zones, client):
    zid = zones["live_cand"]
    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "corrected", "lower": 99.0, "upper": 112.0})
    body = resp.json()
    assert body["assessment"]["review_decision"] == "fix_boundaries"
    assert body["boundary_correction"]["corrected_lower"] == 99.0
    assert body["reviews"][-1]["decision"] == "corrected"  # как нажато


# ---------------------------------------------------------------------------
# Детали зоны: assessments + boundary_corrections (§15.3)
# ---------------------------------------------------------------------------

def test_zone_detail_includes_assessments_and_corrections(zones, client):
    zid = zones["live_cand"]
    client.post(f"/api/zones/{zid}/review", headers=AUTH,
                json={"decision": "fix_boundaries", "lower": 98.5, "upper": 111.0})
    detail = client.get(f"/api/zones/{zid}", headers=AUTH).json()
    assert len(detail["assessments"]) == 1
    assert detail["assessments"][0]["geometry_verdict"] == "needs_correction"
    assert detail["assessments"][0]["review_id"] == detail["reviews"][-1]["id"]
    assert len(detail["boundary_corrections"]) == 1
    assert detail["boundary_corrections"][0]["original_lower"] == 100.0


# ---------------------------------------------------------------------------
# Экспорт labels v2 (§15.3)
# ---------------------------------------------------------------------------

def test_labels_v2_fields(zones, client, settings):
    zid = zones["done_cand"]  # завершённая зона: display-поля ненулевые
    client.post(f"/api/zones/{zid}/review", headers=AUTH,
                json={"decision": "correct", "text": "верно, но заполнен"})
    labels = read_labels(settings)
    assert len(labels) == 1
    rec = labels[0]
    # новые поля v2
    assert rec["labels_version"] == 2
    assert rec["review_id"] is not None
    assert rec["reviewed_at"] > 0 and rec["assessed_as_of"] > 0
    assert rec["review_decision"] == "correct"
    assert rec["geometry_verdict"] == "valid"
    assert rec["lifecycle_verdict"] == "completed"
    assert rec["reason_code"] == "correct"
    assert rec["requires_clarification"] is False
    assert rec["display_from"] == zones["base"] + 86_400_000
    assert rec["display_until"] == zones["base"] + 5 * 86_400_000
    assert rec["end_reason"] == "fvg_filled (§3)"
    # старые поля на месте
    assert rec["decision"] == "correct"
    assert rec["comment"] == "верно, но заполнен"
    assert rec["zone"]["id"] == zid
    assert rec["instrument"]["symbol"] == "BTCUSDT"


def test_labels_v2_fix_boundaries_bounds(zones, client, settings):
    """Для fix_boundaries в labels пишутся исходные и исправленные границы."""
    zid = zones["live_cand"]
    client.post(f"/api/zones/{zid}/review", headers=AUTH, json={
        "decision": "fix_boundaries", "lower": 98.5, "upper": 111.0,
        "anchor_candle_open_time": zones["base"],
    })
    rec = read_labels(settings)[0]
    assert rec["original_lower"] == 100.0 and rec["original_upper"] == 110.0
    assert rec["corrected_lower"] == 98.5 and rec["corrected_upper"] == 111.0
    assert rec["anchor_candle_open_time"] == zones["base"]
    assert rec["geometry_verdict"] == "needs_correction"


# ---------------------------------------------------------------------------
# R02/§15.1.2: исторический пересчёт актуальности при ревью
# ---------------------------------------------------------------------------

def test_review_recalculates_lifecycle_from_history(zones, client, db):
    """Approve кандидата, который по свечам давно прошёл зону насквозь,
    не активирует его: перед оценкой состояние воспроизводится из истории,
    assessed_as_of — закрытие последней свечи ТФ."""
    from .conftest import make_candle

    zid = zones["live_cand"]  # OB bull [100,110] D1, кандидат
    ins = db.get_zone(zid).instrument_id
    base = zones["base"]
    d1 = TIMEFRAME_MINUTES["D1"] * 60_000
    candles = [
        make_candle(base + d1, 120.0, 121.0, 111.0, 115.0, instrument_id=ins),
        make_candle(base + 2 * d1, 115.0, 116.0, 95.0, 96.0, instrument_id=ins),
        make_candle(base + 3 * d1, 96.0, 97.0, 90.0, 92.0, instrument_id=ins),
    ]
    db.insert_candles(candles)

    resp = client.post(f"/api/zones/{zid}/review", headers=AUTH,
                       json={"decision": "correct", "text": "форма верна"})
    assert resp.status_code == 200
    body = resp.json()
    # пересчёт увидел проход насквозь: зона завершена, approve не воскрешает (§15.5)
    assert body["zone"]["status"] != "active"
    assert body["assessment"]["lifecycle_verdict"] == "completed"
    assert body["assessment"]["geometry_verdict"] == "valid"
    assert body["assessment"]["assessed_as_of"] == candles[-1].close_time


# ---------------------------------------------------------------------------
# Миграция существующей БД (§12): без потерь данных
# ---------------------------------------------------------------------------

def test_migrate_existing_db_adds_tables(tmp_path):
    """БД старой схемы (без новых таблиц) дополняется миграцией,
    данные зон сохраняются."""
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
    raw.execute("CREATE TABLE review (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " zone_id INTEGER NOT NULL, decision TEXT NOT NULL)")
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
    assert "review_assessment" in tables
    assert "boundary_correction" in tables
    # данные не потеряны
    row = db.conn.execute("SELECT * FROM zone").fetchone()
    assert row["lower"] == 100.0 and row["status"] == "active"
    # повторный запуск миграции идемпотентен
    db.close()
    db2 = Database(str(path))
    assert "review_assessment" in {
        r["name"] for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    db2.close()


# ---------------------------------------------------------------------------
# Выгрузка разметки: GET /api/export/labels
# ---------------------------------------------------------------------------

def test_export_labels_requires_auth(client):
    assert client.get("/api/export/labels").status_code in (401, 403)


def test_export_labels_404_when_empty(client):
    resp = client.get("/api/export/labels", headers=AUTH)
    assert resp.status_code == 404


def test_export_labels_downloads_jsonl(zones, client, settings):
    zid = zones["live_cand"]
    client.post(f"/api/zones/{zid}/review", headers=AUTH,
                json={"decision": "correct", "text": "проверено"})
    resp = client.get("/api/export/labels", headers=AUTH)
    assert resp.status_code == 200
    assert "labels.jsonl" in resp.headers["content-disposition"]
    rows = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["zone"]["id"] == zid
    assert rows[0]["review_decision"] == "correct"
    # отдаётся ровно тот же append-only файл, что пишет export_label (R13)
    assert resp.content == (
        Path(settings.db_path).parent / "labels.jsonl"
    ).read_bytes()


# ---------------------------------------------------------------------------
# Полная выгрузка проверок: GET /api/export/reviews
# ---------------------------------------------------------------------------

def test_export_reviews_requires_auth(client):
    assert client.get("/api/export/reviews").status_code in (401, 403)


def test_export_reviews_empty_is_200(client):
    """Нет ни разметки, ни решений — валидный ответ с пустыми списками."""
    resp = client.get("/api/export/reviews", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "htf-review-export"
    assert body["labels"] == []
    assert body["reviews"] == []
    assert body["review_assessments"] == []
    assert body["boundary_corrections"] == []


def test_export_reviews_includes_all_sources(zones, client):
    """После ревью выгрузка содержит и разметку (labels), и записи БД."""
    zid = zones["live_cand"]
    client.post(f"/api/zones/{zid}/review", headers=AUTH,
                json={"decision": "correct", "text": "проверено"})
    resp = client.get("/api/export/reviews", headers=AUTH)
    assert resp.status_code == 200
    assert "reviews.json" in resp.headers["content-disposition"]
    body = resp.json()
    assert len(body["labels"]) == 1
    assert body["labels"][0]["zone"]["id"] == zid
    assert [r["zone_id"] for r in body["reviews"]] == [zid]
    assert body["reviews"][0]["decision"] == "correct"
    assert len(body["review_assessments"]) == 1
    assert body["review_assessments"][0]["review_decision"] == "correct"
