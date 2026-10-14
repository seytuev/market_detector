"""Оптимизация startup-restore LTF (ТЗ «LTF Current Setup» §6/п.9.9):
per-candle N+1 устранён кэшем LTF-чтений — число SELECT по ltf_scenario_entry
на replay не растёт от длины истории, а итоговое состояние replay с кэшем
построчно идентично состоянию без кэша (та же торговая семантика)."""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Instrument
from tests.conftest import H1_MS, make_h1_candles
from tests.test_ltf_breaks import _series
from tests.test_ltf_engine import (
    SERIES_H2_CLOSES,
    SERIES_H2_HL,
    SERIES_H_CLOSES,
    SERIES_H_HL,
    T0,
    _feed,
    _setup,
)

LTF_TABLES = [
    "ltf_observation", "ltf_scenario", "ltf_pivot", "ltf_pivot_role_log",
    "ltf_structure_event", "ltf_movement", "ltf_range", "ltf_entry_zone",
    "ltf_scenario_entry", "ltf_liquidity_test", "ltf_event",
]


def test_replay_reuses_pivot_rows_until_storage_count_changes(db, instrument_id, monkeypatch):
    from app.engine.ltf.structure import StructureBatch
    from app.models_ltf import LtfPivot
    batch = StructureBatch()
    reads = []
    original = db.list_ltf_pivots

    def counted(iid):
        reads.append(iid)
        return original(iid)

    monkeypatch.setattr(db, "list_ltf_pivots", counted)
    for _ in range(100):
        assert batch.existing_for(db, instrument_id) == {}
    assert len(reads) == 1
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, kind="low", price=10,
        pivot_at=1, candle_open_time=1, confirmed_at=2,
    ))
    assert ("low", 1) in batch.existing_for(db, instrument_id)
    assert len(reads) == 2

_CFG = DetectorConfig()

# 37 свечей: слом → диапазон → касания FVG/BSL → отмена → переоткрытие
SERIES_FULL_HL = SERIES_H_HL + SERIES_H2_HL
SERIES_FULL_CLOSES = SERIES_H2_CLOSES


def _instrument(db: Database) -> int:
    return db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))


def _series_h(instrument_id: int):
    return _series(SERIES_FULL_HL, SERIES_FULL_CLOSES, instrument_id)


def _flat_tail(n: int, start_open_time: int, instrument_id: int, price: float = 100.0):
    """Флэт-хвост: ни pivots (строгие неравенства), ни касаний зон серии H."""
    return make_h1_candles(
        [(price, price, price, price)] * n, start_open_time, instrument_id
    )


def _build_db(tail: int, ltf_cache: bool) -> Database:
    """Одинаковый живой прогон серии H; хвост флэта лежит в БД необработанным
    (модель restore после простоя: replay встречает новые свечи)."""
    db = Database(":memory:", ltf_cache=ltf_cache)
    iid = _instrument(db)
    engine = LtfEngine(db, cfg=_CFG)
    candles = _series_h(iid)
    zid = _setup(db, iid)
    obs = engine.on_htf_zone_touched(iid, db.get_zone(zid), T0)
    _feed(db, engine, iid, candles, len(candles) - 1)
    assert db.list_ltf_scenarios(observation_id=obs.id)  # сценарий открыт
    if tail:
        db.insert_candles(_flat_tail(tail, candles[-1].open_time + H1_MS, iid))
    return db


def _count_entry_queries(db: Database, fn) -> dict:
    """Счётчик SQL по ltf_scenario_entry за время fn() (trace callback)."""
    raw = db.conn._conn  # sqlite3.Connection под _LockedConnection
    hits = {"select": 0, "write": 0}

    def cb(sql: str) -> None:
        if "ltf_scenario_entry" not in sql:
            return
        if sql.lstrip().upper().startswith("SELECT"):
            hits["select"] += 1
        else:
            hits["write"] += 1

    raw.set_trace_callback(cb)
    try:
        fn()
    finally:
        raw.set_trace_callback(None)
    return hits


def test_structure_event_membership_loads_once_and_tracks_writes(monkeypatch):
    from dataclasses import replace
    db = _build_db(0, ltf_cache=True)
    scenario = db.list_ltf_scenarios_for_instrument(1)[0]
    event = db.list_ltf_structure_events(scenario.id)[0]
    reads = []
    execute = db.conn.execute
    def counted(sql, parameters=None):
        if sql.lstrip().upper().startswith('SELECT') and 'FROM ltf_structure_event' in sql:
            reads.append(sql)
        return execute(sql, parameters) if parameters is not None else execute(sql)
    monkeypatch.setattr(db.conn, 'execute', counted)
    for i in range(100):
        assert not db.has_ltf_structure_event(scenario.id, f'absent:{i}', 'primary')
    assert len(reads) == 1
    assert db.has_ltf_structure_event(scenario.id, event.level_key, event.stage)
    fresh = replace(event, id=None, level_key='new-membership')
    db.insert_ltf_structure_event(fresh)
    assert db.has_ltf_structure_event(scenario.id, fresh.level_key, fresh.stage)
    with pytest.raises(ValueError):
        with db.batch_writes():
            db.insert_ltf_structure_event(replace(event, id=None, level_key='rolled-back'))
            assert db.has_ltf_structure_event(scenario.id, 'rolled-back', event.stage)
            raise ValueError('rollback')
    assert not db.has_ltf_structure_event(scenario.id, 'rolled-back', event.stage)
    db.close()


