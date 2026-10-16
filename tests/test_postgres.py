"""PostgreSQL contract and migration tests; require a disposable test server."""
import concurrent.futures
import json
import os
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlencode
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

from app.db import Database
from app.models import Instrument
from app.postgres import Connection, SCHEMA_VERSION, translate
from app.services.postgres_migration import IMPORT_KEY, import_sqlite
from app.services.runtime import claim_owner
from app.services.maintenance import backup
from app.services.postgres_backup import restore_database
from app.config import Settings


@pytest.fixture
def pg():
    url = os.environ.get("HTF_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HTF_TEST_POSTGRES_URL is not configured")
    schema = "test_"+uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as raw:
        raw.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    target = url+("&" if "?" in url else "?")+urlencode({"options":"-csearch_path="+schema})
    db = Database(target)
    try:
        yield db
    finally:
        db.close()
        with psycopg.connect(url, autocommit=True) as raw:
            raw.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_parameter_literals():
    assert translate("SELECT '?' AS x, ? AS y, 'user' AS z") == "SELECT '?' AS x, %s AS y, 'user' AS z"


def test_x_accounts_query_accepts_psycopg_parameters(db,monkeypatch):
    """Use the real driver parser: a literal percent with empty args caused prod HTTP 500."""
    from psycopg._queries import PostgresQuery
    from psycopg.adapt import Transformer
    from app.research.accounts import XAccounts
    from app.research.store import Store
    store=Store(db);accounts=XAccounts(store)
    accounts.save({'handle':'example','enabled':True})
    store.set_state('private-credential:x',{'value':'private-test-token'})
    original=store.rows
    def checked_rows(query,args=()):
        PostgresQuery(Transformer()).convert(translate(query),args)
        return original(query,args)
    monkeypatch.setattr(store,'rows',checked_rows)
    assert [a['handle'] for a in accounts.list()]==['example']


