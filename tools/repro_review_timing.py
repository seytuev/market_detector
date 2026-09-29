"""Замер стоимости одного on_closed_candle на полной истории D1 (копия БД)."""
import sys
import time

from app.config import load_detector_config
from app.db import Database
from app.engine.scanner import Scanner
from app.models import ZoneType

db = Database(sys.argv[1])
cfg = load_detector_config()
sc = Scanner(db, cfg)

candles = db.get_candles(1, "D1")
print("D1 candles:", len(candles), flush=True)

t0 = time.time()
zs_fvg = db.get_zones(1, types=[ZoneType.FVG])
t1 = time.time()
print("get_zones(FVG):", len(zs_fvg), f"{t1-t0:.3f}s", flush=True)

t0 = time.time()
zs_ob = db.get_zones(1, types=[ZoneType.OB])
t1 = time.time()
print("get_zones(OB):", len(zs_ob), f"{t1-t0:.3f}s", flush=True)

from app.engine import fvg as fvg_mod  # noqa
import app.engine.scanner as sc_mod
from app.engine.fvg import scan_fvgs

t0 = time.time()
fvgs = scan_fvgs(candles, "D1")
t1 = time.time()
print("scan_fvgs full:", len(fvgs), f"{t1-t0:.3f}s", flush=True)

# стоимость одного _insert_zone для уже существующей зоны (дедуп-путь)
z = zs_fvg[0]
t0 = time.time()
for _ in range(10):
    sc._insert_zone(z)
t1 = time.time()
print("insert_zone(existing) x10:", f"{t1-t0:.3f}s", flush=True)

# один on_closed_candle на последней свече
last = candles[-1]
t0 = time.time()
sc.on_closed_candle(last)
t1 = time.time()
print("on_closed_candle(last):", f"{t1-t0:.3f}s", flush=True)
