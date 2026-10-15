"""D1 notifications in the same website format and scale as H1/HTF images."""
from __future__ import annotations

from .alt_templates import _payload
from .formatting import fmt_price_ru, fmt_time_msk
from .h1_image import candle_rows, render_snapshot

DAY = 86_400_000
_LABELS = {
    "forming_started": "Формирование диапазона", "mature_frozen": "Диапазон зафиксирован",
    "manipulation_started": "Манипуляция", "manipulation_ended": "Возврат в диапазон",
    "ssl_taken": "Снятие SSL", "bos_confirmed": "BOS", "sms_confirmed": "SMS",
    "breakout": "Выход из диапазона", "retest": "Ретест", "target_hit": "Цель достигнута",
    "cancelled": "Отмена", "expired_no_retest": "Истёк срок ретеста",
    "targets_completed": "Цели выполнены", "entry_a": "Вход A", "entry_b": "Вход B",
    "review_required": "Проверка", "data_stale": "Данные устарели",
}
_TERMINAL = {"cancelled", "expired_no_retest", "targets_completed"}


def alt_snapshot(db, ctx, event):
    if ctx.setup is None:
        return None
    moment = event.event_time_ms
    candles = db.get_alt_candles(ctx.setup.source_id, start_ms=moment - 168 * DAY, end_ms=moment)
    candles = [c for c in candles if c.open_time + DAY - 1 <= moment]
    if not candles:
        return None
    frozen = ctx.frozen
    overlays = []
    caption = "Сетап #" + str(ctx.setup.id)
    # Today's setup can be closed or frozen later. Those future states must
    # not recolor an earlier event or supply its missing geometry.
    if frozen is not None and frozen.mature_at_ms <= moment:
        overlays.append({
            "from": frozen.start_anchor_open_time, "lower": frozen.lower,
            "upper": frozen.upper, "mid": frozen.mid, "type": "OB",
            "direction": "bull", "label": "Диапазон D1",
            "muted": event.event_type in _TERMINAL,
        })
        caption += f" · диапазон {fmt_price_ru(frozen.lower)}–{fmt_price_ru(frozen.upper)}"
    markers = []
    for row in db.list_alt_structure_events(ctx.setup.id):
        if row.candle_open_time + DAY - 1 <= moment:
            markers.append({"time": row.candle_open_time // 1000,
                            "position": "aboveBar" if row.kind == "SSL" else "belowBar",
                            "shape": "arrowDown" if row.kind == "SSL" else "arrowUp",
                            "color": "#A13244" if row.kind == "SSL" else "#186B50",
                            "text": row.kind})
    for row in db.list_alt_events(ctx.setup.id):
        if row.event_time_ms > moment or row.event_type not in {
                "entry_a", "entry_b", "breakout", "retest", "target_hit", *_TERMINAL}:
            continue
        payload = _payload(row)
        opened = payload.get("candle_open_time", row.event_time_ms - DAY)
        if opened + DAY - 1 > moment:
            continue
        markers.append({"time": opened // 1000, "position": "aboveBar",
                        "shape": "arrowDown", "color": "#865514",
                        "text": _LABELS[row.event_type]})
    return {
        "title": f"{ctx.asset.symbol if ctx.asset else 'ALT'} · Накопление D1 · "
                 f"{_LABELS.get(event.event_type, event.event_type)}",
        "subtitle": f"LevelFrame · {fmt_time_msk(moment)} · снимок события",
        "timeframe": "D1", "as_of": moment, "candles": candle_rows(candles),
        "overlays": overlays, "markers": markers, "caption": caption,
    }


def render_alt_chart(db, ctx, event, output):
    snapshot = alt_snapshot(db, ctx, event)
    return render_snapshot(snapshot, output) if snapshot else None