def test_v1_upgrade_creates_w1_event_journal_and_preserves_data(pg):
    from app.alt.engine_v3 import analyze
    from app.alt.store_v3 import persist_result, select_frame
    from app.models_alt import AltAsset, AltInstrumentSource
    pg.set_meta("upgrade:keep", "preserved")
    pg.set_meta("alt:v3:initialized", "failed_previous_replay")
    pg.conn.execute("DROP TABLE alt_v3_event")
    pg.conn.execute("DELETE FROM postgres_schema_version WHERE version>1")
    pg.conn.execute("INSERT INTO postgres_schema_version VALUES (1) ON CONFLICT DO NOTHING")
    pg.conn.migrate()
    pg.conn.migrate()  # restart is idempotent
    assert pg.conn.execute("SELECT max(version) FROM postgres_schema_version").fetchone()[0] == SCHEMA_VERSION
    assert pg.get_meta("upgrade:keep") == "preserved"
    assert pg.get_meta("alt:v3:initialized") is None
    asset = pg.upsert_alt_asset(AltAsset(None, 6535, "NEAR"))
    source = pg.upsert_alt_instrument_source(AltInstrumentSource(None, asset.id, "binance", "NEARUSDT"))
    rows = json.loads((Path(__file__).parent / "fixtures/alt_v2/ohlc/NEAR.json").read_text())
    result = analyze(rows)
    persist_result(pg, asset.id, source.id, result, backfill=True)
    persist_result(pg, asset.id, source.id, result)
    frame = select_frame(pg, asset.id, source.id)
    assert (frame["lower"], frame["upper"]) == (1.55, 3.383)
    assert pg.conn.execute("SELECT count(*) FROM alt_v3_event").fetchone()[0] > 0
    assert pg.conn.execute("SELECT count(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0


def test_failed_upgrade_rolls_back_ddl_and_schema_version(pg, monkeypatch):
    from app.postgres import MIGRATIONS
    pg.conn.execute("DROP TABLE alt_v3_event")
    pg.conn.execute("DELETE FROM postgres_schema_version WHERE version>1")
    pg.conn.execute("INSERT INTO postgres_schema_version VALUES (1) ON CONFLICT DO NOTHING")
    pg.set_meta("alt:v3:initialized", "keep_if_upgrade_fails")
    monkeypatch.setitem(MIGRATIONS, 2, MIGRATIONS[2] + "; SELECT 1/0")
    with pytest.raises(psycopg.errors.DivisionByZero):
        pg.conn.migrate()
    assert pg.conn.execute("SELECT max(version) FROM postgres_schema_version").fetchone()[0] == 1
    assert pg.conn.execute("SELECT to_regclass('alt_v3_event')").fetchone()[0] is None
    assert pg.get_meta("alt:v3:initialized") == "keep_if_upgrade_fails"


def test_pivot_selection_retains_history_but_hydrates_only_current(pg):
    from app.models_ltf import LtfPivot
    iid = pg.upsert_instrument(Instrument(None, 'BTC', 'binance', 'spot', 'BTCUSDT', 'USDT'))
    ids = [pg.insert_ltf_pivot(LtfPivot(id=None, instrument_id=iid, kind='high', price=100+i,
                                      pivot_at=1000, candle_open_time=1000,
                                      confirmed_at=2000)) for i in range(20)]
    assert [p.id for p in pg.list_ltf_pivots(iid)] == [ids[-1]]
    assert len(pg.list_ltf_pivots(iid, include_superseded=True)) == 20


def test_scenario_entry_returning_and_version_selection(pg):
    from app.models import Direction
    from app.models_ltf import LtfObservation,LtfScenario
    from tests.test_ltf_db import _make_zone,test_scenario_entry_upsert
    iid=pg.upsert_instrument(Instrument(None,'BTC','binance','spot','BTCUSDT','USDT'))
    zid=_make_zone(pg,iid)
    obs=pg.insert_ltf_observation(LtfObservation(None,iid,zid,1,1,Direction.BULL,activated_at=1000))
    sc=pg.insert_ltf_scenario(LtfScenario(None,obs.id,Direction.BULL,'BOS','primary',created_at=1000,updated_at=1000))
    test_scenario_entry_upsert(pg,sc,iid)


def test_bulk_scenario_entry_updates_are_atomic_and_preserve_original_admission(pg):
    from dataclasses import replace
    from tests.test_htf_ideas import seed
    from app.models_ltf import LtfScenarioEntry
    iid=pg.upsert_instrument(Instrument(None,'BTC','binance','spot','BTCUSDT','USDT'))
    _, _, scenario, zone = seed(pg, iid)
    entry = LtfScenarioEntry(None,scenario.id,zone.id,2,True,1.0,'fresh','ok',100,100)
    pg.upsert_ltf_scenario_entries([entry])
    pg.upsert_ltf_scenario_entries([replace(entry,eligible=False,state='tested',added_at=999,updated_at=200)])
    row=next(e for e in pg.list_ltf_scenario_entries(scenario.id) if e.range_version==2)
    assert (row.added_at,row.updated_at,row.eligible,row.state)==(100,200,False,'tested')
    import pytest
    with pytest.raises(Exception):
        pg.upsert_ltf_scenario_entries([replace(entry,updated_at=300),replace(entry,entry_zone_id=-100)])
    row=next(e for e in pg.list_ltf_scenario_entries(scenario.id) if e.range_version==2)
    assert row.updated_at==200


def test_ideas_reconciliation_bulk_snapshot_and_atomic_publication(pg, monkeypatch):
    from tests.test_htf_ideas import (
        test_reconcile_uses_bulk_originals_without_per_zone_reads,
        test_reconcile_rolls_back_lifecycle_when_idea_publication_fails,
        test_bulk_publication_preserves_manual_close_and_avoids_point_reads,
        test_delivery_and_chart_retries_do_not_invalidate_market_projections,
    )
    iid = pg.upsert_instrument(Instrument(None, 'BTC', 'binance', 'spot', 'BTCUSDT', 'USDT'))
    test_reconcile_uses_bulk_originals_without_per_zone_reads(pg, iid, monkeypatch)
    other = pg.upsert_instrument(Instrument(None, 'ETH', 'binance', 'spot', 'ETHUSDT', 'USDT'))
    test_reconcile_rolls_back_lifecycle_when_idea_publication_fails(pg, other, monkeypatch)
    third = pg.upsert_instrument(Instrument(None, 'SOL', 'binance', 'spot', 'SOLUSDT', 'USDT'))
    test_bulk_publication_preserves_manual_close_and_avoids_point_reads(pg, third, monkeypatch)
    fourth = pg.upsert_instrument(Instrument(None, 'LINK', 'binance', 'spot', 'LINKUSDT', 'USDT'))
    test_delivery_and_chart_retries_do_not_invalidate_market_projections(pg, fourth)


def test_ltf_context_batch_and_live_projection_cache(pg, monkeypatch):
    from tests.test_ltf_context_read_cache import (
        test_context_batch_preserves_latest_active_and_independent_parent_models,
        test_live_bundle_reuses_ltf_rows_across_parent_quotes_but_not_ltf_changes,
    )
    iid = pg.upsert_instrument(Instrument(None, 'BTC', 'binance', 'spot', 'BTCUSDT', 'USDT'))
    test_context_batch_preserves_latest_active_and_independent_parent_models(pg, iid, monkeypatch)
    other = pg.upsert_instrument(Instrument(None, 'SOL', 'binance', 'spot', 'SOLUSDT', 'USDT'))
    test_live_bundle_reuses_ltf_rows_across_parent_quotes_but_not_ltf_changes(pg, other, monkeypatch)


def test_graceful_calculation_role_handoff(pg):
    import asyncio
    from tests.test_runtime_startup import (
        test_calculation_role_released_after_writer_before_unrelated_cleanup,
    )
    asyncio.run(test_calculation_role_released_after_writer_before_unrelated_cleanup(pg))


def test_committed_materialized_ideas_projection(pg, monkeypatch):
    from tests.test_materialized_ideas import (
        test_committed_projection_is_exact_after_memory_cache_loss,
        test_uncommitted_or_changed_inputs_are_never_materialized,
        test_corrupt_snapshot_falls_back_and_new_facts_invalidate,
        test_parent_quote_depth_keeps_snapshot_but_invalidation_does_not,
    )
    for index, mode in enumerate(('exact', 'depth', 'nested_rollback', 'historical', 'parent_edit', 'future_fact', 'corrupt')):
        iid = pg.upsert_instrument(Instrument(None, f'TEST{index}', 'binance', 'spot', f'TEST{index}USDT', 'USDT'))
        with monkeypatch.context() as scoped:
            if mode == 'exact':
                test_committed_projection_is_exact_after_memory_cache_loss(pg, iid, scoped)
            elif mode == 'depth':
                test_parent_quote_depth_keeps_snapshot_but_invalidation_does_not(pg, iid, scoped)
            elif mode == 'corrupt':
                test_corrupt_snapshot_falls_back_and_new_facts_invalidate(pg, iid, scoped)
            else:
                test_uncommitted_or_changed_inputs_are_never_materialized(pg, iid, scoped, mode)


def test_instrument_flag_idempotence_and_independent_asset_versions(pg, monkeypatch):
    import asyncio
    from tests.test_domain_versions import test_repeated_instrument_flags_preserve_config_and_cancel_disabled_quotes
    from tests.test_runtime_startup import test_repeated_startup_seed_does_not_invalidate_all_asset_snapshots
    from tests.test_materialized_ideas import test_adding_another_asset_keeps_projection_but_real_rule_changes_invalidate
    iid = pg.upsert_instrument(Instrument(None, 'ETH', 'binance', 'spot', 'ETHUSDT', 'USDT'))
    test_repeated_instrument_flags_preserve_config_and_cancel_disabled_quotes(pg, iid)
    asyncio.run(test_repeated_startup_seed_does_not_invalidate_all_asset_snapshots(pg))
    other = pg.upsert_instrument(Instrument(None, 'SOL', 'binance', 'spot', 'SOLUSDT', 'USDT'))
    test_adding_another_asset_keeps_projection_but_real_rule_changes_invalidate(pg, other, monkeypatch)


def test_bulk_market_history_matches_scalar_and_rolls_back(pg):
    from tests.test_events_store_v2 import test_bulk_buckets_match_scalar_updates_and_rollback
    test_bulk_buckets_match_scalar_updates_and_rollback(pg)


def test_multi_asset_calculation_ownership_and_engine_isolation(pg, monkeypatch):
    import asyncio
    from tests.test_multi_asset_loading import (
        test_engine_batches_are_isolated_and_slow_asset_does_not_block_another,
        test_cancelled_calculation_retains_its_instrument_without_blocking_other_assets,
        test_calculating_new_asset_preserves_other_assets_projection_versions,
    )
    asyncio.run(test_engine_batches_are_isolated_and_slow_asset_does_not_block_another(pg, monkeypatch))
    asyncio.run(test_cancelled_calculation_retains_its_instrument_without_blocking_other_assets(pg))
    asyncio.run(test_calculating_new_asset_preserves_other_assets_projection_versions(pg))


def test_coordinated_htf_history_keeps_idempotence_and_delayed_events(pg):
    from app.config import DetectorConfig
    from tests.test_replay import test_replay_idempotent, test_replay_events_delayed
    cfg = DetectorConfig()
    btc = pg.upsert_instrument(Instrument(None, 'BTC', 'binance', 'spot', 'BTCUSDT', 'USDT'))
    test_replay_idempotent(pg, cfg, btc)
    eth = pg.upsert_instrument(Instrument(None, 'ETH', 'binance', 'spot', 'ETHUSDT', 'USDT'))
    test_replay_events_delayed(pg, cfg, eth)


def test_deferred_publication_flags_do_not_open_nested_postgres_units(pg, monkeypatch):
    from app.postgres import Session
    statements=[]
    native=Session.execute
    def counted(self,sql,parameters=None):
        statements.append(sql)
        return native(self,sql,parameters)
    monkeypatch.setattr(Session,'execute',counted)
    initial=pg.get_state_seq()
    with pg.batch_writes():
        for _ in range(100):
            pg.bump_state_seq()
            pg.bump_domain('seq:flag-test')
    assert not any(sql.startswith('SAVEPOINT') for sql in statements)
    assert pg.get_state_seq()==initial+1
    assert pg.get_domain('seq:flag-test')==1


def test_coordinated_repository_bar_and_explicit_postgres_savepoint(pg, monkeypatch):
    from app.postgres import Session
    statements=[]
    native=Session.execute
    def counted(self,sql,parameters=None):
        statements.append(sql)
        return native(self,sql,parameters)
    monkeypatch.setattr(Session,'execute',counted)
    published=[]
    with pg.batch_writes(join_repository_writes=True):
        pg.set_meta('coordinated:outer','1')
        with pytest.raises(psycopg.errors.DivisionByZero):
            with pg.batch_writes():
                pg.set_meta('coordinated:nested','1')
                pg.conn.execute('SELECT 1/0')
        pg.on_commit(lambda: published.append('committed'))
    assert sum(sql.startswith('SAVEPOINT') for sql in statements)==1
    assert pg.get_meta('coordinated:outer')=='1'
    assert pg.get_meta('coordinated:nested') is None
    with pytest.raises(ValueError):
        with pg.batch_writes(join_repository_writes=True):
            pg.set_meta('coordinated:failed','1')
            pg.on_commit(lambda: published.append('incorrect'))
            raise ValueError('failed bar')
    assert pg.get_meta('coordinated:failed') is None
    assert published==['committed']
    assert not pg._local.join_repository_writes


def test_identity_conflict_and_nested_rollback(pg):
    iid = pg.upsert_instrument(Instrument(id=None, asset="BTC", venue="binance", market_type="spot", symbol="BTCUSDT", quote_asset="USDT"))
    assert iid>0
    cursor = pg.conn.execute("INSERT OR IGNORE INTO instrument(asset,venue,market_type,symbol,quote_asset) VALUES(?,?,?,?,?)", ("BTC","binance","spot","BTCUSDT","USDT"))
    assert cursor.rowcount == 0 and cursor.lastrowid is None
    with pg.batch_writes():
        pg.set_meta("outer", "ok")
        with pytest.raises(ValueError):
            with pg.batch_writes():
                pg.set_meta("inner", "bad")
                raise ValueError("rollback")
    assert pg.get_meta("outer") == "ok" and pg.get_meta("inner") is None


def test_concurrent_owner_claim(pg):
    barrier = threading.Barrier(4)
    def claim(index):
        barrier.wait()
        return claim_owner(pg, "runtime:calc_owner", {"instance_id":str(index)})["owned"]
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(claim, range(4))) == 1