def test_replay_entry_queries_not_linear_in_candles():
    """SELECT ltf_scenario_entry на replay ограничены числом изменений состояния,
    а не длиной истории: хвост +200/+400 свечей запросов не добавляет
    (до оптимизации — запросы на КАЖДУЮ свечу на каждый активный сценарий)."""
    counts: list[dict] = []
    candles_seen: list[int] = []
    for tail in (0, 200, 400):
        db = _build_db(tail, ltf_cache=True)
        engine = LtfEngine(db, cfg=_CFG)
        obs = db.list_ltf_observations()[0]
        box: dict = {}

        def run() -> None:
            box["res"] = engine.replay_observation(obs.id)

        counts.append(_count_entry_queries(db, run))
        candles_seen.append(box["res"].processed)
        db.close()
    # replay действительно прогнал разную длину истории
    assert candles_seen[0] + 200 == candles_seen[1]
    assert candles_seen[1] + 200 == candles_seen[2]
    # без кэша было бы сотни запросов пропорционально свечам
    for hits in counts:
        assert hits["select"] <= 60, hits
    # главное: рост истории не даёт роста числа запросов
    assert counts[2]["select"] - counts[0]["select"] <= 10, counts


def _dump_state(db: Database) -> dict:
    out = {}
    for table in LTF_TABLES:
        out[table] = db.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    out["meta:ltf"] = db.conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'ltf:%' ORDER BY key"
    ).fetchall()
    return out


def test_replay_state_identical_with_and_without_cache():
    """Оптимизации (SQL-кэш LTF-чтений + инкрементальные курсоры сканирования)
    не меняют торговую семантику: полное состояние всех ltf_* таблиц после
    live-прогона + двух replay построчно совпадает со старым кодом."""
    states = {}
    for enabled in (False, True):
        db = Database(":memory:", ltf_cache=enabled)
        iid = _instrument(db)
        engine = LtfEngine(db, cfg=_CFG, scan_cursors=enabled)
        candles = _series_h(iid)
        zid = _setup(db, iid)
        obs = engine.on_htf_zone_touched(iid, db.get_zone(zid), T0)
        _feed(db, engine, iid, candles, 24)                # касания FVG/BSL + отмена
        engine.replay_observation(obs.id)
        _feed(db, engine, iid, candles, len(candles) - 1)  # переоткрытие
        engine.replay_observation(obs.id)
        states[enabled] = _dump_state(db)
        db.close()
    off, on = states[False], states[True]
    diffs = [t for t in off if off[t] != on[t]]
    assert diffs == [], f"расхождения таблиц: {diffs}"


def test_resumable_replay_matches_uninterrupted_replay_after_crash():
    snapshots = []
    for interrupted in (False, True):
        db = _build_db(20, ltf_cache=True)
        obs = db.list_ltf_observations()[0]
        engine = LtfEngine(db, _CFG)
        if interrupted:
            original = engine._process_candle
            count = 0
            def crash(*args, **kwargs):
                nonlocal count
                count += 1
                original(*args, **kwargs)
                if count == 22:
                    raise RuntimeError('simulated crash inside atomic bar')
            engine._process_candle = crash
            with pytest.raises(RuntimeError, match='simulated crash'):
                engine.replay_observation(obs.id, resume=True)
            engine = LtfEngine(db, _CFG)
        engine.replay_observation(obs.id, resume=True)
        snapshots.append(_dump_state(db))
        db.close()
    assert snapshots[0] == snapshots[1]


