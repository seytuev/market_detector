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
from ..models import EventKind
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
    instruments = {i.id: i for i in db.get_instruments(include_retired=True, include_removed=True)}

    # Приближение не попадает в хронологию: уведомление по нему выключено.
    for e in db.get_events(limit=limit, exclude_kinds=(EventKind.APPROACH.value,)):
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
        ref["event_id"] = e.id
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
    instruments = {i.id: i for i in db.get_instruments(include_retired=True, include_removed=True)}

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
            sc = db.conn.execute(
                "SELECT observation_id FROM ltf_scenario WHERE id=?",
                (r["scenario_id"],),
            ).fetchone()
            if sc is not None and sc["observation_id"] is not None:
                ref["observation_id"] = sc["observation_id"]
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


def journal_page(db: Database, *, kind: str = "all", limit: int = 100,
                 cursor: str = "", symbol: str = "", query: str = "",
                 scope: str = "", event_type: str = "",
                 from_ms: Optional[int] = None, to_ms: Optional[int] = None) -> dict:
    """Search the complete durable journal before pagination.

    A source-qualified (time, source, id) keyset preserves distinct events at
    the same time and avoids offset drift when new events are inserted.
    The legacy list endpoint remains compatible with existing clients.
    """
    import base64
    from ..texts_ru import ALT_EVENT_KIND_RU

    params: list[Any] = []
    json_object = 'json_build_object' if db.is_postgres else 'json_object'

    def labels(column, mapping):
        cases = []
        for key, value in mapping.items():
            params.extend([getattr(key, "value", key), value])
            cases.append("WHEN ? THEN ?")
        return f"CASE {column} {' '.join(cases)} ELSE 'Событие' END"

    # All branches have exactly the same named columns. SQL filters operate
    # on the human-readable text, not on a capped Python snapshot.
    branches = [f"""
        SELECT e.id, e.occurred_at AS at, 'htf' AS source, 'market' AS category,
          e.kind, {labels('e.kind', KIND_RU)} AS label,
          'Цена ' || e.price AS text, i.symbol, z.instrument_id,
          {json_object}('event_id',e.id,'zone_id',e.zone_id) AS ref_json, '' AS status
        FROM event e LEFT JOIN zone z ON z.id=e.zone_id
        LEFT JOIN instrument i ON i.id=z.instrument_id WHERE e.kind!='approach'
    """, f"""
        SELECT e.id, e.occurred_at, 'ltf', 'market', e.kind,
          {labels('e.kind', LTF_EVENT_KIND_RU)}, '', i.symbol, o.instrument_id,
          {json_object}('event_id',e.id,'observation_id',e.observation_id,
            'scenario_id',e.scenario_id), ''
        FROM ltf_event e LEFT JOIN ltf_observation o ON o.id=e.observation_id
        LEFT JOIN instrument i ON i.id=o.instrument_id
    """, f"""
        SELECT r.id,r.created_at,'htf','decisions',r.decision,
          'Решение: ' || {labels('r.decision', DECISION_RU)},r.text,i.symbol,z.instrument_id,
          {json_object}('review_id',r.id,'zone_id',r.zone_id),''
        FROM review r LEFT JOIN zone z ON z.id=r.zone_id
        LEFT JOIN instrument i ON i.id=z.instrument_id
    """, f"""
        SELECT r.id,r.created_at,'ltf','decisions',r.decision,
          'Решение: ' || {labels('r.decision', DECISION_RU)},r.text,i.symbol,z.instrument_id,
          {json_object}('review_id',r.id,'entry_zone_id',r.entry_zone_id,
            'scenario_id',r.scenario_id,'observation_id',s.observation_id),''
        FROM ltf_review r LEFT JOIN ltf_entry_zone z ON z.id=r.entry_zone_id
        LEFT JOIN ltf_scenario s ON s.id=r.scenario_id
        LEFT JOIN instrument i ON i.id=z.instrument_id
    """, f"""
        SELECT d.id,COALESCE(d.delivered_at,0),'htf','delivery',d.status,
          'Уведомление',d.destination,NULL,NULL,
          {json_object}('delivery_id',d.id),d.status FROM delivery d
    """]
    tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'alt_event' in tables:
        branches.append(f"""
            SELECT e.id,e.event_time_ms,'alt','market',e.event_type,
              {labels('e.event_type', ALT_EVENT_KIND_RU)},'',a.symbol,NULL,
              {json_object}('event_id',e.id,'setup_id',e.setup_id),''
            FROM alt_event e LEFT JOIN alt_setup s ON s.id=e.setup_id
            LEFT JOIN alt_asset a ON a.id=s.asset_id
        """)
    if 'events_publication' in tables:
        branches.append(f"""
            SELECT p.id,p.created_at,'context','market',p.kind,'Рыночный фон',
              COALESCE(json_extract(p.payload_json,'$.text'),''),NULL,NULL,
              {json_object}('event_id',p.id,'assessment_id',p.assessment_id,
                'bundle_id',p.bundle_id),'' FROM events_publication p
        """)
    if 'events_delivery' in tables:
        branches.append(f"""
            SELECT d.id,COALESCE(d.delivered_at,d.created_at),'context','delivery',
              d.status,'Уведомление о рынке',d.channel,NULL,NULL,
              {json_object}('delivery_id',d.id,'bundle_id',d.bundle_id),d.status
            FROM events_delivery d
        """)
    cte = 'WITH entries AS (' + ' UNION ALL '.join(branches) + ') '
    where = ['1=1']
    filters: list[Any] = []
    for col, value in [('category', kind if kind != 'all' else ''),
                       ('symbol', symbol), ('source', scope), ('kind', event_type)]:
        if value:
            where.append(f'{col}=?')
            filters.append(value)
    if from_ms is not None:
        where.append('at>=?')
        filters.append(from_ms)
    if to_ms is not None:
        where.append('at<?')
        filters.append(to_ms)
    if query:
        if db.is_postgres:
            where.append("strpos(lower(COALESCE(symbol,'') || ' ' || label || ' ' || text),?)>0")
            filters.append(query.lower())
        else:
            # SQLite's lower is ASCII-only; Russian search needs casefold.
            db.conn.create_function('lf_casefold', 1, lambda v: str(v or '').casefold(), deterministic=True)
            where.append("instr(lf_casefold(COALESCE(symbol,'') || ' ' || label || ' ' || text),?)>0")
            filters.append(query.casefold())
    predicate = ' AND '.join(where)
    total = db.conn.execute(cte + 'SELECT count(*) FROM entries WHERE ' + predicate,
                            params + filters).fetchone()[0]
    symbols = [r[0] for r in db.conn.execute(cte +
        'SELECT DISTINCT symbol FROM entries WHERE symbol IS NOT NULL ORDER BY symbol', params)]
    types = [dict(r) for r in db.conn.execute(cte +
        'SELECT kind,MIN(label) AS label FROM entries GROUP BY kind ORDER BY label', params)]
    if cursor:
        try:
            values = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
            if (not isinstance(values, list) or len(values) != 4
                    or not isinstance(values[0], int) or not isinstance(values[1], str)
                    or not isinstance(values[2], str) or not isinstance(values[3], int)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError('Некорректная страница журнала') from exc
        where.append('(at,source,category,id)<(?,?,?,?)')
        filters.extend(values)
    rows = db.conn.execute(cte + 'SELECT * FROM entries WHERE ' + ' AND '.join(where) +
        ' ORDER BY at DESC,source DESC,category DESC,id DESC LIMIT ?', params + filters + [limit + 1]).fetchall()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            [last['at'], last['source'], last['category'], last['id']]).encode()).decode()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        ref = item.pop('ref_json')
        item['ref'] = ref if isinstance(ref, dict) else json.loads(ref)
        item['title'] = _title(item['symbol'], item.pop('label'))
        item['uid'] = f"{item['source']}:{item['category']}:{item['id']}"
        items.append(item)
    return {'items': items, 'total': total, 'symbols': symbols, 'types': types,
            'next_cursor': next_cursor, 'scope': 'full_history'}
