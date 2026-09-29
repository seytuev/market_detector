"""Разовая догрузка HTF-истории по раздельным окнам (D1 — год, W1 — 2 года).

Запуск из корня проекта: .venv/Scripts/python tools/backfill_htf_history.py
Идёт в рабочую БД (HTF_DB_PATH), конфиг детектора — как у сервиса:
ENV HTF_DET_* < data/settings.json. Доставка — в лог (LogSender).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.binance import BinanceSpotAdapter
from app.adapters.hyperliquid import HyperliquidSpotAdapter
from app.config import load_detector_config, load_settings
from app.db import Database
from app.notify.queue import EventDispatcher
from app.notify.telegram import LogSender
from app.web.api import _load_detector_from_file
from app.worker import Worker

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> None:
    settings = load_settings()
    settings.detector = load_detector_config()
    _load_detector_from_file(settings)  # файл приоритетнее, как у сервиса
    cfg = settings.detector
    print(
        f"lookback: D1={cfg.lookback_days_for('D1')}д, "
        f"W1={cfg.lookback_days_for('W1')}д, TF={cfg.scan_timeframes}"
    )

    db = Database(settings.db_path)
    adapters = {
        "binance": BinanceSpotAdapter(settings.binance_base_url),
        "hyperliquid": HyperliquidSpotAdapter(settings.hyperliquid_base_url),
    }
    charts_dir = str(Path(settings.db_path).parent / "charts")
    dispatcher = EventDispatcher(db, cfg, LogSender(), charts_dir=charts_dir)
    worker = Worker(db, settings, cfg, adapters, dispatcher)

    for ins in db.get_instruments(enabled_only=True):
        print(f"backfill {ins.venue}:{ins.symbol} ...")
        await worker.backfill(ins)
    print("готово")


if __name__ == "__main__":
    asyncio.run(main())
