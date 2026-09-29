"""Общие хелперы тестов движка: явное конструирование свечей, БД в памяти."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.models import Candle, Instrument, TIMEFRAME_MINUTES

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "btc_h4_etalon.json"

# Константы эталона BTCUSDT Binance H4 (спека §4, приёмка §13.19)
ETALON_BASE_OPEN_TIMES = [1780416000000, 1780430400000, 1780444800000, 1780459200000]
ETALON_L = 65426.34
ETALON_U = 68146.30
ETALON_M = 66786.32
ETALON_FVG_INTERNAL_1 = (66373.18, 66656.48, 1780502400000)  # L, U, formed_at
ETALON_FVG_INTERNAL_2 = (65860.00, 66076.00, 1780516800000)
ETALON_FVG_EXTERNAL = (64540.30, 65251.00, 1780531200000)
ETALON_CONFIRMED_AT = 1780545600000  # граница 2026-06-04 04:00 UTC


def make_candle(
    open_time: int,
    o: float,
    h: float,
    l: float,
    c: float,
    timeframe: str = "D1",
    instrument_id: int = 1,
    closed: bool = True,
    source: str = "test",
) -> Candle:
    """Явная синтетическая свеча; close_time — последняя ms интервала."""
    tf_ms = TIMEFRAME_MINUTES[timeframe] * 60_000
    return Candle(
        instrument_id=instrument_id, timeframe=timeframe, open_time=open_time,
        close_time=open_time + tf_ms - 1, open=o, high=h, low=l, close=c,
        closed=closed, source=source,
    )


def load_etalon_candles(instrument_id: int = 1) -> list[Candle]:
    """30 свечей BTCUSDT Binance H4, 1–5 июня 2026 UTC (спека §4)."""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [
        Candle(
            instrument_id=instrument_id, timeframe="H4",
            open_time=r["open_time"], close_time=r["close_time"],
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            closed=True, source="binance-spot",
        )
        for r in raw
    ]


@pytest.fixture
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


H1_MS = 3_600_000


def make_h1_candles(
    bars: list[tuple[float, float, float, float]],
    start_ms: int,
    instrument_id: int = 1,
) -> list[Candle]:
    """Часовые свечи из кортежей (open, high, low, close) с шагом 1ч."""
    return [
        make_candle(start_ms + i * H1_MS, o, h, l, c,
                    timeframe="H1", instrument_id=instrument_id)
        for i, (o, h, l, c) in enumerate(bars)
    ]


@pytest.fixture
def cfg() -> DetectorConfig:
    return DetectorConfig()


@pytest.fixture
def instrument_id(db: Database) -> int:
    return db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
