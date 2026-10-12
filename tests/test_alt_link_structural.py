"""Контрольный пример LINK по ТЗ 12.10.2026.

Движок читает только OHLC. Файл link_structural.json фиксирует измеренные
опоры и допуск проверки; в расчёт он не передаётся. Ориентиры 4.8–9.7 и
7–10.8 константами алгоритма не являются.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import pytest

from app.alt.engine_v2 import AltEngineV2
from app.config import AltConfig
from app.db import Database
from app.models_alt import (
    ALT_RULE_VERSION_V2,
    AltAsset,
    AltCandle,
    AltEpisodeState,
    AltInstrumentSource,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "alt_v2"
OHLC_PATH = FIXTURES / "ohlc" / "LINK.json"
MARKUP_PATH = FIXTURES / "link_structural.json"
DAY_MS = 86_400_000

FROZEN = (
    AltEpisodeState.MATURE.value,
    AltEpisodeState.ACCOMPANIMENT.value,
    AltEpisodeState.TERMINAL.value,
)


def utc_ms(year: int, month: int, day: int) -> int:
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def as_date(ms: int | None):
    if ms is None:
        return None
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).date()


def load_markup() -> dict:
    return json.loads(MARKUP_PATH.read_text(encoding="utf-8"))


def load_candles() -> list[AltCandle]:
    rows = json.loads(OHLC_PATH.read_text(encoding="utf-8"))
    return [
        AltCandle(
            source_id=1,
            open_time=r["open_time"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
        )
        for r in rows
    ]


def run_engine(candles: list[AltCandle]):
    db = Database(":memory:")
    db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=1975, symbol="LINK", name="Chainlink",
    ))
    earliest = candles[0].open_time if candles else 0
    db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="binance", symbol="LINKUSDT",
        earliest_available_ms=earliest, history_scope="full",
    ))
    AltEngineV2(db, AltConfig()).process_asset_history(1, 1, candles)
    rows = db.list_alt_range_episodes(1)
    db.close()
    return rows


def geometry(episode) -> tuple:
    return (
        episode.origin_key,
        episode.anchor_start_open_time,
        episode.base_start_open_time,
        round(episode.lower, 6),
        round(episode.upper, 6),
        round(episode.mid, 6),
        round(episode.width, 6),
        None if episode.wick_low is None else round(episode.wick_low, 6),
        None if episode.wick_high is None else round(episode.wick_high, 6),
        episode.rules_version,
    )


def find_anchor(rows, day: str):
    target = datetime.date.fromisoformat(day)
    found = [e for e in rows if as_date(e.anchor_start_open_time) == target]
    assert found, (
        day,
        [(e.state, as_date(e.anchor_start_open_time), e.lower, e.upper) for e in rows],
    )
    return found[0]


def cluster_prices(episode, side: str) -> list[float]:
    quality = json.loads(episode.quality_json)
    pivots = (quality.get("pair") or {}).get(f"{side}_cluster_pivots") or []
    return [float(p["price"]) for p in pivots]


def assert_supports(prices: list[float], expected: list[float]) -> None:
    for price in expected:
        assert any(abs(got - price) <= 0.001 for got in prices), (price, prices)


@pytest.fixture(scope="module")
def markup() -> dict:
    return load_markup()


@pytest.fixture(scope="module")
def candles() -> list[AltCandle]:
    return load_candles()


@pytest.fixture(scope="module")
def episodes(candles):
    return run_engine(candles)


def test_ohlc_checksum_matches_markup(markup):
    digest = hashlib.sha256(OHLC_PATH.read_bytes()).hexdigest()
    assert digest == markup["ohlc_sha256"]
    assert markup["rules_version"] == ALT_RULE_VERSION_V2


def test_link_two_bases_use_outer_wick_lines(markup, episodes):
    tol = markup["line_tolerance_abs"]
    assert tol <= 0.02
    for spec in markup["episodes"]:
        episode = find_anchor(episodes, spec["anchor_start"])
        assert episode.rules_version == ALT_RULE_VERSION_V2
        assert episode.state in FROZEN
        assert as_date(episode.base_end_open_time) == datetime.date.fromisoformat(
            spec["base_end"]
        )
        assert episode.base_end_reason == spec["base_end_reason"]
        assert episode.lower == pytest.approx(spec["lower"], abs=tol)
        assert episode.upper == pytest.approx(spec["upper"], abs=tol)
        assert episode.lower > spec["lower_must_exceed"]
        assert episode.upper < spec["upper_must_be_below"]
        assert episode.width == pytest.approx(episode.upper - episode.lower)
        assert episode.mid == pytest.approx((episode.upper + episode.lower) / 2)
        assert episode.detected_at_ms > episode.base_start_open_time
        lows = cluster_prices(episode, "lower")
        highs = cluster_prices(episode, "upper")
        assert_supports(lows, spec["lower_supports"])
        assert_supports(highs, spec["upper_supports"])
        assert all(price >= spec["forbidden_in_lower_cluster_below"] for price in lows)
        assert all(
            price < spec["forbidden_in_upper_cluster_at_or_above"] for price in highs
        )
        # Широкая полка: линия внешней полосы, не медиана всей цепочки.
        if spec.get("line_below_chain_median"):
            ordered = sorted(lows)
            chain = ordered[len(ordered) // 2]
            assert episode.lower < chain - 0.1


def test_decline_does_not_set_the_2022_frame(markup, episodes):
    spec = markup["separate_decline"]
    cutoff = datetime.date.fromisoformat(spec["anchor_before"])
    decline = [
        e for e in episodes
        if e.state in FROZEN
        and as_date(e.anchor_start_open_time) < cutoff
        and e.lower > spec["lower_above"]
    ]
    assert decline
    base = find_anchor(episodes, "2022-04-30")
    assert base.upper < min(e.lower for e in decline)


def test_prefix_before_october_breakout_keeps_frozen_geometry(candles, episodes):
    """Префикс до выхода октября 2023 не двигает уже замороженные L/U."""
    full = {
        e.origin_key: geometry(e)
        for e in episodes
        if e.state in FROZEN and e.anchor_start_open_time < utc_ms(2023, 10, 1)
    }
    assert any(as_date(e.anchor_start_open_time) == datetime.date(2022, 4, 30)
               for e in episodes)
    prefix = run_engine([c for c in candles if c.open_time < utc_ms(2023, 10, 1)])
    got = {e.origin_key: geometry(e) for e in prefix if e.state in FROZEN}
    for key, tup in got.items():
        assert key in full
        assert got[key] == full[key]
    april = find_anchor(prefix, "2022-04-30")
    assert april.lower == pytest.approx(5.41, abs=0.02)
    assert april.upper == pytest.approx(9.57, abs=0.02)
    assert april.state != AltEpisodeState.TERMINAL.value


def test_june_2023_sweep_is_not_required_to_form_the_line(candles, episodes):
    """Низ 4.76 июня 2023 лежит после freeze и линию не задаёт."""
    prefix = run_engine([c for c in candles if c.open_time < utc_ms(2023, 6, 1)])
    early = find_anchor(prefix, "2022-04-30")
    full = find_anchor(episodes, "2022-04-30")
    assert geometry(early) == geometry(full)
    assert early.lower > 5.0
    assert not any(price < 5.0 for price in cluster_prices(full, "lower"))
