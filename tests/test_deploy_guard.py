"""§7.6: внешнее размещение — dev-token guard и проверка Origin.

- запуск со стандартным dev-token на не-loopback хосте запрещён;
- изменяющие запросы с чужим Origin отклоняются 403;
- WebSocket с чужим Origin закрывается (4403);
- небраузерные клиенты без Origin работают как раньше.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import check_deploy_token
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
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


def test_dev_token_forbidden_on_external_host():
    s = Settings()
    s.auth_token = "dev-token"
    s.host = "0.0.0.0"
    with pytest.raises(SystemExit):
        check_deploy_token(s)


def test_dev_token_allowed_on_loopback():
    s = Settings()
    s.auth_token = "dev-token"
    for host in ("127.0.0.1", "localhost", "::1"):
        s.host = host
        check_deploy_token(s)  # не бросает


def test_own_token_allowed_on_external_host():
    s = Settings()
    s.auth_token = "real-secret-token"
    s.host = "0.0.0.0"
    check_deploy_token(s)  # не бросает


def test_mutating_request_foreign_origin_rejected(client):
    r = client.post(
        "/api/settings", headers={**AUTH, "Origin": "http://evil.example"},
        json={"approach_pct": 0.03},
    )
    assert r.status_code == 403


def test_mutating_request_same_origin_allowed(client):
    host = client.base_url.netloc.decode()
    r = client.post(
        "/api/settings", headers={**AUTH, "Origin": f"http://{host}"},
        json={"approach_pct": 0.03},
    )
    assert r.status_code == 200


def test_request_without_origin_allowed(client):
    r = client.post("/api/settings", headers=AUTH, json={"approach_pct": 0.03})
    assert r.status_code == 200


def test_get_with_foreign_origin_allowed(client):
    # Origin режет только изменяющие запросы; чтение не трогаем
    r = client.get("/api/settings",
                   headers={**AUTH, "Origin": "http://evil.example"})
    assert r.status_code == 200
