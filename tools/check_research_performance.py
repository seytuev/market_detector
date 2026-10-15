"""Reproducible local read benchmark; uses only a disposable SQLite database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time

from app.config import Settings
from app.db import Database
from app.models import Instrument
from app.research.catalysts import Catalysts, DAY
from app.research.contracts import encode
from app.research.store import Store


def main():
    moment=int(datetime(2026,10,15,12,tzinfo=timezone.utc).timestamp()*1000)
    cfg=Settings(research_enabled=True)
    with tempfile.TemporaryDirectory(prefix="research-perf-") as directory, closing(Database(str(Path(directory)/"benchmark.db"))) as db:
        s=Store(db,clock=lambda:moment)
        assets=[]; instruments=[]
        for i in range(10):
            asset=s.create_asset({"display_symbol":"BENCH"+str(i),"name":"Benchmark "+str(i),
                "external_ids":[{"namespace":"benchmark","external_id":str(i)}]})["asset_uid"]
            instrument=db.upsert_instrument(Instrument(None,"BENCH"+str(i),"binance","spot","BENCH"+str(i)+"USDT","USDT"))
            s.link(instrument,{"asset_uid":asset,"reason":"Disposable benchmark identity"})
            assets.append(asset); instruments.append(instrument)
        with db.batch_writes():
            for i in range(10000):
                identifier="m"+str(i); revision=identifier+"-9"
                published=moment-(i%1000)*60000
                s.execute("INSERT INTO research_material VALUES(?,?,?,?,?,?,?)",
                          (identifier,"benchmark",identifier,revision,"active",published,moment-DAY))
                for version in range(10):
                    rid=identifier+"-"+str(version)
                    payload=encode({"title":"Source publication "+str(i),"source_kind":"telegram","author":"benchmark",
                        "source_id":"source"+str(i%7),"canonical_url":"https://example.org/material/"+str(i),
                        "origin_material_id":identifier,"original_text":"Evidence retained in archive "*10})
                    s.execute("INSERT INTO research_material_revision VALUES(?,?,?,?,?,?,?,?,?)",
                              (rid,identifier,moment-10000+version,version,published,"telegram","active",rid,payload))
                    s.execute("INSERT INTO research_material_asset VALUES(?,?,?,?,?)",
                              (rid,assets[i%10],"subject","verified","{}"))
                analysis=encode({"summary":"Archived source analysis","claims":[],"opinions":[],"risks":[],"unknowns":[]})
                s.execute("INSERT INTO research_analysis VALUES(?,?,?,?,?)",("a"+str(i),revision,moment-1,1,analysis))
        template=Catalysts(s).save({"title":"Benchmark event","kind":"upgrade","stage":"announced","scope":"asset",
            "asset_uids":[assets[0]],"precision":"day","date_label":"2026-10-20","timezone":"UTC"})
        with db.batch_writes():
            for i in range(1999):
                identifier="c"+str(i); revision=identifier+"-1"
                value={**template,"id":identifier,"revision_id":revision,"asset_uids":[assets[i%10]]}
                s.execute("INSERT INTO research_catalyst VALUES(?,?,?,?,NULL)",(identifier,revision,moment,"accepted"))
                s.execute("INSERT INTO research_catalyst_revision VALUES(?,?,?,?,?,?,?,?,?,?,?)",(revision,identifier,moment,
                    value["kind"],value["scope"],value["stage"],value["precision"],value["starts_at"],value["ends_at"],
                    value["review_status"],encode(value)))
                s.execute("INSERT INTO research_catalyst_asset VALUES(?,?,?)",(revision,assets[i%10],"direct"))
        def read(index):
            start=time.perf_counter()
            with db.read_tx():
                result=s.context(instruments[index%10],cfg)
            return (time.perf_counter()-start)*1000,len(encode(result).encode())
        for i in range(10): read(i)
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=5) as pool:
            measurements=list(pool.map(read,range(100)))
        times=sorted(value[0] for value in measurements)
        result={"runtime":platform.python_version(),"platform":platform.platform(),"cpu_count":os.cpu_count(),
                "backend":"SQLite","materials":10000,"material_revisions":100000,"catalysts":2000,
                "concurrency":5,"warmup":10,"requests":100,"duration_s":round(time.perf_counter()-started,3),
                "p50_ms":round(statistics.median(times),2),"p95_ms":round(times[94],2),
                "max_payload_bytes":max(value[1] for value in measurements),
                "scope":"Repository read with native algorithm cache; excludes HTTP and market worker baseline"}
    destination=Path("tmp/research-performance.json")
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
