"""D1 snapshot from the setup's own source, bounded by event time."""
from pathlib import Path

from .chartimg import candles_to_df, make_dark_style
from .chartlabels import FIG_SIZE, FIG_DPI, set_header, apply_layout, layout_price_labels, set_footer
from .formatting import fmt_price_ru, fmt_time_msk


def render_alt_chart(db, ctx, event, output):
    import mplfinance as mpf
    from matplotlib import pyplot as plt
    if ctx.setup is None or ctx.frozen is None:
        return None
    candles = db.get_alt_candles(ctx.setup.source_id,
                               start_ms=event.event_time_ms - 100 * 86_400_000,
                               end_ms=event.event_time_ms)
    candles = [c for c in candles if c.open_time + 86_400_000 - 1 <= event.event_time_ms][-75:]
    if not candles:
        return None
    fig, axes = mpf.plot(candles_to_df(candles), type="candle", style=make_dark_style(),
                        figsize=FIG_SIZE, returnfig=True, datetime_format="%d.%m", xrotation=0)
    try:
        ax = axes[0]
        frozen = ctx.frozen
        color = "#94a3b8" if ctx.setup.state in {"cancelled", "expired_no_retest", "targets_completed"} else "#34d399"
        ax.axhspan(frozen.lower, frozen.upper, alpha=.16, color=color)
        labels = []
        for price in (frozen.lower, frozen.mid, frozen.upper):
            ax.axhline(price, color=color, linewidth=1.2, linestyle="--" if price == frozen.mid else "-")
            labels.append((price, fmt_price_ru(price), color))
        header = set_header(fig, f"{ctx.asset.symbol} · Накопление D1",
                            f"Сетап #{ctx.setup.id} · {fmt_time_msk(event.event_time_ms)}")
        apply_layout(fig, ax, header)
        layout_price_labels(fig, ax, labels)
        set_footer(fig, "Свечи D1 · время открытия — МСК")
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=FIG_DPI)
        return str(out)
    finally:
        plt.close(fig)
