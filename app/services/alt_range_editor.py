"""Preview and activation of user-owned ALT range revisions."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

from ..db import Database
from ..models import now_ms
from ..models_alt import AltRangeRevision


def _subject(db: Database, kind: str, subject_id: int):
    if kind == "setup":
        setup = db.get_alt_setup(subject_id)
        if setup is None:
            raise LookupError("Сетап не найден")
        frozen = db.get_alt_frozen_range(setup.range_id)
        if frozen is None:
            raise LookupError("Замороженный диапазон не найден")
        source = db.get_alt_instrument_source(setup.asset_id)
        return setup, frozen, source, frozen.start_anchor_open_time
    if kind == "candidate":
        candidate = db.get_alt_range_candidate(subject_id)
        if candidate is None:
            raise LookupError("Кандидат не найден")
        source = db.get_alt_instrument_source(candidate.asset_id)
        return None, candidate, source, candidate.start_anchor_open_time
    raise ValueError("subject_kind: setup | candidate")


def _validate(lower: Any, upper: Any, start: Any, end: Any) -> tuple[float, float, int, int | None]:
    try:
        lower_f, upper_f, start_i = float(lower), float(upper), int(start)
        end_i = int(end) if end not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("L, U и даты должны быть числами") from exc
    if not math.isfinite(lower_f) or not math.isfinite(upper_f) or not (0 < lower_f < upper_f):
        raise ValueError("Требуется 0 < L < U")
    if start_i <= 0 or (end_i is not None and end_i < start_i):
        raise ValueError("Некорректный интервал базы")
    return lower_f, upper_f, start_i, end_i


def preview_range_revision(db: Database, kind: str, subject_id: int,
                           payload: dict[str, Any]) -> dict[str, Any]:
    setup, obj, source, default_start = _subject(db, kind, subject_id)
    lower, upper, start, end = _validate(
        payload.get("lower"), payload.get("upper"),
        payload.get("base_start_open_time", default_start),
        payload.get("base_end_open_time"),
    )
    if source is None:
        raise LookupError("Источник свечей не найден")
    candles = db.get_alt_candles(source.id, start_ms=start, end_ms=end)
    if not candles:
        raise ValueError("В выбранном интервале нет свечей")
    width = upper - lower
    mid = (lower + upper) / 2
    targets = [{"tp": n, "price": upper + n * width, "hit": False,
                "hit_open_time": None} for n in range(1, 5)]
    excursions_below: list[dict[str, Any]] = []
    below = None
    breakouts: list[dict[str, Any]] = []
    for candle in candles:
        if candle.close < lower:
            if below is None:
                below = {"start_open_time": candle.open_time,
                         "min_price": candle.low, "end_open_time": None}
            below["min_price"] = min(below["min_price"], candle.low)
        elif below is not None:
            below["end_open_time"] = candle.open_time
            excursions_below.append(below)
            below = None
        if candle.close > upper:
            breakouts.append({"open_time": candle.open_time, "close": candle.close})
        for target in targets:
            if not target["hit"] and candle.high >= target["price"]:
                target["hit"] = True
                target["hit_open_time"] = candle.open_time
    if below is not None:
        excursions_below.append(below)
    active = db.active_alt_range_revision(kind, subject_id)
    original = {
        "lower": obj.lower, "upper": obj.upper,
        "base_start_open_time": default_start,
    }
    derived = {
        "mid": mid, "width": width, "targets": targets,
        "cancel": {"price": 2 * lower - upper,
                   "reachable": (2 * lower - upper) > 0},
        "position": ("above" if candles[-1].close > upper else
                     "below" if candles[-1].close < lower else "inside"),
        "breakouts": breakouts,
        "excursions_below": excursions_below,
        "replay_through": candles[-1].open_time,
        "candle_count": len(candles),
        "original_auto": original,
    }
    return {
        "subject_kind": kind, "subject_id": subject_id,
        "expected_revision": active.revision if active else 0,
        "range": {"lower": lower, "upper": upper, "mid": mid, "width": width,
                  "base_start_open_time": start, "base_end_open_time": end},
        "derived": derived,
        "changes": {
            "lower": lower - obj.lower, "upper": upper - obj.upper,
            "superseded_pending_events": (
                len([e for e in db.list_alt_events(subject_id) if not e.delivered])
                if setup is not None else 0
            ),
        },
    }


def save_range_revision(db: Database, kind: str, subject_id: int,
                        payload: dict[str, Any]) -> dict[str, Any]:
    idem = str(payload.get("idempotency_key") or "")
    if idem:
        existing = db.conn.execute(
            "SELECT * FROM alt_range_revision WHERE subject_kind=? AND subject_id=? "
            "AND idempotency_key=?", (kind, subject_id, idem),
        ).fetchone()
        if existing:
            return revision_to_dict(db._to_alt_range_revision(existing), created=False)
    preview = preview_range_revision(db, kind, subject_id, payload)
    expected = int(payload.get("expected_revision", -1))
    if expected != preview["expected_revision"]:
        raise ValueError(f"revision_conflict:{preview['expected_revision']}")
    r = preview["range"]
    rev = AltRangeRevision(
        id=None, subject_kind=kind, subject_id=subject_id, revision=0,
        lower=r["lower"], upper=r["upper"], mid=r["mid"], width=r["width"],
        base_start_open_time=r["base_start_open_time"],
        base_end_open_time=r["base_end_open_time"],
        source_kind=str(payload.get("source_kind") or "manual"),
        derived_json=json.dumps(preview["derived"], ensure_ascii=False),
        reason=str(payload.get("reason") or ""),
        expected_previous_revision=expected,
        idempotency_key=idem or str(uuid.uuid4()),
        created_ms=now_ms(),
    )
    rev, created = db.insert_alt_range_revision(rev)
    if created:
        setup, obj, _, _ = _subject(db, kind, subject_id)
        if kind == "setup":
            db.conn.execute(
                "UPDATE alt_frozen_range SET lower=?,upper=?,mid=?,width=?,"
                "start_anchor_open_time=?,range_version=? WHERE id=?",
                (rev.lower, rev.upper, rev.mid, rev.width,
                 rev.base_start_open_time, rev.revision + 1, obj.id),
            )
            derived = json.loads(rev.derived_json)
            targets = [{"tp": t["tp"], "price": t["price"]} for t in derived["targets"]]
            db.update_alt_setup(
                subject_id, targets_json=json.dumps(targets),
                cancel_price=derived["cancel"]["price"],
                cancel_reachable=derived["cancel"]["reachable"], updated_ms=now_ms(),
            )
        else:
            db.conn.execute(
                "UPDATE alt_range_candidate SET lower=?,upper=?,mid=?,width=?,"
                "start_anchor_open_time=?,version=?,updated_ms=? WHERE id=?",
                (rev.lower, rev.upper, rev.mid, rev.width,
                 rev.base_start_open_time, obj.version + 1, now_ms(), subject_id),
            )
        db.conn.commit()
    return revision_to_dict(rev, created=created)


def revision_to_dict(rev: AltRangeRevision, *, created: bool | None = None) -> dict[str, Any]:
    result = {
        "id": rev.id, "subject_kind": rev.subject_kind, "subject_id": rev.subject_id,
        "revision": rev.revision, "source_kind": rev.source_kind,
        "lower": rev.lower, "upper": rev.upper, "mid": rev.mid, "width": rev.width,
        "base_start_open_time": rev.base_start_open_time,
        "base_end_open_time": rev.base_end_open_time, "reason": rev.reason,
        "active": rev.active, "created_ms": rev.created_ms,
        "derived": json.loads(rev.derived_json or "{}"),
    }
    if created is not None:
        result["created"] = created
    return result
