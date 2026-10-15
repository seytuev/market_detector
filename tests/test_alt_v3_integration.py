"""W1 persistence, authenticated API, immediate trade touches and outbox."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.alt.engine_v3 import DAY, analyze
from app.alt.monitor_v3 import observe_quote
from app.alt.store_v3 import persist_result, select_frame
from app.config import Settings
from app.db import Database
from app.models_alt import AltAsset, AltCandle, AltInstrumentSource
from app.notify.alt_queue import AltDispatcher
from app.web.api import create_app
from tests.test_alt_engine_v3 import stamp
from tests.test_alt_notify import FakeSender


@pytest.fixture()
def market():
    db = Database(":memory:")
    settings = Settings()
    settings.auth_token = "v3-test"
    asset = db.upsert_alt_asset(AltAsset(id=None, cmc_id=6535, symbol="NEAR", name="Near", cmc_rank=30, mapping_status="mapped"))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(id=None, asset_id=asset.id, venue="binance", symbol="NEARUSDT", history_scope="full"))
    rows = json.loads((Path(__file__).parent/"fixtures/alt_v2/ohlc/NEAR.json").read_text())
    rows = [r for r in rows if r["open_time"] < stamp("2026-08-17")]
    db.insert_alt_candles([AltCandle(source_id=src.id, **{k:r[k] for k in ("open_time","open","high","low","close")}) for r in rows])
    persist_result(db, asset.id, src.id, analyze(rows, computed_at_ms=stamp("2026-08-17")), backfill=True)
    frame = select_frame(db, asset.id, src.id)
    yield db, settings, src, frame
    db.close()


def test_api_v3_rows_and_as_of(market):
    db, settings, src, f = market
    with TestClient(create_app(db, settings)) as client:
        auth = {"Authorization":"Bearer v3-test"}
        assert client.get(f"/api/alt/structure/{f['structure_id']}").status_code == 401
        table = client.get("/api/alt/setups?bucket=all", headers=auth).json()
        row = next(r for r in table["rows"] if r.get("structure_id") == f["structure_id"])
        assert row["range"]["lower"] == 1.55 and row["range"]["upper"] == 3.383
        url = f"/api/alt/structure/{f['structure_id']}"
        detail = client.get(url, headers=auth).json()
        assert detail["versions"]["rule"] == "alt-0.5"
        assert detail["cancel"]["price"] == .841
        assert not detail["targets"]
        early = client.get(url+f"?as_of_ms={stamp('2026-05-24')}", headers=auth).json()
        assert not early["pd_zones"]
        assert not early["confirmation"]
        assert all(c["open_time"]+DAY<=stamp("2026-05-24") for c in early["candles"])
        assert all(e["event_time_ms"]<=stamp("2026-05-24") for e in early["events"])
        unknown = client.get(url+f"?as_of_ms={stamp('2025-04-07')}", headers=auth)
        assert unknown.status_code == 404


def test_immediate_repeated_touches_and_no_tick_cancellation(market):
    db, _, src, _ = market
    cancelled_before = db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE event_type='cancelled'").fetchone()[0]
    at = stamp("2026-08-18")+1000
    assert observe_quote(db, src.id, 1.8, at, at) == 0
    assert observe_quote(db, src.id, 1.6, at+1000, at+1000) >= 1
    assert observe_quote(db, src.id, 1.59, at+2000, at+2000) == 0
    observe_quote(db, src.id, 1.8, at+3000, at+3000)
    observe_quote(db, src.id, 1.6, at+4000, at+4000)
    events = [json.loads(r["payload_json"]) for r in db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE delivered=0").fetchall()]
    ob_touches = [e for e in events if e["payload"].get("kind") == "OB"]
    assert len(ob_touches) == 2
    assert ob_touches[0]["event_time_ms"] == at+1000  # no D1 or W1 waiting
    assert observe_quote(db, src.id, 1.6, at+4000, at+4000) == 0
    assert observe_quote(db, src.id, .7, at+5000, at+5000) >= 0
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE event_type='cancelled'").fetchone()[0] == cancelled_before


def test_invalid_fvg_does_not_receive_live_touches(market):
    db, _, src, f = market
    at = stamp("2026-08-18")
    observe_quote(db, src.id, 1.9, at, at)
    rows = db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE delivered=0").fetchall()
    invalid = next(z for z in f["scenario_state"]["pd_zones"] if z["lower"]==1.692)
    assert not any(json.loads(r[0])["payload"].get("zone_id")==invalid["id"] for r in rows)


def test_stale_trade_out_of_order_and_pending_week_are_ignored(market):
    db, _, src, _ = market
    at = stamp("2026-08-18")
    assert observe_quote(db, src.id, 1.6, at, at+61000) == 0
    assert observe_quote(db, src.id, 1.6, None, at, price_kind="ticker") == 0
    observe_quote(db, src.id, 1.8, at+1000, at+1000)
    assert observe_quote(db, src.id, 1.6, at, at) == 0
    # Monday roll: resolve the newly closed week's invalidations first.
    assert observe_quote(db, src.id, 1.6, stamp("2026-08-24"), stamp("2026-08-24")) == 0


async def test_v3_outbox_no_history_spam_and_delivery_dedup(market):
    db, settings, src, _ = market
    sender = FakeSender()
    dispatcher = AltDispatcher(db, settings, sender)
    await dispatcher.dispatch_v3()
    assert sender.alt == []
    at = stamp("2026-08-18")
    observe_quote(db, src.id, 1.6, at, at)
    await dispatcher.dispatch_v3()
    assert len(sender.alt) == 1
    assert "NEAR" in sender.alt[0] and "OB" in sender.alt[0]
    await dispatcher.dispatch_v3()
    assert len(sender.alt) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0


async def test_v3_muted_events_are_not_sent_later(market):
    db, settings, src, _ = market
    settings.alt_config.engine_version = "v1"  # rollback disables W1 channel
    sender = FakeSender()
    dispatcher = AltDispatcher(db, settings, sender)
    at = stamp("2026-08-18")
    observe_quote(db, src.id, 1.6, at, at)
    await dispatcher.dispatch_v3()
    assert not sender.alt


async def test_v3_backfill_suppresses_previously_queued_events(market):
    from app.notify.outbox import Card
    db, settings, src, _ = market
    sender = FakeSender()
    dispatcher = AltDispatcher(db, settings, sender)
    at = stamp("2026-08-18")
    observe_quote(db, src.id, 1.6, at, at)
    ids = [r[0] for r in db.conn.execute("SELECT id FROM alt_v3_event WHERE delivered=0").fetchall()]
    assert ids
    dispatcher.outbox.put("alt_v3", "before-rollback", Card(""), ids)
    replay = analyze(db.get_alt_candles(src.id), computed_at_ms=at)
    persist_result(db, src.asset_id, src.id, replay, backfill=True)
    await dispatcher.dispatch_v3()
    assert not sender.alt
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0


def test_sub_80_percent_drawdown_does_not_block_base():
    rows = json.loads((Path(__file__).parent/"fixtures/alt_v2/ohlc/NEAR.json").read_text())
    rows = [r for r in rows if stamp("2025-02-01")<=r["open_time"]<stamp("2025-11-01")]
    assert 1-min(r["low"] for r in rows)/max(r["high"] for r in rows) < .80
    result = analyze(rows)
    assert any(b["geometry"]["lower"] == 1.55 and b["geometry"]["upper"] == 3.383 for b in result["structures"])


async def test_runner_migrates_to_v3_without_legacy_history_or_spam(market):
    from app.alt.runner import AltRunner
    db, settings, src, _ = market
    asset = db.get_alt_asset(src.asset_id)
    db.set_meta(f"alt:loaded:{asset.id}:{src.id}", "old")
    db.set_meta(f"alt:engine:{asset.id}:{src.id}", "v1")
    runner = AltRunner(db, settings, {}, None)
    assert runner.should_run(datetime.fromtimestamp(stamp("2026-08-18")/1000, timezone.utc))
    mode, _ = await runner._process_asset(asset, src, 0, stamp("2026-08-18"))
    assert mode == "backfill"
    assert db.get_meta(f"alt:engine:{asset.id}:{src.id}") == "v3"
    assert db.conn.execute("SELECT COUNT(*) FROM alt_event").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0
    assert db.conn.execute("SELECT DISTINCT rules_version FROM alt_structure").fetchall()[0][0] == "alt-0.5"
    assert (await runner._process_asset(asset, src, 0, stamp("2026-08-18")))[0] == "live"


async def test_week_roll_resolves_cancellations_before_live_touch(market, monkeypatch):
    from types import SimpleNamespace
    from app.alt.runner import AltRunner
    db, settings, src, f = market
    db.set_meta(f"alt:engine:{src.asset_id}:{src.id}", "v3")
    db.set_meta(f"alt:loaded:{src.asset_id}:{src.id}", "old")
    at = stamp("2026-08-24")
    monkeypatch.setattr("app.alt.runner.now_ms", lambda: at)

    class Venue:
        async def last_price(self, symbol):
            return SimpleNamespace(price=1.6, event_at=at, received_at=at, price_kind="trade", source_seq="1")

    runner = AltRunner(db, settings, {"binance": Venue()}, None)

    async def load(source):
        db.insert_alt_candles([AltCandle(source_id=source.id, open_time=stamp("2026-08-17")+i*DAY,
                                       open=1.6, high=1.65, low=.7, close=.8) for i in range(7)])
        return {"new_days": 7}

    monkeypatch.setattr(runner, "_load_history", load)
    await runner.poll_live()
    from app.alt.store_v3 import frame
    assert frame(db, f["structure_id"])["scenario_state"]["scenarios"][0]["cancelled_at_ms"] == at
    events = [json.loads(r[0]) for r in db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE event_time_ms>=? AND structure_key=?", (at, f["structure_key"])).fetchall()]
    assert not any(e["event_type"] == "retest" and e["payload"].get("kind") in ("FVG", "OB")
                   and e["payload"].get("precision") != "D1" for e in events)


async def test_binance_stream_records_repeated_contacts(market, monkeypatch):
    import asyncio
    from app.alt.runner import AltRunner
    db, settings, src, _ = market
    at = stamp("2026-08-18")
    stop = asyncio.Event()
    runner = AltRunner(db, settings, {}, None)
    runner._live_sources[src.id] = src
    runner._live_intervals[src.id] = [(1.469, 1.692)]
    monkeypatch.setattr("app.alt.runner.now_ms", lambda: at+10000)

    class Stream:
        prices = iter(enumerate([1.8, 1.6, 1.8, 1.6]))

        async def recv(self):
            i, price = next(self.prices)
            if i == 3:
                stop.set()
            return json.dumps({"stream": "nearusdt@aggTrade", "data": {"p": str(price), "T": at+i*1000, "a": i}})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *a, **kw: Stream())
    await runner.live_stream(stop)
    events = [json.loads(r[0]) for r in db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE delivered=0").fetchall()]
    assert len([e for e in events if e["payload"].get("kind") == "OB"]) == 2


async def test_runner_refetches_internal_history_gap(market, monkeypatch):
    from app.alt.runner import AltRunner
    db, settings, src, _ = market
    saved = db.get_alt_candles(src.id)
    missing = stamp("2026-03-03")
    db.conn.execute("DELETE FROM alt_candle WHERE source_id=? AND open_time=?", (src.id, missing))
    db.conn.commit()
    src.last_closed_ms = saved[-1].open_time
    db.set_meta(f"alt:gaps:{src.asset_id}:{src.id}", json.dumps([{"from_open_time": missing-DAY, "to_open_time": missing+DAY, "missing_days": 1}]))
    starts = []
    monkeypatch.setattr("app.alt.runner.now_ms", lambda: stamp("2026-08-18"))

    class Venue:
        async def klines(self, symbol, timeframe, start_ms, end_ms, **kwargs):
            starts.append(start_ms)
            return [c for c in saved if start_ms <= c.open_time <= end_ms]

    runner = AltRunner(db, settings, {"binance": Venue()}, None)
    loaded = await runner._load_history(src)
    assert starts[0] == missing
    assert loaded["gaps"] == []
    assert len(db.get_alt_candles(src.id)) == len(saved)
