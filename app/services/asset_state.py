"""Состояние актива по структуре H1, отдельно от выбранной HTF-зоны.

Навигация (ближайшая зона, ручной выбор) меняет только блок просмотра.
Уровень BOS берётся из ключа канонической машины, не из границы зоны.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..notify.formatting import fmt_price_ru, fmt_time_msk
from .h1_setup import local_leg_at, revision_at
from .d1_structure import project_daily, project_weekly
from .structure_sync import synchronize_scenario

RULE_VERSION = "asset-context-4"
H1_MS = 3_600_000
_LOOKBACK_BARS = 20
_ACTIVE_SCENARIO = ("range_pending", "monitoring_entries")
_LIVE_OBS = ("waiting_structure", "active", "paused_data")
_WORKED_STATUS = {"archived", "worked", "taken", "invalidated", "rejected"}
_WORKED_INTERACTION = {"full_fill"}

_QUALITY_RU = {
    "no_h1_candles": "нет закрытых свечей H1",
    "current_candle_pending": "ожидается текущая свеча H1",
    "no_quote": "нет котировки",
    "quote_stale": "котировка устарела",
    "h1_stale": "свечи H1 устарели",
    "source_stale": "источник данных устарел",
    "processing_lag": "обработка H1 отстаёт",
    "history_gap": "в истории H1 есть разрыв",
    "replay_in_progress": "идёт исторический расчёт LTF",
}

_BASIS_RU = {
    "manual": "ручной выбор",
    "price_inside": "цена внутри зоны",
    "nearest": "близость к цене",
    "last_scenario": "последний сценарий",
    "last_contact": "последний контакт",
}

_TYPE_RU = {
    "fvg": "FVG", "ob": "OB", "manual": "MANUAL", "bsl": "BSL", "ssl": "SSL",
    "prb": "PRB", "breaker": "Breaker",
}

_INTERACTION_RU = {
    "touch": "касание",
    "partial_fill": "частичное заполнение",
    "full_fill": "заполнение",
    "level_cross": "пересечение",
    "sweep_reclaim": "снятие с возвратом",
    "break_acceptance": "принятие пробоя",
}


def project_asset(
    db,
    instrument_id: int,
    as_of: int,
    *,
    navigation: Optional[dict[str, Any]] = None,
    price: Optional[float] = None,
    data_state: Optional[dict[str, Any]] = None,
    setup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Один снимок на as_of. Запись сценариев и уведомлений не создаёт."""
    moment = int(as_of)
    nav = navigation or {}
    leg = local_leg_at(db, instrument_id, moment)
    transition = _transition(db, leg) if leg else None
    proven = bool(transition and transition.get("proven"))
    revision = revision_at(db, int(leg["id"]), moment) if leg else None
    pd = _pd(leg, revision, setup)
    quality_bad, quality_text = _quality(data_state)
    forming_note = _forming_note(data_state)
    bull = bool(leg and leg.get("direction") == "bull")
    sides = _sides(db, instrument_id, moment, leg)
    zones = _zones(db, instrument_id, leg, pd, setup) if leg and proven else []
    display_zones = _entry_zone_states(db, instrument_id, zones, leg, setup, moment, price, data_state) if proven else []
    zones = [z for z in display_zones if z.get("eligible", True) and z.get("invalidated_at") is None]
    evidence = _evidence_rows(db, sides, leg)
    liquidity = _liquidity(db, instrument_id, moment)
    history = _history(db, instrument_id, moment, leg, evidence)
    invalidation = _invalidation(leg, transition if proven else None)
    state = _state(
        leg, proven, quality_bad, sides, zones, price, pd, data_state,
    )
    compact = _compact(
        state, transition if proven else None, evidence, zones, pd,
        invalidation, quality_text, leg, quality_bad, forming_note,
    )
    compact_rows = _compact_rows(
        state, transition if proven else None, evidence, display_zones, pd,
        invalidation, quality_text, leg, quality_bad, forming_note, moment,
    )
    main = _main_structure(db, instrument_id, moment)
    if main and main.get("lower") is not None and main.get("upper") is not None:
        compact_rows.append({
            "kind": "pd", "k": "Основной PD H1",
            "v": f"{fmt_price_ru(main['lower'])} → {fmt_price_ru(main['upper'])} · 50% {fmt_price_ru(main['eq'])}",
            "note": "BOS ↑" if main["direction"] == "bull" else "BOS ↓",
        })
    daily = project_daily(db, instrument_id, moment)
    alignment = synchronize_scenario(
        db, instrument_id, daily, state["trade_direction"], moment,
        reference=price if price is not None else (transition or {}).get("close_price"),
        h1_confirmed=proven, has_htf_source=bool(evidence),
    )
    daily_line = daily["text"] + ". " + alignment["status"]
    if alignment["target_text"]:
        daily_line += ". Цель: " + alignment["target_text"]
    if len(compact) < 5:
        compact.append(daily_line)
    else:
        compact[-1] += "\n" + daily_line
    lead_rows = [r for r in compact_rows if r["kind"] in ("grounds", "bos")]
    tail_rows = [r for r in compact_rows if r["kind"] not in ("grounds", "bos")]
    compact_rows = [
        {"kind": "daily_structure", "k": "Структура D1", "v": daily["text"],
         "dir": "long" if daily["direction"] == "bull" else "short" if daily["direction"] else None},
    ] + sorted(lead_rows, key=lambda r: r["kind"] == "bos") + [
        {"kind": "daily_alignment", "k": "Приоритет D1",
         "v": "По направлению D1" if alignment["priority"] == "main"
         else "Контртрендовый · против D1" if alignment["priority"] == "countertrend"
         else "Направление D1 не определено",
         "note": "H1 подтверждён из HTF-зоны" if alignment["confirmation_complete"]
         else "Ждём подтверждение H1 из HTF-зоны"},
    ]
    if alignment["target_text"]:
        compact_rows.append({"kind": "target", "k": "Цель", "v": alignment["target_text"]})
    compact_rows.extend(tail_rows)
    snapshot_id = "asset:{iid}:{as_of}:{move}:{key}:{code}".format(
        iid=instrument_id,
        as_of=moment,
        move=int(leg["id"]) if leg else 0,
        key=(transition or {}).get("event_key") or "none",
        code=state["code"],
    )
    other = _other_contexts(db, instrument_id, moment, leg, nav, sides)
    # Navigation may point at a nearby, still waiting HTF zone. The header
    # must describe the same instrument-wide scenario as the facts below it.
    side = (leg or {}).get("direction") if proven else (
        "bull" if state["trade_direction"] == "long" else
        "bear" if state["trade_direction"] == "short" else None
    )
    candidates = ((sides["linked"] if leg else sides["active"]).get(side) or []) if side else []
    current = max(candidates, key=lambda row: (row["created_at"], row["id"]), default=None)
    scenario_summary = {
        "id": current["id"] if current else None,
        "confirmed": current is not None,
        "state": state["setup_state"],
        "label": "Сценарий подтверждён" if current else state["title"],
        "direction": side,
        "confirmed_at": current["created_at"] if current else None,
        "detail": state["explanation"],
        "market_state": state["title"],
    }
    return {
        "governs": state["governs"],
        "scenario_summary": scenario_summary,
        "asset_state": {
            "code": state["code"],
            "direction": state["trade_direction"],
            "title": state["title"],
            "reason_code": state["reason_code"],
        },
        "structure": {
            "direction": leg.get("direction") if leg else None,
            "epoch_id": leg.get("structure_epoch_id") if leg else None,
            "movement_id": int(leg["id"]) if leg else None,
            "last_transition": transition if proven else None,
            "proven": proven,
            "main": _main_structure(db, instrument_id, moment),
        },
        "daily_structure": daily,
        "weekly_structure": project_weekly(db, instrument_id, moment),
        "trade_setup": {
            "daily_alignment": alignment,
            "state": state["setup_state"],
            "direction": state["trade_direction"],
            "context_episode_ids": sides["episode_ids"],
            "blockers": state["blockers"],
        },
        "context_evidence": evidence,
        "next_action": {
            "kind": state["action"],
            "zones": [
                {"type": z["type"], "admitted": z["admitted"]} for z in zones
            ],
            "explanation": state["explanation"],
        },
        "invalidation": invalidation,
        "navigation": {
            "selected_context_id": nav.get("selected_context_id"),
            "selection_basis": nav.get("selection_basis"),
        },
        "history_summary": history["summary"],
        "as_of": moment,
        "snapshot_id": snapshot_id,
        "data_quality": _data_quality_label(
            quality_bad, forming_note, leg, proven,
        ),
        "pd": pd,
        "compact": compact,
        "compact_rows": compact_rows,
        "details": {
            "structure_sync": [
                daily["text"] + " · снимок " + fmt_time_msk(moment),
                *[f"BOS D1 {'вверх' if e['direction'] == 'bull' else 'вниз'}: "
                  f"{fmt_price_ru(e['level'])} · {fmt_time_msk(e['occurred_at'])}"
                  for e in [next((e for e in reversed(daily["events"]) if e["kind"] == "BOS"), None)]
                  if e is not None],
                *([alignment["target_text"]] if alignment["target_text"] else []),
                "BOS меняет основную структуру D1; SMS меняет локальное направление внутри её опор.",
                "Новый контртрендовый сценарий требует подтверждения H1 из HTF-зоны.",
                "Обратный BOS D1 меняет приоритет действующего сценария, сохраняя его зоны входа.",
                "Цель контртренда — ближняя граница актуальной противоположной OB/FVG/Breaker/PRB D1.",
            ],
            "grounds": _ground_lines(evidence),
            "zones": [
                "{typ} {lo}–{hi}".format(
                    typ=z.get("type") or "зона",
                    lo=fmt_price_ru(z["admitted"][0]),
                    hi=fmt_price_ru(z["admitted"][1]),
                )
                for z in zones if z.get("admitted")
            ],
            "other_contexts": other,
            "liquidity": liquidity,
            "history": history["lines"],
            "diagnostics": _diagnostics(
                instrument_id, moment, leg, transition, state, nav, sides, bull,
            ),
        },
    }


