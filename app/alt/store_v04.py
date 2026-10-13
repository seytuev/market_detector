"""Запись и выбор структур alt-0.4.

Прогон пишет только свои таблицы. Эпизоды v1/v2, ручные ревизии и
уведомления не изменяются. Повтор на тех же свечах заменяет строки
этой версии, не добавляя дубли.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .rules_v04 import RULES_VERSION, analyze

ELIGIBLE_STATES = ("forming", "mature", "active", "completed")


def persist_result(db: Any, asset_id: int, source_id: int, result: dict[str, Any]) -> int:
    with db.batch_writes():
        return _persist_result(db,asset_id,source_id,result)


def _persist_result(db: Any, asset_id: int, source_id: int, result: dict[str, Any]) -> int:
    """Заменяет структуры этой версии для источника. Возвращает число структур."""
    version = result.get("rules_version") or RULES_VERSION
    computed = int(result.get("computed_at_ms") or 0)
    conn = db.conn
    conn.execute(
        """DELETE FROM alt_structure_revision
           WHERE structure_id IN (
             SELECT id FROM alt_structure
             WHERE source_id=? AND rules_version=?
           )""",
        (source_id, version),
    )
    conn.execute(
        "DELETE FROM alt_structure WHERE source_id=? AND rules_version=?",
        (source_id, version),
    )
    count = 0
    for structure in result.get("structures") or []:
        geometry = structure.get("geometry") or {}
        cur = conn.execute(
            """INSERT INTO alt_structure (
                 asset_id, source_id, structure_key, rules_version, state, eligible,
                 lower, upper, base_start_open_time, base_end_open_time,
                 available_at_ms, computed_at_ms, payload_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                asset_id, source_id, structure["structure_key"], version,
                structure.get("state") or "",
                1 if structure.get("eligible") else 0,
                geometry.get("lower"), geometry.get("upper"),
                geometry.get("base_start_open_time"),
                geometry.get("base_end_open_time"),
                geometry.get("available_at_ms"), computed,
                json.dumps(
                    {"children": structure.get("children") or [], "probes": structure.get("probes") or []},
                    ensure_ascii=False,
                ),
            ),
        )
        structure_id = int(cur.lastrowid)
        revisions = structure.get("revisions") or []
        previous = None
        for revision in revisions:
            conn.execute(
                """INSERT INTO alt_structure_revision (
                     structure_id, revision_id, rules_version, geometry_kind,
                     lower, upper, mid, width,
                     base_start_open_time, base_end_open_time, base_end_confirmed_at_ms,
                     available_at_ms, computed_at_ms, supersedes_revision_id,
                     revision_reason, state, boundary_evidence_json, quality_json,
                     selection_reason, data_coverage_json, params_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    structure_id, revision["revision_id"], version,
                    revision.get("geometry_kind") or "",
                    revision["lower"], revision["upper"], revision["mid"], revision["width"],
                    revision.get("base_start_open_time"),
                    revision.get("base_end_open_time"),
                    revision.get("base_end_confirmed_at_ms"),
                    int(revision.get("available_at_ms") or 0),
                    int(revision.get("computed_at_ms") or computed),
                    previous,
                    revision.get("revision_reason") or "",
                    revision.get("state") or "",
                    json.dumps(revision.get("boundary_evidence") or {}, ensure_ascii=False),
                    json.dumps(revision.get("quality_metrics") or {}, ensure_ascii=False),
                    revision.get("selection_reason") or "",
                    json.dumps(revision.get("data_coverage") or {}, ensure_ascii=False),
                    json.dumps(revision.get("params") or result.get("params") or {}, ensure_ascii=False),
                ),
            )
            previous = revision["revision_id"]
        count += 1
    db._commit()
    return count


def persist_candles(
    db: Any, asset_id: int, source_id: int, candles: list[Any], computed_at_ms: int,
) -> dict[str, Any]:
    result = analyze(candles, computed_at_ms=computed_at_ms)
    persist_result(db, asset_id, source_id, result)
    return result


def list_structures(
    db: Any, asset_id: int, source_id: int, as_of_ms: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Структуры источника. as_of отбирает ревизию, уже доступную на эту дату."""
    rows = db.conn.execute(
        """SELECT * FROM alt_structure
           WHERE asset_id=? AND source_id=? AND rules_version=?
           ORDER BY base_start_open_time""",
        (asset_id, source_id, RULES_VERSION),
    ).fetchall()
    frames = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        revision = _latest_revision(db, int(row["id"]), as_of_ms)
        if revision is None:
            continue
        children = []
        for child in payload.get("children") or []:
            item = dict(child)
            item["parent_structure_id"] = int(row["id"])
            children.append(item)
        frames.append(_frame(row, revision, children))
    return frames


def select_frame(
    db: Any, asset_id: int, source_id: int, as_of_ms: Optional[int],
) -> Optional[dict[str, Any]]:
    """Одна доступная ревизия: открытая база, иначе последняя завершённая.

    Цены берутся из ревизии, чьё available_at_ms уже наступило. Ручной
    приоритет здесь не решается — его накладывает effective_range.
    """
    frames = [
        frame for frame in list_structures(db, asset_id, source_id, as_of_ms)
        if frame.get("eligible")
        and frame["state"] in ELIGIBLE_STATES
        and frame["upper"] > frame["lower"]
        and _available(frame, as_of_ms)
    ]
    if not frames:
        return None
    open_frames = [frame for frame in frames if frame.get("base_end_open_time") is None]
    pool = open_frames or frames
    pool.sort(key=lambda frame: int(frame.get("available_at_ms") or 0))
    return pool[-1]


def _available(frame: dict[str, Any], as_of_ms: Optional[int]) -> bool:
    if as_of_ms is None:
        return True
    return int(frame.get("available_at_ms") or 0) <= int(as_of_ms)


def _latest_revision(db: Any, structure_id: int, as_of_ms: Optional[int]) -> Optional[Any]:
    if as_of_ms is None:
        row = db.conn.execute(
            """SELECT * FROM alt_structure_revision
               WHERE structure_id=? ORDER BY available_at_ms DESC, id DESC LIMIT 1""",
            (structure_id,),
        ).fetchone()
    else:
        row = db.conn.execute(
            """SELECT * FROM alt_structure_revision
               WHERE structure_id=? AND available_at_ms<=?
               ORDER BY available_at_ms DESC, id DESC LIMIT 1""",
            (structure_id, int(as_of_ms)),
        ).fetchone()
    return row


def _frame(structure_row: Any, revision_row: Any, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "range_id": int(structure_row["id"]),
        "revision_id": revision_row["revision_id"],
        "structure_key": structure_row["structure_key"],
        "source_id": int(structure_row["source_id"]),
        "rules_version": revision_row["rules_version"],
        "geometry_kind": revision_row["geometry_kind"],
        "geometry_source": "structural",
        "state": revision_row["state"] or structure_row["state"],
        "eligible": bool(structure_row["eligible"]),
        "lower": revision_row["lower"],
        "upper": revision_row["upper"],
        "mid": revision_row["mid"],
        "width": revision_row["width"],
        "base_start_open_time": revision_row["base_start_open_time"],
        "base_end_open_time": revision_row["base_end_open_time"],
        "base_end_confirmed_at_ms": revision_row["base_end_confirmed_at_ms"],
        "available_at_ms": revision_row["available_at_ms"],
        "computed_at_ms": revision_row["computed_at_ms"],
        "revision_reason": revision_row["revision_reason"],
        "selection_reason": revision_row["selection_reason"],
        "children": children,
        "boundary_evidence": json.loads(revision_row["boundary_evidence_json"] or "{}"),
    }
