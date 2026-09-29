"""Standalone repro review_zone против копии БД: где висит POST /review."""
import faulthandler
import sys
import time

faulthandler.dump_traceback_later(30, repeat=True)

from app.config import load_detector_config, load_settings
from app.db import Database
from app.engine.scanner import Scanner

db_path = sys.argv[1]
settings = load_settings()
settings.db_path = db_path
settings.detector = load_detector_config()
db = Database(db_path)

zone = db.get_zone(164385)
print("zone:", zone.id, zone.status, zone.timeframe, flush=True)

t0 = time.time()
print("replay start", flush=True)
Scanner(db, settings.detector).replay_instrument(
    zone.instrument_id, timeframes={zone.timeframe}
)
print("replay done in", time.time() - t0, flush=True)

t0 = time.time()
candles = db.get_candles(zone.instrument_id, zone.timeframe)
print("candles:", len(candles), "in", time.time() - t0, flush=True)
print("OK", flush=True)