def _main_structure(db, instrument_id, as_of):
    snapshot = _loads(db.get_meta(f"h1:main_structure:{instrument_id}"))
    if snapshot and int(snapshot.get("as_of") or 0) <= as_of:
        return snapshot
    return None


def _transition(db, leg: dict) -> dict[str, Any]:
    """Уровень только из 5-частного ключа машины. Граница зоны не читается."""
    raw = str(leg.get("trigger_bos_key") or "")
    evidence = _loads(leg.get("evidence"))
    blank = {
        "event_key": raw or None,
        "kind": None,
        "direction": leg.get("direction"),
        "broken_anchor_key": leg.get("broken_anchor_key"),
        "break_level": None,
        "break_candle_open_time": evidence.get("break_open"),
        "break_candle_close_time": leg.get("bos_at"),
        "close_price": evidence.get("close_price"),
        "occurred_at": leg.get("bos_at"),
        "proven": False,
    }
    if "@" not in raw:
        return blank
    level_key, suffix = raw.rsplit("@", 1)
    parts = level_key.split(":")
    if len(parts) != 5:
        return blank
    kind, _stage, _role, _ref, price_token = parts
    if kind.upper() not in ("BOS", "SMS"):
        return blank
    try:
        level = float(price_token)
        suffix_at = int(suffix)
    except (TypeError, ValueError):
        return blank
    anchor = str(leg.get("broken_anchor_key") or "")
    break_open = evidence.get("break_open")
    bos_at = leg.get("bos_at")
    if not anchor.startswith("pivot:"):
        return blank
    if break_open is None or bos_at is None:
        return blank
    if int(bos_at) != suffix_at:
        return blank
    close_price = evidence.get("close_price")
    if close_price is None:
        close_price = _candle_close(db, int(leg.get("instrument_id") or 0), int(break_open))
    if close_price is None:
        return blank
    blank.update({
        "kind": kind.upper(),
        "break_level": level,
        "break_candle_open_time": int(break_open),
        "break_candle_close_time": int(bos_at),
        "close_price": float(close_price),
        "occurred_at": int(bos_at),
        "proven": True,
    })
    return blank


