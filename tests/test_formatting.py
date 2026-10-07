"""Форматирование времени/чисел (ТЗ 07.10.2026 §5.1, §6; приёмка T11–T13)."""
from datetime import datetime, timezone

from app.notify.formatting import (
    candle_close_ms,
    fmt_pct_ru,
    fmt_price_ru,
    fmt_time_msk,
)


def _ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_msk_conversion_basic():
    # 07.10.2026 02:01 UTC -> 07.10.2026 05:01 МСК
    assert fmt_time_msk(_ms("2026-10-07 02:01")) == "07.10.2026 05:01 МСК"


def test_msk_conversion_hour():
    # 07.10.2026 01:00 UTC -> 07.10.2026 04:00 МСК
    assert fmt_time_msk(_ms("2026-10-07 01:00")) == "07.10.2026 04:00 МСК"


def test_msk_conversion_date_rollover():
    # 06.10.2026 22:30 UTC -> 07.10.2026 01:30 МСК
    assert fmt_time_msk(_ms("2026-10-06 22:30")) == "07.10.2026 01:30 МСК"


def test_msk_with_seconds():
    ms = _ms("2026-10-07 02:01") + 45_000
    assert fmt_time_msk(ms, with_seconds=True) == "07.10.2026 05:01:45 МСК"


def test_candle_close_time_h1():
    open_ms = _ms("2026-10-07 02:00")
    assert fmt_time_msk(candle_close_ms(open_ms, "H1")) == "07.10.2026 06:00 МСК"


def test_price_ru_thousands_and_decimal():
    assert fmt_price_ru(83827.72) == "83 827,72"
    assert fmt_price_ru(82563.00) == "82 563,00"
    assert fmt_price_ru(82257.00) == "82 257,00"


def test_price_ru_small_nominal():
    assert fmt_price_ru(0.00001234) == "0,00001234"
    assert fmt_price_ru(1.5) == "1,5"
    assert fmt_price_ru(2600.15) == "2 600,15"


def test_price_ru_nan():
    assert fmt_price_ru(float("nan")) == "?"


def test_pct_ru():
    assert fmt_pct_ru(1.51) == "1,51%"
    assert fmt_pct_ru(-2.354, signed=True) == "-2,35%"
    assert fmt_pct_ru(2.354, signed=True) == "+2,35%"
