"""Bounded subprocess entry point. Input/output contain frozen public research data."""
import json
import os
import sys
from .lab_compute import compute


def replay_factors(payload):
    """Reuse the production HTF scanner on prefixes, entirely in disposable storage."""
    data=payload["dataset"];spec=payload["spec"]
    if spec.get("technical_filter")!="in_htf_zone":return
    from contextlib import closing
    from ..config import DetectorConfig
    from ..db import Database
    from ..engine.scanner import Scanner
    from ..models import Candle,Instrument,ZoneStatus
    for episode in data["episodes"]:
        bars=[b for b in data["d1"].get(str(episode["instrument_id"]),[]) if b["close_boundary"]<=episode["decision_at"] and b["valid"]]
        if len(bars)<100:episode["technical_factor"]=None;continue
        with closing(Database()) as db:
            instrument=Instrument(None,"REPLAY","binance","spot","REPLAYUSDT","USDT");identifier=db.upsert_instrument(instrument)
            db.insert_candles([Candle(identifier,"D1",b["open_time"],b["close_boundary"]-1,b["open"],b["high"],b["low"],b["close"],True,b.get("source","archive")) for b in bars])
            Scanner(db,DetectorConfig(**data["detector_config"])).replay_instrument(identifier,timeframes={"D1"})
            known=[b for b in data["h1"].get(str(episode["instrument_id"]),[]) if b["close_boundary"]<=episode["decision_at"] and b["valid"]]
            price=known[-1]["close"] if known else None
            episode["technical_factor"]=any(z.status in {ZoneStatus.ACTIVE,ZoneStatus.CANDIDATE,ZoneStatus.WEAKENED} and z.confirmed_at is not None and z.lower<=price<=z.upper for z in db.get_zones(identifier)) if price is not None else None


def main():
    if os.name!="nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU,(60,60))
    payload=json.load(sys.stdin)
    replay_factors(payload)
    result=compute(payload)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False))


if __name__=="__main__":main()
