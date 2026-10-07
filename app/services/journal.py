"""Агрегирующий журнал раздела «Журнал» ребрендинга LevelFrame (план §6.D).

GET /api/journal собирает единую хронологию из трёх источников и смешивает
её в Python (свежие записи первыми):

- market    — рыночные события: HTF ``event`` + LTF ``ltf_event``;
- decisions — решения пользователя: ``review`` (HTF-зоны) + ``ltf_review``
              (разметка Entry Zones);
- delivery  — доставка уведомлений: ``delivery``.

Выборки переиспользуют репозиторные методы Database (get_events,
list_ltf_events, get_reviews); для ltf_review и delivery метода «все строки
с лимитом» в db.py нет, поэтому запросы к ним — здесь, прямым SQL.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..db import Database
from ..texts_ru import KIND_RU, LTF_EVENT_KIND_RU, LTF_REVIEW_DECISION_RU

JOURNAL_KINDS = ("all", "market", "decisions", "delivery")

# Русские подписи решений ревью. Отдельного словаря для HTF-решений в
# texts_ru нет: LTF_REVIEW_DECISION_RU покрывает §15.3-коды и legacy
# (confirmed/rejected/corrected), добавляем «заметку».
DECISION_RU = {**LTF_REVIEW_DECISION_RU, "note": "заметка"}

# Статусы delivery: pending | sent | failed (app/models.py)
_DELIVERY_TITLE_RU = {
    "sent": "Уведомление доставлено",
    "failed": "Ошибка доставки",
    "pending": "Ожидание доставки",
}


def _title(symbol: Optional[str], rest: str) -> str:
    return f"{symbol} · {rest}" if symbol else rest


def _market_items(db: Database, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    instruments = {i.id: i for i in db.get_instruments()}

    for e in db.get_events(limit=limit):
        zone = db.get_zone(e.zone_id)
        ins = instruments.get(zone.instrument_id) if zone else None
        symbol = ins.symbol if ins else None
        kind_ru = KIND_RU.get(e.kind, e.kind.value)
        text = f"цена {e.price:g}"
        if e.depth:
            text += f", глубина {e.depth:.0%}"
        items.append({
            "at": e.occurred_at,
            "category": "market",
            "kind": e.kind.value,
            "title": _title(symbol, kind_ru),
            "text": text,
            "symbol": symbol,
            "instrument_id": zone.instrument_id if zone else None,
            "ref": {"event_id": e.id, "zone_id": e.zone_id},
            "source": "htf",
        })

    for e in db.list_ltf_events(limit=limit):
        obs = db.get_ltf_observation(e.observation_id)
        ins = instruments.get(obs.instrument_id) if obs else None
        symbol = ins.symbol if ins else None
        ref: dict[str, Any] = {"observation_id": e.observation_id}
        if e.scenario_id is not None:
            ref["scenario_id"] = e.scenario_id
        items.append({
            "at": e.occurred_at,
            "category": "market",
            "kind": e.kind,
            "title": _title(symbol, LTF_EVENT_KIND_RU.get(e.kind, e.kind)),
            "text": "",
            "symbol": symbol,
            "instrument_id": obs.instrument_id if obs else None,
            "ref": ref,
            "source": "ltf",
        })
    return items


def _decision_items(db: Database, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    instruments = {i.id: i for i in db.get_instruments()}

    # get_reviews отдаёт по возрастанию без лимита — берём свежий хвост
    reviews = db.get_reviews()
    for r in sorted(reviews, key=lambda r: (r.created_at, r.id or 0))[-limit:]:
        zone = db.get_zone(r.zone_id)
        ins = instruments.get(zone.instrument_id) if zone else None
        symbol = ins.symbol if ins else None
        decision_ru = DECISION_RU.get(r.decision, r.decision)
        items.append({
            "at": r.created_at,
            "category": "decisions",
            "kind": r.decision,
            "title": _title(symbol, f"Решение: {decision_ru}"),
            "text": r.text,
            "symbol": symbol,
            "instrument_id": zone.instrument_id if zone else None,
            "ref": {"review_id": r.id, "zone_id": r.zone_id},
            "source": "htf",
        })

    rows = db.conn.execute(
        "SELECT * FROM ltf_review ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    for r in rows:
        zone = db.get_ltf_entry_zone(r["entry_zone_id"])
        ins = instruments.get(zone.instrument_id) if zone else None
        symbol = ins.symbol if ins else None
        decision_ru = DECISION_RU.get(r["decision"], r["decision"])
        ref = {"review_id": r["id"], "entry_zone_id": r["entry_zone_id"]}
        if r["scenario_id"] is not None:
            ref["scenario_id"] = r["scenario_id"]
        items.append({
            "at": r["created_at"],
            "category": "decisions",
            "kind": r["decision"],
            "title": _title(symbol, f"Решение: {decision_ru}"),
            "text": r["text"],
            "symbol": symbol,
            "instrument_id": zone.instrument_id if zone else None,
            "ref": ref,
            "source": "ltf",
        })
    return items


def _delivery_items(db: Database, limit: int) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM delivery "
        "ORDER BY COALESCE(delivered_at, 0) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        event_ids = json.loads(r["event_ids"] or "[]")
        parts = [r["destination"], f"событий: {len(event_ids)}"]
        if r["error"]:
            parts.append(f"ошибка: {r['error']}")
        items.append({
            "at": r["delivered_at"] or 0,
            "category": "delivery",
            "kind": r["status"],
            "title": _DELIVERY_TITLE_RU.get(r["status"], "Доставка уведомления"),
            "text": " · ".join(parts),
            "symbol": None,
            "instrument_id": None,
            "ref": {"delivery_id": r["id"]},
            "status": r["status"],
            "source": "htf",
        })
    return items


def collect_journal(db: Database, kind: str = "all", limit: int = 100) -> list[dict[str, Any]]:
    """Единая хронология журнала: слияние категорий по времени (desc),
    обрезка до limit уже после merge."""
    items: list[dict[str, Any]] = []
    if kind in ("all", "market"):
        items.extend(_market_items(db, limit))
    if kind in ("all", "decisions"):
        items.extend(_decision_items(db, limit))
    if kind in ("all", "delivery"):
        items.extend(_delivery_items(db, limit))
    items.sort(key=lambda it: it["at"], reverse=True)
    return items[:limit]
