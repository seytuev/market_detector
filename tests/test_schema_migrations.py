"""Этап 1: схема и миграции под правила HTF/LTF v1 (10.10.2026).

Покрытие:
- идемпотентность миграции на свежей БД;
- миграция БД со старыми данными: колонки добавляются ALTER'ами,
  state_v2 переносится из прежнего state (терминальные — как есть);
- unknown-разметка истории (R25): старые визиты не превращаются в
  активации, depth_before/fill_before исторической строки — NULL;
- CRUD новых сущностей (zone_activation, pd_revision, candle_revision,
  episode state_v2) и расширенная котировка §10.1.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import Database
from app.models import Candle, Direction, Zone, ZoneStatus, ZoneType

T = 1_780_012_800_000
H = 3_600_000

EPISODE_V2_COLS = (
    "state_v2", "predecessor_episode_id", "opposing_source_id",
    "htf_pass_confirmed_at", "reverse_break_event_id",
    "leg_anchor_at", "leg_anchor_available_at",
    "horizon_extended", "data_quality_note",
)


def _zone(db, iid) -> int:
    return db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=90, upper=110, formed_at=T - 24 * H,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))


# --------------------------------------------------------------------- #
# Свежая БД: таблицы/колонки на месте, миграция идемпотентна
# --------------------------------------------------------------------- #

def test_fresh_db_has_v2_schema(db):
    cols = {r["name"] for r in db.conn.execute(
        "PRAGMA table_info(htf_context_episode)").fetchall()}
    assert set(EPISODE_V2_COLS) <= cols
    assert "revision" in {r["name"] for r in db.conn.execute(
        "PRAGMA table_info(candle)").fetchall()}
    assert {"visit_id", "activation_id"} <= {r["name"] for r in db.conn.execute(
        "PRAGMA table_info(event)").fetchall()}
    assert {"htf_interaction_id", "pd_anchor_structure_key", "pd_anchor_kind"} <= {
        r["name"] for r in db.conn.execute("PRAGMA table_info(ltf_scenario)").fetchall()
    }
    for table in ("zone_activation", "pd_revision", "candle_revision"):
        assert db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None


def test_migration_idempotent_on_fresh_db(db):
    db.migrate()
    db.migrate()
    # повторная миграция не падает и не дублирует схему
    db.migrate()


# --------------------------------------------------------------------- #
# Старая БД: ALTER + перенос state в state_v2
# --------------------------------------------------------------------- #

@pytest.fixture
def legacy_db_path(tmp_path):
    """Файловая БД со старой схемой: затронутые таблицы пересозданы DDL до
    правил v1 (без новых колонок) и содержат старые строки; повторное
    открытие прогоняет миграцию ALTER'ами."""
    path = str(tmp_path / "legacy.sqlite")
    Database(path).close()  # свежая схема; соединение закрыто до raw-правок
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    raw.executescript(
        """
        DROP TABLE htf_context_episode;
        CREATE TABLE htf_context_episode (
            id INTEGER PRIMARY KEY,
            instrument_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            last_distinct_interaction_at INTEGER NOT NULL,
            candidate_direction TEXT NOT NULL,
            basis TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            invalidated_at INTEGER,
            invalid_reason TEXT,
            confirmed_scenario_id INTEGER,
            confirmed_at INTEGER,
            protected_price REAL,
            protected_candle_open INTEGER,
            protected_pivot_ref INTEGER,
            protected_kind TEXT,
            intrabar_order_unknown INTEGER NOT NULL DEFAULT 0,
            rule_version TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            closed_at INTEGER,
            close_reason TEXT
        );
        DROP TABLE candle;
        CREATE TABLE candle (
            instrument_id INTEGER NOT NULL REFERENCES instrument(id),
            timeframe TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            close_time INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            closed INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            PRIMARY KEY (instrument_id, timeframe, open_time)
        );
        DROP TABLE event;
        CREATE TABLE event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_id INTEGER NOT NULL REFERENCES zone(id),
            cycle_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            occurred_at INTEGER NOT NULL,
            detected_at INTEGER NOT NULL,
            price REAL NOT NULL,
            depth REAL NOT NULL DEFAULT 0,
            delayed INTEGER NOT NULL DEFAULT 0,
            evidence TEXT NOT NULL DEFAULT '{}',
            UNIQUE (zone_id, cycle_id, kind, occurred_at)
        );
        """
    )
    # старые строки эпизода в прежних состояниях
    raw.executemany(
        """INSERT INTO htf_context_episode
           (instrument_id, started_at, last_distinct_interaction_at,
            candidate_direction, basis, state, expires_at, rule_version,
            evidence)
           VALUES (1, ?, ?, 'bear', '', ?, ?, 'htf-context-1', '{}')""",
        [(T, T, "awaiting_h1", T + 168 * H),
         (T + H, T + H, "confirmed", T + 200 * H),
         (T + 2 * H, T + 2 * H, "expired", T + 3 * H),
         (T + 3 * H, T + 3 * H, "invalidated", T + 4 * H)],
    )
    # старый визит без разметки активации (для R25)
    raw.execute(
        """INSERT INTO instrument (asset, venue, market_type, symbol, quote_asset)
           VALUES ('BTC', 'binance', 'spot', 'BTCUSDT', 'USDT')"""
    )
    raw.execute(
        """INSERT INTO zone (instrument_id, type, direction, timeframe, lower,
                             upper, formed_at, status, cycle_id, source,
                             rule_version, evidence, created_at)
           VALUES (1, 'OB', 'bear', 'D1', 90, 110, ?, 'active', 1, 'auto',
                   '0.2', '{}', 0)""",
        (T - 24 * H,),
    )
    raw.execute(
        """INSERT INTO visit (zone_id, cycle_id, entered_at, max_depth)
           VALUES (1, 1, ?, 0.95)""",
        (T - 12 * H,),
    )
    raw.commit()
    raw.close()
    return path


