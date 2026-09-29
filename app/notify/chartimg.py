"""Генерация изображения зоны (§11 п.7).

График строится по тем же свечам и границам, которые использовал детектор;
источник данных и время снимка обозначены на изображении.
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


def render_zone_chart(
    candles: list[Candle],
    zone: Zone,
    out_path: str | Path,
    source_label: str,
) -> str:
    """Рисует свечи (mplfinance, тёмная тема), прямоугольник зоны [L, U]
    и линию середины M. Возвращает путь к PNG.

    Свечи — ровно те, что передал детектор; источник и время снимка
    подписаны на графике (§11 п.7).
    """
    if not candles:
        raise ValueError("нужны свечи детектора — пустой снимок не рисуем")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(
        {
            "Open": [c.open for c in candles],
            "High": [c.high for c in candles],
            "Low": [c.low for c in candles],
            "Close": [c.close for c in candles],
        },
        index=pd.DatetimeIndex(
            [
                datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc)
                for c in candles
            ],
            tz="UTC",
        ),
    )

    # Тёмная тема
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        gridstyle=":",
        facecolor="#12161c",
        figcolor="#12161c",
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        returnfig=True,
        figsize=(10, 5.5),
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        tight_layout=True,
    )
    ax = axes[0]

    # Внутренняя ось X mplfinance — позиции свечей 0..n-1
    n = len(candles)
    # Прямоугольник зоны: от свечи формирования до правого края
    x0 = sum(1 for c in candles if c.open_time < zone.formed_at) - 0.5
    x1 = n - 0.5
    # пустота справа после последней свечи — в неё уходят линии границ
    pad_right = max(16, int(n * 0.32))
    xr = x1 + pad_right
    if zone.is_level:
        # Уровень SSL/BSL — одна цена (§7)
        ax.hlines(zone.lower, -0.5, xr, color="#ffb74d", linewidth=1.5, zorder=4)
    else:
        ax.add_patch(
            Rectangle(
                (max(x0, -0.5), zone.lower),
                x1 - max(x0, -0.5),
                zone.upper - zone.lower,
                facecolor="#ffb74d",
                alpha=0.22,
                edgecolor="#ffb74d",
                linewidth=1.0,
                zorder=1,
            )
        )
        # Границы зоны — линии от формирования вправо (в пустоту справа):
        # у молодой зоны прямоугольник узкий по времени, линии читаются всегда
        ax.hlines(zone.upper, max(x0, -0.5), xr, color="#ffb74d", linewidth=1.1, zorder=4)
        ax.hlines(zone.lower, max(x0, -0.5), xr, color="#ffb74d", linewidth=1.1, zorder=4)
        ax.hlines(
            zone.mid, max(x0, -0.5), xr, color="#ffb74d",
            linewidth=1.0, linestyles="--", zorder=4,
        )
        # подписи цен границ у правого края (внутри осей — не обрезаются)
        for price in (zone.upper, zone.lower):
            ax.text(
                0.995, price, f"{price:g}",
                transform=ax.get_yaxis_transform(),
                fontsize=8, color="#ffb74d",
                ha="right", va="bottom", zorder=5,
            )

    # Подпись типа/ТФ/направления/статуса
    title = (
        f"{TYPE_RU.get(zone.type.value, zone.type.value)} {zone.timeframe} "
        f"({DIRECTION_RU.get(zone.direction.value, zone.direction.value)}) — "
        f"{STATUS_RU.get(zone.status.value, zone.status.value)}"
    )
    ax.set_title(title, loc="left", fontsize=11)

    # Воздух вокруг данных: пустота справа после последней свечи и отступ
    # сверху (линия границы/хай не упирается в край), небольшой снизу
    ax.set_xlim(-0.5, xr)
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    hi = max(hi, zone.lower if zone.is_level else zone.upper)
    lo = min(lo, zone.lower)
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * 0.04, hi + span * 0.10)

    # Источник данных и время снимка (§11 п.7)
    snap = datetime.fromtimestamp(now_ms() / 1000, tz=timezone.utc)
    ax.text(
        0.0,
        -0.12,
        f"Источник: {source_label}. Снимок: {snap:%Y-%m-%d %H:%M UTC}.",
        transform=ax.transAxes,
        fontsize=8,
        color="#8b95a3",
    )

    fig.savefig(out, dpi=110)
    plt.close(fig)
    return str(out)