def test_replay_state_identical_random_walk_multi_obs():
    """Стресс-идентичность: 600 свечей random-walk (фиксированный seed),
    два наблюдения (BEAR+BULL), промежуточные replay — состояние всех
    ltf_* таблиц старого и нового кода совпадает построчно."""
    import random

    from app.models import Direction, Zone, ZoneStatus, ZoneType

    random.seed(42)
    price = 100.0
    bars = []
    for _ in range(600):
        drift = random.uniform(-2.2, 2.2)
        o, c = price, price + drift
        h = max(o, c) + random.uniform(0, 1.2)
        lo = min(o, c) - random.uniform(0, 1.2)
        bars.append((o, h, lo, c))
        price = c

    def build(enabled: bool) -> dict:
        db = Database(":memory:", ltf_cache=enabled)
        iid = _instrument(db)
        engine = LtfEngine(db, cfg=_CFG, scan_cursors=enabled)
        candles = make_h1_candles(bars, T0, iid)
        for d, z_lo, z_hi in ((Direction.BEAR, 90.0, 110.0),
                              (Direction.BULL, 85.0, 95.0)):
            zid = db.insert_zone(Zone(
                id=None, instrument_id=iid, type=ZoneType.OB, direction=d,
                timeframe="D1", lower=z_lo, upper=z_hi,
                formed_at=T0 - 10_000_000, confirmed_at=T0 - 9_000_000,
                status=ZoneStatus.ACTIVE,
            ))
            engine.on_htf_zone_touched(iid, db.get_zone(zid), T0)
        for i, candle in enumerate(candles):
            db.insert_candles([candle])
            engine.process_h1_close(iid, now_ms=candle.close_time)
            if i in (200, 400):
                for o in db.list_ltf_observations(instrument_id=iid):
                    engine.replay_observation(o.id)
        state = _dump_state(db)
        db.close()
        return state

    off, on = build(False), build(True)
    assert len(off["ltf_event"]) > 100  # прогон содержателен (события/сценарии)
    diffs = [t for t in off if off[t] != on[t]]
    assert diffs == [], f"расхождения таблиц: {diffs}"


def test_local_leg_checkpoints_match_full_role_rebuild(monkeypatch):
    import random
    import app.services.h1_setup as setup
    native = setup.H1LegCursor
    rng = random.Random(107)
    price = 100.0
    bars = []
    for _ in range(400):
        close = price + rng.uniform(-2.2, 2.2)
        bars.append((price, max(price, close) + rng.uniform(0, 1.2),
                     min(price, close) - rng.uniform(0, 1.2), close))
        price = close
    snapshots = {}
    for interval in (0, 32):
        def factory(*args, **kwargs):
            kwargs["ordered_candles"] = bool(interval)
            return native(*args, **kwargs, checkpoint_interval=interval)
        monkeypatch.setattr(setup, "H1LegCursor", factory)
        db = Database(":memory:")
        iid = _instrument(db)
        engine = LtfEngine(db, cfg=_CFG)
        candles = make_h1_candles(bars, T0, iid)
        db.insert_candles(candles)
        engine.process_h1_close(iid, now_ms=candles[-1].close_time)
        snapshots[interval] = _dump_state(db)
        for table in ("h1_local_leg", "h1_local_leg_revision"):
            snapshots[interval][table] = [tuple(r) for r in db.conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        assert snapshots[interval]["h1_local_leg"]
        if interval:
            assert engine._h1_cursors[iid].rewinds > 0
        db.close()
    assert snapshots[0] == snapshots[32]


@pytest.mark.asyncio
async def test_restore_replays_once_per_instrument():
    """_ltf_restore_all: 2 открытых наблюдения одного инструмента — один replay
    (replay_observation обрабатывает все наблюдения инструмента за проход)."""
    from tests.test_ltf_worker import FakeAdapter, _make_worker, _parent_zone

    db = Database(":memory:")
    adapter = FakeAdapter()
    worker, engine = _make_worker(db, adapter)
    await worker.seed_instruments()
    ins = next(i for i in db.get_instruments() if i.symbol == "BTCUSDT")
    zone1 = _parent_zone(db, ins.id)
    # вторая зона с другими границами: insert_zone идемпотентен по дедуп-ключу
    from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms

    zid2 = db.insert_zone(Zone(
        id=None, instrument_id=ins.id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=70.0, upper=80.0,
        formed_at=now_ms() - 10 * 86_400_000, confirmed_at=now_ms() - 9 * 86_400_000,
        status=ZoneStatus.ACTIVE,
    ))
    zone2 = db.get_zone(zid2)
    engine.on_htf_zone_touched(ins.id, zone1, 1_000)
    engine.on_htf_zone_touched(ins.id, zone2, 2_000)
    open_obs = [
        o for o in db.list_ltf_observations(instrument_id=ins.id)
        if o.state in ("waiting_structure", "active", "paused_data")
    ]
    assert len(open_obs) == 2

    candles = make_h1_candles(
        [(110.0, 112.0, 108.0, 111.0)] * 8,
        now_ms() - 10 * H1_MS, ins.id,
    )
    db.insert_candles(candles)
    calls: list[int] = []
    orig = engine.replay_observation

    def spy(observation_id: int):
        calls.append(observation_id)
        return orig(observation_id)

    engine.replay_observation = spy
    try:
        await worker._ltf_restore_all()
        assert int(db.get_meta(f"ltf:h1:last_close:{ins.id}")) == candles[-1].close_time
        assert int(db.get_meta(f"ltf:h1:scenario_last_close:{ins.id}")) == candles[-1].close_time
        await worker._ltf_restore_all()
    finally:
        engine.replay_observation = orig
    assert len(calls) == 1
