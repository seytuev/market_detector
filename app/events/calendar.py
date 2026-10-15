"""Conditional day statistics. Each target uses only strictly earlier closed days.

These are separate descriptive samples, never votes in the market direction.
Historical selections are reconstructions, not previously published forecasts.
"""
from __future__ import annotations

import math
from statistics import mean, median

from .mathutil import DAY_MS
from .stats import wilson_interval
from .timeutil import WEEKDAY_RU, msk_day_start_ms, msk_dt, msk_weekday
from .validation import complete_window, hourly_bars

CALENDAR_VERSION = "calendar-msk-1.0"
BTC_SERIES = "calendar_btc_msk_day"
# Lower bound of this project's BTC sample, using full Moscow days.
BTC_HISTORY_START = 1503003600000  # 2017-08-18 00:00 MSK
BACKFILL_DAYS = 180


def daily_prices(bars: list[dict], *, now: int) -> list[dict]:
    indexed = hourly_bars(bars)
    if not indexed:
        return []
    first = msk_day_start_ms(min(indexed))
    today = msk_day_start_ms(now)
    rows = []
    for start in range(first, today, DAY_MS):
        segment = complete_window(indexed, start, start + DAY_MS)
        if not segment:
            continue
        open_, close = float(segment[0]["open"]), float(segment[-1]["close"])
        has_range = all("high" in b and "low" in b for b in segment)
        rows.append({"bucket_start": start, "return": close / open_ - 1,
                     "range": ((max(float(b["high"]) for b in segment)
                                - min(float(b["low"]) for b in segment)) / open_) if has_range else None,
                     "complete": True, "method_version": CALENDAR_VERSION})
    return rows


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_range(value) -> bool:
    return _number(value) and value >= 0


def _sign(row: dict | None):
    if not row:
        return None
    return 1 if row["return"] > 0 else -1 if row["return"] < 0 else 0


def _streak(index: dict, start: int) -> tuple[int | None, int | None]:
    sign = _sign(index.get(start - DAY_MS))
    if sign in (None, 0):
        return sign, 0
    length = 0
    while length < 4 and _sign(index.get(start - (length + 1) * DAY_MS)) == sign:
        length += 1
    # A gap cannot establish that this is an exact 1/2/3-day run.
    if length < 4 and start - (length + 1) * DAY_MS not in index:
        return sign, None
    return sign, min(length, 4)


def _range_class(index: dict, start: int) -> str | None:
    row = index.get(start)
    if not row or not _valid_range(row.get("range")):
        return None
    baseline = [index[t]["range"] for t in range(start - 30 * DAY_MS, start, DAY_MS)
                if t in index and _valid_range(index[t].get("range"))]
    if len(baseline) < 20 or median(baseline) <= 0:
        return None
    ratio = row["range"] / median(baseline)
    return "wide" if ratio >= 1.5 else "narrow" if ratio <= 2 / 3 else "normal"


def _summary(rows: list[dict], min_n: int) -> dict:
    n = len(rows)
    up = sum(r["return"] > 0 for r in rows)
    down = sum(r["return"] < 0 for r in rows)
    available = n >= min_n
    ranges = [r["range"] for r in rows if _valid_range(r.get("range"))]
    low, high = wilson_interval(up, n) if available else (None, None)
    return {"n": n, "up": up, "down": down, "flat": n - up - down,
            "available": available, "min_n": min_n,
            "up_frequency": up / n if available else None,
            "down_frequency": down / n if available else None,
            "up_interval": {"low": low, "high": high} if available else None,
            "mean_return": mean(r["return"] for r in rows) if available else None,
            "median_return": median(r["return"] for r in rows) if available else None,
            "mean_range": mean(ranges) if len(ranges) >= min_n else None,
            "range_n": len(ranges),
            "period": {"from": rows[0]["bucket_start"], "to": rows[-1]["bucket_start"]} if rows else None}


