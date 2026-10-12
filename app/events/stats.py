"""Честная статистика движка «События и время» (§7 ТЗ).

Историческая частота — именно частота, не прогнозная вероятность.
Числа не выдумываются: при подвыборке меньше min_n значение None и
текст ровно «Статистика недостаточна». Рядом — интервальная
неопределённость (Вильсон) и размер выборки.
"""
from __future__ import annotations

import math

MIN_N_DEFAULT = 100

INSUFFICIENT_TEXT = "Статистика недостаточна"


def wilson_interval(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Доверительный интервал Вильсона для доли hits/total."""
    if total <= 0:
        return (0.0, 1.0)
    p = hits / total
    z2 = z * z
    denom = 1 + z2 / total
    centre = (p + z2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _pct_text(value: float) -> str:
    return f"{round(value * 100, 1):g}"


def frequency(hits: int, total: int, *, min_n: int = MIN_N_DEFAULT, label: str = "") -> dict:
    """Историческая частота исхода с гейтом достаточности выборки.

    available только при total >= min_n; иначе value=None и текст
    «Статистика недостаточна» — никаких придуманных процентов (§7).
    """
    available = total >= min_n
    value = hits / total if available else None
    low, high = wilson_interval(hits, total) if available else (None, None)
    if available:
        text = (
            "В похожих условиях рост до конца дня наблюдался в "
            f"{_pct_text(value)}% случаев; n={total}"
        )
    else:
        text = INSUFFICIENT_TEXT
    return {
        "kind": "historical_frequency",
        "available": available,
        "value": value,
        "n": total,
        "numerator": hits,
        "denominator": total,
        "min_n": min_n,
        "uncertainty": {"low": low, "high": high} if available else None,
        "label": label,
        "text": text,
    }


def insufficient(reason: str, *, n: int = 0) -> dict:
    """Тот же контракт, что у frequency, но заведомо unavailable, с причиной."""
    return {
        "kind": "historical_frequency",
        "available": False,
        "value": None,
        "n": n,
        "numerator": 0,
        "denominator": n,
        "min_n": MIN_N_DEFAULT,
        "uncertainty": None,
        "label": "",
        "text": INSUFFICIENT_TEXT,
        "reason": reason,
    }
