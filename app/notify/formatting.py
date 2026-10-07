"""Единое форматирование времени и чисел для всех пользовательских поверхностей
(ТЗ 07.10.2026 §5, §6).

Время: Europe/Moscow, «ДД.ММ.ГГГГ ЧЧ:ММ МСК». Хранение остаётся ms UTC —
меняется только представление.
Числа: пробел между тысячами, запятая как десятичный разделитель. Округление
только для отображения; сравнения границ/BOS/жизненного цикла идут по исходным
значениям.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# Длительности свечей в ms — для вычисления close_time по open_time.
TF_MS = {
    "M15": 15 * 60_000,
    "H1": 3_600_000,
    "H4": 4 * 3_600_000,
    "D1": 86_400_000,
    "W1": 7 * 86_400_000,
}


def fmt_time_msk(ms: int, with_seconds: bool = False) -> str:
    """Момент (ms UTC) в московском представлении (ТЗ §6)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK)
    fmt = "%d.%m.%Y %H:%M:%S МСК" if with_seconds else "%d.%m.%Y %H:%M МСК"
    return dt.strftime(fmt)


def candle_close_ms(open_ms: int, timeframe: str) -> int:
    """Момент закрытия свечи по её open_time (ТЗ §6: не путать open/close)."""
    return open_ms + TF_MS[timeframe]


def _to_ru_number(s: str) -> str:
    """en-US строка числа -> ru: пробелы-тысячи, запятая-десятичная."""
    return s.replace(",", " ").replace(".", ",")


def fmt_price_ru(p: float) -> str:
    """Цена с точностью инструмента в ru-формате (ТЗ §5.1).

    Та же градация точности, что была в боте: >=100 — 2 знака, >=1 — до 4,
    иначе до 8, без хвостовых нулей. Только представление — исходное значение
    не меняется.
    """
    if p != p:  # NaN
        return "?"
    if abs(p) >= 100:
        return _to_ru_number(f"{p:,.2f}")
    if abs(p) >= 1:
        s = f"{p:.4f}".rstrip("0").rstrip(".")
    else:
        s = f"{p:.8f}".rstrip("0").rstrip(".")
    return _to_ru_number(s)


def fmt_pct_ru(pct: float, decimals: int = 2, signed: bool = False) -> str:
    """Процент в ru-формате: «1,51%» / «+2,35%»."""
    spec = f"{{:+.{decimals}f}}" if signed else f"{{:.{decimals}f}}"
    return _to_ru_number(spec.format(pct)) + "%"