def test_unrelated_write_during_transaction(pg):
    with pg.batch_writes():
        pg.set_meta("long_calculation", "busy")
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            pool.submit(pg.set_meta, "heartbeat", "alive").result(timeout=3)
    assert pg.get_meta("heartbeat") == "alive"


def test_verified_import_and_identity(pg, tmp_path):
    source_path = tmp_path / "legacy.db"
    legacy = Database(str(source_path))
    iid = legacy.upsert_instrument(Instrument(id=None, asset="BTC", venue="binance", market_type="spot", symbol="BTCUSDT", quote_asset="USDT"))
    legacy.set_meta("settings:active_profile", json.dumps({"hello":"Привет"}))
    legacy.set_meta("runtime:calc_owner", json.dumps({"expires_at":0}))
    legacy.close()
    with sqlite3.connect(source_path) as archive:
        archive.execute("CREATE TABLE ltf_range_bak_ltfmig(id INTEGER, evidence TEXT)")
        archive.execute("INSERT INTO ltf_range_bak_ltfmig VALUES(7,'old evidence')")
    archive.close()
    manifest = import_sqlite(source_path, pg)
    assert manifest["instrument"]["rows"] == 1
    assert manifest["ltf_range_bak_ltfmig"]["rows"] == 1
    assert pg.get_instrument(iid).asset == "BTC"
    assert pg.get_meta(IMPORT_KEY)
    assert pg.get_meta("runtime:calc_owner") is None
    nxt = pg.upsert_instrument(Instrument(id=None, asset="ETH", venue="binance", market_type="spot", symbol="ETHUSDT", quote_asset="USDT"))
    assert nxt>iid
    with pytest.raises(RuntimeError, match="not empty"):
        import_sqlite(source_path, pg)