def test_legacy_episode_columns_added_and_state_v2_backfilled(legacy_db_path):
    db = Database(legacy_db_path)
    try:
        cols = {r["name"] for r in db.conn.execute(
            "PRAGMA table_info(htf_context_episode)").fetchall()}
        assert set(EPISODE_V2_COLS) <= cols
        # колонки остальных старых таблиц тоже добавлены ALTER'ами
        assert "revision" in {r["name"] for r in db.conn.execute(
            "PRAGMA table_info(candle)").fetchall()}
        assert {"visit_id", "activation_id"} <= {r["name"] for r in db.conn.execute(
            "PRAGMA table_info(event)").fetchall()}
        rows = db.conn.execute(
            "SELECT state, state_v2 FROM htf_context_episode ORDER BY id"
        ).fetchall()
        assert [(r["state"], r["state_v2"]) for r in rows] == [
            ("awaiting_h1", "awaiting_initial_confirmation"),
            ("confirmed", "context_active"),
            # терминальные значения сохраняются без переименования
            ("expired", "expired"),
            ("invalidated", "invalidated"),
        ]
        # повторная миграция не трогает уже перенесённые значения
        db.migrate()
        again = db.conn.execute(
            "SELECT state_v2 FROM htf_context_episode ORDER BY id"
        ).fetchall()
        assert [r["state_v2"] for r in again] == [
            "awaiting_initial_confirmation", "context_active",
            "expired", "invalidated",
        ]
    finally:
        db.close()


def test_r25_history_visits_not_converted_to_activations(legacy_db_path):
    db = Database(legacy_db_path)
    try:
        # исторический визит на месте, но активации из него не созданы
        assert db.conn.execute("SELECT COUNT(*) AS n FROM visit").fetchone()["n"] == 1
        assert db.list_zone_activations(1) == []
        # новая активация старой зоны: глубина/заполнение до неё — unknown
        aid = db.create_zone_activation(1, 1, T)
        row = db.get_zone_activation(aid)
        assert row["depth_before"] is None
        assert row["fill_before"] is None
    finally:
        db.close()


# --------------------------------------------------------------------- #
# CRUD новых сущностей
# --------------------------------------------------------------------- #

def test_zone_activation_crud(db, instrument_id):
    zid = _zone(db, instrument_id)
    aid = db.create_zone_activation(
        zid, 1, T, episode_id=7, depth_before=0.5, fill_before=0.2,
        fresh_for_new_activation=False, created_at=T,
    )
    row = db.get_open_zone_activation(zid, 1)
    assert row["id"] == aid
    assert row["episode_id"] == 7
    assert row["depth_before"] == 0.5
    assert row["fresh_for_new_activation"] == 0
    assert row["current_activation_valid"] == 1
    db.update_zone_activation(
        aid, max_depth_current=0.9, fill_current=1.0,
        current_activation_valid=True,
    )
    db.update_zone_activation(aid, ended_at=T + 24 * H, end_reason="reverse_bos")
    assert db.get_open_zone_activation(zid, 1) is None
    rows = db.list_zone_activations(zid)
    assert len(rows) == 1
    assert rows[0]["ended_at"] == T + 24 * H
    assert rows[0]["max_depth_current"] == 0.9
    with pytest.raises(ValueError):
        db.update_zone_activation(aid, started_at=0)


def test_pd_revision_insert_and_list(db, instrument_id):
    from app.models_ltf import LtfObservation, LtfScenario
    zid = _zone(db, instrument_id)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="SMS", stage="primary", state="monitoring_entries",
        created_at=T, updated_at=T,
    ))
    first = db.insert_pd_revision(
        sc.id, "struct:sms:1", "SMS", [{"pivot": 1, "price": 100.0}],
        T + H, range_version=1, created_at=T + H,
    )
    second = db.insert_pd_revision(
        sc.id, "struct:bos:2", "BOS", [{"pivot": 2, "price": 99.0}],
        T + 2 * H, range_version=2, created_at=T + 2 * H,
    )
    rows = db.list_pd_revisions(sc.id)
    assert [r["id"] for r in rows] == [first, second]
    # старая ревизия не переписывается; тип якоря сохраняется (BOS/SMS равны)
    assert rows[0]["anchor_kind"] == "SMS"
    assert rows[1]["anchor_kind"] == "BOS"
    assert json.loads(rows[0]["anchors_json"]) == [{"pivot": 1, "price": 100.0}]


