"""Смоук-тест схемы и репозитория модуля «Altcoins D1 accumulation»:
Database(":memory:") + по одной строке в каждую alt_* таблицу и чтение назад."""
from __future__ import annotations

import sqlite3

from app.config import AltConfig, load_alt_config
from app.db import Database
from app.models_alt import (
    ALT_RULE_VERSION_V2,
    AltAsset,
    AltCandle,
    AltEntryOpportunity,
    AltEpisodeState,
    AltEvent,
    AltEventType,
    AltFrozenRange,
    AltInstrumentSource,
    AltManipulationEpisode,
    AltRangeCandidate,
    AltRangeEpisode,
    AltRun,
    AltSetup,
    AltState,
    AltStructureEvent,
    AltSweepEpisode,
    AltSweepState,
)


def _db() -> Database:
    return Database(":memory:")


def test_alt_config_env_override(monkeypatch) -> None:
    cfg = load_alt_config()
    assert cfg.drawdown_threshold == 0.80
    assert cfg.forming_min_days == 50
    assert cfg.mature_min_days == 100
    assert cfg.cancel_mode == "wick_on_closed_d1"
    assert cfg.job_hour_msk == 3 and cfg.job_minute_msk == 15
    assert cfg.job_enabled is True
    assert cfg.cmc_rank_min == 11 and cfg.cmc_rank_max == 300
    # v2 (ТЗ 07.10.2026): значения по умолчанию и ENV-переопределение
    assert cfg.engine_version == "v1"
    assert cfg.v2_max_candidates == 8
    assert cfg.v2_admission_window_days == 1095
    assert cfg.v2_min_reactions == 2
    assert cfg.v2_sweep_max_days == 75
    assert cfg.v2_breakout_tol == 0.0

    monkeypatch.setenv("HTF_ALT_DRAWDOWN_THRESHOLD", "0.75")
    monkeypatch.setenv("HTF_ALT_JOB_ENABLED", "false")
    monkeypatch.setenv("HTF_ALT_FORMING_MIN_DAYS", "not-an-int")  # не роняет старт
    monkeypatch.setenv("HTF_ALT_V2_MIN_REACTIONS", "3")
    monkeypatch.setenv("HTF_ALT_ENGINE_VERSION", "v2")
    cfg2 = load_alt_config()
    assert cfg2.drawdown_threshold == 0.75
    assert cfg2.job_enabled is False
    assert cfg2.forming_min_days == 50
    assert cfg2.v2_min_reactions == 3
    assert cfg2.engine_version == "v2"