def _candle_close(db, instrument_id: int, open_time: int) -> Optional[float]:
    if not instrument_id:
        return None
    row = db.conn.execute(
        """SELECT close FROM candle
           WHERE instrument_id=? AND timeframe='H1' AND open_time=? AND closed=1
           LIMIT 1""",
        (instrument_id, int(open_time)),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _pd(leg, revision, setup) -> Optional[dict[str, Any]]:
    if setup and leg and setup.get("movement_id") == leg.get("id"):
        if setup.get("lower") is None:
            return None
        return {
            "lower": setup.get("lower"),
            "upper": setup.get("upper"),
            "eq": setup.get("eq"),
            "range_status": setup.get("range_status"),
            "label": setup.get("pd_label"),
        }
    if revision is None or revision.get("lower") is None or revision.get("eq") is None:
        return None
    status = revision.get("range_status")
    label = "PD не построен"
    if status == "provisional":
        label = "предварительный PD"
    elif status == "confirmed":
        label = "PD подтверждён"
    return {
        "lower": revision.get("lower"),
        "upper": revision.get("upper"),
        "eq": revision.get("eq"),
        "range_status": status,
        "label": label,
    }


def _forming_note(data_state: Optional[dict]) -> str:
    """Текущая незакрытая H1 — не дыра в истории закрытых свечей."""
    if data_state and data_state.get("reason") == "current_candle_pending":
        return "Текущая свеча формируется. Ожидаем данные текущей свечи H1."
    return ""


def _data_quality_label(quality_bad: bool, forming_note: str, leg, proven: bool) -> str:
    if forming_note and not quality_bad and not (leg and not proven):
        return "forming"
    if quality_bad or (leg and not proven):
        return "incomplete"
    return "ok"


def _quality(data_state: Optional[dict]) -> tuple[bool, str]:
    if not data_state:
        return False, ""
    reason = data_state.get("reason")
    if reason == "current_candle_pending":
        return False, ""
    state = data_state.get("state")
    if state in (None, "ok"):
        return False, ""
    if reason == "no_h1_candles":
        return True, "Не хватает свечей H1"
    text = _QUALITY_RU.get(reason) or "данные H1 неполные"
    if reason == "replay_in_progress":
        progress = data_state.get("channels", {}).get("replay", {}).get("progress") or {}
        if progress.get("total"):
            text += f" · {progress['processed']:,} из {progress['total']:,} свечей H1".replace(",", " ")
    return True, text


def _sides(db, instrument_id: int, as_of: int, leg) -> dict[str, Any]:
    """Обе стороны на одной отсечке. Близость зоны сюда не входит."""
    bos_at = int(leg["bos_at"]) if leg else None
    direction_started_after = -1
    if leg:
        # A later same-direction BOS/SMS can start a new local PD without
        # opening a second trade. Keep the active scenario from the current
        # uninterrupted direction, but never resurrect one across a reversal.
        row = db.conn.execute(
            """SELECT MAX(bos_at) AS reversed_at FROM h1_local_leg
               WHERE instrument_id=? AND direction<>? AND bos_at<=?""",
            (instrument_id, leg["direction"], bos_at),
        ).fetchone()
        if row is not None and row["reversed_at"] is not None:
            direction_started_after = int(row["reversed_at"])
    scenarios = [
        dict(row) for row in db.conn.execute(
            """SELECT s.id, s.state, s.direction, s.created_at, s.cancelled_at,
                      s.cancellation_reason, o.id AS observation_id, o.zone_id,
                      o.activated_at, z.type AS zone_type, z.timeframe,
                      z.lower, z.upper, z.status AS zone_status
               FROM ltf_scenario s
               JOIN ltf_observation o ON o.id=s.observation_id
               LEFT JOIN zone z ON z.id=o.zone_id
               WHERE o.instrument_id=? AND s.created_at<=?""",
            (instrument_id, as_of),
        ).fetchall()
    ]
    observations = [
        dict(row) for row in db.conn.execute(
            """SELECT o.id, o.direction, o.state, o.activated_at, o.zone_id,
                      z.type AS zone_type, z.timeframe, z.lower, z.upper, z.status AS zone_status
               FROM ltf_observation o
               JOIN zone z ON z.id=o.zone_id
               WHERE o.instrument_id=? AND o.activated_at<=?
                 AND o.state IN ('waiting_structure','active','paused_data')""",
            (instrument_id, as_of),
        ).fetchall()
    ]
    episodes = _episodes(
        db, instrument_id, as_of, bos_at,
        leg.get("direction") if leg else None,
    )
    linked: dict[str, list] = {"bull": [], "bear": []}
    active: dict[str, list] = {"bull": [], "bear": []}
    cancelled: dict[str, list] = {"bull": [], "bear": []}
    for row in scenarios:
        direction = row.get("direction")
        if direction not in active:
            continue
        if row.get("state") in _ACTIVE_SCENARIO:
            active[direction].append(row)
            if leg is not None and int(row.get("created_at") or 0) > direction_started_after:
                linked[direction].append(row)
        elif row.get("state") == "cancelled":
            cancelled[direction].append(row)
    return {
        "linked": linked,
        "active": active,
        "cancelled": cancelled,
        "observations": observations,
        "episodes": episodes,
        "episode_ids": [int(e["id"]) for e in episodes if e.get("supports_current")],
    }


def _episodes(db, instrument_id: int, as_of: int, bos_at: Optional[int],
               direction: Optional[str]) -> list[dict]:
    try:
        from .htf_context import context_supports_direction, episode_sources
    except Exception:
        return []
    rows = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND started_at<=?""",
        (instrument_id, as_of),
    ).fetchall()
    out = []
    for raw in rows:
        episode = dict(raw)
        if episode.get("invalidated_at") and int(episode["invalidated_at"]) <= as_of:
            continue
        if episode.get("closed_at") and int(episode["closed_at"]) <= as_of:
            episode["closed"] = True
        else:
            episode["closed"] = False
        if episode["closed"] and episode.get("state") not in ("confirmed",):
            continue
        loaded = episode_sources(db, int(episode["id"]), as_of)
        episode["sources"] = loaded
        before = [
            s for s in loaded
            if bos_at is None or int(s.get("interaction_at") or 0) <= bos_at
        ]
        episode["sources_before"] = before
        episode["supports"] = {}
        for side in ("bull", "bear"):
            episode["supports"][side] = bool(before) and context_supports_direction(
                db, episode, before, side,
            )
        episode["supports_current"] = bool(
            direction and episode["supports"].get(direction) and not episode["closed"]
        )
        out.append(episode)
    return out


def _zones(db, instrument_id: int, leg, pd, setup) -> list[dict]:
    if setup and setup.get("movement_id") == leg.get("id"):
        regions = []
        for row in setup.get("eligible_regions") or []:
            admitted = row.get("admitted")
            if not admitted:
                continue
            regions.append({
                "type": row.get("type") or "зона",
                "timeframe": row.get("timeframe"),
                "lower": row.get("lower"),
                "upper": row.get("upper"),
                "admitted": [admitted[0], admitted[1]],
                "formed_at": row.get("formed_at"),
                "confirmed_at": row.get("confirmed_at"),
                "eligible": True,
            })
        return regions
    if not pd or pd.get("lower") is None or pd.get("eq") is None or pd.get("upper") is None:
        return []
    direction = leg.get("direction")
    bull = direction == "bull"
    half_low, half_high = (pd["lower"], pd["eq"]) if bull else (pd["eq"], pd["upper"])
    origin = leg.get("origin_at")
    window = None if origin is None else int(origin) - _LOOKBACK_BARS * H1_MS
    query = """SELECT type, lower, upper, formed_at, confirmed_at, validity
           FROM ltf_entry_zone
           WHERE instrument_id=? AND direction=? AND confirmed_at IS NOT NULL
             AND lower<=? AND upper>=?"""
    params = [instrument_id, direction, half_high, half_low]
    if window is not None:
        query += ' AND (formed_at IS NULL OR formed_at>=?)'
        params.append(window)
    rows = db.conn.execute(query, params).fetchall()
    found = []
    seen = set()
    for row in rows:
        formed = row["formed_at"]
        if window is not None and formed is not None and int(formed) < window:
            continue
        lo = max(float(row["lower"]), float(half_low))
        hi = min(float(row["upper"]), float(half_high))
        if lo > hi:
            continue
        key = (row["type"], lo, hi)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "type": row["type"],
            "timeframe": None,  # в ltf_entry_zone таймфрейма нет
            "lower": row["lower"],
            "upper": row["upper"],
            "admitted": [lo, hi],
            "formed_at": formed,
            "confirmed_at": row["confirmed_at"],
            "eligible": row["validity"] != "invalid",
        })
    return found


def _entry_zone_states(db, instrument_id, zones, leg, setup, as_of, price, data_state):
    """Факты для карточки: котировка внутри и первое закрытие за дальней границей.

    Неактуальная зона остаётся видимой, но в next_action и торговое состояние
    больше не попадает. Один проход по свечам текущего окна, без записи в БД.
    """
    if not leg:
        return []
    candidates = list(zones)
    if setup and setup.get("movement_id") == leg.get("id"):
        for row in setup.get("candidates") or []:
            if row.get("status") == "forming" or not row.get("admitted"):
                continue
            candidates.append({**row, "eligible": False})
    unique = {}
    for zone in candidates:
        if zone.get("confirmed_at") is not None and zone["confirmed_at"] > as_of:
            continue
        key = (zone["type"], *zone["admitted"])
        unique.setdefault(key, zone)
    if not unique:
        return []
    start = min(z.get("confirmed_at") or z.get("formed_at") or as_of for z in unique.values())
    candles = db.get_candles(instrument_id, "H1", start_ms=int(start), end_ms=as_of, closed_only=True)
    bull = leg.get("direction") == "bull"
    quote_reason = (data_state or {}).get("reason")
    quote_status = ((data_state or {}).get("channels") or {}).get("quote", {}).get("status")
    price_known = price is not None and quote_status not in ("stale", "missing") and quote_reason not in (
        "no_quote", "quote_stale", "quote_processing_failed", "quote_processing_lag",
    )
    display = []
    for zone in unique.values():
        invalidated_at = None
        confirmed = zone.get("confirmed_at") or zone.get("formed_at") or as_of
        far = zone.get("lower") if bull else zone.get("upper")
        if far is not None:
            for candle in candles:
                if candle.open_time < confirmed or candle.close_time > as_of:
                    continue
                beyond = candle.close < far if bull else candle.close > far
                if beyond:
                    invalidated_at = candle.close_time
                    break
        if not zone.get("eligible", True) and invalidated_at is None:
            continue
        lo, hi = zone.get("lower"), zone.get("upper")
        if lo is None or hi is None:
            lo, hi = zone["admitted"]
        display.append({**zone, "invalidated_at": invalidated_at,
                        "price_inside": bool(price_known and lo <= price <= hi)})
    return display


def _evidence_rows(db, sides, leg) -> list[dict]:
    if not leg:
        return []
    direction = leg.get("direction")
    rows = []
    seen = set()
    for episode in sides["episodes"]:
        if not episode.get("supports", {}).get(direction) or episode.get("closed"):
            continue
        for source in episode.get("sources_before") or []:
            zone_id = source.get("zone_id")
            key = ("episode", zone_id, source.get("interaction"), source.get("interaction_at"))
            if key in seen:
                continue
            seen.add(key)
            zone = db.get_zone(int(zone_id)) if zone_id else None
            interaction = source.get("interaction")
            worked = interaction in _WORKED_INTERACTION or (
                zone is not None and str(zone.status.value if hasattr(zone.status, "value") else zone.status) in _WORKED_STATUS
            )
            rows.append({
                "source_id": zone_id,
                "type": zone.type.value if zone is not None else None,
                "timeframe": zone.timeframe if zone is not None else None,
                "interaction": interaction,
                "occurred_at": source.get("interaction_at"),
                "role": "worked_support" if worked else "support",
                "lower": zone.lower if zone is not None else None,
                "upper": zone.upper if zone is not None else None,
                "zone_status": zone.status.value if zone is not None and hasattr(zone.status, "value") else None,
            })
    for scenario in sides["linked"].get(direction) or []:
        zone_id = scenario.get("zone_id")
        key = ("scenario", zone_id)
        if key in seen or zone_id is None:
            continue
        seen.add(key)
        status = scenario.get("zone_status")
        worked = status in _WORKED_STATUS
        rows.append({
            "source_id": zone_id,
            "type": scenario.get("zone_type"),
            "timeframe": scenario.get("timeframe"),
            "interaction": "scenario",
            "occurred_at": scenario.get("activated_at"),
            "role": "worked_support" if worked else "support",
            "lower": scenario.get("lower"),
            "upper": scenario.get("upper"),
            "zone_status": status,
        })
    return rows


def _liquidity(db, instrument_id: int, as_of: int) -> list[str]:
    rows = db.conn.execute(
        """SELECT e.price, e.occurred_at, z.type, z.timeframe
           FROM event e
           JOIN zone z ON z.id=e.zone_id
           WHERE z.instrument_id=? AND e.kind='level_taken' AND e.occurred_at<=?
           ORDER BY e.occurred_at DESC LIMIT 8""",
        (instrument_id, as_of),
    ).fetchall()
    lines = []
    for row in rows:
        kind = _TYPE_RU.get(row["type"], str(row["type"] or "уровень"))
        lines.append(
            "{kind} {tf} · {level} · пересечение · {when} · не сценарий".format(
                kind=kind,
                tf=row["timeframe"] or "",
                level=fmt_price_ru(row["price"]),
                when=fmt_time_msk(int(row["occurred_at"])),
            )
        )
    return lines


def _history(db, instrument_id: int, as_of: int, leg, evidence) -> dict[str, Any]:
    lines = []
    summary = "Прежних переходов на этой отсечке нет."
    if leg:
        previous = db.conn.execute(
            """SELECT direction, trigger_bos_key, bos_at, broken_anchor_key, evidence, superseded_at
               FROM h1_local_leg
               WHERE instrument_id=? AND bos_at<?
               ORDER BY bos_at DESC LIMIT 1""",
            (instrument_id, int(leg["bos_at"])),
        ).fetchone()
        if previous is not None:
            prev = dict(previous)
            prev["instrument_id"] = instrument_id
            parsed = _transition(db, prev)
            side = "вниз" if prev.get("direction") == "bear" else "вверх"
            if parsed.get("proven"):
                lines.append(
                    "Прежний BOS {side}: {level} · {when}".format(
                        side=side,
                        level=fmt_price_ru(parsed["break_level"]),
                        when=fmt_time_msk(int(parsed["occurred_at"])),
                    )
                )
            else:
                lines.append(f"Прежний переход {side} без полного происхождения.")
            summary = lines[0]
    linked_ids = {int(row["source_id"]) for row in evidence if row.get("source_id")}
    cutoff = int(leg["bos_at"]) if leg else as_of
    old_zones = db.conn.execute(
        """SELECT id, type, timeframe, display_until, end_reason, status
           FROM zone
           WHERE instrument_id=? AND display_until IS NOT NULL AND display_until<?
           ORDER BY display_until DESC LIMIT 12""",
        (instrument_id, cutoff),
    ).fetchall()
    shown = 0
    for zone in old_zones:
        if int(zone["id"]) in linked_ids:
            continue
        if shown >= 3:
            break
        shown += 1
        when = fmt_time_msk(int(zone["display_until"]))
        reason = zone["end_reason"] or "завершение записано"
        lines.append(
            "История: {typ} {tf} завершена {when}. {reason}".format(
                typ=_TYPE_RU.get(zone["type"], zone["type"]),
                tf=zone["timeframe"] or "",
                when=when,
                reason=reason,
            )
        )
    return {"summary": summary, "lines": lines}


def _invalidation(leg, transition) -> dict[str, Any]:
    if not leg or leg.get("origin_price") is None:
        return {
            "level": None,
            "rule": "Уровень отмены не определён: нет исходной опоры ноги.",
            "source_event_key": (transition or {}).get("event_key"),
        }
    bull = leg.get("direction") == "bull"
    side = "ниже" if bull else "выше"
    return {
        "level": float(leg["origin_price"]),
        "rule": f"закрытие H1 {side} исходной опоры ноги",
        "source_event_key": leg.get("trigger_bos_key"),
    }


def _pause_entries(packed: dict, quality_text: str) -> dict:
    """Сценарий и направление сохраняются. Новые входы при плохих данных не обещаются."""
    why = quality_text or "данные неполные"
    blockers = list(packed.get("blockers") or [])
    if "data_incomplete" not in blockers:
        blockers.append("data_incomplete")
    out = dict(packed)
    out["action"] = "blocked"
    out["blockers"] = blockers
    out["explanation"] = f"Уведомления о новых условиях входа приостановлены: {why}."
    return out


def _state(leg, proven, quality_bad, sides, zones, price, pd, data_state) -> dict[str, Any]:
    quality_text = ""
    if quality_bad and data_state:
        _, quality_text = _quality(data_state)
    if leg and not proven:
        blockers = ["provenance_incomplete"]
        explanation = "Происхождение перехода неполное: нет опоры, свечи или времени."
        if quality_bad:
            blockers.append("data_incomplete")
            if quality_text:
                explanation = explanation + " " + quality_text + "."
        return _pack(
            True, "data_incomplete", None, "Происхождение перехода неполное",
            "provenance_incomplete", "blocked", explanation, blockers, "blocked",
        )
    if leg and proven:
        packed = _proven_state(leg, sides, zones, price, pd, data_state)
        if quality_bad:
            return _pause_entries(packed, quality_text)
        return packed
    if quality_bad:
        return _pack(
            True, "data_incomplete", None, "Недостаточно данных для оценки",
            "data_incomplete", "blocked",
            "Сохранённого сценария нет, поэтому направление не назначается.",
            ["data_incomplete"], "blocked",
        )
    return _no_leg_state(sides)


def _proven_state(leg, sides, zones, price, pd, data_state) -> dict[str, Any]:
    direction = leg.get("direction")
    trade = "long" if direction == "bull" else "short"
    word = "LONG" if direction == "bull" else "SHORT"
    up = "вверх" if direction == "bull" else "вниз"
    linked = sides["linked"].get(direction) or []
    support = [
        e for e in sides["episodes"]
        if e.get("supports", {}).get(direction) and not e.get("closed")
    ]
    inconsistent = _inconsistent(support, linked)
    if inconsistent:
        return _pack(
            True, "state_inconsistent", trade, "Состояние уточняется", "state_inconsistent",
            "recover",
            "Эпизод подтверждён, а запись сценария отсутствует. Восстановление — отдельным пересчётом, не чтением карточки.",
            ["scenario_row_missing"], "inconsistent",
        )
    if linked:
        inside = _price_inside(price, zones, data_state)
        if inside:
            return _pack(
                True, f"{trade}_in_zone", trade, f"{word} · в зоне входа", "price_in_admitted",
                "wait", "Цена в допущенной области текущего PD.", [], "in_zone",
            )
        if zones:
            return _pack(
                True, f"{trade}_wait", trade, f"{word} · ждём откат", "zones_ready",
                "wait", "Ждём откат в допущенную область текущего PD.", [], "waiting_pullback",
            )
        return _pack(
            True, f"{trade}_no_zones", trade, f"{word} · нет подходящих зон", "no_admitted_zones",
            "wait", "Сценарий есть, подходящей зоны в текущем PD нет.", ["no_admitted_zones"],
            "no_zones",
        )
    if support:
        return _pack(
            True, f"structure_{direction}_no_scenario", trade,
            f"H1 {up} · торговый сценарий пока не открыт", "scenario_not_opened",
            "wait", "Есть HTF-эпизод этого направления, сценарий на этот переход не открыт.",
            ["scenario_not_opened"], "scenario_not_opened",
        )
    return _pack(
        True, f"structure_{direction}_unconfirmed", trade,
        f"H1 {up} · торговый контекст не подтверждён", "context_missing",
        "wait", "Структура есть. Ближайшая противоположная зона статус не заменяет.",
        ["context_missing"], "context_missing",
    )


def _inconsistent(support, linked) -> bool:
    if linked:
        return False
    for episode in support:
        confirmed = episode.get("state") == "confirmed" or episode.get("confirmed_at")
        if confirmed and not episode.get("confirmed_scenario_id"):
            return True
    return False


def _price_inside(price, zones, data_state) -> bool:
    if price is None or not zones:
        return False
    if data_state and data_state.get("state") not in (None, "ok"):
        # Незакрытая текущая H1 не отменяет уже известную котировку.
        if data_state.get("reason") != "current_candle_pending":
            return False
    for zone in zones:
        admitted = zone.get("admitted")
        if admitted and admitted[0] <= float(price) <= admitted[1]:
            return True
    return False


def _no_leg_state(sides) -> dict[str, Any]:
    scenario_dirs = [d for d in ("bull", "bear") if sides["active"].get(d)]
    if len(scenario_dirs) == 1:
        direction = scenario_dirs[0]
        word = "LONG" if direction == "bull" else "SHORT"
        trade = "long" if direction == "bull" else "short"
        return _pack(
            True, f"{trade}_no_zones", trade, f"{word} · нет подходящих зон",
            "scenario_without_leg", "wait",
            "Сценарий этого направления есть. Текущей ноги H1 с доказанным BOS нет, поэтому PD не подставлен.",
            ["no_proven_leg"], "no_zones",
        )
    waiting_dirs = sorted({
        row.get("direction") for row in sides["observations"] if row.get("direction") in ("bull", "bear")
    })
    episode_dirs = sorted({
        d for e in sides["episodes"] if not e.get("closed")
        for d in ("bull", "bear") if e.get("supports", {}).get(d)
    })
    context_dirs = waiting_dirs or episode_dirs
    if len(context_dirs) == 1 and not scenario_dirs:
        direction = context_dirs[0]
        word = "LONG" if direction == "bull" else "SHORT"
        trade = "long" if direction == "bull" else "short"
        cancelled = sides["cancelled"].get(direction) or []
        if cancelled and not sides["active"].get(direction):
            return _pack(
                True, "scenario_cancelled", trade, "Сценарий отменён · ждём подтверждение",
                "cancelled", "wait", "Нового сценария этого направления нет.",
                ["cancelled"], "cancelled",
            )
        return _pack(
            True, f"context_wait_{trade}", trade, f"Контекст {word} · ждём H1",
            "waiting_h1", "wait", "HTF-основание есть. Подтверждения H1 этого направления нет.",
            ["waiting_h1"], "waiting_h1",
        )
    if len(context_dirs) > 1 and not scenario_dirs:
        return _pack(
            True, "structure_absent", None, "Ждём подтверждение H1", "structure_absent",
            "wait", "Разнонаправленные зоны не выбирают сторону.", ["structure_absent"],
            "structure_absent",
        )
    cancelled_dirs = [d for d in ("bull", "bear") if sides["cancelled"].get(d)]
    if len(cancelled_dirs) == 1 and not scenario_dirs and not context_dirs:
        return _pack(
            True, "scenario_cancelled", None, "Сценарий отменён · ждём подтверждение",
            "cancelled", "wait", "Нового сценария нет.", ["cancelled"], "cancelled",
        )
    return _pack(
        False, "none", None, None, "navigation_only", "none", "", [], "absent",
    )


def _pack(governs, code, trade, title, reason, action, explanation, blockers, setup_state):
    return {
        "governs": governs,
        "code": code,
        "trade_direction": trade,
        "title": title,
        "reason_code": reason,
        "action": action,
        "explanation": explanation,
        "blockers": blockers,
        "setup_state": setup_state,
    }


def _compact(state, transition, evidence, zones, pd, invalidation, quality_text, leg, quality_bad, forming_note="") -> list[str]:
    if not state.get("governs"):
        return []
    lines: list[str] = []
    if quality_bad:
        lines.append(f"Качество: {quality_text}.")
        if transition and transition.get("proven"):
            lines.append("Последнее достоверное: " + _bos_line(transition))
        lines.append(
            f"Уведомления о новых условиях входа приостановлены: {quality_text}."
        )
        if transition and transition.get("proven"):
            entry = _entry_line(zones, leg)
            if entry:
                lines.insert(-1, "Сохранённые зоны: " + entry.removeprefix("Вход: "))
            pd_line = _pd_line(pd)
            if pd_line:
                lines.insert(-1, pd_line)
        return lines[:5]
    if forming_note:
        lines.append(forming_note)
    if transition and transition.get("proven"):
        lines.append(_bos_line(transition))
    grounds = _grounds_summary(evidence)
    if grounds:
        lines.append(grounds)
    elif state.get("reason_code") == "context_missing":
        lines.append("Основание: действующего HTF-эпизода этого направления нет.")
    entry = _entry_line(zones, leg)
    pd_line = _pd_line(pd)
    if entry:
        lines.append(entry)
    if pd_line:
        lines.append(pd_line)
    elif leg:
        lines.append("PD H1 не построен: диапазон текущей ноги ещё не записан.")
    if invalidation.get("level") is not None:
        lines.append(
            "Отмена: {rule}, уровень {level}".format(
                rule=invalidation["rule"],
                level=fmt_price_ru(invalidation["level"]),
            )
        )
    elif leg:
        lines.append(invalidation["rule"])
    blocker = _blocker_line(state)
    if blocker:
        lines.append(blocker)
    return _shrink(lines, blocker)


def _bos_line(transition: dict) -> str:
    bull = transition.get("direction") == "bull"
    arrow = "↑" if bull else "↓"
    side = "выше" if bull else "ниже"
    kind = transition.get("kind") or "BOS"
    return "{kind} {arrow}: закрытие {side} {level} · {when}".format(
        kind=kind,
        arrow=arrow,
        side=side,
        level=fmt_price_ru(transition["break_level"]),
        when=fmt_time_msk(int(transition["occurred_at"])),
    )


def _grounds_summary(evidence: list[dict]) -> str:
    names = []
    for row in evidence:
        label = _source_name(row)
        if label and label not in names:
            names.append(label)
    if not names:
        return ""
    if len(names) <= 2:
        text = ", ".join(names)
    else:
        text = ", ".join(names[:2]) + f" + ещё {len(names) - 2}"
    return "Основание: " + text


def _source_name(row: dict) -> str:
    kind = _TYPE_RU.get(str(row.get("type") or "").lower(), None)
    if not kind:
        return ""
    tf = row.get("timeframe") or ""
    return f"{kind} {tf}".strip()


def _entry_line(zones, leg) -> str:
    if not zones or not leg:
        return ""
    bull = leg.get("direction") == "bull"
    half = "discount" if bull else "premium"
    lows = [z["admitted"][0] for z in zones]
    highs = [z["admitted"][1] for z in zones]
    lo, hi = fmt_price_ru(min(lows)), fmt_price_ru(max(highs))
    span = "–".join([lo, hi]) if lo != hi else lo
    return "Вход: {n} {word} в {half} · {span}".format(
        n=len(zones),
        word=_zones_word(len(zones)),
        half=half,
        span=span,
    )


def _zones_word(count: int) -> str:
    tail = abs(int(count)) % 100
    if 11 <= tail <= 14:
        return "зон"
    last = tail % 10
    if last == 1:
        return "зона"
    if 2 <= last <= 4:
        return "зоны"
    return "зон"


def _pd_line(pd) -> str:
    if not pd or pd.get("lower") is None or pd.get("upper") is None or pd.get("eq") is None:
        return ""
    label = pd.get("label") or ""
    return "PD H1 {lo} → {hi} · 50% {eq} · {label}".format(
        lo=fmt_price_ru(pd["lower"]),
        hi=fmt_price_ru(pd["upper"]),
        eq=fmt_price_ru(pd["eq"]),
        label=label,
    ).rstrip(" ·")


def _blocker_line(state) -> str:
    reason = state.get("reason_code")
    if reason == "state_inconsistent":
        return "Причина: эпизод подтверждён, сценарий не записан. Карточка сценарий не создаёт."
    if reason == "scenario_not_opened":
        return "Причина: сценарий на этот переход не открыт. Далее нужно подтверждение эпизода движком."
    if reason == "context_missing":
        return "Причина: нет HTF-эпизода этого направления. Ближняя зона другой стороны не подставляется."
    if reason == "provenance_incomplete":
        return "Происхождение перехода неполное."
    if reason == "no_admitted_zones":
        return "Подходящей зоны в текущем PD нет."
    return ""


def _shrink(lines: list[str], blocker: str) -> list[str]:
    rows = [line for line in lines if line]
    if len(rows) <= 5:
        return rows
    merged = []
    entry = next((line for line in rows if line.startswith("Вход:")), None)
    pd_line = next((line for line in rows if line.startswith("PD H1")), None)
    for line in rows:
        if entry and pd_line and line == entry:
            continue
        if entry and pd_line and line == pd_line:
            merged.append(entry + ". " + pd_line)
            continue
        merged.append(line)
    if len(merged) <= 5:
        return merged
    if blocker:
        kept = [line for line in merged if line == blocker or line.startswith((
            "BOS ", "SMS ", "Качество:", "Последнее", "Причина:", "Происхождение",
            "Текущая", "Уведомления",
        ))]
        rest = [line for line in merged if line not in kept]
        return (kept + rest)[:5] if blocker in kept else merged[:4] + [blocker]
    return merged[:5]


_MSK_TZ = timezone(timedelta(hours=3))


def _short_time(ms: int, now_ms: int) -> str:
    """Время по МСК: для сегодняшней отсечки — только часы, иначе дата."""
    dt = datetime.fromtimestamp(int(ms) / 1000, _MSK_TZ)
    now_dt = datetime.fromtimestamp(int(now_ms) / 1000, _MSK_TZ)
    hm = dt.strftime("%H:%M")
    if dt.date() == now_dt.date():
        return f"сегодня {hm}"
    return f"{dt.strftime('%d.%m')} · {hm}"


def _compact_rows(state, transition, evidence, zones, pd, invalidation,
                  quality_text, leg, quality_bad, forming_note, now_ms) -> list[dict]:
    """Те же факты, что в compact, но подпись + значение + тип ряда.

    Фронтенд рисует их сеткой и красит по kind; рынок не пересчитывает.
    """
    if not state.get("governs"):
        return []
    rows: list[dict] = []
    if quality_bad:
        rows.append({"kind": "quality", "k": "Качество", "v": quality_text})
        if transition and transition.get("proven"):
            rows.append({"kind": "bos", "dir": _bos_dir(transition),
                         "k": _bos_label(transition), "v": _bos_value(transition, now_ms)})
            entries = _entry_rows(zones, leg)
            rows.extend(entries)
            parts = _pd_parts(pd)
            if parts:
                rows.append({"kind": "pd", "k": "PD H1", "v": parts["v"],
                             "note": parts.get("note")})
        rows.append({
            "kind": "blocker", "k": "Режим",
            "v": f"Уведомления о новых условиях входа приостановлены: {quality_text}",
        })
        return rows
    if forming_note:
        rows.append({"kind": "quality", "k": "Свеча", "v": forming_note})
    if transition and transition.get("proven"):
        rows.append({"kind": "bos", "dir": _bos_dir(transition),
                     "k": _bos_label(transition), "v": _bos_value(transition, now_ms)})
    grounds = _grounds_summary(evidence)
    if grounds:
        rows.append({"kind": "grounds", "k": "Основание",
                     "v": grounds.removeprefix("Основание: ")})
    elif state.get("reason_code") == "context_missing":
        rows.append({"kind": "grounds", "k": "Основание",
                     "v": "действующего HTF-эпизода этого направления нет"})
    rows.extend(_entry_rows(zones, leg))
    pd_parts = _pd_parts(pd)
    if pd_parts:
        rows.append({"kind": "pd", "k": "PD H1", "v": pd_parts["v"],
                     "note": pd_parts.get("note")})
    elif leg:
        rows.append({"kind": "pd", "k": "PD H1",
                     "v": "диапазон текущей ноги ещё не записан"})
    if invalidation.get("level") is not None:
        rows.append({"kind": "cancel", "k": "Отмена",
                     "v": "{rule} · {level}".format(
                         rule=invalidation["rule"],
                         level=fmt_price_ru(invalidation["level"]),
                     )})
    elif leg:
        rows.append({"kind": "cancel", "k": "Отмена", "v": invalidation["rule"]})
    blocker = _blocker_line(state)
    if blocker:
        rows.append({"kind": "blocker", "k": "Причина", "v": blocker})
    return rows


def _bos_dir(transition: dict) -> str:
    return "long" if transition.get("direction") == "bull" else "short"


def _bos_label(transition: dict) -> str:
    bull = transition.get("direction") == "bull"
    arrow = "↑" if bull else "↓"
    return "{kind} {arrow}".format(kind=transition.get("kind") or "BOS", arrow=arrow)


def _bos_value(transition: dict, now_ms: int) -> str:
    bull = transition.get("direction") == "bull"
    side = "выше" if bull else "ниже"
    return "закрытие {side} {level} · {when}".format(
        side=side,
        level=fmt_price_ru(transition["break_level"]),
        when=_short_time(int(transition["occurred_at"]), now_ms),
    )


def _entry_rows(zones, leg) -> list[dict]:
    """Каждая допущенная зона — отдельный ряд «название: цена»."""
    if not zones or not leg:
        return []
    half = "в discount" if leg.get("direction") == "bull" else "в premium"
    rows = []
    for i, zone in enumerate(zones):
        name = _source_name({"type": zone.get("type"), "timeframe": zone.get("timeframe")}) or "зона"
        lo, hi = fmt_price_ru(zone["admitted"][0]), fmt_price_ru(zone["admitted"][1])
        span = f"{lo}–{hi}" if lo != hi else lo
        rows.append({
            "kind": "entry",
            "k": "Вход" if i == 0 else "",
            "v": f"{name}: {span}",
            "note": half,
            "zone": {"lower": zone.get("lower") if zone.get("lower") is not None else zone["admitted"][0],
                     "upper": zone.get("upper") if zone.get("upper") is not None else zone["admitted"][1],
                     "price_inside": bool(zone.get("price_inside")),
                     "invalidated_at": zone.get("invalidated_at")},
        })
    return rows


def _pd_parts(pd) -> dict:
    if not pd or pd.get("lower") is None or pd.get("upper") is None or pd.get("eq") is None:
        return {}
    label = pd.get("label") or ""
    note = ""
    if "предварительный" in label:
        note = "предварительный"
    elif "подтвержд" in label:
        note = "подтверждён"
    return {
        "v": "{lo} → {hi} · 50% {eq}".format(
            lo=fmt_price_ru(pd["lower"]),
            hi=fmt_price_ru(pd["upper"]),
            eq=fmt_price_ru(pd["eq"]),
        ),
        "note": note,
    }


def _ground_lines(evidence: list[dict]) -> list[str]:
    lines = []
    for row in evidence:
        name = _source_name(row) or "источник"
        bounds = ""
        if row.get("lower") is not None and row.get("upper") is not None:
            bounds = f" {fmt_price_ru(row['lower'])}–{fmt_price_ru(row['upper'])}"
        when = ""
        if row.get("occurred_at"):
            when = " · " + fmt_time_msk(int(row["occurred_at"]))
        interaction = _INTERACTION_RU.get(row.get("interaction") or "", "сценарий")
        line = f"{name}{bounds} · {interaction}{when}"
        if row.get("role") == "worked_support":
            line += ", зона отработана, факт поддерживает контекст"
        lines.append(line)
    return lines


def _other_contexts(db, instrument_id, as_of, leg, nav, sides) -> list[str]:
    lines = []
    current = leg.get("direction") if leg else None
    rows = db.conn.execute(
        """SELECT o.id, o.direction, z.type, z.timeframe, z.lower, z.upper
           FROM ltf_observation o
           JOIN zone z ON z.id=o.zone_id
           WHERE o.instrument_id=? AND o.activated_at<=?
             AND o.state IN ('waiting_structure','active','paused_data')
           ORDER BY o.activated_at DESC LIMIT 24""",
        (instrument_id, as_of),
    ).fetchall()
    opposite = []
    for row in rows:
        if current and row["direction"] == current:
            continue
        name = "{typ} {tf} {lo}–{hi}".format(
            typ=_TYPE_RU.get(row["type"], row["type"]),
            tf=row["timeframe"] or "",
            lo=fmt_price_ru(row["lower"]),
            hi=fmt_price_ru(row["upper"]),
        )
        if current:
            side = "вверх" if current == "bull" else "вниз"
            opposite.append(f"{name} не задаёт состояние: структура H1 {side}.")
        else:
            opposite.append(f"{name} — зона просмотра, не состояние актива.")
    if len(opposite) > 4:
        lines.extend(opposite[:4])
        lines.append(f"Ещё {len(opposite) - 4} контекстов в списке зон.")
    else:
        lines.extend(opposite)
    selected = nav.get("selected_context_id")
    basis = nav.get("selection_basis")
    if selected is not None:
        basis_text = _BASIS_RU.get(basis, "выбор просмотра")
        lines.append(f"Для просмотра открыт контекст {selected}. Основание: {basis_text}.")
    if current == "bull" and sides["cancelled"].get("bear"):
        lines.append("Короткий сценарий на этой отсечке уже отменён и статус не занимает.")
    if current == "bear" and sides["cancelled"].get("bull"):
        lines.append("Длинный сценарий на этой отсечке уже отменён и статус не занимает.")
    return lines


def _diagnostics(instrument_id, as_of, leg, transition, state, nav, sides, bull) -> list[str]:
    move = int(leg["id"]) if leg else None
    proven = bool(transition and transition.get("proven"))
    return [
        f"Правило: {RULE_VERSION}",
        f"Инструмент {instrument_id}, движение {move}, as_of {fmt_time_msk(as_of)}",
        f"Снимок отделён от выбора зоны. Доказанный переход: {'да' if proven else 'нет'}.",
        "Причина статуса: " + str(state.get("reason_code") or "нет"),
        "Выбор просмотра в состояние актива не входит.",
    ]


def _loads(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