def test_candle_revision_insert_dedup_and_list(db, instrument_id):
    rid = db.insert_candle_revision(
        instrument_id, "H1", T, [5, 12, 1, 5], [5, 9, 1, 5], T + H,
    )
    assert rid is not None
    # §10.3: дедуп по целевому OHLC — та же ревизия (даже в другое время
    # обнаружения) не плодит строки
    assert db.insert_candle_revision(
        instrument_id, "H1", T, [5, 12, 1, 5], [5, 9, 1, 5], T + H,
    ) is None
    assert db.insert_candle_revision(
        instrument_id, "H1", T, [5, 12, 1, 5], [5, 9, 1, 5], T + 2 * H,
    ) is None
    # новая ревизия к другому OHLC — новая строка (даже в ту же ms)
    assert db.insert_candle_revision(
        instrument_id, "H1", T, [5, 9, 1, 5], [5, 7, 1, 5], T + H,
    ) is not None
    rows = db.list_candle_revisions(instrument_id, "H1")
    assert len(rows) == 2
    assert json.loads(rows[0]["old_ohlc_json"]) == [5, 12, 1, 5]
    assert json.loads(rows[0]["new_ohlc_json"]) == [5, 9, 1, 5]


def test_candle_revision_column_defaults(db, instrument_id):
    db.insert_candles([Candle(
        instrument_id=instrument_id, timeframe="H1", open_time=T,
        close_time=T + H - 1, open=1, high=2, low=0.5, close=1, closed=True,
        source="test",
    )])
    row = db.conn.execute(
        "SELECT revision FROM candle WHERE instrument_id=?", (instrument_id,)
    ).fetchone()
    assert row["revision"] == 0


def test_episode_state_v2_update(db, instrument_id):
    db.conn.execute(
        """INSERT INTO htf_context_episode
           (instrument_id, started_at, last_distinct_interaction_at,
            candidate_direction, basis, state, expires_at, rule_version,
            evidence)
           VALUES (?, ?, ?, 'bear', '', 'awaiting_h1', ?, 'htf-context-1', '{}')""",
        (instrument_id, T, T, T + 168 * H),
    )
    ep_id = int(db.conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    db.set_episode_state_v2(
        ep_id, "opposing_confirmed",
        predecessor_episode_id=ep_id - 0, opposing_source_id=42,
        leg_anchor_at=T - 24 * H, leg_anchor_available_at=T,
    )
    row = db.conn.execute(
        "SELECT * FROM htf_context_episode WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["state_v2"] == "opposing_confirmed"
    assert row["opposing_source_id"] == 42
    assert row["leg_anchor_at"] == T - 24 * H
    assert row["horizon_extended"] == 0
    # прежний state для совместимости не изменён
    assert row["state"] == "awaiting_h1"
    with pytest.raises(ValueError):
        db.set_episode_state_v2(ep_id, "context_active", unknown_field=1)


# --------------------------------------------------------------------- #
# Котировка §10.1: вид цены и раздельные времена, обратная совместимость
# --------------------------------------------------------------------- #

def test_quote_extended_fields_roundtrip(db, instrument_id):
    db.set_quote(instrument_id, 100.5, T, price_kind="trade",
                 event_at=T - 500, received_at=T + 100)
    detail = db.get_quote_detail(instrument_id)
    assert detail == {
        "price": 100.5, "ts": T, "price_kind": "trade",
        "event_at": T - 500, "received_at": T + 100,
        "age_ms": None, "detection_lag_ms": None,
        # этап 2 (§8): источник и его последовательность для tie-break
        # равных по времени сделок; старые записи — None
        "source": None, "source_seq": None,
    }
    # старый кортежный интерфейс не изменился
    assert db.get_quote(instrument_id) == (100.5, T)


def test_quote_legacy_record_without_new_fields(db, instrument_id):
    db.set_meta(f"quote:{instrument_id}", json.dumps({"price": 90.0, "ts": T}))
    detail = db.get_quote_detail(instrument_id)
    # R45: время старой записи за время сделки задним числом не выдаётся
    assert detail["price_kind"] == "unknown"
    assert detail["event_at"] is None
    assert detail["received_at"] == T
    assert db.get_quote(instrument_id) == (90.0, T)


def test_quote_defaults_for_minimal_write(db, instrument_id):
    db.set_quote(instrument_id, 100.5, T)
    detail = db.get_quote_detail(instrument_id)
    assert detail["price_kind"] == "unknown"
    # R45: минимальная запись без времени сделки — event_at отсутствует,
    # монотонное время (ts) на месте
    assert detail["event_at"] is None
    assert detail["ts"] == T
    assert detail["received_at"] >= T
    # монотонность F8 сохраняется и для расширенной записи
    db.set_quote(instrument_id, 90.0, T - H, price_kind="trade")
    assert db.get_quote_detail(instrument_id)["price"] == 100.5
