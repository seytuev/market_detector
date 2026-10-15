"""Local read-only ALTs preview from saved fixtures; no worker/network feeds."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alt.engine_v3 import analyze
from app.alt.store_v3 import persist_result
from app.config import Settings
from app.db import Database
from app.models_alt import AltAsset, AltCandle, AltInstrumentSource
from app.web.api import create_app


def create_preview():
    settings = Settings()
    settings.auth_token = "alt-v3-local-preview"
    settings.public_base_url = ""
    db = Database(":memory:")
    path = Path(__file__).resolve().parents[1]/"tests/fixtures/alt_v2/ohlc/NEAR.json"
    rows = json.loads(path.read_text())
    asset = db.upsert_alt_asset(AltAsset(id=None, cmc_id=6535, symbol="NEAR", name="Near", cmc_rank=30, mapping_status="mapped"))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(id=None, asset_id=asset.id, venue="binance", symbol="NEARUSDT", history_scope="full"))
    db.insert_alt_candles([AltCandle(source_id=src.id, **{k:r[k] for k in ("open_time","open","high","low","close")}) for r in rows])
    persist_result(db, asset.id, src.id, analyze(rows), backfill=True)
    return create_app(db, settings)


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8891)
    args = parser.parse_args()
    uvicorn.run(create_preview(), host="127.0.0.1", port=args.port, log_level="warning")
