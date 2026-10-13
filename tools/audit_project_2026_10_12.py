"""Offline audit probes. No production DB, credentials or network are used.

Run from repository root with Python >=3.11. AST probes execute the original
function bodies, with external framework dependencies replaced by test doubles.
They supplement, and do not replace, integration tests.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import Database
from app.config import Settings, DetectorConfig, validate_detector_payload, validate_detector_config
from app.models import Instrument, now_ms
from app.adapters.base import QuoteTick
from app.services.market_queue import MarketQueue


@contextlib.contextmanager
def workspace_temp(label):
    parent = (ROOT / "tmp" / "audit-probes").resolve()
    folder = (parent / f"{label}-{uuid.uuid4().hex}").resolve()
    assert folder.is_relative_to(parent)
    folder.mkdir(parents=True)
    yield str(folder)


def extract(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(path), "exec"), namespace)
    return namespace[name]


def transaction_probe():
    results = {}
    for foreign_commit in (False, True):
        db = Database()
        entered, written = threading.Event(), threading.Event()
        def replay():
            try:
                with db.batch_writes():
                    db.set_meta("replay", "uncommitted")
                    entered.set()
                    assert written.wait(3)
                    raise RuntimeError("simulated replay failure")
            except RuntimeError:
                pass
        t = threading.Thread(target=replay)
        t.start()
        assert entered.wait(3)
        db.set_meta("parallel_request", "acknowledged")
        if foreign_commit:
            db.conn.commit()  # e.g. notification outbox uses this public wrapper
        written.set()
        t.join(3)
        assert not t.is_alive()
        results["foreign_commit" if foreign_commit else "foreign_write"] = {
            "replay_after_rollback": db.get_meta("replay"),
            "parallel_request_after_rollback": db.get_meta("parallel_request"),
        }
        db.close()
    return results


async def queue_probe():
    db = Database()
    ins = Instrument(id=None, asset="BTC", venue="binance", market_type="spot", symbol="BTCUSDT", quote_asset="USDT")
    ins.id = db.upsert_instrument(ins)
    @contextlib.asynccontextmanager
    async def owned(iid):
        yield
    async def fail(*args):
        raise RuntimeError("simulated detector failure")
    async def noop(*args):
        pass
    worker = SimpleNamespace(db=db, data_sync=SimpleNamespace(instrument=owned),
        scanner=SimpleNamespace(on_price=lambda: None), _calculate=fail,
        scan_tfs=("D1",), _dispatch_events=noop, _ltf_on_poll=noop)
    queue = MarketQueue(worker, queue_capacity=0)
    tick = QuoteTick(price=100, event_at=now_ms(), received_at=now_ms(), price_kind="trade", source_seq="1")
    db.spool_quote(ins.id, source="ws", event_at=tick.event_at, received_at=tick.received_at,
                   price=tick.price, price_kind=tick.price_kind, source_seq=tick.source_seq)
    state = queue._state(ins.id)
    state.ins, state.spooled, state.degraded = ins, 1, True
    before = db.spool_counts()
    await queue._drain(state)
    idle = state.idle.is_set()
    result = {"spool_before": before, "spool_after_failed_calculation": db.spool_counts(),
              "reports_idle": idle, "degraded_after_failure": db.get_meta(f"quote:degraded:{ins.id}")}
    db.close()
    return result


async def delivery_batch_probe():
    db = Database()
    ins = Instrument(id=None, asset="BTC", venue="binance", market_type="spot", symbol="BTCUSDT", quote_asset="USDT")
    ins.id = db.upsert_instrument(ins)
    @contextlib.asynccontextmanager
    async def owned(iid):
        yield
    count = 0
    deliveries = []
    def tick(n):
        return QuoteTick(100, n, n, "trade", str(n))
    async def calc(*args):
        nonlocal count
        count += 1
        if count < 8:
            state.queue.append(_Entry.from_tick(ins.id, tick(count + 1), "ws"))
        return [count]
    async def dispatch(events):
        deliveries.append({"processed_at_delivery": count, "events": list(events)})
    async def noop(*args):
        pass
    worker = SimpleNamespace(db=db, data_sync=SimpleNamespace(instrument=owned),
        scanner=SimpleNamespace(on_price=lambda: None), _calculate=calc,
        scan_tfs=("D1",), _dispatch_events=dispatch, _ltf_on_poll=noop)
    queue = MarketQueue(worker)
    from app.services.market_queue import _Entry
    state = queue._state(ins.id)
    state.ins = ins
    state.queue.append(_Entry.from_tick(ins.id, tick(1), "ws"))
    await queue._drain(state)
    db.close()
    return deliveries


async def recalc_probe():
    with workspace_temp("settings") as folder:
        db = Database()
        settings = Settings(db_path=str(Path(folder) / "test.db"))
        finished = threading.Event()
        def resync():
            time.sleep(.08)
            finished.set()
        engine = SimpleNamespace(resync_structure_params=resync)
        threads = []
        class TimeoutScheduler:
            """No socket/event loop needed: exercise the actual timeout branch."""
            TimeoutError = TimeoutError
            @staticmethod
            def to_thread(fn):
                return fn
            @staticmethod
            async def wait_for(fn, timeout):
                thread = threading.Thread(target=fn)
                threads.append(thread)
                thread.start()
                thread.join(timeout)
                if thread.is_alive():
                    raise TimeoutError()
        ns = dict(asyncio=TimeoutScheduler, json=json, os=os, Path=Path, asdict=dataclasses.asdict,
            replace=dataclasses.replace, now_ms=now_ms, db=db, settings=settings,
            ltf_engine=engine, Body=lambda *args: None,
            validate_detector_payload=validate_detector_payload,
            validate_detector_config=validate_detector_config,
            _RECALC_WAIT_SECONDS=.01, _RECALC_META_KEY="settings:recalc")
        for name in ("_settings_path", "_save_detector_to_file", "_set_recalc_status", "_get_recalc_status", "save_settings"):
            extract("app/web/api.py", name, ns)
        reply = await ns["save_settings"]({"ltf_structure_left": 4})
        for thread in threads:
            thread.join(2)
        result = {"reply": reply["recalc"], "thread_finished": finished.is_set(),
                  "scheduler": "test double; actual route body and worker thread",
                  "persisted_status_after_thread_finished": ns["_get_recalc_status"](db)}
        db.close()
        return result


def dotenv_probe():
    keys = ["HTF_PORT", "HTF_POLL_SECONDS", "HTF_QUOTE_POLL_SECONDS", "HTF_HOST"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(HTF_PORT="8765", HTF_POLL_SECONDS="123", HTF_QUOTE_POLL_SECONDS="7", HTF_HOST="example.invalid")
        settings = Settings()
        return {"env_after_import": {k: os.environ[k] for k in keys},
                "settings": {k: getattr(settings, k) for k in ["port", "poll_seconds", "quote_poll_seconds", "host"]}}
    finally:
        for k, value in saved.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value


def config_file_probe():
    import logging
    from app.config import parse_bool
    with workspace_temp("config") as folder:
        settings = Settings(db_path=str(Path(folder) / "test.db"))
        ns = dict(Path=Path, json=json, fields=dataclasses.fields, parse_bool=parse_bool,
                  logger=logging.getLogger("audit"))
        for name in ("_settings_path", "_apply_detector_payload", "_load_detector_from_file"):
            extract("app/web/api.py", name, ns)
        path = Path(folder) / "settings.json"
        path.write_text('{"detector":{"ltf_structure_left":-999}}', encoding="utf-8")
        ns["_load_detector_from_file"](settings)
        result = {"invalid_numeric_loaded": settings.detector.ltf_structure_left}
        path.write_text('[]', encoding="utf-8")
        try:
            ns["_load_detector_from_file"](settings)
        except Exception as exc:
            result["valid_json_wrong_shape_exception"] = type(exc).__name__
        return result


def invalid_candle_probe():
    from app.models import Candle
    db = Database()
    iid = db.upsert_instrument(Instrument(id=None, asset="BTC", venue="binance",
        market_type="spot", symbol="BTCUSDT", quote_asset="USDT"))
    invalid = Candle(instrument_id=iid, timeframe="H1", open_time=0,
        close_time=3599999, open=100, high=90, low=110, close=105, closed=True, source="audit")
    db.insert_candles([invalid])
    stored = db.candle_at(iid, "H1", 0)
    result = {"invalid_ohlc_persisted": stored is not None,
              "high": stored.high, "low": stored.low}
    db.close()
    return result


def owner_race_probe():
    db = Database()
    barrier = threading.Barrier(2)
    def simultaneous_read(db, key):
        raw = db.get_meta(key)
        barrier.wait(timeout=3)
        return json.loads(raw) if raw else None
    ns = dict(json=json, now_ms=now_ms, _read_claim=simultaneous_read,
              _pid_alive=lambda pid: True)
    claim = extract("app/services/runtime.py", "claim_owner", ns)
    result = []
    def contender(pid):
        result.append(claim(db, "audit:owner", {"pid": pid, "role": "calc"}))
    threads = [threading.Thread(target=contender, args=(pid,)) for pid in (101, 102)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)
    db.close()
    return {"both_claims_owned": len(result) == 2 and all(r["owned"] for r in result),
            "claims": result, "scope": "real claim_owner body; read barrier simulates concurrent requests"}


async def main():
    import logging
    logging.disable(logging.CRITICAL)
    result = {"python": sys.version, "scope": "temporary databases; AST probes are not HTTP integration tests",
              "transaction_isolation": transaction_probe(),
              "queue_failure": await queue_probe(),
              "delivery_batch": await delivery_batch_probe(),
              "recalc_timeout": await recalc_probe(),
              "env_late_load": dotenv_probe(),
              "config_file": config_file_probe(),
              "invalid_candle": invalid_candle_probe(),
              "owner_race": owner_race_probe()}
    out = ROOT / "docs/audit/2026-10-12/reproductions.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # All awaited probes complete synchronously with explicit test doubles;
    # the restricted Windows host cannot create asyncio's loopback socketpair.
    coroutine = main()
    try:
        coroutine.send(None)
        raise AssertionError("Probe unexpectedly required an event loop")
    except StopIteration:
        pass
