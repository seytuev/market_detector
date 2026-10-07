"""Генерация изображения зоны (§11 п.7, ТЗ 07.10.2026 §9–§10).

График строится по тем же свечам и границам, которые использовал детектор.
Заголовок — в зарезервированной полосе (не обрезается), подписи цен — в
правой колонке без наложений, время — МСК; биржа/тип рынка на изображение
не выводятся (§5.1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # без GUI — для серверного процесса

import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from ..models import Candle, Zone, now_ms
from ..texts_ru import DIRECTION_RU, STATUS_RU, TYPE_RU
from .chartlabels import (
    FIG_DPI,
    FIG_SIZE,
    apply_layout,
    layout_price_labels,
    set_footer,
    set_header,
)
from .formatting import MSK, fmt_price_ru, fmt_time_msk


def candles_to_df(candles: list[Candle]) -> "pd.DataFrame":
    """OHLC-DataFrame для mplfinance. Индекс — open_time свечи по МСК
    (ТЗ 07.10.2026 §6: ось времени подписывается временем открытия)."""
    return pd.DataFrame(
        {
            "Open": [c.open for c in candles],
            "High": [c.high for c in candles],
            "Low": [c.low for c in candles],
            "Close": [c.close for c in candles],
        },
        index=pd.DatetimeIndex(
            [
                datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc)
                .astimezone(MSK)
                for c in candles
            ],
            tz=MSK,
        ),
    )


def make_dark_style():
    """Тёмная тема графиков (общая для снимков зон и LTF-графиков бота)."""
    return mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        gridstyle=":",
        facecolor="#12161c",
        figcolor="#12161c",
    )


def render_zone_chart(
    candles: list[Candle],
    zone: Zone,
    out_path: str | Path,
    source_label: str,
    symbol: str | None = None,
) -> str:
    """Рисует свечи (mplfinance, тёмная тема), прямоугольник зоны [L, U]
    и линию середины M. Возвращает путь к PNG.

    Свечи — ровно те, что передал детектор. source_label оставлен в
    сигнатуре для совместимости вызывающих: метаданные источника на
    изображение больше не выводятся (ТЗ 07.10.2026 §5.1).
    """
    if not candles:
        raise ValueError("нужны свечи детектора — пустой снимок не рисуем")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if symbol is None:
        # «binance spot / ETHUSDT» → «ETHUSDT»
        symbol = source_label.split("/")[-1].strip() or source_label

    df = candles_to_df(candles)
    style = make_dark_style()

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        returnfig=True,
        figsize=FIG_SIZE,
        datetime_format="%d.%m %H:%M",
        xrotation=0,
        tight_layout=False,
    )
    ax = axes[0]
    ax.set_ylabel("Цена")

    # Внутренняя ось X mplfinance — позиции свечей 0..n-1
    n = len(candles)
    # Прямоугольник зоны: от свечи формирования до правого края
    x0 = sum(1 for c in candles if c.open_time < zone.formed_at) - 0.5
    x1 = n - 0.5
    # пустота справа после последней свечи — в неё уходят линии границ
    pad_right = max(16, int(n * 0.32))
    xr = x1 + pad_right

    type_ru = TYPE_RU.get(zone.type.value, zone.type.value)
    zone_color = "#ffb74d"
    labels: list[tuple[float, str, str]] = []
    if zone.is_level:
        # Уровень SSL/BSL — одна цена, с подписью (§9.6, §10.2)
        ax.hlines(zone.lower, -0.5, xr, color=zone_color, linewidth=1.5, zorder=4)
        labels.append(
            (zone.lower, f"{type_ru} {zone.timeframe} "
             f"{fmt_price_ru(zone.lower)}", zone_color)
        )
    else:
        ax.add_patch(
            Rectangle(
                (max(x0, -0.5), zone.lower),
                x1 - max(x0, -0.5),
                zone.upper - zone.lower,
                facecolor=zone_color,
                alpha=0.22,
                edgecolor=zone_color,
                linewidth=1.0,
                zorder=1,
            )
        )
        # Границы зоны — линии от формирования вправо (в пустоту справа):
        # у молодой зоны прямоугольник узкий по времени, линии читаются всегда
        ax.hlines(zone.upper, max(x0, -0.5), xr, color=zone_color, linewidth=1.1, zorder=4)
        ax.hlines(zone.lower, max(x0, -0.5), xr, color=zone_color, linewidth=1.1, zorder=4)
        ax.hlines(
            zone.mid, max(x0, -0.5), xr, color=zone_color,
            linewidth=1.0, linestyles="--", zorder=4,
        )
        labels += [
            (zone.upper, fmt_price_ru(zone.upper), zone_color),
            (zone.mid, f"50% {fmt_price_ru(zone.mid)}", zone_color),
            (zone.lower, fmt_price_ru(zone.lower), zone_color),
        ]

    # Воздух вокруг данных: пустота справа после последней свечи и отступ
    # сверху (линия границы/хай не упирается в край), небольшой снизу
    ax.set_xlim(-0.5, xr)
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    hi = max(hi, zone.lower if zone.is_level else zone.upper)
    lo = min(lo, zone.lower)
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * 0.04, hi + span * 0.10)

    # Заголовок: символ · тип ТФ · направление (для уровня — без направления);
    # вторая строка — статус и время снимка по МСК (§9.1)
    title = f"{symbol} · {type_ru} {zone.timeframe}"
    if not zone.is_level:
        title += (
            f" · {DIRECTION_RU.get(zone.direction.value, zone.direction.value)}"
        )
    status_ru = STATUS_RU.get(zone.status.value, zone.status.value)
    subtitle = f"LevelFrame · {status_ru} · снимок {fmt_time_msk(now_ms())}"
    header_bottom = set_header(fig, title, subtitle)
    apply_layout(fig, ax, header_bottom)
    layout_price_labels(fig, ax, labels)
    set_footer(fig, f"Свечи {zone.timeframe}, время открытия — МСК")

    fig.savefig(out, dpi=FIG_DPI)
    plt.close(fig)
    return str(out)
