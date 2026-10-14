"""PostgreSQL contract and migration tests; require a disposable test server."""
import concurrent.futures
import json
import os
import sqlite3
import threading
import uuid
from urllib.parse import urlencode
from pathlib import Path

import psycopg
from psycopg import sql
import pytest

from app.db import Database
from app.models import Instrument
from app.postgres import translate
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
