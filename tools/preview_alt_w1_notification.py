"""Render an illustrative HBAR touch in an admitted base; no Telegram calls.

The old .08503 FVG belonged to a rejected alt-0.5 base. The default .045
is a formatting example inside a valid 2022-cycle FVG, not a live signal.
"""
from pathlib import Path
import argparse
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alt.engine_v3 import analyze, DAY
from app.alt.store_v3 import persist_result, list_structures, insert_event
from app.db import Database
from app.models_alt import AltAsset, AltCandle, AltInstrumentSource
from app.notify.alt_w1 import load_context, message, render_chart


def example(db, touch_price=.045):
    rows = json.loads((Path(__file__).resolve().parents[1]/"tests/fixtures/alt_v2/ohlc/HBAR.json").read_text())
    asset = db.upsert_alt_asset(AltAsset(None, 4642, "HBAR", name="Hedera", cmc_rank=25))
    src = db.upsert_alt_instrument_source(AltInstrumentSource(None, asset.id, "binance", "HBARUSDT", history_scope="full"))
    db.insert_alt_candles([AltCandle(source_id=src.id, **{k:r[k] for k in ("open_time","open","high","low","close")}) for r in rows])
    persist_result(db, asset.id, src.id, analyze(rows), backfill=True)
    at = int(datetime(2026, 10, 15, 20, 53, tzinfo=timezone.utc).timestamp()*1000)
    candidates = [(f, z) for f in list_structures(db, asset.id, src.id, at)
                  for z in f["scenario_state"].get("pd_zones", [])
                  if z["kind"] == "FVG" and z["invalidated_at_ms"] is None and z["lower"] <= touch_price <= z["upper"]]
    if not candidates:
        raise ValueError("No admitted base has an active FVG at the requested illustrative price")
    f, z = max(candidates, key=lambda item:item[0]["base_start_open_time"])
    event = {"id": "hbar-local-preview", "event_type": "retest", "event_time_ms": at, "available_at_ms": at,
        "price": touch_price, "payload": {"candle_open_time": at//DAY*DAY, "kind": "FVG", "zone_id": z["id"],
        "zone": {"lower": z["lower"], "upper": z["upper"]}, "precision": "trade", "timeframe": "W1"}}
    insert_event(db, asset.id, src.id, f["structure_key"], event)
    saved = db.conn.execute("SELECT * FROM alt_v3_event WHERE delivered=0 ORDER BY id").fetchall()
    return load_context(db, saved), saved


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/alt_w1_notification_preview.png"))
    args = parser.parse_args()
    db = Database(":memory:")
    try:
        ctx, _ = example(db)
        text, details = message(ctx, delivered_at=ctx.lead["event_time_ms"])
        output = render_chart(db, ctx, args.output)
        args.output.with_suffix(".txt").write_text(text+"\n\n"+details, encoding="utf-8")
        print(text)
        print("IMAGE="+str(output))
    finally:
        db.close()