def test_backup_restore(pg, tmp_path):
    pg.set_meta("unicode", "Привет\tline\n\\")
    original = pg.upsert_instrument(Instrument(id=None, asset="BTC", venue="binance", market_type="spot", symbol="BTCUSDT", quote_asset="USDT"))
    folder = tmp_path / "backup"
    backup(pg,folder,Settings(db_path=str(tmp_path/"assets.db"),database_url=""))
    with pg.conn._conn.raw.transaction():
        names = [r[0] for r in pg.conn._conn.raw.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name<>'postgres_schema_version'")]
        pg.conn._conn.raw.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(",").join(map(sql.Identifier,names))))
    restore_database(pg,folder)
    assert pg.get_meta("unicode") == "Привет\tline\n\\"
    assert pg.get_instrument(original).asset == "BTC"
    nxt = pg.upsert_instrument(Instrument(id=None, asset="ETH", venue="binance", market_type="spot", symbol="ETHUSDT", quote_asset="USDT"))
    assert nxt>original


def test_failed_import_rolls_back_every_table(pg, tmp_path):
    source_path = tmp_path/"broken.db"
    legacy = Database(str(source_path))
    legacy.set_meta("must_rollback", "yes")
    legacy.close()
    with sqlite3.connect(source_path) as source:
        source.execute("INSERT INTO candle(instrument_id,timeframe,open_time,close_time,open,high,low,close,closed,source) VALUES(999,'H1',0,3599999,1,2,1,2,1,'test')")
    source.close()
    with pytest.raises(psycopg.IntegrityError):
        import_sqlite(source_path,pg)
    assert pg.get_meta("must_rollback") is None
    assert pg.get_meta(IMPORT_KEY) is None
    assert not Path(str(source_path)+".postgres-migrated").exists()


