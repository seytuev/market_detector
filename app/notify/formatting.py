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


def _group_plain(plain: str) -> str:
    """'81822.67' / '-0.001678' → пробелы тысяч и запятая."""
    neg = plain.startswith("-")
    body = plain[1:] if neg else plain
    if "." in body:
        whole, frac = body.split(".", 1)
    else:
        whole, frac = body, ""
    whole = whole or "0"
    chunks: list[str] = []
    while whole:
        chunks.append(whole[-3:])
        whole = whole[:-3]
    text = " ".join(reversed(chunks))
    if frac:
        text += "," + frac
    return ("-" if neg else "") + text


def _plain_decimal(value: float) -> str:
    """Десятичная запись без научной нотации и хвостовых нулей."""
    from decimal import Decimal

    raw = format(Decimal(str(value)), "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw or "0"


def format_level(value: float | None, tick: float | None = None) -> dict:
    """Уровень для карточки: точное значение, компакт и признак округления.

    |p| ≥ 100 и значение уже лежит на двух знаках — «81 822,67» без ≈.
    Лишние знаки не подменяются компактом в поле text: условие не должно
    молча сменить цену. Границы зоны с approximate помечает вызывающий код.
    |p| < 1 сохраняет до восьми значащих знаков после запятой.
    """
    from decimal import Decimal, ROUND_HALF_UP

    empty = {
        "text": "?", "exact": "?", "compact": "?",
        "approximate": False, "value": None,
    }
    if value is None:
        return empty
    try:
        number = float(value)
    except (TypeError, ValueError):
        return empty
    if number != number:  # NaN
        return empty
    dec = Decimal(str(number))
    if tick:
        step = Decimal(str(tick))
        if step > 0:
            units = (dec / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            dec = units * step
            number = float(dec)
    exact_plain = format(dec, "f")
    if "." in exact_plain:
        exact_plain = exact_plain.rstrip("0").rstrip(".")
    exact_plain = exact_plain or "0"
    exact = _group_plain(exact_plain)
    abs_v = abs(number)
    if abs_v >= 100:
        compact_dec = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        compact = _group_plain(format(compact_dec, "f"))
        approximate = dec != compact_dec
        text = exact if approximate else compact
    elif abs_v >= 1:
        compact_dec = dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        compact_plain = format(compact_dec, "f").rstrip("0").rstrip(".")
        compact = _group_plain(compact_plain or "0")
        approximate = exact != compact
        text = exact if approximate else compact
    else:
        compact_dec = dec.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        compact_plain = format(compact_dec, "f").rstrip("0").rstrip(".")
        compact = _group_plain(compact_plain or "0")
        exact = compact
        text = compact
        approximate = False
    return {
        "text": text,
        "exact": exact,
        "compact": compact,
        "approximate": approximate,
        "value": number,
    }


def format_level_set(
    values: list[float | None], tick: float | None = None,
) -> list[dict]:
    """Если два уровня дают одну строку, у компактных увеличить точность."""
    from decimal import Decimal, ROUND_HALF_UP

    formatted = [format_level(v, tick) for v in values]
    texts = [item["text"] for item in formatted]
    if len(texts) == len(set(texts)):
        return formatted
    for places in range(3, 9):
        quant = Decimal("1").scaleb(-places)
        trial = []
        for item in formatted:
            if item["value"] is None:
                trial.append(item["text"])
                continue
            dec = Decimal(str(item["value"])).quantize(quant, rounding=ROUND_HALF_UP)
            plain = format(dec, "f").rstrip("0").rstrip(".")
            trial.append(_group_plain(plain or "0"))
        if len(trial) == len(set(trial)):
            for item, text in zip(formatted, trial):
                if item["value"] is None:
                    continue
                item["text"] = text
                item["exact"] = text
                item["approximate"] = False
            break
    return formatted
