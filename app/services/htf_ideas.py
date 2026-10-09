"""Durable HTF ideas and their causal zones. No signal emission, including repair.

Projection is read-only and time-bounded; reconciliation persists the same
projection after worker batches. Existing reverse/stale cancellations are entry
attempt history, so legacy ideas can be recovered without rewriting that history.
"""
from __future__ import annotations

from dataclasses import replace

from ..engine.ltf.relevance import VERSION, zone_at
from ..engine.ltf.eligibility import evaluate_entry, evaluate_final
from ..engine.ltf.ranges import RangeDraft
from ..engine.ltf.context import context_complete, context_flags
from ..models import ZoneStatus

# Терминальная причина не откатывается. Пропуск zone_at допустим только когда
# исключение уже наступило к as_of. Пустой excluded_at историю не доказывает.
_TERMINAL_LIFECYCLE = frozenset({
    "fvg_filled", "level_broken", "swept_level", "tested_too_deep",
})


def _stored_terminal(zone, as_of):
    evidence = zone.evidence or {}
    reason = evidence.get("lifecycle_reason")
    excluded = evidence.get("excluded_at")
    if reason not in _TERMINAL_LIFECYCLE or excluded is None:
        return None
    try:
        excluded_at = int(excluded)
    except (TypeError, ValueError):
        return None
    if excluded_at > as_of:
        return None
    return zone, {
        "relevant": False,
        "reason": reason,
        "data_quality": "complete",
        "excluded_at": excluded_at,
    }


def ideas_checkpoint_fresh(db, instrument_id) -> bool:
    """Снимок идей совпадает с текущей версией правил и state_seq.

    Метка seq пишется после reconcile и якорей. Иначе следующий пустой
    опрос снова видит устаревший снимок и считает его заново.
    """
    return (
        db.get_meta(f"htf_ideas:version:{instrument_id}") == VERSION
        and db.get_meta(f"htf_ideas:seq:{instrument_id}") == str(db.get_state_seq())
    )


def mark_ideas_checkpoint(db, instrument_id) -> None:
    db.set_meta(f"htf_ideas:version:{instrument_id}", VERSION)
    db.set_meta(f"htf_ideas:seq:{instrument_id}", str(db.get_state_seq()))


