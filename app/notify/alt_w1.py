"""Plain-text W1 cards and causal chart snapshots for the ALTs event journal."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from ..alt.engine_v3 import DAY, WEEK, RULES_VERSION, aggregate_weeks
from ..alt.store_v3 import frame
from .formatting import fmt_price_ru as price, fmt_time_msk as time

_RANK = {"cancelled": 100, "zone_invalidated": 90, "bos_confirmed": 80,
         "breakout": 70, "retest": 60}
_TITLES = {"bos_confirmed": "BOS подтверждён W1", "breakout": "База завершена W1",
           "cancelled": "Сценарий отменён W1", "zone_invalidated": "PD-зона отменена W1",
           "manipulation_started": "Депрессия под базой W1", "mature_frozen": "Новая база W1",
           "boundary_refined": "Уточнение границы базы W1", "cycle_superseded": "Новый цикл W1"}


def load_context(db, rows):
    events = [json.loads(r["payload_json"]) for r in rows]
    events.sort(key=lambda e: (e["event_time_ms"], e["id"]))
    lead = max(events, key=lambda e: (_RANK.get(e["event_type"], 10), e["event_time_ms"]))
    structure = db.conn.execute("SELECT id FROM alt_structure WHERE source_id=? AND structure_key=? AND rules_version=?",
                                (rows[0]["source_id"], rows[0]["structure_key"], RULES_VERSION)).fetchone()
    f = frame(db, structure[0], lead["event_time_ms"]) if structure else None
    source = db.conn.execute("SELECT id,asset_id,venue,symbol FROM alt_instrument_source WHERE id=?", (rows[0]["source_id"],)).fetchone()
    return SimpleNamespace(asset=db.get_alt_asset(rows[0]["asset_id"]),
        source=SimpleNamespace(**dict(source)) if source else None, frame=f,
        structure_id=structure[0] if structure else None, events=events, lead=lead)


def zone_name(event):
    p = event.get("payload", {})
    if p.get("kind") == "boundary":
        return "Нижняя граница базы" if p.get("zone_id", "").endswith(":L") else "Верхняя граница базы"
    return str(p.get("kind") or "PD-зона") + " W1"


def event_title(event):
    if event["event_type"] == "retest":
        p = event.get("payload", {})
        if p.get("kind") == "boundary":
            side = "нижней" if p.get("zone_id", "").endswith(":L") else "верхней"
            return ("Пересечение " if p.get("precision") == "quote_crossing" else "Тест ") + side + " границы базы W1"
        if event.get("payload", {}).get("precision") == "quote_crossing":
            return "Пересечение " + zone_name(event)
        return "Касание " + zone_name(event)
    if event["event_type"] == "zone_invalidated":
        return "Зона " + str(event.get("payload", {}).get("kind") or "PD") + " отменена W1"
    return _TITLES.get(event["event_type"], "Событие базы W1")


def base_period(f):
    start = datetime.fromtimestamp(f["base_start_open_time"]/1000, timezone.utc).year
    end = datetime.fromtimestamp((f["base_end_open_time"] or f["available_at_ms"])/1000, timezone.utc).year
    return str(start) if start == end else f"{start}–{end}"


def selected_scenario(ctx):
    if not ctx.frame:
        return None
    state = ctx.frame["scenario_state"]
    p = ctx.lead.get("payload", {})
    sid = p.get("scenario_id")
    if not sid:
        zone = next((z for z in state.get("pd_zones", []) if z["id"] == p.get("zone_id")), None)
        sid = zone["scenario_id"] if zone else None
    return next((s for s in reversed(state.get("scenarios", []))
                 if s["id"] == sid or sid is None and s["cancelled_at_ms"] is None), None)


def _bounds(z):
    return price(z["lower"]) if z["lower"] == z["upper"] else price(z["lower"])+"–"+price(z["upper"])


def message(ctx, *, delivered_at):
    """Transport alone owns HTML escaping and first-line emphasis."""
    from .compact import delivery_delay
    e, f, src = ctx.lead, ctx.frame, ctx.source
    symbol = ctx.asset.symbol if ctx.asset else "ALTs"
    lines = [f"{symbol} · {event_title(e)}"]
    if src:
        lines.append(f"{src.venue.capitalize()} Spot · {src.symbol}")
    if e["event_type"] == "zone_invalidated" and f:
        zone = next((z for z in f["scenario_state"].get("pd_zones", []) if z["id"] == e.get("payload", {}).get("zone_id")), None)
        if zone:
            lines.append(f"{zone['kind']} W1: {_bounds(zone)}")
    touches = [item for item in ctx.events if item["event_type"] == "retest"]
    seen = set()
    for item in touches[:3]:
        p = item.get("payload", {})
        z = p.get("zone")
        key = p.get("zone_id")
        if z and key not in seen:
            lines.append(f"{zone_name(item)}: {_bounds(z)}")
            seen.add(key)
    precision = e.get("payload", {}).get("precision")
    if e["event_type"] == "retest":
        if precision == "D1":
            lines.append("Тест по закрытой D1 · точное время касания неизвестно")
        else:
            label = "Цена сделки после пересечения" if precision == "quote_crossing" else "Цена касания"
            lines.append(f"{label}: {price(e['price'])}")
    elif e["event_type"] == "bos_confirmed":
        lines.append(f"Закрытие W1 выше уровня BOS {price(e['price'])}")
    elif e["event_type"] in ("breakout", "cancelled", "zone_invalidated"):
        lines.append(f"Закрытие W1: {price(e['price'])}")
    if f:
        archived = " · исторические уровни" if f["base_end_open_time"] is not None else ""
        lines.append(f"База {base_period(f)}: {price(f['lower'])}–{price(f['upper'])}{archived}")
        depression = f["scenario_state"].get("depression")
        if depression and e["event_type"] == "manipulation_started":
            lines.append(f"Минимум депрессии: {price(depression['minimum'])}")
            if depression.get("bos_level") is not None:
                lines.append(f"Ожидается закрытие W1 выше {price(depression['bos_level'])}")
    scenario = selected_scenario(ctx)
    if scenario and e["event_type"] == "retest":
        lines.append(f"BOS W1 подтверждён {time(scenario['bos_at_ms']).split(' ')[0]}")
    p = e.get("payload", {})
    if e["event_type"] == "retest" and p.get("kind") in ("FVG", "OB") and p.get("zone"):
        lines.append(f"Отмена {p['kind']}: закрытие W1 ниже {price(p['zone']['lower'])}")
    if scenario and scenario["cancelled_at_ms"] is None:
        lines.append(f"Отмена сценария: закрытие W1 ниже {price(scenario['minimum'])}")
    if len(ctx.events) > 1:
        lines.append(f"Событий в сообщении: {len(ctx.events)} · все в подробностях")
    lines.append(f"Событие: {time(e['event_time_ms'])}")
    delay = delivery_delay(e["event_time_ms"], delivered_at)
    if delay:
        lines.append(delay)
    text = "\n".join(lines)
    details = [text]
    if f:
        begin = time(f["base_start_open_time"]).split(' ')[0]
        end = time(f["base_end_confirmed_at_ms"]) if f["base_end_confirmed_at_ms"] else "продолжается"
        details += [f"\nПериод базы: с {begin}; {end}.",
                    f"L {price(f['lower'])} · M {price(f['mid'])} · U {price(f['upper'])}."]
    if scenario:
        details += [f"BOS: закрытие W1 выше {price(scenario['bos_level'])}; подтверждён {time(scenario['bos_at_ms'])}.",
                    f"Минимум манипуляции: {price(scenario['minimum'])}."]
    details += ["\nСобытия:"]
    for item in ctx.events:
        z = item.get("payload", {}).get("zone")
        details.append(f"{time(item['event_time_ms'])} · {event_title(item)}" + (f" · {_bounds(z)}" if z else ""))
    details += ["\nПовторные тесты FVG/OB учитываются до закрытия W1 ниже нижней границы зоны.",
                "Отмена PD-зоны и отмена сценария проверяются отдельно. Тени W1 сценарий не отменяют.",
                "Касание фиксирует тест зоны; исполнение ордера по нему не определяется.",
                "График показывает закрытые W1, известные к событию. Цена сделки отмечена отдельно."]
    return text, "\n".join(details)


def snapshot(db, ctx):
    f, e = ctx.frame, ctx.lead
    if not f or not ctx.source:
        return None
    at = e["event_time_ms"]
    begin = max(f["base_start_open_time"]-4*WEEK, at-52*WEEK)
    candles = db.get_alt_candles(ctx.source.id, start_ms=begin, end_ms=at-DAY)
    weeks = aggregate_weeks(candles, as_of_ms=at)
    if not weeks:
        return None
    bars = [{"time": w.t//1000, "open": w.open, "high": w.high, "low": w.low, "close": w.close} for w in weeks]
    overlays = [{"from": f["base_start_open_time"], "to": f["base_end_confirmed_at_ms"] or at,
        "lower": f["lower"], "upper": f["upper"], "mid": f["mid"], "type": "base",
        "color": "#957025", "fill": "rgba(194,153,64,.10)", "label": "База " + base_period(f),
        "price_label": _bounds(f)}]
    if f["base_end_confirmed_at_ms"] and f["base_end_confirmed_at_ms"] <= weeks[0].t:
        overlays = []  # Historical levels remain; do not extend the old base into this cycle.
    state = f["scenario_state"]
    announced = {item.get("payload", {}).get("zone_id") for item in ctx.events}
    touched = {item.get("payload", {}).get("zone_id") for item in ctx.events if item["event_type"] == "retest"}
    scenario = selected_scenario(ctx)
    active = [z for z in state.get("pd_zones", []) if z["available_at_ms"] <= at
              and (z["id"] in announced or scenario and z["scenario_id"] == scenario["id"]
                   and z["invalidated_at_ms"] is None)]
    active.sort(key=lambda z: (z["id"] not in announced, min(abs(e["price"]-z["lower"]), abs(e["price"]-z["upper"]))))
    count = max(4, sum(z["id"] in announced for z in active))
    for z in active[:count]:
        overlays.append({"from": z["available_at_ms"], "to": z["invalidated_at_ms"] or at,
            "lower": z["lower"], "upper": z["upper"], "type": z["kind"],
            "color": "#0b7b6a" if z["kind"] == "FVG" else "#3d67a8",
            "fill": "rgba(11,123,106,.16)" if z["kind"] == "FVG" else "rgba(61,103,168,.12)",
            "muted": z["invalidated_at_ms"] is not None or bool(scenario and scenario["cancelled_at_ms"] is not None),
            "label": z["kind"] + " W1" + (" · отменена" if z["invalidated_at_ms"] is not None else " · тест" if z["id"] in touched else ""),
            "price_label": _bounds(z)})
    levels = [{"from": f["base_start_open_time"], "price": f[side], "label": name, "color": "#957025"}
              for side, name in (("lower", "L"), ("mid", "M"), ("upper", "U"))]
    markers = []
    if scenario:
        levels += [{"price": scenario["bos_level"], "label": "BOS W1", "from": scenario["bos_anchor"], "color": "#0b7b6a"},
                   {"price": scenario["minimum"], "label": "Отмена по закрытию W1", "from": scenario["bos_at_ms"], "color": "#a13244"}]
        markers.append({"time": scenario["bos_at_ms"]//1000-WEEK//1000, "position": "belowBar",
                        "shape": "arrowUp", "color": "#0b7b6a", "text": "BOS W1"})
    symbol = ctx.asset.symbol if ctx.asset else ctx.source.symbol
    result = {"title": f"{symbol} · {event_title(e)} · База {base_period(f)}", "subtitle": f"{ctx.source.venue.capitalize()} Spot · {ctx.source.symbol} · {time(at)}",
        "timeframe": "W1", "as_of": at, "candles": bars, "fit_content": True,
        "overlays": overlays, "levels": levels, "markers": markers,
        "price_bounds": {"min": min([z["lower"] for z in active] + ([scenario["minimum"]] if scenario else [weeks[-1].low])),
                         "max": max([z["upper"] for z in active] or [weeks[-1].high])},
        "caption": ("Исторические уровни базы · " if f["base_end_open_time"] is not None else "") + "W1 закрыты по " + time(weeks[-1].available_at).split(' ')[0]}
    if e["event_type"] == "retest" and e.get("payload", {}).get("precision") != "D1":
        result["quote"] = {"price": e["price"], "label": "Сделка после пересечения" if e["payload"].get("precision") == "quote_crossing" else "Цена касания"}
        result["caption"] += " · цена сделки показана отдельно"
    return result


def render_chart(db, ctx, output):
    from .h1_image import render_snapshot
    data = snapshot(db, ctx)
    return render_snapshot(data, output) if data else None
