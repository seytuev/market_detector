"""Общие хелперы рендера экспортируемых графиков (ТЗ 07.10.2026 §9–§10).

- заголовок: зарезервированная верхняя область fig.text, высота от числа
  строк, перенос длинного текста — ничего не обрезается краем PNG (§10.1);
- подписи цен: раскладка в правой колонке без наложений, выносные линии,
  числовые значения никогда не сдвигаются (§10.2);
- размер ≥1280 px по ширине (§10.3).
"""
from __future__ import annotations

import textwrap

# Ширина экспорта ≥ 1280 px (§10.3): 12.8" × 100 dpi = 1280 px
FIG_SIZE = (12.8, 7.2)
FIG_DPI = 100

_COLOR_TEXT = "#e6e9ee"
_COLOR_MUTED = "#8b95a3"


def set_header(fig, title: str, subtitle: str | None = None) -> float:
    """Рисует заголовок в зарезервированной верхней полосе фигуры.

    Возвращает нижнюю границу заголовка (fig fraction) — её использует
    apply_layout, чтобы ось не заехала под текст. Высота зависит от
    фактического числа строк после переноса, а не от фиксированной
    позиции за пределами изображения (§10.1).
    """
    y = 0.985
    for ln in textwrap.wrap(title, 78) or [""]:
        fig.text(0.008, y, ln, va="top", ha="left", fontsize=16,
                 color=_COLOR_TEXT, fontweight="bold")
        y -= 0.045
    if subtitle:
        for ln in textwrap.wrap(subtitle, 110):
            fig.text(0.008, y, ln, va="top", ha="left", fontsize=11,
                     color=_COLOR_MUTED)
            y -= 0.033
    return y


def apply_layout(fig, ax, header_bottom: float, footer_lines: int = 1,
                 right_margin: float = 0.20) -> None:
    """Позиционирует ось: сверху — заголовок, снизу — футер, справа —
    колонка ценовых подписей (свечи не сжимаются подписи ради, §10.3)."""
    top = max(header_bottom - 0.012, 0.5)
    bottom = 0.06 + 0.028 * (footer_lines - 1)
    ax.set_position([0.06, bottom, 1.0 - 0.06 - right_margin, top - bottom])
    # тики цены — слева, правая колонка отведена под подписи уровней
    ax.yaxis.tick_left()


def set_footer(fig, text: str) -> None:
    fig.text(0.008, 0.012, text, va="bottom", ha="left", fontsize=8,
             color=_COLOR_MUTED)


def _spread_positions(ys: list[float], line_h: float) -> list[float]:
    """Желаемые y-позиции подписей → позиции без пересечений.

    Сдвиг только по вертикали и только для ПОДПИСЕЙ — сами уровни/числа
    не меняются (§10.2). Чистая функция — покрыта тестом.
    """
    ys = list(ys)
    for i in range(1, len(ys)):
        ys[i] = min(ys[i], ys[i - 1] - line_h)
    for i in range(len(ys) - 2, -1, -1):
        ys[i] = max(ys[i], ys[i + 1] + line_h)
    return ys


def layout_price_labels(fig, ax, items: list[tuple[float, str, str]],
                        fontsize: int = 11) -> list:
    """Подписи уровней в правой колонке без наложений (§10.2).

    items — [(price, text, color)], порядок по цене сохраняется. Точное
    совпадение уровней → одна общая подпись с перечислением инструментов.
    Связь с реальной линией при сдвиге — тонкая выносная линия.
    Возвращает созданные text-артисты (для тестов).
    """
    if not items:
        return []
    merged: list[list] = []
    for price, text, color in sorted(items, key=lambda t: -t[0]):
        if merged and abs(price - merged[-1][0]) <= 1e-9 * max(1.0, abs(price)):
            merged[-1][1] = f"{merged[-1][1]} + {text}"
        else:
            merged.append([price, text, color])

    y0, y1 = ax.get_ylim()
    span = (y1 - y0) or 1.0
    pos = ax.get_position()
    ax_h_in = fig.get_size_inches()[1] * pos.height
    line_h = (fontsize / 72 * 1.5) / ax_h_in * span
    ys = _spread_positions([m[0] for m in merged], line_h)
    margin = line_h * 0.3
    ys = [min(max(y, y0 + margin), y1 - margin) for y in ys]

    trans = ax.get_yaxis_transform()
    artists = []
    for (price, text, color), y in zip(merged, ys):
        t = ax.text(
            1.005, y, text, transform=trans, fontsize=fontsize, color=color,
            ha="left", va="center", zorder=6, clip_on=False,
        )
        artists.append(t)
        if abs(y - price) > line_h * 0.4:
            ax.annotate(
                "", xy=(1.0, price), xytext=(1.005, y),
                xycoords=trans, textcoords=trans, annotation_clip=False,
                arrowprops=dict(arrowstyle="-", lw=0.5, color=color,
                                alpha=0.55),
                zorder=5,
            )
    return artists


def add_legend(fig, entries: list[tuple[str, str]], dashed: tuple[str, ...] = ()):
    """Компактная легенда цветов/типов линий (§10.3) — в пустой правой
    части осей (padding после последней свечи), не перекрывая правую
    колонку ценовых подписей и футер. Состояние читается не только по
    цвету — текстовые подписи у линий."""
    from matplotlib.lines import Line2D

    handles = [
        Line2D([0], [0], color=color, lw=1.4,
               linestyle="--" if label in dashed else "-", label=label)
        for label, color in entries
    ]
    fig.legend(
        handles=handles, loc="lower right", bbox_to_anchor=(0.855, 0.02),
        fontsize=7, framealpha=0.25, facecolor="#12161c",
        edgecolor="#3a4250", labelcolor=_COLOR_TEXT,
    )
