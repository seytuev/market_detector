"""Recover current market channels without sending queued notifications.

Run from the project root: python -m tools.recover_market_data
Takes a SQLite backup and calculation lease before using normal sync/engines.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.binance import BinanceSpotAdapter
from app.adapters.bybit import BybitSpotAdapter
from app.adapters.hyperliquid import HyperliquidSpotAdapter
from app.config import load_detector_config, load_settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.main import load_dotenv
from app.notify.ltf_queue import LtfDispatcher
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.quality import data_quality
from app.services.runtime import CALC_OWNER, claim_owner, lease_heartbeat, release_owner
from app.models import now_ms
from app.web.api import create_app
from app.worker import Worker


async def main():
    load_dotenv()
    settings = load_settings()
    settings.detector = load_detector_config()
    db = Database(settings.db_path)
    claim = claim_owner(db, CALC_OWNER, {"pid": os.getpid(), "role": "market_recovery"})
    if not claim["owned"]:
        db.close()
        raise RuntimeError("Calculation owner is active; use the application's synchronization instead")
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(lease_heartbeat(db, CALC_OWNER, claim["owner"], stop))
    adapters = {}
    worker = None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(settings.db_path).parent / "backups" / f"market-recovery-{stamp}.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        def save_backup():
            with sqlite3.connect(settings.db_path) as source, sqlite3.connect(backup) as target:
                source.backup(target)
        await asyncio.to_thread(save_backup)
        print(f"Backup: {backup.resolve()}", flush=True)
        engine = LtfEngine(db, settings.detector)
        create_app(db, settings, ltf_engine=engine)  # apply the saved detector settings
        settings.calc_queue_enabled = False
        adapters = {
            "binance": BinanceSpotAdapter(settings.binance_base_url, trust_env=settings.market_trust_env),
            "hyperliquid": HyperliquidSpotAdapter(settings.hyperliquid_base_url, trust_env=settings.market_trust_env),
            "bybit": BybitSpotAdapter(settings.bybit_base_url, trust_env=settings.market_trust_env),
        }
        sender = LogSender()
        dispatcher = EventDispatcher(db, settings.detector, sender, chat_id=settings.telegram_chat_id or None)
        dispatcher.defer_flush = True
        dispatcher.outbox.inline_delivery = False
        ltf_dispatcher = LtfDispatcher(db, settings.detector, sender, settings=settings)
        worker = Worker(db, settings, settings.detector, adapters, dispatcher,
                        ltf_engine=engine, ltf_dispatcher=ltf_dispatcher)
        worker._calc_claim = claim["owner"]
        worker.data_sync.recover_interrupted()
        worker.data_sync.ready = True
        for ins in db.get_instruments(enabled_only=True):
            # Restore parents before H1 so its scenarios use the recovered
            # HTF context, and do not need another replay on the next poll.
            for tf in dict.fromkeys((*[tf for tf in worker.scan_tfs if tf != "H1"], "H1")):
                try:
                    if tf == "H1":
                        unseeded = [o for o in db.list_ltf_observations(instrument_id=ins.id)
                                    if o.state in ("active", "waiting_structure", "paused_data")
                                    and not db.get_meta(f"ltf:h1:seeded:{o.id}")]
                        if unseeded:
                            # Interrupted/new observations share one instrument
                            # replay rather than replaying it once per parent.
                            await worker._calculate(engine.replay_observation, unseeded[0].id)
                            for obs in unseeded:
                                await worker.data_sync._write(db.set_meta, f"ltf:h1:seeded:{obs.id}", "1")
                    result = await worker.data_sync.sync_candles(ins, tf, "manual_recovery")
                    print(ins.symbol, tf, result["status"], result.get("reason"), flush=True)
                except Exception as exc:
                    print(ins.symbol, tf, type(exc).__name__, str(exc)[:300], flush=True)
        worker.market_queue.start()
        for ins in db.get_instruments(enabled_only=True):
            try:
                await worker._poll_quote(ins)
            except Exception as exc:
                print(ins.symbol, "quote", type(exc).__name__, str(exc)[:300], flush=True)
        for ins in db.get_instruments(enabled_only=True):
            if not await worker.market_queue.wait_idle(ins.id, timeout=60):
                raise RuntimeError(f"Quote processing did not finish for {ins.symbol}")
        report = [{"symbol": ins.symbol, **data_quality(db, settings, ins.id, now_ms())}
                  for ins in db.get_instruments(enabled_only=True)]
        path = Path(settings.db_path).parent / "diag" / f"market-recovery-{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in report:
            print(item["symbol"], "quality", item["state"], item["reason"], flush=True)
        print(f"Report: {path.resolve()}", flush=True)
        if sender.cards:
            raise RuntimeError("Recovery unexpectedly delivered notifications")
        if any(item["state"] != "ok" for item in report):
            raise RuntimeError("Some channels are still unhealthy; inspect the saved report")
    finally:
        if worker:
            await worker.market_queue.stop()
            await worker.calc_queue.stop()
            await worker.data_sync.shutdown()
        stop.set()
        await heartbeat
        release_owner(db, CALC_OWNER, claim["owner"])
        for adapter in adapters.values():
            await adapter.aclose()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
