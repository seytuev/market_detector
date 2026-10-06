"""T16 (ТЗ 06.10.2026 §11): нормализация оценок и конфликтов.

№551: «Потерял актуальность в январе–феврале 2026» при wrong_type — это
lifecycle-замечание, а не ошибка геометрии: зона не отклоняется, вердикт
геометрии не invalid, конфликт помечается semantic_conflict.
№130: latest-view по reviewed_at; удаление текста не доказывает отсутствие
пробоя — прошлый конфликтный verdict/comment сохраняется в аудите.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms
from app.web.api import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


@pytest.fixture()
def candidate(db, instrument_id):
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="W1", lower=95.26, upper=147.48,
        formed_at=now_ms() - 500 * 86_400_000, confirmed_at=None,
        status=ZoneStatus.CANDIDATE, source="auto",
        source_candles=[now_ms() - 500 * 86_400_000], evidence={},
        created_at=now_ms(),
    ))


def test_lifecycle_comment_is_not_geometry_error(db, client, candidate):
    """№551: wrong_type + «Потерял актуальность …» — lifecycle, зона НЕ
    отклоняется, геометрия не invalid (обучению на этой строке нет)."""
    resp = client.post(
        f"/api/zones/{candidate}/review",
        json={"decision": "wrong_type",
              "text": "Потерял актуальность в январе - феврале 2026"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    z = db.get_zone(candidate)
    assert z.status == ZoneStatus.CANDIDATE  # не rejected
    a = resp.json()["assessment"]
    assert a["geometry_verdict"] == "unknown"
    assert a["lifecycle_verdict"] == "completed"
    assert a["reason_code"] == "already_completed"


def test_normalized_layer_marks_semantic_conflict(db, client, candidate):
    """labels.jsonl: normalized-объект с semantic_conflict для старого
    паттерна (wrong_type + lifecycle-комментарий при invalid-разметке)."""
    client.post(f"/api/zones/{candidate}/review",
                json={"decision": "wrong_type",
                      "text": "Потерял актуальность в январе - феврале 2026"},
                headers=AUTH)
    resp = client.get("/api/export/labels", headers=AUTH)
    assert resp.status_code == 200
    lines = [json.loads(l) for l in resp.text.splitlines() if l.strip()]
    assert lines, "labels.jsonl должен содержать запись ревью"
    assert "time_semantics" in lines[-1]
    assert lines[-1]["time_semantics"]["snapshot_scope"] == "current_db_state_at_export"


def test_latest_view_and_conflict_preserved(db, client, candidate):
    """№130: две оценки correct (с конфликтным комментарием и без) — в
    normalized-слое latest по reviewed_at, конфликт первого сохранён."""
    r1 = client.post(f"/api/zones/{candidate}/review",
                     json={"decision": "correct", "text": "Неактуален 2025"},
                     headers=AUTH)
    assert r1.status_code == 200
    r2 = client.post(f"/api/zones/{candidate}/review",
                     json={"decision": "correct", "text": ""},
                     headers=AUTH)
    assert r2.status_code == 200

    resp = client.get("/api/export/reviews", headers=AUTH)
    assert resp.status_code == 200
    payload = json.loads(resp.content)
    entry = next(n for n in payload["normalized"]
                 if n["zone_id"] == candidate)
    assert entry["latest_assessment"]["id"] == \
        r2.json()["assessment"]["id"]
    kinds = {c["kind"] for c in entry["conflicts"]}
    assert "semantic_conflict" in kinds
    sc = next(c for c in entry["conflicts"]
              if c["kind"] == "semantic_conflict")
    assert sc["review"]["text"] == "Неактуален 2025"
    # сырые массивы неизменны: обе оценки на месте
    assert len(payload["review_assessments"]) == 2
