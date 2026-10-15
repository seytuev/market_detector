"""Disposable workbench read benchmark and optional frozen real-history replay."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time
from app.db import Database
from app.models import Instrument,Candle
from app.research.store import Store
from app.research.records import Records
from app.research.contracts import encode
from app.research.lab import Lab
from app.research.strength import Strength
from app.research.market import DAY,HOUR


def measure(fn):
    for _ in range(3):fn()
    values=[]
    for _ in range(30):
        start=time.perf_counter();fn();values.append((time.perf_counter()-start)*1000)
    return {"p50_ms":round(statistics.median(values),2),"p95_ms":round(sorted(values)[28],2),"requests":30}


def benchmark(directory):
    with closing(Database(str(Path(directory)/"benchmark.db"))) as db:
        now=1792100000000;s=Store(db,clock=lambda:now);r=Records(s)
        with db.batch_writes():
            for kind,count in (("idea",5000),("example",10000),("rule",100),("match",50000),("basket",100)):
                for i in range(count):
                    identifier=kind+str(i);revision=identifier+"-1"
                    value={"id":identifier,"revision_id":revision,"title":"Benchmark "+identifier,"status":"watching" if kind=="idea" else "draft",
                           "created_at":now-i,"available_at":now-i,"section_id":"section1","text":"Original research evidence "*20}
                    payload=encode(value)
                    s.execute("INSERT INTO research_object VALUES(?,?,?,?,?,?,?,?,?,?)",(identifier,kind,revision,value["status"],None,value["title"],now-i,now-i,None,payload))
                    s.execute("INSERT INTO research_revision VALUES(?,?,NULL,?,?)",(revision,identifier,now-i,payload))
            for i in range(50000):
                s.execute("INSERT INTO research_note VALUES(?,?,?,NULL,?,?,?)",("note"+str(i),"idea"+str(i%5000),"review",now-i,str(i),encode({"conclusion":"Review of the frozen source"})))
            for i in range(100000):
                s.execute("INSERT INTO research_series VALUES(?,?,?,?,?,?,?)",("point"+str(i),"basket_observation","segment"+str(i%100),now-i*HOUR,now,str(i),encode({"index_value":100+i/1000,"at":now-i*HOUR})))
        result={"backend":"SQLite","platform":platform.platform(),"scope":"Repository reads, no HTTP; isolated from production worker",
                "counts":{"ideas":5000,"reviews":50000,"examples":10000,"rules":100,"matches":50000,"baskets":100,"observations":100000}}
        result["reads"]={kind:measure(lambda k=kind:r.list(k,limit=100)) for kind in ("idea","example","rule","match","basket")}
        result["reads"]["reviews"]=measure(lambda:r.notes("idea1"))
        result["reads"]["basket_history"]=measure(lambda:s.rows("SELECT payload FROM research_series WHERE kind='basket_observation' AND object_id=? ORDER BY at_ms,id LIMIT 100",("segment1",)))
        instruments=[];boundary=now//DAY*DAY
        for i in range(200):
            symbol="BTC" if i==0 else "BENCH"+str(i)
            identifier=db.upsert_instrument(Instrument(None,symbol,"binance","spot",symbol+"USDT","USDT"));instruments.append(identifier)
            db.insert_candles([Candle(identifier,"D1",boundary-(1095-k)*DAY,boundary-(1094-k)*DAY-1,100+k+i,102+k+i,99+k+i,101+k+i,True,"benchmark") for k in range(1095)])
        started=time.perf_counter();strength=Strength(s);snapshot=strength.calculate(instruments,instruments[0])
        result["strength"]={"instruments":200,"stored_days":1095,"calculate_s":round(time.perf_counter()-started,3),"read":measure(lambda:strength.list(snapshot_id=snapshot["id"],limit=100))}
        return result


def real_history(source,directory):
    target=str(Path(directory)/"history.db")
    # All experimental writes stay in this consistent temporary copy.
    with closing(sqlite3.connect(str(Path(source).resolve().as_uri())+"?mode=ro",uri=True)) as origin,closing(sqlite3.connect(target)) as backup:origin.backup(backup)
    with closing(Database(target)) as db:
        s=Store(db);lab=Lab(s)
        rows=s.rows("SELECT i.id,MAX(c.open_time) AS last_at FROM instrument i JOIN candle c ON c.instrument_id=i.id WHERE i.asset='BTC' AND i.market_type='spot' AND i.quote_asset='USDT' AND c.timeframe='H1' AND c.closed=1 GROUP BY i.id ORDER BY last_at DESC")
        if not rows:return {"status":"unavailable","reason":"No BTC spot USDT H1 history"}
        identifier=rows[0]["id"];end=(rows[0]["last_at"]+HOUR)//DAY*DAY;start=end-30*DAY
        experiment=lab.experiment({"title":"Frozen actual BTC history verification","question":"Does repeated execution preserve results on the same actual market history?","experiment_type":"simulation","from_at":start,"to_at":end,"instrument_ids":[identifier],"benchmark_instrument_id":identifier})
        strategy=lab.strategy({"title":"Frozen breakout reference","config":{"universe":{"type":"manual_list","tickers":["BTC"]},
            "entry":{"op":"AND","rules":[{"template":"breakout","params":{"lookback_days":20}}]},"exit":{"op":"OR","rules":[{"template":"take_profit","params":{"take_profit_pct":20}}]},
            "risk":{"max_position_pct":50,"max_positions":1,"stop_loss_pct":10,"time_stop":168,"max_drawdown_kill_pct":50,"cooldown_days":1},"costs":{"fee_bps":10,"slippage_bps":5},"params":{}}})
        data=lab.prepare(experiment["id"]);outputs=[];durations=[]
        for _ in range(2):
            run=lab.create_run({"dataset_id":data["id"],"strategy_id":strategy["id"]});started=time.perf_counter();value=lab.run(run["id"])
            durations.append(round(time.perf_counter()-started,3));outputs.append(value)
        hashes=[v.get("result",{}).get("result_hash") for v in outputs]
        return {"status":[v["status"] for v in outputs],"instrument_id":identifier,"from_at":start,"to_at":end,"rows":data["rows"],"coverage":data["coverage"],
                "data_mode":data["data_mode"],"duration_s":durations,"result_hashes":hashes,"reproducible":bool(hashes[0]) and hashes[0]==hashes[1],"metrics":outputs[0].get("result",{}).get("metrics"),"diagnostic":outputs[0].get("error")}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--history-database");args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="research-workbench-") as directory:
        result=benchmark(directory)
        if args.history_database:result["real_history"]=real_history(args.history_database,directory)
    destination=Path("tmp/research-workbench-performance.json");destination.parent.mkdir(exist_ok=True);destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":main()
