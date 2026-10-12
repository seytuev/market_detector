"""Московское время движка «События и время» (§4 ТЗ).

Timezone продукта — Europe/Moscow (UTC+3, без переходов DST); все
пользовательские дни, недели и окна считаются по Москве. Хранение и
аргументы — абсолютные unix-миллисекунды: часовой пояс не меняет
физический момент, только границы календаря.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .mathutil import DAY_MS

MSK = ZoneInfo("Europe/Moscow")
HOUR_MS = 3_600_000
WEEKDAY_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def msk_dt(ms: int) -> datetime:
    """Aware datetime в МСК для абсолютного момента ms."""
    return datetime.fromtimestamp(ms / 1000, MSK)


def msk_day_start_ms(ms: int) -> int:
    """00:00 МСК московского дня, содержащего ms.

    Момент ровно 00:00 принадлежит новому дню (полуоткрытый интервал
    [00:00, 00:00 следующего дня)).
    """
    d = msk_dt(ms)
    start = datetime(d.year, d.month, d.day, tzinfo=MSK)
    return int(start.timestamp() * 1000)


def msk_day_end_ms(ms: int) -> int:
    """Исключительный конец дня: 00:00 МСК следующего дня."""
    return msk_day_start_ms(ms + DAY_MS)


def msk_weekday(ms: int) -> int:
    """День недели по Москве: 0=понедельник .. 6=воскресенье."""
    return msk_dt(ms).weekday()


def msk_hour(ms: int) -> int:
    """Час по Москве, 0..23."""
    return msk_dt(ms).hour


def fmt_msk(ms: int) -> str:
    """«11.10.2026 14:32 МСК» — пользовательские метки всегда с МСК."""
    return msk_dt(ms).strftime("%d.%m.%Y %H:%M") + " МСК"


def fmt_msk_date(ms: int) -> str:
    """«11.10.2026»."""
    return msk_dt(ms).strftime("%d.%m.%Y")


def completed_msk_days(now_ms: int, count: int) -> list[tuple[int, int]]:
    """count последних полностью завершённых московских дней (start, end).

    Порядок — от старых к новым. Текущий незавершённый день не входит;
    ровно в 00:00 МСК предыдущий день уже завершён и входит последним.
    """
    today_start = msk_day_start_ms(now_ms)
    return [
        (today_start - i * DAY_MS, today_start - (i - 1) * DAY_MS)
        for i in range(count, 0, -1)
    ]


def same_msk_day(a_ms: int, b_ms: int) -> bool:
    """Оба момента внутри одного московского дня."""
    return msk_day_start_ms(a_ms) == msk_day_start_ms(b_ms)
