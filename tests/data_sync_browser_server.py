"""Isolated, in-memory application for browser acceptance (no live exchange)."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile

from fastapi import Body

from app.adapters.base import QuoteTick, TIMEFRAME_MS
from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Instrument, now_ms
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.services.data_sync import period_open
from app.web.api import create_app
from app.worker import Worker
from tests.conftest import make_candle

db = Database(":memory:")
settings = Settings()
settings.db_path = str(Path(tempfile.gettempdir()) / "levelframe-sync-browser-fixture" / "test.db")
settings.detector = DetectorConfig()
settings.detector.ltf_enabled = True
engine = LtfEngine(db, settings.detector)


class Exchange:
    async def klines(self, symbol, tf, start, end, include_forming=False):
        await asyncio.sleep(.15)
        iid = 1 if symbol == "BTCUSDT" else 2
        opening = period_open(tf, now_ms())
        rows = [make_candle(opening - i * TIMEFRAME_MS[tf], 100, 101, 99, 100,
                            tf, iid, source="binance") for i in range(32, 0, -1)]
        rows.append(make_candle(opening, 100, 101, 99, 100, tf, iid, closed=False, source="binance"))
        return [c for c in rows if start <= c.open_time <= end]

    async def last_price(self, symbol):
        await asyncio.sleep(.05)
        return QuoteTick(100, now_ms(), now_ms(), "trade")


app = create_app(db, settings, ltf_engine=engine)
worker = Worker(db, settings, settings.detector, {"binance": Exchange()},
                EventDispatcher(db, settings.detector, LogSender()),
                ltf_engine=engine, broadcast=app.state.ws_hub.broadcast)
app.state.data_sync = worker.data_sync
worker.data_sync.ready = True
for symbol in ("BTCUSDT", "ETHUSDT"):
    iid = db.upsert_instrument(Instrument(None, symbol[:-4], "binance", "spot", symbol, "USDT"))
    for tf in ("H1", "D1", "W1"):
        opening = period_open(tf, now_ms())
        rows = [make_candle(opening - i * TIMEFRAME_MS[tf], 100, 101, 99, 100,
                            tf, iid, source="binance") for i in range(32, 2, -1)]
        db.insert_candles(rows)
    engine.process_h1_close(iid)
    db.set_quote(iid, 100, now_ms())


@app.post("/__test/change/{iid}")
async def change(iid: int, price: float = 110):
    candle = db.last_candle(iid, "H1")
    db.insert_candles([replace(candle, high=max(111, price), close=price)])
    await worker._calculate(engine.process_h1_close, iid)
    return {"close": price, "open_time": candle.open_time}


@app.post("/__test/broadcast")
async def test_broadcast(message: dict = Body(...)):
    """Сырой WS-бroadcast через хаб (штампы epoch/seq/domain_versions — настоящие)."""
    app.state.ws_hub.broadcast(dict(message))
    return {"ok": True}


@app.post("/__test/add-zone/{iid}")
async def add_zone(iid: int, lower: float = 90.0, upper: float = 95.0):
    """Вставка зоны: реальный bump домена zones без WS-рассылки (проверка
    восстановления по diff'у версий статуса)."""
    from app.models import Direction, Zone, ZoneStatus, ZoneType
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=Direction.BEAR,
        timeframe="D1", lower=lower, upper=upper, formed_at=now_ms() - 10_000,
        confirmed_at=now_ms() - 9_000, status=ZoneStatus.ACTIVE))
    return {"zone_id": zid, "zones_version": db.get_domain("seq:zones")}


@app.post("/__test/close-candle/{iid}")
async def close_candle(iid: int, tf: str = "H1", close: float = 107, extra_revisions: int = 0):
    """Закрытие последнего бара без пересчёта движка: один bump домена свечей
    (+ extra_revisions дополнительных bump'ов для проверки разрыва версии)
    и WS-рассылка candle с OHLC-нагрузкой, как у worker._ws_closed_kline."""
    candle = db.last_candle(iid, tf, closed_only=False)
    for _ in range(extra_revisions):
        db.insert_candles([replace(candle, high=candle.high + 0.01)])
    db.insert_candles([replace(
        candle, close=close, closed=True,
        high=max(candle.high, close), low=min(candle.low, close))])
    app.state.ws_hub.broadcast({
        "type": "candle", "instrument_id": iid, "timeframe": tf,
        "open_time": candle.open_time, "open": candle.open,
        "high": max(candle.high, close), "low": min(candle.low, close),
        "close": close,
    })
    return {"open_time": candle.open_time,
            "candles_version": db.get_domain(f"seq:candles:{iid}:{tf}")}


# The production application mounts static files at "/" after all API routes;
# the __test endpoints above were registered later and would fall under the
# static mount (405) — move the mount back to the end.
static_mount = next(r for r in app.router.routes if getattr(r, "name", "") == "static")
app.router.routes.remove(static_mount)
app.router.routes.append(static_mount)
