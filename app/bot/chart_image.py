"""Interactive bot requests in the shared STV Market Researcher chart style."""
from __future__ import annotations

from ..models import now_ms
from ..notify.chart_snapshot import zone_on_scale
from ..notify.formatting import fmt_time_msk
from ..notify.h1_image import candle_rows, render_snapshot
from .charts import _chart_candles


def current_snapshot(db, observation_id, *, tf="H1", period_days=7,
                     layers=("htf", "bos", "range", "entries"), settings,
                     now=None, instrument_id=None, quote_snapshot=None):
    moment = now if now is not None else now_ms()
    obs = db.get_ltf_observation(observation_id) if observation_id is not None else None
    if observation_id is not None and obs is None:
        return None
    iid = obs.instrument_id if obs is not None else instrument_id
    ins = db.get_instrument(iid) if iid is not None else None
    if ins is None:
        return None
    candles = _chart_candles(db, iid, tf, period_days, moment)
    if not candles:
        return None
    snapshot = {"title": f"{ins.symbol} · {tf}" + (f" · {period_days} дн." if tf == "H1" else ""),
                "subtitle": f"STV Market Researcher · {fmt_time_msk(moment)} · текущая ситуация",
                "timeframe": tf, "as_of": moment, "candles": candle_rows(candles),
                "fit_content": True,
                "caption": f"{len(candles)} свечей {tf}" + (" · последняя свеча формируется" if not candles[-1].closed else "")}
    quote = quote_snapshot if quote_snapshot is not None else db.get_quote(iid)
    if quote and quote[0] is not None and quote[1] is not None and quote[1] <= moment:
        snapshot["quote"] = {"price": quote[0], "at": quote[1]}
    enabled = set(layers)
    if tf == "H1":
        from ..services.h1_chart import assemble_h1_layers
        projected = assemble_h1_layers(
            db, settings, iid, as_of=moment, window_from=candles[0].open_time,
            window_to=moment, context_id=observation_id, setup_event=False, nearby_zones=True)
        if "range" not in enabled:
            projected.update(setup=None, main_structure=None, reversal=None)
        if "htf" not in enabled:
            projected["htf_ideas"] = []
        snapshot["layers"] = projected
        snapshot["layer_settings"] = {"breaks": "bos" in enabled, "expected": "bos" in enabled,
                                      "points": "recent" if "bos" in enabled else "hidden",
                                      "zones": "entries" in enabled, "htfContext": "htf" in enabled}
    else:
        from .cards import htf_zones
        lo, hi = min(c.low for c in candles), max(c.high for c in candles)
        relevant = [z for z in htf_zones(db, iid) if z.timeframe in ("D1", "W1")]
        visible = [z for z in relevant if z.formed_at <= moment and zone_on_scale(z.lower, z.upper, lo, hi)]
        # The card retains the full inventory. A PNG shows the nearest eight
        # zones so that labels and candles remain readable on a phone.
        price = snapshot.get("quote", {}).get("price", candles[-1].close)
        visible.sort(key=lambda z: (0 if z.lower <= price <= z.upper else min(abs(price-z.lower), abs(price-z.upper)), z.id))
        shown = visible[:8] if "htf" in enabled else []
        snapshot["overlays"] = [
            {"from": z.display_from if z.display_from is not None else z.formed_at,
             "lower": z.lower, "upper": z.upper, "mid": z.mid, "type": z.type.value.upper(),
             "direction": z.direction.value, "label": f"{z.type.value.upper()} {z.timeframe}"}
            for z in shown]
        if "htf" in enabled and len(relevant) > len(shown):
            snapshot["caption"] += f" · ещё зон: {len(relevant)-len(shown)} — в карточке HTF"
    return snapshot


def render_bot_chart(db, observation_id, out_path, *, source_label="", **kwargs):
    snapshot = current_snapshot(db, observation_id, **kwargs)
    return render_snapshot(snapshot, out_path) if snapshot else None