def test_pool_bounds_connections_and_keeps_buffered_results(pg):
    connection = Connection(pg.path, max_size=4)
    barrier = threading.Barrier(48)

    def query(index):
        barrier.wait(timeout=10)
        cursor = connection.execute("SELECT ? AS value, pg_sleep(0.005)", (index,))
        # Other threads can reuse this connection before we read its result.
        time.sleep(0.02)
        return cursor.fetchone()["value"]

    try:
        with concurrent.futures.ThreadPoolExecutor(48) as pool:
            assert list(pool.map(query, range(48))) == list(range(48))
        # Replacing request threads must not retain their old connections.
        for index in range(120):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                assert pool.submit(lambda: connection.execute("SELECT 1").fetchone()[0]).result() == 1
        assert connection._pool.get_stats()["pool_size"] <= 4
        assert not connection._sessions
    finally:
        connection.close()


def test_pool_pins_nested_transactions_and_releases_on_rollback(pg):
    with pg.batch_writes():
        pid = pg.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        pg.set_meta("pool:outer", "kept")
        with pytest.raises(ValueError):
            with pg.batch_writes():
                assert pg.conn.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
                pg.set_meta("pool:inner", "discarded")
                raise ValueError("rollback nested write")
        assert pg.conn.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
    assert not pg.conn._sessions
    assert pg.get_meta("pool:outer") == "kept"
    assert pg.get_meta("pool:inner") is None
    with pg.read_tx():
        pid = pg.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        with pg.read_tx():
            assert pg.conn.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
    assert not pg.conn._sessions


def test_pool_recovers_after_failed_transaction_and_closed_connection(pg):
    with pytest.raises(psycopg.errors.UndefinedTable):
        with pg.conn.session() as session:
            session.execute("BEGIN")
            session.execute("SELECT * FROM missing_pool_regression_table")
    assert not pg.conn._sessions
    assert pg.conn.execute("SELECT 1").fetchone()[0] == 1
    with pg.conn.session() as session:
        session.raw.close()
    assert pg.conn.execute("SELECT 2").fetchone()[0] == 2
    assert not pg.conn._sessions
