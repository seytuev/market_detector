"""Рендер LTF-графика для Telegram-бота (п.7 ТЗ бота).

Свечи H1/D1/W1 + включаемые слои поверх read model
app/services/overview.py::observation_chart_layers. Стиль — общий с
app/notify/chartimg.py (тёмная тема снимков зон).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import now_ms
from ..notify.chartimg import candles_to_df, make_dark_style
from ..services.overview import observation_chart_layers

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
    db: Database, instrument_id: int, tf: str, period_days: int, now: int
) -> list:
    if tf == "H1":
        since = now - period_days * 86_400_000
        return db.get_candles(instrument_id, "H1", start_ms=since)
    limit = _TF_CANDLE_LIMIT.get(tf)
    if limit is None:
        return []
    return db.get_candles(instrument_id, tf)[-limit:]


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
) -> Optional[str]:
    """PNG графика выбранного контекста: свечи + слои htf/bos/range/entries.

    Подтверждённый уровень BOS/SMS — сплошная линия, ожидаемый — пунктир.
    now — момент снимка: хендлер передаёт тот же now, что использовал для
    caption, чтобы цена/сценарий/время на картинке и в подписи совпадали.
    None — наблюдение не найдено или свечей нет (хендлер отвечает текстом).
    """
    import mplfinance as mpf
    from matplotlib import pyplot as plt
    from matplotlib.patches import Rectangle

    obs = db.get_ltf_observation(observation_id)
    if obs is None:
        return None
    ins = db.get_instrument(obs.instrument_id)
    now = now if now is not None else now_ms()
    candles = _chart_candles(db, obs.instrument_id, tf, period_days, now)
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
        figsize=(10, 5.5),
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        tight_layout=True,
    )
    ax = axes[0]

    # Внутренняя ось X mplfinance — позиции свечей 0..n-1 (как в chartimg)
    n = len(candles)
    pad_right = max(16, int(n * 0.32))
    xr = n - 0.5 + pad_right

    def x_since(ts: int) -> float:
        return max(sum(1 for c in candles if c.open_time < ts) - 0.5, -0.5)

    extra_levels: list[float] = []

    def tag(price: float, color: str, label: str) -> None:
        ax.text(
            0.995, price, f"{label} {price:g}",
            transform=ax.get_yaxis_transform(),
            fontsize=7, color=color, ha="right", va="bottom", zorder=5,
        )

    # ---- слой HTF-контекста: прямоугольник родительской зоны ----
    pz = (info or {}).get("parent_zone")
    if "htf" in layer_set and pz:
        lo, hi = pz["lower"], pz["upper"]
        x0 = x_since(pz.get("display_from") or pz["formed_at"])
        ax.add_patch(Rectangle(
            (x0, lo), n - 0.5 - x0, hi - lo,
            facecolor=_COLOR_HTF, alpha=0.15,
            edgecolor=_COLOR_HTF, linewidth=1.0, zorder=1,
        ))
        for price in (hi, lo):
            ax.hlines(price, x0, xr, color=_COLOR_HTF, linewidth=1.0, zorder=4)
        ax.hlines((lo + hi) / 2, x0, xr, color=_COLOR_HTF,
                  linewidth=0.9, linestyles="--", zorder=4)
        tag(hi, _COLOR_HTF, f"HTF {pz['type'].upper()} {pz['timeframe']}")
        extra_levels += [lo, hi]

    # ---- слой BOS/SMS: подтверждённый сплошной, ожидаемый пунктиром ----
    if "bos" in layer_set and info:
        for ev in info["structure_events"]:
            level = ev.get("break_level")
            if ev.get("kind") not in ("bos", "sms") or level is None:
                continue
            x0 = x_since(ev.get("break_candle_open_time") or ev["occurred_at"])
            ax.hlines(level, x0, xr, color=_COLOR_BOS, linewidth=1.2, zorder=4)
            tag(level, _COLOR_BOS, f"{ev['kind'].upper()} ✓")
            extra_levels.append(level)
        expected = info.get("expected") or {}
        for key in ("bos", "sms"):
            exp = expected.get(key)
            if exp:
                ax.hlines(exp["level"], -0.5, xr, color=_COLOR_BOS,
                          linewidth=1.0, linestyles="--", zorder=4)
                tag(exp["level"], _COLOR_BOS, f"{key.upper()} ?")
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
            tag(cur_range["upper"], _COLOR_RANGE, "P/D")
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
            extra_levels += [e["lower"], e["upper"]]

    label = source_label
    if not label and ins is not None:
        label = f"{ins.venue} {ins.market_type} / {ins.symbol}"
    title = f"{label} — {tf}"
    if tf == "H1":
        title += f", {period_days} дн."
    ax.set_title(title, loc="left", fontsize=11)

    ax.set_xlim(-0.5, xr)
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if extra_levels:
        hi = max(hi, max(extra_levels))
        lo = min(lo, min(extra_levels))
    span = (hi - lo) or 1.0
    ax.set_ylim(lo - span * 0.04, hi + span * 0.10)

    snap = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    ax.text(
        0.0, -0.12,
        f"Источник: {label}. Снимок: {snap:%Y-%m-%d %H:%M UTC}.",
        transform=ax.transAxes, fontsize=8, color="#8b95a3",
    )

    fig.savefig(out, dpi=110)
    plt.close(fig)
    return str(out)