def test_alt_schema_roundtrip() -> None:
    db = _db()

    # alt_asset: upsert идемпотентен по cmc_id
    asset = db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=5426, symbol="SOL", name="Solana", cmc_rank=6,
        mapping_status="mapped", created_ms=1, updated_ms=1,
    ))
    assert asset.id is not None
    again = db.upsert_alt_asset(AltAsset(id=None, cmc_id=5426, symbol="SOL", cmc_rank=7))
    assert again.id == asset.id
    assert db.get_alt_asset(asset.id).cmc_rank == 7

    # alt_universe_snapshot
    snap_id = db.insert_alt_universe_snapshot(1_000, '[{"cmc_id": 5426}]')
    snap = db.get_latest_alt_universe_snapshot()
    assert snap is not None and snap["id"] == snap_id

    # alt_instrument_source: upsert идемпотентен по UNIQUE-ключу
    src = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="bybit", symbol="SOLUSDT",
        earliest_available_ms=86_400_000, last_closed_ms=86_400_000,
    ))
    assert src.id is not None
    src2 = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="bybit", symbol="SOLUSDT",
        last_closed_ms=172_800_000,
    ))
    assert src2.id == src.id
    assert db.get_alt_instrument_source(asset.id).last_closed_ms == 172_800_000

    # alt_candle: INSERT OR IGNORE + чтение диапазона по порядку
    candles = [
        AltCandle(source_id=src.id, open_time=t, open=10, high=11, low=9,
                  close=10.5, volume=100)
        for t in (0, 86_400_000, 172_800_000)
    ]
    assert db.insert_alt_candles(candles) == 3
    assert db.insert_alt_candles(candles) == 0  # дубликаты игнорируются
    fetched = db.get_alt_candles(src.id, start_ms=86_400_000)
    assert [c.open_time for c in fetched] == [86_400_000, 172_800_000]

    # alt_range_candidate
    cand = db.insert_alt_range_candidate(AltRangeCandidate(
        id=None, asset_id=asset.id, origin_key="0:86400000",
        start_anchor_open_time=0, rebound_anchor_open_time=86_400_000,
        lower=9.0, upper=11.0, width=2.0, mid=10.0, n_days=120,
        state=AltState.FORMING.value, first_seen_ms=1, updated_ms=1,
    ))
    assert db.get_alt_range_candidate(cand.id).n_days == 120

    # alt_frozen_range
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=cand.id, lower=9.0, upper=11.0, width=2.0, mid=10.0,
        start_anchor_open_time=0, rebound_anchor_open_time=86_400_000,
        included_candles=120, mature_at_ms=2, classifier_version="v1",
    ))
    assert db.get_alt_frozen_range(frozen.id).included_candles == 120

    # alt_setup: INSERT OR IGNORE по UNIQUE(asset_id, range_id) + update
    setup = AltSetup(
        id=None, asset_id=asset.id, source_id=src.id, range_id=frozen.id,
        state=AltState.MATURE.value, targets_json='[{"tp": 1, "price": 13.0}]',
        cancel_price=8.5, created_ms=1, updated_ms=1,
    )
    setup, created = db.insert_alt_setup(setup)
    assert created and setup.id is not None
    dup, created2 = db.insert_alt_setup(AltSetup(
        id=None, asset_id=asset.id, source_id=src.id, range_id=frozen.id,
    ))
    assert not created2 and dup.id == setup.id
    db.update_alt_setup(setup.id, state=AltState.ACTIVE_CONFIRMED.value,
                        breakout_close=11.5, terminated_ms=None)
    assert db.get_alt_setup(setup.id).state == AltState.ACTIVE_CONFIRMED.value

    # alt_structure_event
    se_id = db.insert_alt_structure_event(AltStructureEvent(
        id=None, setup_id=setup.id, kind="BOS", level_price=11.0,
        close_price=11.5, candle_open_time=172_800_000,
    ))
    events = db.list_alt_structure_events(setup.id)
    assert [e.id for e in events] == [se_id]

    # alt_manipulation_episode
    ep_id = db.insert_alt_manipulation_episode(AltManipulationEpisode(
        id=None, setup_id=setup.id, started_candle_open_time=86_400_000,
        min_price=8.9, ended_candle_open_time=172_800_000, days_below=1,
    ))
    assert db.list_alt_manipulation_episodes(setup.id)[0].id == ep_id

    # alt_entry_opportunity
    entry = db.insert_alt_entry_opportunity(AltEntryOpportunity(
        id=None, setup_id=setup.id, kind="A", event_time_ms=3, price=11.2,
    ))
    assert db.list_alt_entry_opportunities(setup.id)[0].id == entry.id

    # alt_run
    run = db.insert_alt_run(AltRun(id=None, started_ms=1, as_of_ms=172_800_000,
                                   universe_snapshot_id=snap_id))
    db.update_alt_run(run.id, status="ok", finished_ms=2, processed=1)
    got_run = db.get_alt_run(run.id)
    assert got_run.status == "ok" and got_run.processed == 1

    # alt_event: дедуп по UNIQUE(setup_id, event_type, source_event_id),
    # pending → delivered
    ev = AltEvent(
        id=None, setup_id=setup.id, event_type=AltEventType.BOS_CONFIRMED.value,
        source_event_id=str(se_id), event_time_ms=172_800_000,
        detected_at_ms=172_800_001, run_id=run.id, created_ms=2,
    )
    ev, ev_created = db.insert_alt_event(ev)
    assert ev_created
    ev_dup, ev_dup_created = db.insert_alt_event(AltEvent(
        id=None, setup_id=setup.id, event_type=AltEventType.BOS_CONFIRMED.value,
        source_event_id=str(se_id),
    ))
    assert not ev_dup_created and ev_dup.id == ev.id
    pending = db.pending_alt_events()
    assert [e.id for e in pending] == [ev.id]
    db.mark_alt_event_delivered(ev.id)
    assert db.pending_alt_events() == []


