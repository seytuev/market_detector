"""Concise notification copy; full original templates remain in Details."""
from __future__ import annotations

from .formatting import fmt_pct_ru, fmt_price_ru, fmt_time_msk
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


def delivery_delay(event_at, delivered_at):
    minutes = max(0, (delivered_at - event_at) // 60_000)
    if minutes < 2:
        return ""
    age = f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"
    return f"⚠️ Доставка через {age} после события · проверьте текущее состояние"


def ltf_key(ev, ctx):
    # Whitelist actual market facts. Scenario-local IDs and range versions
    # are bookkeeping, not distinct opportunities. Preserve eligibility flags.
    p = ev.payload or {}
    ins, sc = ctx.instrument, ctx.scenario
    # Один рыночный слом — одна карточка, даже если отменены несколько сценариев.
    # Ключ не включает observation, range_version и время опроса.
    transition = p.get("market_transition_key")
    if transition:
        return fingerprint([
            ins.venue if ins else None,
            ins.market_type if ins else None,
            ins.id if ins else ev.observation_id,
            transition,
            "market_transition",
        ])
    entries = [{k: e.get(k) for k in (
        "type", "lower", "upper", "mid", "overlap", "outside_premium", "context"
    )} for e in p.get("entries", [])]
    entries.sort(key=fingerprint)
    actual = {k: p.get(k) for k in (
        "type", "lower", "upper", "price", "level", "close_price", "break_level",
        "close", "stage", "outcome", "reason", "outside_premium", "range_pending",
    )}
    actual["range"] = {k: (p.get("range") or {}).get(k) for k in ("lower", "upper", "mid")}
    if ev.kind in ("bos", "sms"):
        # Range, stage and readiness belong to a parent scenario. They cannot
        # turn the same physical break into several market notifications.
        actual = {k: p.get(k) for k in ("break_level", "close", "close_price")}
        actual["entries"] = []
    elif ev.kind == "entries_ready":
        # All new zones on one H1 close are one delta, across HTF parents.
        actual = {"entries": []}
    else:
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
                        p.get("direction") or (sc.direction.value if sc else None),
                        None if ev.kind in ("bos", "sms", "entries_ready") else
                        sc.trigger if sc else p.get("kind"), ev.kind, ev.occurred_at, actual])


def ordered_entries(entries, reference=None):
    """Unique market objects, nearest to the EVENT price (never a live quote)."""
    unique = {}
    for entry in entries:
        key = (entry.get("type"), entry.get("lower"), entry.get("upper"))
        if key not in unique:
            unique[key] = dict(entry)
        elif entry.get("outside_premium"):
            unique[key]["outside_premium"] = True
    def distance(entry):
        lo, hi = entry.get("lower"), entry.get("upper")
        if reference is None or lo is None or hi is None:
            return float("inf")
        return max(lo - reference, reference - hi, 0)
    return sorted(unique.values(), key=lambda e: (distance(e), e.get("lower") or 0,
                                                 e.get("upper") or 0, e.get("type") or ""))


