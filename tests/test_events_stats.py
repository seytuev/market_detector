"""Честная статистика: гейт min_n, текст недостаточности, интервал Вильсона."""
from __future__ import annotations

import pytest

from app.events.stats import (
    MIN_N_DEFAULT,
    frequency,
    insufficient,
    wilson_interval,
)


def test_frequency_below_min_n_is_unavailable():
    res = frequency(30, 50)
    assert res["kind"] == "historical_frequency"
    assert res["available"] is False
    assert res["value"] is None
    assert res["uncertainty"] is None
    assert res["n"] == 50
    assert res["numerator"] == 30
    assert res["denominator"] == 50
    assert res["min_n"] == MIN_N_DEFAULT
    assert res["text"] == "Статистика недостаточна"


def test_frequency_at_min_n_boundary():
    assert frequency(99, 99)["available"] is False
    res = frequency(100, 100)
    assert res["available"] is True
    assert res["value"] == 1.0


def test_frequency_available_value_and_text():
    res = frequency(60, 100, label="четверг, рост до конца дня")
    assert res["available"] is True
    assert res["value"] == pytest.approx(0.6)
    assert res["label"] == "четверг, рост до конца дня"
    assert res["text"] == (
        "В похожих условиях рост до конца дня наблюдался в 60% случаев; n=100"
    )
    unc = res["uncertainty"]
    assert unc["low"] < res["value"] < unc["high"]


def test_frequency_percent_rounding():
    res = frequency(67, 150)
    assert res["value"] == pytest.approx(67 / 150)
    assert "44.7%" in res["text"]
    assert res["text"].endswith("n=150")


def test_frequency_custom_min_n():
    res = frequency(3, 5, min_n=5)
    assert res["available"] is True
    assert res["value"] == pytest.approx(0.6)
    assert res["min_n"] == 5


def test_wilson_interval_ordered_and_contains_value():
    low, high = wilson_interval(60, 100)
    assert 0.0 <= low < high <= 1.0
    assert low < 0.6 < high
    # Пустая выборка — максимальная неопределённость, без NaN.
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_insufficient_same_shape_with_reason():
    res = insufficient("нет истории корзины", n=12)
    assert res["kind"] == "historical_frequency"
    assert res["available"] is False
    assert res["value"] is None
    assert res["uncertainty"] is None
    assert res["n"] == 12
    assert res["text"] == "Статистика недостаточна"
    assert res["reason"] == "нет истории корзины"
