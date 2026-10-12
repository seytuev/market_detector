"""Московское время: границы дня, дни недели, завершённые дни, форматы."""
from __future__ import annotations

from datetime import datetime, timezone

from app.events.mathutil import DAY_MS
from app.events.timeutil import (
    HOUR_MS,
    MSK,
    WEEKDAY_RU,
    completed_msk_days,
    fmt_msk,
    fmt_msk_date,
    msk_day_end_ms,
    msk_day_start_ms,
    msk_dt,
    msk_hour,
    msk_weekday,
    same_msk_day,
)


def utc_ms(y, mo, d, h=0, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


# 2026-10-11 — воскресенье. 00:00 МСК этого дня = 2026-10-10 21:00 UTC.
SUN_START = utc_ms(2026, 10, 10, 21)


def test_msk_dt_is_aware_and_plus3():
    dt = msk_dt(SUN_START + 14 * HOUR_MS + 32 * 60_000)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == MSK.utcoffset(dt)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 10, 11, 14, 32)


def test_day_boundary_belongs_to_new_day():
    assert msk_day_start_ms(SUN_START) == SUN_START
    assert msk_day_start_ms(SUN_START - 1) == SUN_START - DAY_MS
    assert msk_day_end_ms(SUN_START - 1) == SUN_START
    assert msk_day_end_ms(SUN_START) == SUN_START + DAY_MS


def test_weekday_and_hour_against_known_utc_instants():
    # 2026-10-10 21:00 UTC — воскресенье 00:00 МСК.
    assert msk_weekday(SUN_START) == 6
    assert msk_hour(SUN_START) == 0
    # 2026-10-12 20:59 UTC — понедельник 23:59 МСК; +2 мин — вторник 00:01 МСК.
    mon_late = utc_ms(2026, 10, 12, 20, 59)
    assert msk_weekday(mon_late) == 0
    assert msk_hour(mon_late) == 23
    assert msk_weekday(mon_late + 2 * 60_000) == 1
    assert msk_hour(mon_late + 2 * 60_000) == 0
    # Суббота 12:00 UTC — суббота 15:00 МСК, день тот же.
    sat = utc_ms(2026, 10, 10, 12)
    assert msk_weekday(sat) == 5
    assert msk_hour(sat) == 15


def test_weekday_ru_covers_seven_days():
    assert len(WEEKDAY_RU) == 7
    assert WEEKDAY_RU[6] == "воскресенье"


def test_completed_msk_days_excludes_current_day():
    now = SUN_START + 12 * HOUR_MS  # воскресенье, полдень МСК
    days = completed_msk_days(now, 3)
    assert days == [
        (SUN_START - 3 * DAY_MS, SUN_START - 2 * DAY_MS),
        (SUN_START - 2 * DAY_MS, SUN_START - DAY_MS),
        (SUN_START - DAY_MS, SUN_START),
    ]
    assert all(end <= msk_day_start_ms(now) for _, end in days)


def test_completed_msk_days_at_exact_midnight():
    days = completed_msk_days(SUN_START, 1)
    assert days == [(SUN_START - DAY_MS, SUN_START)]


def test_same_msk_day():
    assert same_msk_day(SUN_START, SUN_START + DAY_MS - 1)
    assert not same_msk_day(SUN_START - 1, SUN_START)


def test_fmt_strings_end_with_msk():
    stamp = SUN_START + 14 * HOUR_MS + 32 * 60_000
    assert fmt_msk(stamp) == "11.10.2026 14:32 МСК"
    assert fmt_msk(stamp).endswith("МСК")
    assert fmt_msk_date(stamp) == "11.10.2026"
