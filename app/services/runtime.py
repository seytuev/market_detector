"""Владелец расчёта, отпечаток профиля и диагностика без секретов (F35–F37)."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

from ..db import SCHEMA_VERSION
from ..models import now_ms
from .htf_parent import parent_decision, policy_types
from .overview import _cursors, _parent_zones

CALC_OWNER = "runtime:calc_owner"
TELEGRAM_OWNER = "runtime:telegram_owner"
TELEGRAM_CONFLICT = "runtime:telegram_conflict"
LAST_ERROR = "runtime:last_error"


def profile_fingerprint(cfg) -> str:
    payload = {
        "htf_context_types": cfg.htf_context_types,
        "ltf_entry_types": cfg.ltf_entry_types,
        "ltf_enabled": bool(cfg.ltf_enabled),
        "ltf_structure_left": cfg.ltf_structure_left,
        "ltf_structure_right": cfg.ltf_structure_right,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_claim(db, key: str) -> Optional[dict[str, Any]]:
    raw = db.get_meta(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def claim_owner(db, key: str, identity: dict[str, Any]) -> dict[str, Any]:
    """Занять meta-ключ владельца. Живой чужой pid не перезаписывается."""
    current = _read_claim(db, key)
    pid = int(identity.get("pid") or 0)
    if current is not None:
        owner_pid = int(current.get("pid") or 0)
        if owner_pid != pid and _pid_alive(owner_pid):
            conflict = {
                "owned": False,
                "owner": current,
                "contender": identity,
            }
            db.set_meta(key + ":conflict", json.dumps(conflict, ensure_ascii=False))
            return conflict
    body = {**identity, "claimed_at": now_ms()}
    db.set_meta(key, json.dumps(body, ensure_ascii=False))
    db.set_meta(key + ":conflict", "")
    return {"owned": True, "owner": body}


def record_telegram_conflict(db, message: str) -> None:
    db.set_meta(TELEGRAM_CONFLICT, json.dumps({
        "at": now_ms(),
        "message": message[:500],
    }, ensure_ascii=False))


def _parent_counts(db, instrument_id: int, cfg) -> dict[str, int]:
    counts: dict[str, int] = {}
    for zone in _parent_zones(db, instrument_id):
        reason = parent_decision(zone, cfg).reason
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def build_diagnostics(db, settings, *, role: str, instance_id: str,
                      started_at: int, build_id: str) -> dict[str, Any]:
    """Защищённый снимок runtime. Токены и секреты не включаются."""
    cfg = settings.detector
    instruments = []
    for ins in db.get_instruments():
        last = db.last_candle(ins.id, "H1")
        cursors = _cursors(db, ins.id)
        instruments.append({
            "id": ins.id,
            "symbol": ins.symbol,
            "venue": ins.venue,
            "market_type": ins.market_type,
            "enabled": bool(ins.enabled),
            "ltf_analyze": bool(ins.ltf_analyze),
            "last_h1": last.close_time if last is not None else None,
            "structure_last_processed_h1": cursors["structure_last_processed_h1"],
            "scenario_last_processed_h1": cursors["scenario_last_processed_h1"],
            "cursor": cursors["cursor"],
            "allowed_types": sorted(policy_types(cfg)),
            "parents_by_reason": _parent_counts(db, ins.id, cfg),
            "replaying": db.get_meta(f"replaying:{ins.id}") == "1",
            "last_error": db.get_meta(f"runtime:last_error:{ins.id}"),
        })
    calc = _read_claim(db, CALC_OWNER)
    telegram = _read_claim(db, TELEGRAM_OWNER)
    conflict_raw = db.get_meta(TELEGRAM_CONFLICT)
    try:
        telegram_conflict = json.loads(conflict_raw) if conflict_raw else None
    except json.JSONDecodeError:
        telegram_conflict = {"message": "unreadable"}
    calc_conflict_raw = db.get_meta(CALC_OWNER + ":conflict")
    try:
        calc_conflict = json.loads(calc_conflict_raw) if calc_conflict_raw else None
    except json.JSONDecodeError:
        calc_conflict = None
    return {
        "build_id": build_id,
        "started_at": started_at,
        "instance_id": instance_id,
        "process_role": role,
        "pid": os.getpid(),
        "schema_version": SCHEMA_VERSION,
        "profile_fingerprint": profile_fingerprint(cfg),
        "allowed_context_types": sorted(policy_types(cfg)),
        "calc_owner": calc,
        "calc_conflict": calc_conflict,
        "telegram_owner": telegram,
        "telegram_conflict": telegram_conflict,
        "last_error": db.get_meta(LAST_ERROR),
        "state_version": db.get_state_seq(),
        "instruments": instruments,
    }