def ltf_text(ev, ctx, contexts=1):
    p, sc = ev.payload, ctx.scenario
    direction = p.get("direction") or (sc.direction.value if sc else "")
    ru = {"bull": "рост", "bear": "снижение"}.get(direction, "")
    symbol = ctx.instrument.symbol if ctx.instrument else "Инструмент"
    subtype = (str(p["type"]) + " ") if p.get("type") else ""
    title = f"{direction_icon(direction)} {symbol} · {subtype}H1" + (f" · {ru}" if ru else "")
    headline = {
        "bos": "✅ BOS подтверждён", "sms": "✅ SMS подтверждён",
        "entries_ready": "🎯 Новые зоны для наблюдения за входом", "range_ready": "⏳ Диапазон подтверждён · зон входа пока нет",
        "touch": "🎯 Цена коснулась зоны входа", "sweep_confirmed": "✅ Снятие ликвидности подтверждено",
        "sweep_failed": "⚠️ Уровень пройден с закреплением", "cancellation": "⚪ Сценарий отменён",
        "correction": "✏️ Исправление исходных данных",
        "context_transition": "🔀 Изменение контекста",
    }.get(ev.kind, ev.kind)
    if ev.kind == "sweep_failed" and p.get("outcome") == "equal_close":
        headline = "⚠️ Закрытие на уровне · снятие не подтверждено"
    if ev.kind == "range_ready" and p.get("range_status") == "provisional":
        headline = "⏳ Предварительный диапазон построен · зон входа пока нет"
    elif ev.kind == "range_ready" and p.get("range_pending"):
        headline = "⏳ Ждём диапазон текущего движения H1"
    lines = [title, headline]
    if ev.kind in ("correction", "context_transition"):
        # §9/§10.3: готовый смысловой текст события в payload
        if p.get("text"):
            lines.append(p["text"])
    elif ev.kind == "cancellation":
        from ..texts_ru import LTF_CANCELLATION_RU
        lines.append("Причина: " + LTF_CANCELLATION_RU.get(p.get("reason"), p.get("reason", "не указана")))
        lines.append("Новые входы по этому сценарию больше не рассматриваются")
    else:
        if ev.kind != "entries_ready":
            range_label = ("PD H1 движения" if p.get("range_source") == "local_h1" else
                           "Диапазон сценария" if p.get("range_source") == "scenario" else "Диапазон")
            lines.append(bounds(p if p.get("lower") is not None else p.get("range") or {},
                                range_label if p.get("range") else "Зона"))
            if p.get("range_status") == "provisional":
                lines.append("PD предварительный · экстремум ещё не подтверждён")
        close = p.get("close_price") if p.get("close_price") is not None else p.get("close")
        reference = p.get("price") if p.get("price") is not None else close
        if p.get("price") is not None:
            lines.append(f"Цена события: {fmt_price_ru(p['price'])}")
        if p.get("level") is not None and p.get("level") != p.get("lower"):
            lines.append(f"Уровень: {fmt_price_ru(p['level'])}")
        if p.get("break_level") is not None and ev.kind in ("bos", "sms"):
            lines.append(f"Уровень слома: {fmt_price_ru(p['break_level'])}")
        if close is not None:
            lines.append(f"Закрытие H1: {fmt_price_ru(close)}")
        entries = ordered_entries(p.get("entries") or [], reference)
        if entries:
            lines.append("Зоны по близости к цене события:" if reference is not None else "Зоны наблюдения:")
        for entry in entries[:2]:
            line = f"{str(entry.get('type', 'Зона')).upper()} H1 · {bounds(entry)}"
            if reference is not None and reference > 0 and entry.get("lower") is not None and entry.get("upper") is not None:
                distance = max(entry["lower"] - reference, reference - entry["upper"], 0)
                line += " · цена в зоне" if distance == 0 else f" · до границы {fmt_pct_ru(distance / reference * 100)}"
            lines.append(line)
        if len(entries) > 2:
            lines.append(f"Ещё зон: {len(entries) - 2} · в подробностях")
        if p.get("excluded_entries"):
            lines.append("Исключённые и непроверенные зоны — в подробностях")
        if p.get("outside_premium") or any(e.get("outside_premium") for e in entries):
            area = "Discount" if direction == "bull" else "Premium"
            lines.append(f"⚠️ Есть зона вне {area} · допущена по HTF-контексту")
        if ev.kind == "touch":
            lines.append("Далее: ждём закрытия H1 для проверки возврата за уровень" if p.get("type") in ("BSL", "SSL")
                         else "Далее: проверяем реакцию цены · касание ещё не подтверждает вход")
        elif ev.kind in ("bos", "sms", "entries_ready", "range_ready"):
            if p.get("range_pending"):
                lines.append("Далее: ждём подтверждения экстремума тремя свечами H1 и расчёта зон")
            elif entries:
                lines.append("Далее: ждём реакцию при касании зоны · сам слом не подтверждает вход")
            else:
                lines.append("Далее: ждём подходящие зоны · вход пока не сформирован")
        elif ev.kind == "sweep_confirmed":
            lines.append("H1 закрылась обратно за уровень · повторный вход по нему исключён")
        elif ev.kind == "sweep_failed":
            lines.append("Далее: ждём однозначного закрытия H1 относительно уровня" if p.get("outcome") == "equal_close"
                         else "H1 закрылась за уровнем без возврата · повторный вход по нему исключён")
    # Counts describe bookkeeping, not the strength of a setup. Full context
    # membership remains available through Details and the context picker.
    if ctx.zone is not None:
        zone = ctx.zone
        lines.append(f"Основание HTF: {zone.type.value.upper()} {zone.timeframe} · {bounds({'lower': zone.lower, 'upper': zone.upper})}")
    lines.append(f"🕒 Событие: {fmt_time_msk(ev.occurred_at)}")
    lines.append("STV Market Researcher")
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
