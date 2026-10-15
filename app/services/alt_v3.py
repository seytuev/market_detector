"""Read model for W1 bases; never borrows v1/v2 events or geometry."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from ..alt import store_v3
from ..alt.engine_v3 import DAY, RULES_VERSION
from ..models import now_ms

LABELS = {"mature_frozen": "База подтверждена · 100+ дней", "boundary_refined": "Уточнение границы базы",
          "manipulation_started": "Депрессия · ожидание BOS W1", "bos_confirmed": "BOS W1 · манипуляция подтверждена",
          "retest": "Касание", "zone_invalidated": "Зона отменена закрытием W1",
          "cancelled": "Сценарий отменён закрытием W1", "breakout": "База завершена · выход W1"}
LABELS["cycle_superseded"] = "Обнаружена самостоятельная база следующего цикла"


def _context(frame):
    state = frame.get("scenario_state") or {}
    active = [s for s in state.get("scenarios", []) if s.get("cancelled_at_ms") is None]
    return state, active[-1] if active else None


def row(db, asset, source, stats, frame, *, as_of_ms=None):
    from .alt_overview import _source_brief, _width_pcts
    state, scenario = _context(frame)
    terminal = frame["base_end_open_time"] is not None
    stage = None if terminal else 2 if scenario else 4
    events = state.get("events", [])
    last = events[-1] if events else None
    at = as_of_ms if as_of_ms is not None else now_ms()
    latest = db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE source_id=? AND structure_key=? AND event_time_ms<=? ORDER BY event_time_ms DESC,id DESC LIMIT 1",
                             (frame["source_id"], frame["structure_key"], at)).fetchone()
    if latest:
        last = json.loads(latest["payload_json"])
    if not terminal and last and last["event_type"] == "retest" and at-last["event_time_ms"] < DAY:
        stage = 1
    coverage = json.loads(db.get_meta(f"alt:v3:coverage:{source.id}") or "{}") if as_of_ms is None else {}
    pending = coverage.get("status") in ("data_pending", "insufficient_history")
    if pending and not terminal:
        stage = 6
    last_close = stats["last_close"] if stats else None
    distance = None
    if last_close and scenario:
        zones = [z for z in state.get("pd_zones", []) if z["invalidated_at_ms"] is None and z["scenario_id"] == scenario["id"]]
        distances = [0 if z["lower"] <= last_close <= z["upper"] else min(abs(last_close-z["lower"]), abs(last_close-z["upper"])) for z in zones]
        distances += [abs(last_close-frame["lower"]), abs(last_close-frame["upper"])]
        distance = min(distances)/last_close*100
    up, down = _width_pcts(frame["lower"], frame["upper"])
    first_year = datetime.fromtimestamp(frame["base_start_open_time"]/1000, timezone.utc).year
    last_year = datetime.fromtimestamp((frame["base_end_open_time"] or at)/1000, timezone.utc).year
    period = str(first_year) if first_year == last_year else f"{first_year}–{last_year}"
    return {"kind": "structure", "structure_id": frame["structure_id"], "setup_id": None, "candidate_id": None,
            "asset": {"id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol, "name": asset.name, "cmc_rank": asset.cmc_rank},
            "source": _source_brief(source), "state": frame["state"],
            "state_ru": f"База {period} · " + ("история цикла" if frame["state"] == "superseded" else "завершена" if terminal else "ожидание теста W1" if scenario else "депрессия · ожидание BOS W1" if state.get("depression") else "зрелая"),
            "stage": stage, "stage_ru": "История" if terminal else "Требуются данные" if pending else "Ожидание ретеста" if scenario else "Зрелый",
            "terminal": terminal, "universe_eligible": bool(asset.enabled),
            "ath_price": stats.get("ath_price") if stats else None, "ath_open_time": stats.get("ath_open_time") if stats else None,
            "p_min": stats.get("p_min") if stats else None, "p_min_open_time": stats.get("p_min_open_time") if stats else None,
            "drawdown_pct": stats["drawdown"]*100 if stats and stats.get("drawdown") is not None else None,
            "last_close": last_close, "last_close_open_time": stats["last_open_time"] if stats else None,
            "range": {k: frame[k] for k in ("lower", "upper", "mid", "width")}, "width_up_pct": up, "width_down_pct": down,
            "age_days": ((frame["base_end_open_time"] + DAY if terminal else frame["available_at_ms"])-frame["base_start_open_time"])//DAY,
            "consolidation_days": ((frame["base_end_open_time"] + DAY if terminal else frame["available_at_ms"])-frame["base_start_open_time"])//DAY,
            "flags": {"structure_event": bool(scenario), "ssl_event": bool(state.get("depression") or state.get("scenarios")),
                      "manipulation_active": bool(state.get("depression")), "breakout_confirmed": terminal,
                      "retest_received": any(e["event_type"]=="retest" for e in events), "targets_hit": []},
            "entry_kinds": [], "entries": [], "targets": [], "cancel": {"price": scenario["minimum"] if scenario else None},
            "breakout_close": None, "breakout_closed_at": frame["base_end_confirmed_at_ms"], "retest_deadline_ms": None,
            "distance_pct": distance, "terminated_ms": frame["base_end_confirmed_at_ms"],
            "updated_ms": frame["computed_at_ms"], "last_event": {**last, "label_ru": LABELS.get(last["event_type"], last["event_type"])} if last else None,
            "run_status": "data_pending" if pending else None,
            "reason": "История неполная: новые касания приостановлены до восстановления D1" if pending else None,
            "rules_version": RULES_VERSION}


def detail(db, structure_id, settings, as_of_ms=None):
    from .alt_overview import _candle_stats, _detail_candles
    at = as_of_ms if as_of_ms is not None else now_ms()
    f = store_v3.frame(db, structure_id, at)
    if f is None:
        return None
    asset = db.get_alt_asset(f["asset_id"])
    source = db.get_alt_instrument_source(f["asset_id"])
    if asset is None or source is None or source.id != f["source_id"]:
        return None
    prefix = db.get_alt_candles(source.id, end_ms=at-DAY)
    stats = None
    if prefix:
        ath = max(prefix, key=lambda c: (c.high, c.open_time))
        after = [c for c in prefix if c.open_time > ath.open_time]
        low = min(after, key=lambda c: (c.low, c.open_time)) if after else None
        stats = {"ath_price": ath.high, "ath_open_time": ath.open_time, "p_min": low.low if low else None,
                 "p_min_open_time": low.open_time if low else None, "drawdown": 1-low.low/ath.high if low else None,
                 "last_close": prefix[-1].close, "last_open_time": prefix[-1].open_time}
    result = row(db, asset, source, stats, f, as_of_ms=at)
    first_revision = db.conn.execute("SELECT available_at_ms FROM alt_structure_revision WHERE structure_id=? ORDER BY available_at_ms,id LIMIT 1", (structure_id,)).fetchone()
    mature_at = first_revision[0] if first_revision else f["available_at_ms"]
    candles, history = _detail_candles(db, source.id, None)
    candles = [c for c in candles if c["open_time"] + DAY <= at]
    history = {**history, "loaded_from_ms": candles[0]["open_time"] if candles else None,
               "loaded_to_ms": candles[-1]["open_time"] if candles else None, "count": len(candles)}
    state, scenario = _context(f)
    stored_events = db.conn.execute("SELECT payload_json FROM alt_v3_event WHERE source_id=? AND structure_key=? AND event_time_ms<=? ORDER BY event_time_ms,id",
                                    (source.id, f["structure_key"], at)).fetchall()
    events = [json.loads(r["payload_json"]) for r in stored_events] or state.get("events", [])
    events = [{**e, "payload": {**e["payload"], "level_price": e["price"]}, "source_event_id": e["id"], "detected_at_ms": e["event_time_ms"],
               "label_ru": LABELS.get(e["event_type"], e["event_type"])} for e in events]
    bos = next((e for e in reversed(events) if e["event_type"] == "bos_confirmed"), None)
    depressions = state.get("scenarios", []) + ([state["depression"]] if state.get("depression") else [])
    result.update({"effective_range": f, "range_revision": None,
        "frozen_range": {**f, "id": f["structure_id"], "start_anchor_open_time": f["base_start_open_time"], "mature_at_ms": mature_at, "included_candles": []},
        "structural_ranges": store_v3.list_structures(db, asset.id, source.id, at), "range_episodes": [], "range_versions": [],
        "pd_zones": state.get("pd_zones", []), "scenarios": state.get("scenarios", []),
        "manipulation_episodes": [{"id": d["id"], "started_candle_open_time": d["started_at"],
            "ended_candle_open_time": d.get("bos_at_ms", 0)-DAY if d.get("bos_at_ms") else None,
            "min_price": d["minimum"], "days_below": None} for d in depressions],
        "structure_events": [], "events": events, "confirmation": {**bos, "event_id": bos["id"], "candle_open_time": bos["payload"]["candle_open_time"]} if bos else None,
        "cancel": {"price": scenario["minimum"] if scenario else None, "mode_ru": "Закрытие W1 ниже минимума манипуляции после BOS",
                   "reachable": bool(scenario), "mode_note": "Отмена PD-зоны не отменяет весь сценарий."},
        "breakout": {"closed_at": f["base_end_confirmed_at_ms"], "close": next((e["price"] for e in events if e["event_type"]=="breakout"), None)} if f["base_end_open_time"] else None,
        "anchors": {"start": {"open_time": f["base_start_open_time"], "available_at_ms": mature_at}},
        "classifier": {"version": RULES_VERSION, "midpoint_wicks": f["quality_metrics"].get("midpoint_wick_reactions", 0)},
        "versions": {"rule": RULES_VERSION, "source": source.source_version, "range": f["revision_id"]},
        "formulas": {"cancel": "Close W1 < минимум манипуляции, только после BOS", "targets": "Границы базы и W1 FVG/OB",
            "retest_zone": "W1 FVG/OB импульса BOS либо граница базы; событие при касании",
            "width_up": "(U/L − 1) × 100%", "width_down": "(1 − L/U) × 100%"},
        "candles": candles, "candle_history": history, "as_of_ms": at, "ath": stats,
        "structure_note": "Аккумуляция W1 после просадки ≥70% от известного ATH · 100+ дней; депрессия не расширяет базу", "target_snapshot": None})
    return result
