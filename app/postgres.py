"""PostgreSQL implementation of the repository connection contract.

SQL compatibility is limited to constructs used by this application's
repositories. Schema migrations use native, versioned PostgreSQL DDL.
"""
from __future__ import annotations

import re
from functools import lru_cache
import threading
import time
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus

SCHEMA_VERSION = 1


class Row(tuple):
    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        return super().__getitem__(self._names.index(key) if isinstance(key, str) else key)

    def keys(self):
        return self._names


def row_factory(cursor):
    names = tuple(c.name for c in cursor.description) if cursor.description else ()
    return lambda values: Row(names, values)


def bind(parameters):
    # Legacy schema represents flags as integers; SQLite adapted bool this way.
    if parameters is None:
        return None
    return tuple(int(value) if isinstance(value,bool) else value for value in parameters)


@lru_cache(maxsize=4096)
def translate(sql: str) -> str:
    """Preserve literals while translating repository parameter markers."""
    sql = sql.strip().rstrip(";")
    if sql.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    if sql.upper() == "BEGIN DEFERRED":
        return "BEGIN ISOLATION LEVEL REPEATABLE READ"
    ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\b", sql, re.I))
    if ignore:
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\b", "INSERT", sql, count=1, flags=re.I)
        sql += " ON CONFLICT DO NOTHING"
    # Every runtime scalar MAX has two simple arguments; aggregate MAX stays.
    sql = re.sub(r"\bMAX\((\w+),\s*(\?)\)", r"GREATEST(\1, \2)", sql, flags=re.I)
    sql = re.sub(r"json_extract\(([^,()]+),\s*'\$\.chart_pending'\)",
                 r"(CASE WHEN (\1::jsonb ->> 'chart_pending') IN ('true','1') THEN 1 ELSE 0 END)", sql, flags=re.I)
    sql = re.sub(r"json_extract\(([^,()]+),\s*'\$\.(\w+)'\)",
                 r"(\1::jsonb ->> '\2')", sql, flags=re.I)
    sql = re.sub(r"json_each\(([^()]+)\)\s+(\w+)",
                 r"jsonb_array_elements_text(\1::jsonb) AS \2(value)", sql, flags=re.I)
    sql = re.sub(r"j\.value\s*=\s*m\.event_id", "j.value::bigint=m.event_id", sql)
    if "sqlite_master" in sql:
        sql = sql.replace("sqlite_master", "information_schema.tables").replace("SELECT name", "SELECT table_name AS name")
        sql = sql.replace("type='table'", "table_schema=current_schema()")
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    has_parameters = any("?" in parts[i] for i in range(0,len(parts),2))
    if has_parameters:
        parts = [part.replace("%", "%%") for part in parts]
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("?", "%s")
        parts[i] = re.sub(r"\buser\b", '"user"', parts[i])
        parts[i] = re.sub(r"\bAS INTEGER\b", "AS BIGINT", parts[i], flags=re.I)
    return "".join(parts)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class Session:
    def __init__(self, raw, owner):
        self.raw = raw
        self.owner = owner

    @property
    def in_transaction(self):
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql, parameters=None):
        query = translate(sql)
        cursor = self.raw.execute(query, bind(parameters), prepare=True)
        return Cursor(cursor)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


class Connection:
    def __init__(self, url):
        self._url = url
        self._local = threading.local()
        self._sessions = []
        self._registry_lock = threading.Lock()
        self._closed = False
        self._id_tables = set()

    @property
    def _lock(self):
        if not hasattr(self._local,"lock"):
            self._local.lock = threading.RLock()
        return self._local.lock

    @property
    def _conn(self):
        if self._closed:
            raise RuntimeError("Database is closed")
        if not hasattr(self._local, "session"):
            raw = psycopg.connect(self._url, autocommit=True, row_factory=row_factory,
                                  connect_timeout=15, application_name="levelframe")
            raw.execute("SET lock_timeout='15s'")
            raw.execute("SET idle_in_transaction_session_timeout='120s'")
            session = Session(raw, self)
            with self._registry_lock:
                self._sessions.append(session)
            self._local.session = session
        return self._local.session

    @property
    def unit_depth(self):
        return getattr(self._local, "unit_depth", 0)

    @unit_depth.setter
    def unit_depth(self, value):
        self._local.unit_depth = value

    def execute(self, sql, parameters=None):
        from .services.metrics import get_registry
        started = time.monotonic()
        query = translate(sql)
        insert = re.match(r"INSERT\s+INTO\s+(\w+)\b", query, re.I)
        returning = insert and insert[1] in self._id_tables and "RETURNING" not in query.upper()
        if returning:
            query += " RETURNING id"
        try:
            cursor = self._conn.raw.execute(query, bind(parameters), prepare=True)
            row = cursor.fetchone() if returning else None
            return Cursor(cursor, row[0] if row else None)
        except psycopg.Error as exc:
            if exc.sqlstate in {"40001","40P01","55P03"}:
                get_registry().counter("sql_busy_errors",labels={"op":"execute"})
            raise
        finally:
            get_registry().histogram("sql_duration", time.monotonic()-started,
                {"op":"execute", "kind":"read" if query.upper().startswith(("SELECT", "WITH")) else "write"})

    def executemany(self, sql, parameters):
        cursor = self._conn.raw.cursor()
        cursor.executemany(translate(sql), (bind(row) for row in parameters))
        return Cursor(cursor)

    def executescript(self, script):
        if self.unit_depth:
            raise RuntimeError("executescript is not allowed inside a unit of work")
        self._conn.raw.execute(script, prepare=False)

    def commit(self):
        if not self.unit_depth:
            self._conn.commit()

    def rollback(self):
        if self.unit_depth:
            raise RuntimeError("Only the unit of work may roll back its transaction")
        self._conn.rollback()

    def migrate(self):
        raw = self._conn.raw
        with raw.transaction():
            raw.execute("SELECT pg_advisory_xact_lock(741852964)")
            raw.execute("CREATE TABLE IF NOT EXISTS postgres_schema_version(version BIGINT PRIMARY KEY)")
            row = raw.execute("SELECT max(version) FROM postgres_schema_version").fetchone()
            if row[0] and row[0] > SCHEMA_VERSION:
                raise RuntimeError("PostgreSQL schema is newer than this application")
            if not row[0]:
                raw.execute(Path(__file__).with_name("schema_postgres.sql").read_text(encoding="utf-8"), prepare=False)
                raw.execute("INSERT INTO postgres_schema_version VALUES (%s)", (SCHEMA_VERSION,))
        self._id_tables = {r[0] for r in raw.execute("SELECT table_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_name='id' AND is_identity='YES'")}

    def close(self):
        with self._registry_lock:
            self._closed = True
            for session in self._sessions:
                session.raw.close()
            self._sessions.clear()
