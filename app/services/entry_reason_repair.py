"""Ремонт сохранённого reason терминальных Entry Zones (F31).

Не запускается при старте. Сначала dry_run на копии: журнал id/old/new.
В рабочую базу — только строки, которые доказывают терминальность
актуальные тесты ликвидности (confirmed/failed), заполненный FVG или
invalid-зона, и только если сохранённый reason пустой или ok
(ложная пригодность). Уже исключающие reason, включая tested_too_deep,
outside_pd и origin_unresolved, не переписываются: read-модель ставит
терминальный запрет выше строки, а массовая замена стирает исходную
причину. display_until и наблюдение не трогаются.
"""
from __future__ import annotations

import json

from ..engine.ltf.eligibility import (
    REASON_FVG_FILLED,
    REASON_INVALID,
    REASON_LEVEL_BROKEN,
    REASON_SWEPT_LEVEL,
    evaluate_final,
)
from ..engine.ltf.entries import fvg_filled
from ..models import now_ms

JOURNAL_KEY = "ltf:entry_reason_repair:journal"
_TERMINAL = {
    REASON_LEVEL_BROKEN,
    REASON_SWEPT_LEVEL,
    REASON_FVG_FILLED,
    REASON_INVALID,
}


def _proven(zone, tests) -> str | None:
    """Причина, которую доказывают тесты или lifecycle зоны, а не старая строка."""
    if zone is None:
        return None
    if zone.validity == "invalid":
        return REASON_INVALID
    if zone.type in ("BSL", "SSL"):
        own = [t for t in tests if t.entry_zone_id == zone.id]
        if any(t.state == "confirmed" for t in own):
            return REASON_SWEPT_LEVEL
        if any(t.state == "failed" for t in own):
            return REASON_LEVEL_BROKEN
    if zone.type == "FVG" and fvg_filled(zone):
        return REASON_FVG_FILLED
    return None


def repair_terminal_entry_reasons(db, *, dry_run: bool = True) -> list[dict]:
    """Вернуть список изменений. dry_run=True ничего не пишет, кроме
    отсутствия записи. dry_run=False обновляет reason и дописывает журнал."""
    changes: list[dict] = []
    for entry in db.list_all_ltf_scenario_entries():
        zone = db.get_ltf_entry_zone(entry.entry_zone_id)
        tests = db.list_ltf_liquidity_tests(scenario_id=entry.scenario_id)
        proven = _proven(zone, tests)
        if proven is None or proven == entry.reason:
            continue
        # Ложный ok (и пустой reason) заменяем. Остальные сохранённые
        # причины уже не допускают строку; tested_too_deep не трогаем.
        if (entry.reason or "") not in ("", "ok"):
            continue
        fe = evaluate_final(
            entry, zone, allow_outside=False, liquidity_tests=tests,
        )
        if fe.primary_reason not in _TERMINAL:
            continue
        if fe.primary_reason != proven:
            continue
        changes.append({
            "id": entry.id,
            "scenario_id": entry.scenario_id,
            "entry_zone_id": entry.entry_zone_id,
            "old": entry.reason,
            "new": proven,
            "reason": proven,
        })
    if dry_run:
        return changes
    with db.batch_writes():
        for row in changes:
            db.set_ltf_scenario_entry_reason(row["id"], row["new"])
    previous = []
    raw = db.get_meta(JOURNAL_KEY)
    if raw:
        try:
            previous = json.loads(raw)
        except json.JSONDecodeError:
            previous = []
    previous.append({"at": now_ms(), "changes": changes})
    db.set_meta(JOURNAL_KEY, json.dumps(previous, ensure_ascii=False))
    return changes
