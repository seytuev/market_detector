"""Concise notification copy; full original templates remain in Details."""
from __future__ import annotations

from .formatting import fmt_price_ru, fmt_time_msk
from .outbox import fingerprint


def direction_icon(direction):
    return {"bull": "🟢", "bear": "🔴"}.get(direction, "🔵")


def bounds(data, label="Зона"):
    lo, hi = data.get("lower"), data.get("upper")
    if lo is None or hi is None:
        return ""
    if lo == hi:
        return f"Уровень: {fmt_price_ru(lo)}"
    return f"{label}: {fmt_price_ru(lo)}–{fmt_price_ru(hi)}"


def ltf_key(ev, ctx):
    # Whitelist actual market facts. Scenario-local IDs and range versions
    # are bookkeeping, not distinct opportunities. Preserve eligibility flags.
    p = ev.payload
    ins, sc = ctx.instrument, ctx.scenario
    entries = [{k: e.get(k) for k in (
        "type", "lower", "upper", "mid", "overlap", "outside_premium", "context"
    )} for e in p.get("entries", [])]
    entries.sort(key=fingerprint)
    actual = {k: p.get(k) for k in (
        "type", "lower", "upper", "price", "level", "close_price", "break_level",
        "close", "stage", "outcome", "reason", "outside_premium", "range_pending",
    )}
    actual["range"] = {k: (p.get("range") or {}).get(k) for k in ("lower", "upper", "mid")}
    actual["entries"] = entries
    # Cancellations of unrelated parent contexts are not equivalent.
    if ev.kind == "cancellation":
        actual["observation"] = ev.observation_id
    def canonical(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v) for v in value]
        return value
    actual = canonical(actual)
    actual["entries"].sort(key=fingerprint)
    return fingerprint([ins.id if ins else ev.observation_id,
                        ins.venue if ins else None, ins.market_type if ins else None,
                        sc.direction.value if sc else p.get("direction"),
                        sc.trigger if sc else p.get("kind"), ev.kind, ev.occurred_at, actual])


def ltf_text(ev, ctx, contexts=1):
    p, sc = ev.payload, ctx.scenario
    direction = sc.direction.value if sc else p.get("direction", "")
    ru = {"bull": "бычий", "bear": "медвежий"}.get(direction, "")
    symbol = ctx.instrument.symbol if ctx.instrument else "Инструмент"
    subtype = (str(p["type"]) + " ") if p.get("type") else ""
    title = f"{direction_icon(direction)} {symbol} · {subtype}H1" + (f" · {ru}" if ru else "")
    headline = {
        "bos": "✅ BOS подтверждён", "sms": "✅ SMS подтверждён",
        "entries_ready": "🎯 Новые зоны входа", "range_ready": "⏳ Диапазон готов · ждём зоны входа",
        "touch": "🎯 Цена коснулась зоны входа", "sweep_confirmed": "✅ Снятие ликвидности подтверждено",
        "sweep_failed": "⚠️ Уровень пройден с закреплением", "cancellation": "⚪ Сценарий отменён",
    }.get(ev.kind, ev.kind)
    if ev.kind == "sweep_failed" and p.get("outcome") == "equal_close":
        headline = "⚠️ Закрытие на уровне · снятие не подтверждено"
    lines = [title, headline]
    if ev.kind == "cancellation":
        from ..texts_ru import LTF_CANCELLATION_RU
        lines.append(LTF_CANCELLATION_RU.get(p.get("reason"), p.get("reason", "")))
    else:
        lines.append(bounds(p if p.get("lower") is not None else p.get("range") or {},
                            "Диапазон" if p.get("range") else "Зона"))
        for label, key in (("Цена события", "price"), ("Уровень", "level"),
                           ("Уровень слома", "break_level"), ("Закрытие H1", "close_price")):
            if p.get(key) is not None:
                lines.append(f"{label}: {fmt_price_ru(p[key])}")
        entries = p.get("entries") or []
        for entry in entries[:2]:
            lines.append(f"{entry.get('type', 'Зона')} H1 · {bounds(entry)}")
        if len(entries) > 2:
            lines.append(f"Ещё зон: {len(entries) - 2} · в подробностях")
        if p.get("outside_premium") or any(e.get("outside_premium") for e in entries):
            lines.append("⚠️ Есть зона вне Premium · допущена по контексту")
        if ev.kind == "touch":
            lines.append("⚠️ Касание ещё не подтверждает вход")
        if ev.kind in ("bos", "sms") and not entries:
            lines.append("⏳ Подходящих зон входа пока нет")
        if ev.kind == "sweep_confirmed" or (ev.kind == "sweep_failed" and p.get("outcome") != "equal_close"):
            lines.append("Уровень снят · повторные входы отключены")
    if contexts > 1:
        lines.append(f"🧩 HTF-контекстов: {contexts}")
    lines.append(f"🕒 {fmt_time_msk(ev.occurred_at)}")
    return "\n".join(line for line in lines if line)


def notification_html(text, limit):
    """Escape all source text; truncate BEFORE escaping, never break entities."""
    from html import escape
    # Conservative UTF-16 budget; emoji may occupy two code units.
    if len(text.encode("utf-16-le")) // 2 > limit:
        suffix = "\n… Остальное — в подробностях"
        budget = limit - len(suffix.encode("utf-16-le")) // 2
        text = text.encode("utf-16-le")[:budget * 2].decode("utf-16-le", errors="ignore").rstrip() + suffix
    head, sep, tail = text.partition("\n")
    return f"<b>{escape(head)}</b>" + (sep + escape(tail) if sep else "")
