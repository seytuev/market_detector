"""Этап 1 (эталоны v2): выгрузка реальных D1 OHLC для 8 альткоинов с Binance.

Эталонные примеры разметки — скриншоты владельца от 06.10.2026 (as_of).
Скачиваем D1-свечи от начала листинга каждого символа до закрытия дня as_of
(2026-10-06) через существующий адаптер app/adapters/binance.py
(контракт app/adapters/base.py: klines(symbol, timeframe, start_ms, end_ms)).

Результат: tests/fixtures/alt_v2/ohlc/<TICKER>.json — массив
[{open_time, open, high, low, close, volume}], open_time в ms UTC.

Примечание: контракт адаптера (app/models.py::Candle) не содержит объёма,
поэтому volume записывается как 0.0 — как и в существующем эталоне
tests/fixtures/btc_h4_etalon.json объём не сохраняется.

Если сеть/API недоступны (геоблок, нет интернета), скрипт завершается с кодом 2
и явным сообщением; в этом случае фикстуры берутся из
tests/fixtures/alt_v2/synthetic.py (см. README.md набора).

Запуск: .venv/Scripts/python.exe tools/fetch_alt_etalons.py
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.base import AdapterError  # noqa: E402
from app.adapters.binance import BinanceSpotAdapter  # noqa: E402
from app.config import load_settings  # noqa: E402

OUT_DIR = ROOT / "tests" / "fixtures" / "alt_v2" / "ohlc"

SYMBOLS = {
    "ONDO": "ONDOUSDT",
    "PUMP": "PUMPUSDT",
    "AAVE": "AAVEUSDT",
    "ENA": "ENAUSDT",
    "TAO": "TAOUSDT",
    "HBAR": "HBARUSDT",
    "SUI": "SUIUSDT",
    "NEAR": "NEARUSDT",
}

AS_OF = "2026-10-06"
# Конец данных: закрытие D1-свечи дня as_of (2026-10-07 00:00 UTC − 1 ms).
END_MS = int(
    datetime.datetime(2026, 10, 7, tzinfo=datetime.timezone.utc).timestamp() * 1000
) - 1


def ts(ms: int) -> str:
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d")


async def fetch_all() -> dict:
    settings = load_settings()
    adapter = BinanceSpotAdapter(settings.binance_base_url)
    summary: dict = {"as_of": AS_OF, "source": "binance_spot", "symbols": {}}
    try:
        for ticker, symbol in SYMBOLS.items():
            # start_ms=0: адаптер пагинирует от самой первой свечи листинга.
            candles = await adapter.klines(symbol, "D1", 0, END_MS)
            candles = [c for c in candles if c.closed]
            if not candles:
                raise AdapterError(f"binance: пустая история D1 по {symbol}")
            rows = [
                {
                    "open_time": c.open_time,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    # контракт Candle объёма не содержит — см. docstring
                    "volume": 0.0,
                }
                for c in candles
            ]
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            path = OUT_DIR / f"{ticker}.json"
            path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            summary["symbols"][ticker] = {
                "symbol": symbol,
                "candles": len(rows),
                "first": ts(rows[0]["open_time"]),
                "last": ts(rows[-1]["open_time"]),
                "file": str(path.relative_to(ROOT)),
            }
            print(
                f"{ticker:5s} {symbol:10s} {len(rows):5d} свечей "
                f"{summary['symbols'][ticker]['first']} .. "
                f"{summary['symbols'][ticker]['last']}"
            )
    finally:
        await adapter.aclose()
    return summary


def main() -> None:
    try:
        summary = asyncio.run(fetch_all())
    except AdapterError as e:
        print(f"ОШИБКА: загрузка с Binance невозможна: {e}", file=sys.stderr)
        print(
            "Используйте синтетические серии: tests/fixtures/alt_v2/synthetic.py "
            "(и отметьте источник в README.md).",
            file=sys.stderr,
        )
        sys.exit(2)
    report = OUT_DIR / "_fetch_report.json"
    report.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"отчёт: {report.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