def project_ideas(db, instrument_id, cfg, as_of, *, zones=None, candles=None):
    zones = zones if zones is not None else db.list_ltf_entry_zones(instrument_id=instrument_id)
    facts = {}
    pending = []
    for z in zones:
        if z.formed_at > as_of:
            continue
        stored = _stored_terminal(z, as_of)
        if stored is not None:
            facts[z.id] = stored
        else:
            pending.append(z)
    if pending:
        if candles is None:
            candles = db.get_candles(instrument_id, "H1", end_ms=as_of)
        for z in pending:
            facts[z.id] = zone_at(z, candles, as_of, cfg)
    saved = {i["scenario_id"]: i for i in db.list_htf_ideas(instrument_id)}
    bundle = db.ltf_projection_bundle(instrument_id)
    ideas = []
    links = {}
    for obs in db.list_ltf_observations(instrument_id=instrument_id):
        if obs.activated_at > as_of:
            continue
        parent = db.get_zone(obs.zone_id)
        if parent is None:
            continue
        for sc in bundle["scenarios_by_obs"].get(obs.id, []):
            events = {e.id: e for e in bundle["structure_events"].get(sc.id, [])
                      if e.occurred_at <= as_of}
            trigger = events.get(sc.origin_break_event_id or sc.trigger_event_id)
            if (trigger is None or trigger.kind not in ("BOS", "SMS")
                    or trigger.direction != sc.direction or trigger.occurred_at < obs.activated_at):
                continue
            movements = {m.id: m for m in bundle["movements"].get(sc.id, [])
                         if m.provenance_status == "ok" and m.break_event_id in events
                         and (m.confirmed_at or m.end_at) <= as_of}
            entries = [e for e in bundle["entries"].get(sc.id, []) if e.added_at <= as_of]
            zone_ids = set()
            for e in entries:
                pair = facts.get(e.entry_zone_id)
                if pair is None:
                    continue
                z = pair[0]
                if z.confirmed_at is None or z.confirmed_at > as_of:
                    continue
                movement = movements.get(z.movement_id)
                if movement is None and z.movement_id:
                    source = bundle["movements_by_id"].get(z.movement_id)
                    if source is None:
                        source = db.get_ltf_movement(z.movement_id)
                    if source and source.provenance_status == "ok":
                        movement = next((m for m in movements.values()
                                         if m.start_at == source.start_at and m.end_at == source.end_at
                                         and m.source_candle_ids == source.source_candle_ids), None)
                if movement is not None and z.direction == sc.direction:
                    zone_ids.add(z.id)
            previous = saved.get(sc.id, {})
            manual_at = previous.get("manual_closed_at")
            if sc.cancellation_reason == "manual":
                manual_at = manual_at or sc.cancelled_at
            if obs.state == "closed_by_user":
                manual_at = manual_at or obs.updated_at
            parent_end = parent.display_until or parent.evidence.get("invalidated_at")
            parent_invalid = ((parent_end is not None and parent_end <= as_of) or
                              (parent_end is None and (parent.market_validity != "active" or
                               parent.status in (ZoneStatus.CONVERTED, ZoneStatus.ARCHIVED, ZoneStatus.REJECTED))))
            detected_at = bundle["discovered"].get(sc.id)
            complete = bool(movements) and (bool(zone_ids) or
                        (detected_at is not None and int(detected_at) <= as_of))
            active = [zid for zid in zone_ids if facts[zid][1]["relevant"]]
            unknown = parent.needs_replay or any(facts[zid][1]["data_quality"] != "complete" for zid in zone_ids)
            unresolved = any(facts[zid][1]["reason"] == "data_gap" for zid in zone_ids)
            state, reason, ended = "active", None, None
            if manual_at is not None and manual_at <= as_of:
                state, reason, ended = "closed", "manual", manual_at
            elif parent_invalid:
                state, reason, ended = "closed", "parent_invalid", parent_end
            elif parent.needs_replay or (unresolved and not active):
                state, reason = "paused_data", "data_gap"
            elif not complete:
                state, reason = "waiting_zones", "discovery_pending"
            elif not active:
                state, reason = "closed", "zones_exhausted"
                ended = max((facts[z][1]["excluded_at"] or trigger.occurred_at for z in zone_ids), default=trigger.occurred_at)
            idea = {
                "id": previous.get("id"), "instrument_id": instrument_id,
                "scenario_id": sc.id, "observation_id": obs.id,
                "parent_zone_id": parent.id, "cycle_id": obs.cycle_id,
                "parent_type": parent.type.value, "parent_timeframe": parent.timeframe,
                "direction": sc.direction.value, "trigger_event_id": trigger.id,
                "trigger_at": trigger.occurred_at, "movement_ids": sorted(movements),
                "zone_ids": sorted(zone_ids), "state": state, "reason": reason,
                "ended_at": ended, "discovery_complete": complete,
                "data_quality": "gap" if unknown else "complete", "rule_version": VERSION,
            }
            ideas.append(idea)
            ranges = [r for r in bundle["ranges"].get(sc.id, []) if r.available_at <= as_of]
            rng = max(ranges, key=lambda r: r.version, default=None)
            version = rng.version if rng else 0
            draft = RangeDraft(sc.direction, rng.lower, rng.upper, rng.mid,
                               rng.anchor_low_pivot_id, rng.anchor_high_pivot_id,
                               rng.available_at) if rng else None
            by_zone = {e.entry_zone_id: e for e in entries if e.range_version == version}
            tests = [t for t in bundle["tests"].get(sc.id, [])
                     if t.resolved_at is not None and t.resolved_at <= as_of]
            allow = context_complete(context_flags([
                e for e in bundle["ltf_events"].get(sc.id, []) if e.occurred_at <= as_of
            ]))
            attempt_open = sc.state not in ("cancelled", "closed") or (
                sc.cancelled_at is not None and sc.cancelled_at > as_of)
            for zid in zone_ids:
                z, fact = facts[zid]
                entry = by_zone.get(zid)
                if entry is not None:
                    evaluated = evaluate_entry(z, sc.direction, cfg, draft,
                                               movements=None, liquidity_tests=tests)
                    entry = replace(entry, eligible=evaluated.eligible, overlap=evaluated.overlap,
                                    reason=evaluated.reason, state=evaluated.state)
                decision = evaluate_final(entry, z, allow_outside=allow, liquidity_tests=tests, cfg=cfg) if entry else None
                ready = (state == "active" and fact["relevant"] and attempt_open
                         and decision is not None and decision.eligible_now)
                ready_reason = (fact["reason"] if not fact["relevant"] else
                                reason if state != "active" else
                                "structure_pending" if not attempt_open else
                                decision.primary_reason if decision else "range_pending")
                links.setdefault(zid, []).append({
                    "idea_id": idea["id"], "scenario_id": sc.id,
                    "direction": idea["direction"], "parent_zone_id": parent.id,
                    "parent_type": idea["parent_type"], "parent_timeframe": parent.timeframe,
                    "trigger_at": trigger.occurred_at, "state": state,
                    "eligible_now": bool(ready), "entry_reason": "ok" if ready else ready_reason,
                })
    return ideas, links, facts


def reconcile_ideas(db, instrument_id, cfg, as_of):
    ideas, _, facts = project_ideas(db, instrument_id, cfg, as_of)
    with db.batch_writes():
        for z, fact in facts.values():
            original = db.get_ltf_entry_zone(z.id)
            if fact["data_quality"] != "complete":
                continue  # missing history must never erase known consumption
            fields = dict(first_test_at=z.first_test_at, max_test_depth=z.max_test_depth,
                          test_extreme=z.test_extreme, validity=z.validity, evidence=z.evidence)
            if any(getattr(original, k) != v for k, v in fields.items()):
                db.update_ltf_entry_zone(z.id, **fields)
        for idea in ideas:
            payload = {k: v for k, v in idea.items() if k != "id"}
            db.save_htf_idea(payload)
            if idea["state"] == "active" and idea["data_quality"] == "complete":
                obs = db.get_ltf_observation(idea["observation_id"])
                if obs.state == "closed_stale":
                    # Resume observation, not the cancelled entry attempt.
                    db.update_ltf_observation(obs.id, state="waiting_structure", updated_at=as_of)
        db.set_meta(f"htf_ideas:version:{instrument_id}", VERSION)
    return ideas
