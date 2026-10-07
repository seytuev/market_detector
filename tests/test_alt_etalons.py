"""Этап 1 эталонов v2 (спека docs/Altcoins_Range_Engine_Spec_RU_2026_10_07.md,
§2/§5): проверка разметки и OHLC-фикстур tests/fixtures/alt_v2/.

Проверяется: markup.json валиден (L<U, min≤max, даты парсятся и упорядочены);
для каждого из 8 тикеров есть OHLC-серия (реальная Binance либо синтетика),
она непустая, отсортирована, OHLC корректен (high≥low, цены положительные)
и покрывает окна эпизодов разметки. Тест самодостаточен: приложение не
импортируется, синтетика подгружается из файла фикстур напрямую.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "alt_v2"
MARKUP_PATH = FIXTURES / "markup.json"
OHLC_DIR = FIXTURES / "ohlc"

DAY_MS = 86_400_000
TICKERS = ["NEAR", "SUI", "HBAR", "TAO", "ENA", "PUMP", "AAVE", "ONDO"]
ROLES = {"historical_base", "current_base", "sweep", "late_consolidation_candidate"}
EVENT_TYPES = {
    "breakout", "retest", "sweep_below_L", "return_to_base",
    "reaction_at_L", "reaction_at_U", "accompaniment", "impulse_exit",
}


def parse_date(s: str) -> datetime.date:
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def date_to_ms(s: str) -> int:
    dt = datetime.datetime.strptime(s, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)


def ms_to_date(ms: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).date()


def load_synthetic_module():
    spec = importlib.util.spec_from_file_location(
        "alt_v2_synthetic", FIXTURES / "synthetic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def markup() -> dict:
    return json.loads(MARKUP_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def as_of(markup) -> datetime.date:
    return parse_date(markup["as_of"])


def load_ohlc(ticker: str) -> tuple[list[dict], str]:
    """(серия, источник): реальный файл ohlc/<T>.json, иначе синтетика."""
    path = OHLC_DIR / f"{ticker}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), "binance"
    return load_synthetic_module().generate(ticker), "synthetic"


# ------------------------- разметка -------------------------


def test_markup_as_of(markup, as_of):
    assert as_of == datetime.date(2026, 10, 6)


def test_markup_tickers(markup):
    assert set(markup["tickers"].keys()) == set(TICKERS)
    for ticker in TICKERS:
        entry = markup["tickers"][ticker]
        assert entry["symbol"] == f"{ticker}USDT"
        assert entry["episodes"], f"{ticker}: нет эпизодов"


def test_markup_episodes_valid(markup, as_of):
    for ticker in TICKERS:
        for ep in markup["tickers"][ticker]["episodes"]:
            where = f"{ticker}/{ep['id']}"
            assert ep["role"] in ROLES, f"{where}: роль {ep['role']!r}"
            start, end = parse_date(ep["start"]), parse_date(ep["end"])
            assert start < end, f"{where}: start >= end"
            assert end <= as_of, f"{where}: эпизод выходит за as_of"
            low, up = ep["L"], ep["U"]
            assert low["min"] <= low["max"], f"{where}: L.min > L.max"
            assert up["min"] <= up["max"], f"{where}: U.min > U.max"
            assert low["min"] > 0, f"{where}: L не положительна"
            assert low["max"] < up["min"], f"{where}: L >= U"
            for ev in ep.get("events", []):
                ewhere = f"{where}/event:{ev['type']}"
                assert ev["type"] in EVENT_TYPES, f"{ewhere}: неизвестный тип"
                df, dt = parse_date(ev["date_from"]), parse_date(ev["date_to"])
                assert df <= dt, f"{ewhere}: date_from > date_to"


# ------------------------- OHLC-серии -------------------------


@pytest.mark.parametrize("ticker", TICKERS)
def test_ohlc_series(ticker, markup, as_of):
    rows, source = load_ohlc(ticker)
    assert rows, f"{ticker}: пустая серия ({source})"

    times = [r["open_time"] for r in rows]
    assert times == sorted(times), f"{ticker}: серия не отсортирована"
    assert len(set(times)) == len(times), f"{ticker}: дубликаты open_time"

    for r in rows:
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        assert min(o, h, l, c) > 0, f"{ticker}: неположительная цена"
        assert h >= l, f"{ticker}: high < low на {r['open_time']}"
        assert h >= max(o, c) and l <= min(o, c), (
            f"{ticker}: OHLC вне диапазона на {r['open_time']}"
        )

    # покрытие: серия начинается не позже самого раннего эпизода
    # и доходит до as_of
    episodes = markup["tickers"][ticker]["episodes"]
    first_ep = min(parse_date(ep["start"]) for ep in episodes)
    assert ms_to_date(times[0]) <= first_ep, (
        f"{ticker}: серия ({ms_to_date(times[0])}) позже первого эпизода ({first_ep})"
    )
    last_date = ms_to_date(times[-1])
    assert as_of - datetime.timedelta(days=2) <= last_date <= as_of, (
        f"{ticker}: последняя свеча {last_date}, ожидалось около as_of {as_of}"
    )

    # окна эпизодов (и событий) лежат внутри диапазона серии
    series_start, series_end = ms_to_date(times[0]), last_date + datetime.timedelta(days=1)
    for ep in episodes:
        assert series_start <= parse_date(ep["start"]), f"{ticker}/{ep['id']}: start вне серии"
        assert parse_date(ep["end"]) <= series_end, f"{ticker}/{ep['id']}: end вне серии"
        for ev in ep.get("events", []):
            ev_end = parse_date(ev["date_to"])
            assert ev_end <= as_of + datetime.timedelta(days=1), (
                f"{ticker}/{ep['id']}: событие {ev['type']} за пределами as_of"
            )


def test_synthetic_fallback_structural():
    """Синтетика (резерв при недоступности Binance) генерируется для всех
    тикеров, детерминирована и покрывает as_of."""
    mod = load_synthetic_module()
    for ticker in TICKERS:
        a = mod.generate(ticker)
        b = mod.generate(ticker)
        assert a == b, f"{ticker}: синтетика не детерминирована"
        assert a, f"{ticker}: пустая синтетика"
        assert ms_to_date(a[-1]["open_time"]) == datetime.date(2026, 10, 6)
