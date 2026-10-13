"""Reproducible SQL-tail benchmark; creates only a fresh synthetic database."""
import hashlib
import json
import platform
import statistics
import sys
import time
import uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.db import Database
from app.models import Instrument

root = (ROOT / "tmp").resolve()
root.mkdir(exist_ok=True)
target = (root / f"storage-benchmark-{uuid.uuid4().hex}.db").resolve()
assert target.is_relative_to(root) and not target.exists()
db = Database(str(target))
iid = db.upsert_instrument(Instrument(id=None,asset="BTC",venue="binance",market_type="spot",symbol="BTCUSDT",quote_asset="USDT"))
report = {"python":sys.version,"platform":platform.platform(),"mode":"synthetic, warm OS cache; SQLite cache released between samples","cases":[]}
previous=0
try:
    for count in (100_000,1_000_000):
        started=time.perf_counter()
        with db.batch_writes():
            db.conn.executemany("INSERT INTO candle(instrument_id,timeframe,open_time,close_time,open,high,low,close,closed,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ((iid,"H1",i*3600_000,(i+1)*3600_000-1,100,101,99,100,1,"benchmark") for i in range(previous,count)))
            db.bump_state_seq()
        previous=count
        load=time.perf_counter()-started
        durations=[]
        for _ in range(30):
            db.conn.execute("PRAGMA shrink_memory")
            at=time.perf_counter()
            candles=db.get_candles(iid,"H1",limit=300)
            durations.append((time.perf_counter()-at)*1000)
            assert len(candles)==300 and candles[0].open_time==(count-300)*3600_000
        plan=[str(tuple(row)) for row in db.conn.execute("EXPLAIN QUERY PLAN SELECT * FROM candle WHERE instrument_id=? AND timeframe=? AND closed=1 ORDER BY open_time DESC LIMIT 300",(iid,"H1"))]
        parameters={"count":count,"timeframe":"H1","open_time_formula":"i*3600000","ohlc":[100,101,99,100]}
        report["cases"].append({"rows":count,"fixture_load_seconds":load,"samples":30,"p50_ms":statistics.median(durations),"p95_ms":sorted(durations)[28],"max_ms":max(durations),"query_plan":plan,"dataset_sha256":hashlib.sha256(json.dumps(parameters,sort_keys=True).encode()).hexdigest()})
finally:
    db.close()
out=ROOT / "docs/audit/stabilization-2026-10-13"
out.mkdir(parents=True,exist_ok=True)
(out / "storage-tail-benchmark.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
