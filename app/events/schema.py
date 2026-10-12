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

-- Память фактов и публикации v2 (спека 2026-10-11): раздельные времена,
-- жизненный цикл, переходы, снимки оценки, пакеты и доставки.

CREATE TABLE IF NOT EXISTS events_fact (
    fact_key TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    scope TEXT NOT NULL,
    episode_id TEXT,
    parent_key TEXT,
    direction TEXT,                 -- long | short | NULL (факт без направления)
    state TEXT NOT NULL,            -- provisional | confirmed | invalidated | expired
    verification TEXT NOT NULL DEFAULT 'unknown',  -- ok | partial | stale | unknown
    occurred_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    time_precision TEXT NOT NULL DEFAULT 'exact',  -- exact | window | day
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_fact_state
    ON events_fact(state, valid_until);

CREATE TABLE IF NOT EXISTS events_fact_transition (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key TEXT NOT NULL,
    transition TEXT NOT NULL,       -- appeared | confirmed | reinterpreted | invalidated | expired | late_record
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_fact_transition_key
    ON events_fact_transition(fact_key, at_ms);

CREATE TABLE IF NOT EXISTS events_assessment (
    assessment_id TEXT PRIMARY KEY,
    evaluated_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_assessment_eval
    ON events_assessment(evaluated_at);

CREATE TABLE IF NOT EXISTS events_bundle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at INTEGER NOT NULL,
    closes_at INTEGER NOT NULL,
    status TEXT NOT NULL,           -- open | published | delivered | unavailable
    kind TEXT NOT NULL DEFAULT 'changes',  -- changes | downtime_digest
    payload_json TEXT NOT NULL DEFAULT '{}',
    published_at INTEGER
);

CREATE TABLE IF NOT EXISTS events_publication (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pub_key TEXT UNIQUE NOT NULL,
    bundle_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    assessment_id TEXT,
    window_ms INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_publication_bundle
    ON events_publication(bundle_id);

CREATE TABLE IF NOT EXISTS events_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending | sent | failed | unavailable
    detail TEXT,
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    UNIQUE(bundle_id, recipient, channel)
);

CREATE TABLE IF NOT EXISTS events_basket (
    version TEXT PRIMARY KEY,
    day_start_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_source_state (
    source TEXT PRIMARY KEY,        -- price | liquidations | oi | funding | calendar | basket
    quality TEXT NOT NULL,          -- ok | partial | stale | unavailable | auth_error | capability_error
    detail TEXT,
    updated_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events_metric (
    series TEXT NOT NULL,           -- например "basket_day_return"
    bucket_start INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (series, bucket_start)
);
"""


def apply_events_schema(conn) -> None:
    conn.executescript(EVENTS_SQL)
