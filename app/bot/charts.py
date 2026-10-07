"""Рендер LTF-графика для Telegram-бота (п.7 ТЗ бота, ТЗ 07.10.2026 §8–§10).

Свечи H1/D1/W1 + включаемые слои поверх read model
app/services/overview.py::observation_chart_layers. Стиль — общий с
app/notify/chartimg.py (тёмная тема снимков зон). Заголовок — в
зарезервированной полосе без обрезки, подписи цен — в правой колонке без
наложений, время — МСК, биржа/тип рынка не выводятся (§5.1).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import now_ms
from ..notify.chartimg import candles_to_df, make_dark_style
from ..notify.chartlabels import (
    FIG_DPI,
    FIG_SIZE,
    add_legend,
    apply_layout,
    layout_price_labels,
    set_footer,
    set_header,
)
from ..notify.formatting import fmt_price_ru, fmt_time_msk
from ..services.overview import observation_chart_layers
from ..texts_ru import DIRECTION_RU, LTF_TYPE_RU

# Биты слоёв для компактного кодирования в callback
# (nav:chart…:<iid>:<tf>:<days>:<mask>)
LAYER_BITS = {"htf": 1, "bos": 2, "range": 4, "entries": 8}
LAYER_LABELS = {
    "htf": "HTF-контекст",
    "bos": "BOS/SMS",
    "range": "Диапазон",
    "entries": "Entry Zones",
}
DEFAULT_MASK = 0
for _bit in LAYER_BITS.values():
    DEFAULT_MASK |= _bit  # 15 — все слои

# Свечей на графике для старших ТФ (period_days на них не действует)
_TF_CANDLE_LIMIT = {"D1": 120, "W1": 52}

_COLOR_HTF = "#ffb74d"     # как прямоугольник зоны в chartimg
_COLOR_BOS = "#ffee58"     # уровни слома структуры
_COLOR_RANGE = "#64b5f6"   # диапазон Premium/Discount
_COLOR_LONG = "#26a69a"    # entry-зоны бычьего сценария
_COLOR_SHORT = "#ef5350"   # entry-зоны медвежьего сценария


def layers_from_mask(mask: int) -> tuple[str, ...]:
    """Битовая маска callback → набор имён слоёв для render_ltf_chart."""
    return tuple(name for name, bit in LAYER_BITS.items() if mask & bit)


def _chart_candles(
    db: Database, instrument_id: int, tf: str, period_days: int, now: int,
    end_ms: Optional[int] = None,
) -> list:
    if tf == "H1":
        since = now - period_days * 86_400_000
        return db.get_candles(instrument_id, "H1", start_ms=since,
                              end_ms=end_ms)
    limit = _TF_CANDLE_LIMIT.get(tf)
    if limit is None:
        return []
    rows = db.get_candles(instrument_id, tf, end_ms=end_ms)
    return rows[-limit:]


def render_ltf_chart(
    db: Database,
    observation_id: int,
    out_path,
    *,
    tf: str = "H1",
    period_days: int = 7,
    layers: tuple[str, ...] = ("htf", "bos", "range", "entries"),
    source_label: str = "",
    settings=None,
    now: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Optional[str]:
    """PNG графика выбранного контекста: свечи + слои htf/bos/range/entries.

    Подтверждённый уровень BOS/SMS — сплошная линия, ожидаемый — пунктир.
    now — момент снимка: хендлер передаёт тот же now, что использовал для
    caption, чтобы цена/сценарий/время на картинке и в подписи совпадали.
    end_ms — правая граница периода (режим «график события», §11.1 ТЗ
    07.10.2026: картинка по снимку, на котором событие произошло);
    None — до последней свечи («текущая ситуация»).
    None — наблюдение не найдено или свечей нет (хендлер отвечает текстом).
    source_label оставлен для совместимости — на изображение источник не
    выводится (ТЗ 07.10.2026 §5.1).
    """
    import mplfinance as mpf
    from matplotlib import pyplot as plt
    from matplotlib.patches import Rectangle

    obs = db.get_ltf_observation(observation_id)
    if obs is None:
        return None
    ins = db.get_instrument(obs.instrument_id)
    now = now if now is not None else now_ms()
    candles = _chart_candles(db, obs.instrument_id, tf, period_days, now,
                             end_ms=end_ms)
    if not candles:
        return None
    layer_set = set(layers)
    info = (
        observation_chart_layers(db, settings, observation_id)
        if settings is not None else None
    )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    df = candles_to_df(candles)
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=make_dark_style(),
        returnfig=True,
        figsize=FIG_SIZE,
        datetime_format="%d.%m %H:%M",
        xrotation=0,
        tight_layout=False,
    )
    ax = axes[0]
    ax.set_ylabel("Цена")

    # Внутренняя ось X mplfinance — позиции свечей 0..n-1 (как в chartimg)
    n = len(candles)
    pad_right = max(16, int(n * 0.32))
    xr = n - 0.5 + pad_right

    def x_since(ts: int) -> float:
        return max(sum(1 for c in candles if c.open_time < ts) - 0.5, -0.5)

    extra_levels: list[float] = []
    labels: list[tuple[float, str, str]] = []

    # рабочий масштаб — по свечам; далёкий HTF-контекст НЕ должен сжимать
    # свечи до нечитаемости (§9.2 ТЗ 07.10.2026): тогда зону не рисуем,
    # а её значения уходят в подзаголовок
    c_hi = max(c.high for c in candles)
    c_lo = min(c.low for c in candles)
    c_span = (c_hi - c_lo) or 1.0

    # ---- слой HTF-контекста: прямоугольник родительской зоны ----
    pz = (info or {}).get("parent_zone")
    htf_far_note: Optional[str] = None
    if "htf" in layer_set and pz:
        lo, hi = pz["lower"], pz["upper"]
        near = (lo <= c_hi + c_span) and (hi >= c_lo - c_span)
        if not near:
            htf_far_note = (
                f"HTF {pz['type'].upper()} {pz['timeframe']} вне окна: "
                f"{fmt_price_ru(lo)}–{fmt_price_ru(hi)}"
            )
        else:
            x0 = x_since(pz.get("display_from") or pz["formed_at"])
            ax.add_patch(Rectangle(
                (x0, lo), n - 0.5 - x0, hi - lo,
                facecolor=_COLOR_HTF, alpha=0.15,
                edgecolor=_COLOR_HTF, linewidth=1.0, zorder=1,
            ))
            for price in (hi, lo):
                ax.hlines(price, x0, xr, color=_COLOR_HTF, linewidth=1.0,
                          zorder=4)
            ax.hlines((lo + hi) / 2, x0, xr, color=_COLOR_HTF,
                      linewidth=0.9, linestyles="--", zorder=4)
            htf_name = f"HTF {pz['type'].upper()} {pz['timeframe']}"
            labels += [
                (hi, f"{htf_name} {fmt_price_ru(hi)}", _COLOR_HTF),
                ((lo + hi) / 2,
                 f"{htf_name} 50% {fmt_price_ru((lo + hi) / 2)}", _COLOR_HTF),
                (lo, f"{htf_name} {fmt_price_ru(lo)}", _COLOR_HTF),
            ]
            extra_levels += [lo, hi]

    # ---- слой BOS/SMS: подтверждённый сплошной, ожидаемый пунктиром ----
    if "bos" in layer_set and info:
        breaks = [
            ev for ev in info["structure_events"]
            if ev.get("kind") in ("bos", "sms")
            and ev.get("break_level") is not None
        ]
        last_idx = len(breaks) - 1
        for i, ev in enumerate(breaks):
            level = ev["break_level"]
            x0 = x_since(ev.get("break_candle_open_time") or ev["occurred_at"])
            # §9.4: актуальный слом — отрезок вправо; старые не тянутся
            # бесконечно (короткий хвост от свечи слома)
            x_end = xr if i == last_idx else min(x0 + 6, xr)
            ax.hlines(level, x0, x_end, color=_COLOR_BOS, linewidth=1.2,
                      zorder=4)
            label = f"{ev['kind'].upper()} ✓ {fmt_price_ru(level)}"
            if i == last_idx:
                labels.append((level, label, _COLOR_BOS))
                # §9.4: для подтверждения — время закрытия H1 по МСК у свечи
                ax.annotate(
                    f"закрытие H1 {fmt_time_msk(ev['occurred_at'])}",
                    xy=(x0, level), xytext=(x0 + 0.5, level),
                    fontsize=6.5, color=_COLOR_BOS, va="bottom", zorder=5,
                )
            extra_levels.append(level)
        expected = info.get("expected") or {}
        side = "ниже" if obs.direction.value == "bear" else "выше"
        for key in ("bos", "sms"):
            exp = expected.get(key)
            if exp:
                ax.hlines(exp["level"], -0.5, xr, color=_COLOR_BOS,
                          linewidth=1.0, linestyles="--", zorder=4)
                labels.append((
                    exp["level"],
                    f"Ожидаем {key.upper()}: закрытие H1 {side} "
                    f"{fmt_price_ru(exp['level'])}",
                    _COLOR_BOS,
                ))
                extra_levels.append(exp["level"])

    # ---- слой диапазона Premium/Discount ----
    if "range" in layer_set and info:
        cur_range = next(
            (r for r in info["ranges"] if r.get("current")), None
        )
        if cur_range:
            for price in (cur_range["lower"], cur_range["upper"]):
                ax.hlines(price, -0.5, xr, color=_COLOR_RANGE,
                          linewidth=1.0, zorder=3)
            ax.hlines(cur_range["mid"], -0.5, xr, color=_COLOR_RANGE,
                      linewidth=0.9, linestyles="--", zorder=3)
            labels += [
                (cur_range["upper"],
                 f"P/D {fmt_price_ru(cur_range['upper'])}", _COLOR_RANGE),
                (cur_range["mid"],
                 f"P/D 50% {fmt_price_ru(cur_range['mid'])}", _COLOR_RANGE),
                (cur_range["lower"],
                 f"P/D {fmt_price_ru(cur_range['lower'])}", _COLOR_RANGE),
            ]
            extra_levels += [cur_range["lower"], cur_range["upper"]]

    # ---- слой entry-зон: зелёные long / красные short ----
    if "entries" in layer_set and info:
        for e in info["entries"]:
            direction = e.get("direction")
            direction = getattr(direction, "value", direction)
            color = _COLOR_LONG if direction == "bull" else _COLOR_SHORT
            x0 = x_since(e["formed_at"])
            height = max(e["upper"] - e["lower"], 1e-9)
            ax.add_patch(Rectangle(
                (x0, e["lower"]), n - 0.5 - x0, height,
                facecolor=color, alpha=0.25,
                edgecolor=color, linewidth=0.8, zorder=2,
            ))
            t = LTF_TYPE_RU.get(e.get("type", ""), e.get("type", "?"))
            if e["lower"] == e["upper"]:
                labels.append(
                    (e["lower"], f"{t} H1 {fmt_price_ru(e['lower'])}", color))
            else:
                labels.append((
                    (e["lower"] + e["upper"]) / 2,
                    f"{t} H1 {fmt_price_ru(e['lower'])}–"
                    f"{fmt_price_ru(e['upper'])}",
                    color,
                ))
            extra_levels += [e["lower"], e["upper"]]

    ax.set_xlim(-0.5, xr)
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if extra_levels:
        hi = max(hi, max(extra_levels))
        lo = min(lo, min(extra_levels))
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * 0.04, hi + span * 0.10)

    # Заголовок (§9.1): символ · ТФ · период; вторая строка — HTF-контекст
    # и время снимка по МСК; биржа/рынок не выводятся (§5.1)
    symbol = ins.symbol if ins is not None else "?"
    title = f"{symbol} · {tf}"
    if tf == "H1":
        title += f", {period_days} дн."
    subtitle = None
    if pz:
        pz_dir = DIRECTION_RU.get(pz.get("direction", ""), "")
        subtitle = (
            f"LevelFrame · HTF: {pz['type'].upper()} {pz['timeframe']}"
            + (f" {pz_dir}" if pz_dir else "")
            + f" · снимок {fmt_time_msk(now)}"
        )
    else:
        subtitle = f"LevelFrame · снимок {fmt_time_msk(now)}"
    if htf_far_note:
        # §9.2: значения далёкого HTF-контекста — текстом, без сжатия свечей
        subtitle += f"\n{htf_far_note}"
    header_bottom = set_header(fig, title, subtitle)
    apply_layout(fig, ax, header_bottom)
    layout_price_labels(fig, ax, labels)
    set_footer(fig, f"Свечи {tf}, время открытия — МСК")
    add_legend(
        fig,
        [
            ("HTF-контекст", _COLOR_HTF),
            ("BOS/SMS подтверждён", _COLOR_BOS),
            ("BOS/SMS ожидаемый", _COLOR_BOS),
            ("Диапазон P/D", _COLOR_RANGE),
            ("Entry long", _COLOR_LONG),
            ("Entry short", _COLOR_SHORT),
        ],
        dashed=("BOS/SMS ожидаемый",),
    )

    fig.savefig(out, dpi=FIG_DPI)
    plt.close(fig)
    return str(out)
