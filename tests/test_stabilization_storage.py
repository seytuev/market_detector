"""Real SQLite fault/concurrency tests for the stabilization contract."""
import json
import threading
import time
import os
import sys
import subprocess
import pytest
from app.db import Database
from app.models import Candle, Instrument
from app.services.runtime import claim_owner, renew_owner, assert_owner


@pytest.mark.skipif(os.name != "nt", reason="Windows process API contract")
def test_windows_pid_probe_does_not_change_child():
    from app.services.runtime import _pid_alive
    child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(1)"],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert _pid_alive(child.pid)
    assert child.poll() is None
    assert child.wait(timeout=5) == 0


def test_publication_runs_after_commit_and_not_after_rollback(db):
    published=[]
    with db.batch_writes():
        db.set_meta("state","published")
        db.on_commit(lambda: published.append(db.get_meta("state")))
        assert published == []
    assert published == ["published"]
    with pytest.raises(ValueError):
        with db.batch_writes():
            db.set_meta("state","discarded")
            db.on_commit(lambda: published.append("wrong"))
            raise ValueError("rollback")
    assert published == ["published"]


def test_rollback_does_not_rollback_acknowledged_parallel_write(tmp_path):
    db = Database(str(tmp_path / "isolation.db"))
    entered, release = threading.Event(), threading.Event()
    failures = []
    def replay():
        try:
            with db.batch_writes():
                db.set_meta("replay", "partial")
                entered.set()
                assert release.wait(5)
                raise ValueError("rollback")
        except ValueError:
            pass
        except BaseException as exc:
            failures.append(exc)
    def writer():
        try:
            db.set_meta("request", "acknowledged")
        except BaseException as exc:
            failures.append(exc)
    first = threading.Thread(target=replay)
    first.start()
    assert entered.wait(5)
    # A reader has its own committed snapshot and sees no dirty data.
    assert db.get_meta("replay") is None
    second = threading.Thread(target=writer)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert not failures
    assert not first.is_alive() and not second.is_alive()
    assert db.get_meta("replay") is None
    assert db.get_meta("request") == "acknowledged"
    db.close()


def test_direct_commit_cannot_commit_an_active_unit(db):
    try:
        with db.batch_writes():
            db.set_meta("partial", "value")
            db.conn.commit()
            raise ValueError("rollback")
    except ValueError:
        pass
    assert db.get_meta("partial") is None


def test_nested_rollback_is_a_savepoint(db):
    with db.batch_writes():
        db.set_meta("outer", "ok")
        try:
            with db.batch_writes():
                db.set_meta("inner", "discard")
                raise ValueError()
        except ValueError:
            pass
    assert db.get_meta("outer") == "ok"
    assert db.get_meta("inner") is None


def test_claim_is_atomic_and_fences_previous_generation(tmp_path):
    path = str(tmp_path / "lease.db")
    first, second = Database(path), Database(path)
    barrier = threading.Barrier(2)
    results = []
    def claim(db, iid):
        barrier.wait()
        results.append(claim_owner(db, "owner", {"instance_id": iid, "pid": 1}))
    threads = [threading.Thread(target=claim, args=pair) for pair in ((first,"a"),(second,"b"))]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert len(results) == 2 and sum(r["owned"] for r in results) == 1
    winner = next(r["owner"] for r in results if r["owned"])
    renew_owner(first,"owner",winner)
    current = json.loads(first.get_meta("owner"))
    current["expires_at"] = 0
    first.set_meta("owner", json.dumps(current))
    new = claim_owner(second,"owner",{"instance_id":"c","pid":1})["owner"]
    assert new["owner_generation"] > winner["owner_generation"]
    try:
        assert_owner(first,"owner",winner)
    except RuntimeError:
        pass
    else:
        raise AssertionError("old owner must be fenced")
    first.close()
    second.close()


def test_quote_receipt_survives_processing_and_is_deduplicated(db, instrument_id):
    args = dict(source="ws", event_at=1000, received_at=1001, price=100,
                price_kind="trade", source_seq="1")
    row = db.spool_quote(instrument_id, **args)
    db.fail_quote(row,"calculation failed")
    saved = db.load_spool(instrument_id)[0]
    assert saved["status"] == "retry" and saved["attempts"] == 1
    db.delete_spool([row])
    assert db.load_spool(instrument_id) == []
    assert db.spool_quote(instrument_id, **args) == row
    assert db.spool_counts() == {}


def test_invalid_ohlc_is_rejected_before_any_batch_write(db, instrument_id):
    good = Candle(instrument_id=instrument_id,timeframe="H1",open_time=0,close_time=3599999,
                  open=100,high=110,low=90,close=105,closed=True,source="test")
    bad = Candle(instrument_id=instrument_id,timeframe="H1",open_time=3600000,close_time=7199999,
                 open=100,high=90,low=110,close=105,closed=True,source="test")
    try:
        db.insert_candles([good,bad])
    except ValueError:
        pass
    else:
        raise AssertionError("invalid OHLC accepted")
    assert db.get_candles(instrument_id,"H1") == []


def test_disable_cancels_pending_work_and_preserves_receipt(db,instrument_id):
    row = db.spool_quote(instrument_id,source="ws",event_at=1000,received_at=1001,
        price=100,price_kind="trade",source_seq="disable-1")
    version = db.get_state_seq()
    db.set_instrument_active(instrument_id,False)
    assert db.spool_counts() == {}
    receipt = db.conn.execute("SELECT * FROM quote_spool WHERE id=?",(row,)).fetchone()
    assert receipt["status"] == "cancelled" and receipt["last_error"] == "instrument_disabled"
    assert db.get_state_seq() > version
def test_deferred_version_flags_need_no_savepoints_and_follow_outer_rollback(db):
    previous=db.get_state_seq()
    trace=[]
    db.conn._conn.set_trace_callback(trace.append)
    try:
        with db.batch_writes():
            db.bump_state_seq()
            db.bump_state_seq()
            db.bump_domain('seq:flag-test')
            db.bump_domain('seq:flag-test')
        assert not any(sql.startswith('SAVEPOINT') for sql in trace)
        assert db.get_state_seq()==previous+1
        assert db.get_domain('seq:flag-test')==1
        with pytest.raises(ValueError):
            with db.batch_writes():
                db.bump_state_seq()
                db.bump_domain('seq:flag-test')
                raise ValueError('rollback')
        assert db.get_state_seq()==previous+1
        assert db.get_domain('seq:flag-test')==1
    finally:
        db.conn._conn.set_trace_callback(None)


def test_coordinated_repository_writes_keep_explicit_nested_rollback(db):
    statements=[]
    published=[]
    db.conn._conn.set_trace_callback(statements.append)
    try:
        with db.batch_writes(join_repository_writes=True):
            db.set_meta('coordinated:outer','1')
            with pytest.raises(ValueError):
                with db.batch_writes():
                    db.set_meta('coordinated:nested','1')
                    raise ValueError('nested failure')
            db.on_commit(lambda: published.append('success'))
        assert sum(sql.startswith('SAVEPOINT') for sql in statements)==1
        assert db.get_meta('coordinated:outer')=='1'
        assert db.get_meta('coordinated:nested') is None
        assert published==['success']
        with pytest.raises(ValueError):
            with db.batch_writes(join_repository_writes=True):
                db.set_meta('coordinated:failed','1')
                db.on_commit(lambda: published.append('incorrect'))
                raise ValueError('bar failure')
        assert db.get_meta('coordinated:failed') is None
        assert published==['success']
        assert not db._local.join_repository_writes
    finally:
        db.conn._conn.set_trace_callback(None)
