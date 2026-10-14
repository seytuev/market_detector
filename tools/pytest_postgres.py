"""Run existing behavioral tests on isolated PostgreSQL schemas.

HTF_TEST_POSTGRES_URL=... pytest -p tools.pytest_postgres tests/...
Use a disposable local database only. Each test's schemas are removed afterwards.
"""
import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql
import pytest

from app.db import Database


@pytest.fixture(autouse=True)
def postgres_repositories(monkeypatch):
    url = os.environ.get("HTF_TEST_POSTGRES_URL")
    if not url:
        pytest.fail("HTF_TEST_POSTGRES_URL is required for tools.pytest_postgres")
    original = Database.__init__
    schemas = {}
    instances = []
    def init(self, path=":memory:", **kwargs):
        if str(path).startswith(("postgres://", "postgresql://")):
            return original(self, path, **kwargs)
        key = uuid.uuid4().hex if path == ":memory:" else str(path)
        if key not in schemas:
            schema = "test_"+uuid.uuid4().hex
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            schemas[key] = schema
        parts = urlsplit(url)
        query = parse_qsl(parts.query)+[("options", "-csearch_path="+schemas[key])]
        target = urlunsplit(parts._replace(query=urlencode(query)))
        original(self, target, **kwargs)
        instances.append(self)
    monkeypatch.setattr(Database, "__init__", init)
    yield
    for db in instances:
        db.close()
    with psycopg.connect(url, autocommit=True) as conn:
        for schema in schemas.values():
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
