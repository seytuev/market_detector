"""Таблицы модуля «События». Добавляются идемпотентно, данные H1 не трогают."""
from __future__ import annotations

EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS events_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    evaluated_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_snapshot_symbol
    ON events_snapshot(symbol, evaluated_at);

CREATE TABLE IF NOT EXISTS events_situation (
    symbol TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (symbol, rule_id, rule_version, episode_id)
);

CREATE TABLE IF NOT EXISTS events_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    transition TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_gate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    status TEXT NOT NULL,
    evaluated_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_bucket (
    scope_id TEXT NOT NULL,
    series TEXT NOT NULL,
    interval TEXT NOT NULL,
    bucket_start INTEGER NOT NULL,
    bucket_end INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    quality TEXT NOT NULL,
    available_at INTEGER NOT NULL,
    PRIMARY KEY (scope_id, series, interval, bucket_start)
);

CREATE TABLE IF NOT EXISTS events_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_key TEXT NOT NULL UNIQUE,
    event_key TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def apply_events_schema(conn) -> None:
    conn.executescript(EVENTS_SQL)