def test_alt_episode_schema_roundtrip() -> None:
    """Таблицы v2 (R-01/R-05/R-07): дедуп по origin_key, обновление полей
    конца заливки/геометрии, жизненный цикл выноса вниз."""
    db = _db()
    asset = db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=20107, symbol="TAO", name="Bittensor", cmc_rank=40,
    ))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue="bybit", symbol="TAOUSDT",
        earliest_available_ms=0, last_closed_ms=172_800_000,
    ))

    ep = AltRangeEpisode(
        id=None, asset_id=asset.id, source_id=src.id,
        origin_key=f"{asset.id}:0:86400000",
        anchor_start_open_time=0, base_start_open_time=86_400_000,
        lower=140.0, upper=500.0, width=360.0, mid=320.0,
        state=AltEpisodeState.MATURE.value,
        wick_low=130.0, wick_high=618.5,
        quality_json='{"reactions_lower": 3, "reactions_upper": 2}',
        detected_at_ms=1, created_ms=1, updated_ms=1,
    )
    ep, created = db.insert_alt_range_episode(ep)
    assert created and ep.id is not None
    assert ep.rules_version == ALT_RULE_VERSION_V2

    # дедуп: тот же origin_key не создаёт вторую строку (R-10)
    dup, dup_created = db.insert_alt_range_episode(AltRangeEpisode(
        id=None, asset_id=asset.id, source_id=src.id,
        origin_key=ep.origin_key,
        anchor_start_open_time=0, base_start_open_time=86_400_000,
        lower=1.0, upper=2.0, width=1.0, mid=1.5,
    ))
    assert not dup_created and dup.id == ep.id
    assert db.get_alt_range_episode(ep.id).lower == 140.0
    assert db.get_alt_range_episode_by_origin(ep.origin_key).id == ep.id

    # конец заливки отделён от конца сопровождения (R-07)
    db.update_alt_range_episode(
        ep.id, state=AltEpisodeState.ACCOMPANIMENT.value,
        base_end_open_time=172_800_000, base_end_reason="breakout_confirmed",
        base_end_confirmed_at_ms=172_800_001,
        selection_rank_reason="inside_base", updated_ms=2,
    )
    got = db.get_alt_range_episode(ep.id)
    assert got.state == AltEpisodeState.ACCOMPANIMENT.value
    assert got.base_end_reason == "breakout_confirmed"
    assert got.base_end_confirmed_at_ms == 172_800_001
    assert got.accompaniment_end_open_time is None
    assert got.wick_low == 130.0 and got.wick_high == 618.5

    # порядок истории — от старых к новым по якорю (R-08)
    older = AltRangeEpisode(
        id=None, asset_id=asset.id, source_id=src.id,
        origin_key=f"{asset.id}:old",
        anchor_start_open_time=-86_400_000, base_start_open_time=-86_400_000,
        lower=45.0, upper=125.0, width=80.0, mid=85.0,
        state=AltEpisodeState.TERMINAL.value,
        base_end_open_time=0, base_end_reason="decay",
        accompaniment_end_open_time=86_400_000,
    )
    older, older_created = db.insert_alt_range_episode(older)
    assert older_created
    ordered = db.list_alt_range_episodes(asset.id)
    assert [e.id for e in ordered] == [older.id, ep.id]

    # вынос вниз: open → returned, возврат подтверждён закрытием D1 (R-05)
    sw = db.insert_alt_sweep_episode(AltSweepEpisode(
        id=None, episode_id=ep.id, start_open_time=86_400_000,
        min_price=135.0, min_open_time=86_400_000, created_ms=1, updated_ms=1,
    ))
    assert sw.id is not None and sw.state == AltSweepState.OPEN.value
    db.update_alt_sweep_episode(
        sw.id, min_price=128.0, state=AltSweepState.RETURN_PENDING.value,
        updated_ms=2,
    )
    db.update_alt_sweep_episode(
        sw.id, end_open_time=172_800_000, return_confirmed=True,
        return_confirmed_at_ms=172_800_001,
        state=AltSweepState.RETURNED.value, updated_ms=3,
    )
    sweeps = db.list_alt_sweep_episodes(ep.id)
    assert [s.id for s in sweeps] == [sw.id]
    got_sw = sweeps[0]
    assert got_sw.min_price == 128.0
    assert got_sw.state == AltSweepState.RETURNED.value
    assert got_sw.return_confirmed is True
    assert got_sw.return_confirmed_at_ms == 172_800_001
    assert got_sw.end_open_time == 172_800_000
    assert db.get_alt_sweep_episode(sw.id).id == sw.id
    assert db.list_alt_sweep_episodes(older.id) == []


def test_alt_episode_migrate_existing_db(tmp_path) -> None:
    """Старая файловая БД (без таблиц v2): Database(path) добавляет
    alt_range_episode/alt_sweep_episode, повторное открытие не падает."""
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """CREATE TABLE schema_version (version INTEGER NOT NULL);
           INSERT INTO schema_version VALUES (1);
           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
           CREATE TABLE zone (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               instrument_id INTEGER NOT NULL, type TEXT NOT NULL,
               direction TEXT NOT NULL, timeframe TEXT NOT NULL,
               lower REAL NOT NULL, upper REAL NOT NULL,
               formed_at INTEGER NOT NULL, confirmed_at INTEGER,
               status TEXT NOT NULL, cycle_id INTEGER NOT NULL DEFAULT 1,
               source TEXT NOT NULL DEFAULT 'auto',
               rule_version TEXT NOT NULL DEFAULT '0.1',
               evidence TEXT NOT NULL DEFAULT '{}',
               created_at INTEGER NOT NULL DEFAULT 0);
           CREATE TABLE visit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               zone_id INTEGER NOT NULL, cycle_id INTEGER NOT NULL,
               entered_at INTEGER NOT NULL, exited_at INTEGER,
               max_depth REAL NOT NULL DEFAULT 0,
               observed INTEGER NOT NULL DEFAULT 1);"""
    )
    raw.commit()
    raw.close()

    db = Database(path)
    tables = {
        r["name"] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "alt_range_episode" in tables
    assert "alt_sweep_episode" in tables
    db.close()
    # повторное открытие — миграция идемпотентна
    db2 = Database(path)
    db2.close()