def calendar_statistics(rows: list[dict], *, target: int, now: int, min_n: int = 100) -> dict:
    """Report pre-open conditions for target; never use its return to select peers."""
    target, today = msk_day_start_ms(target), msk_day_start_ms(now)
    min_n = max(1, int(min_n))
    index = {r["bucket_start"]: r for r in rows
             if r.get("complete") and _number(r.get("return"))
             and r["return"] > -1
             and isinstance(r.get("bucket_start"), int)
             and r["bucket_start"] == msk_day_start_ms(r["bucket_start"])
             and r["bucket_start"] < today}
    history = sorted((r for t, r in index.items() if t < target), key=lambda r: r["bucket_start"])
    weekday = msk_weekday(target)
    baseline = _summary(history, min_n)
    weekday_rows = [r for r in history if msk_weekday(r["bucket_start"]) == weekday]
    patterns = []

    def add(key, label, condition, *, ready=True, reason=None):
        matches = [r for r in history if condition(r["bucket_start"])] if ready else []
        result = _summary(matches, min_n)
        result.update({"id": key, "label": label, "condition_known": ready, "reason": reason})
        result["lift_pp"] = ((result["up_frequency"] - baseline["up_frequency"]) * 100
                             if result["available"] and baseline["available"] else None)
        patterns.append(result)

    add("weekday", f"День недели: {WEEKDAY_RU[weekday]}", lambda t: msk_weekday(t) == weekday)
    previous = index.get(target - DAY_MS)
    prev_sign = _sign(previous)
    words = {1: "ростом", -1: "снижением", 0: "без изменения"}
    label = f"После дня, закрытого {words[prev_sign]}" if prev_sign is not None else "После закрытия предыдущего дня"
    add("previous_day", label, lambda t: _sign(index.get(t - DAY_MS)) == prev_sign,
        ready=previous is not None, reason="Предыдущий день ещё не закрыт или отсутствуют полные данные.")
    add("previous_day_weekday", f"{label} · {WEEKDAY_RU[weekday]}",
        lambda t: msk_weekday(t) == weekday and _sign(index.get(t - DAY_MS)) == prev_sign,
        ready=previous is not None, reason="Нужно завершённое закрытие предыдущего дня.")
    sign, length = _streak(index, target)
    streak_label = (f"После серии {'не менее 4' if length == 4 else length} дней "
                    f"{'роста' if sign == 1 else 'снижения'}") if length else "После серии закрытий"
    add("streak", streak_label, lambda t: _streak(index, t) == (sign, length),
        ready=sign in (-1, 1) and length is not None and length > 0,
        reason="Для серии нужны непрерывные завершённые дни и её начало.")
    if weekday in (2, 3):
        def week_start(t):
            monday = t - msk_weekday(t) * DAY_MS
            return (_sign(index.get(monday)), _sign(index.get(monday + DAY_MS)))
        combo = week_start(target)
        arrows = {1: "↑", -1: "↓", 0: "↔", None: "?"}
        add("mon_tue", f"Пн {arrows[combo[0]]} + Вт {arrows[combo[1]]} → {WEEKDAY_RU[weekday]}",
            lambda t: msk_weekday(t) == weekday and week_start(t) == combo,
            ready=None not in combo, reason="Понедельник и вторник должны завершиться.")
    prev_class = _range_class(index, target - DAY_MS)
    if prev_class is not None:
        classes = {"wide": "широким", "narrow": "узким", "normal": "обычным"}
        add("previous_range", f"После дня с {classes[prev_class]} диапазоном",
            lambda t: _range_class(index, t - DAY_MS) == prev_class)
        if prev_sign == -1 and prev_class == "wide":
            add("red_wide", "После снижения с широким диапазоном",
                lambda t: _sign(index.get(t - DAY_MS)) == -1 and _range_class(index, t - DAY_MS) == "wide")
        if prev_class in ("wide", "narrow") and _range_class(index, target - 2 * DAY_MS) == prev_class:
            add("two_ranges", f"После двух дней с {'широким' if prev_class == 'wide' else 'узким'} диапазоном",
                lambda t: all(_range_class(index, t - d * DAY_MS) == prev_class for d in (1, 2)))
    month_key = msk_dt(target).strftime("%Y-%m")
    return {"target_day": target, "as_of": now, "method_version": CALENDAR_VERSION,
            "completed_days": [{"day_start_ms": t, "median_return": r["return"], "sign": _sign(r), "complete": True}
                               for t, r in sorted(index.items()) if msk_dt(t).strftime("%Y-%m") == month_key],
            "mode": "reconstruction" if target < today else "current" if target == today else "future",
            "baseline": baseline, "weekday_baseline": _summary(weekday_rows, min_n),
            "previous_days": [{"day_start_ms": target - d * DAY_MS,
                               "sign": _sign(index.get(target - d * DAY_MS)),
                               "return": (index.get(target - d * DAY_MS) or {}).get("return")}
                              for d in (3, 2, 1)],
            "patterns": patterns, "used_for_market_direction": False,
            "method_note": "Исторические частоты, не вероятность сделки. Условия могут пересекаться: их проценты не складываются. "
                           "Для выбранного дня используются только предшествующие завершённые дни. "
                           "Интервал Вильсона описательный: зависимость наблюдений и множественные проверки не скорректированы. "
                           "Прошлые даты — реконструкция по доступной сейчас истории, не сохранённый прогноз.",
            "range_note": "Диапазон = (максимум − минимум) / открытие дня. Широкий ≥1,5×, узкий ≤2/3 медианы предыдущих 30 дней "
                          "(минимум 20). Пороги — адаптация проекта; исходные проценты PDF не подставляются."}
