"""Запись снимков, журнала и корзин. Повтор того же перехода не создаёт вторую строку."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .mathutil import DAY_MS, RULE_VERSION


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json)


def _json(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


def _key(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_INTERVAL_MS = {"1d": DAY_MS, "4h": 14_400_000, "1h": 3_600_000}

_BUCKET_UPSERT = """INSERT INTO events_bucket (
    scope_id, series, interval, bucket_start, bucket_end,
    payload_json, quality, available_at
) VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT (scope_id, series, interval, bucket_start) DO UPDATE SET
    payload_json=excluded.payload_json, quality=excluded.quality,
    available_at=excluded.available_at, bucket_end=excluded.bucket_end"""


def upsert_bucket(db, *, scope_id: str, series: str, interval: str,
                  bucket_start: int, payload: dict, quality: str,
                  available_at: int) -> None:
    interval_ms = _INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise ValueError("поддерживаются интервалы 1d, 4h, 1h")
    db.conn.execute(
        _BUCKET_UPSERT,
        (
            scope_id, series, interval, bucket_start, bucket_start + interval_ms,
            _dump(payload), quality, available_at,
        ),
    )


def upsert_buckets(db, *, scope_id: str, series: str, interval: str,
                   buckets, available_at: int) -> None:
    """Persist one source sample atomically with pipelined PostgreSQL writes."""
    interval_ms = _INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise ValueError("поддерживаются интервалы 1d, 4h, 1h")
    rows = [(scope_id, series, interval, bar["start"],
             bar["start"] + interval_ms, _dump(bar["payload"]),
             bar["quality"], available_at) for bar in buckets]
    if rows:
        with db.batch_writes():
            db.conn.executemany(_BUCKET_UPSERT, rows)


def load_buckets(db, scope_id: str, series: str, interval: str = "1d", *,
                 since_ms: int | None = None) -> list[dict]:
    extra = " AND bucket_start>=?" if since_ms is not None else ""
    params = (scope_id, series, interval) + ((since_ms,) if since_ms is not None else ())
    rows = db.conn.execute(
        """SELECT bucket_start, bucket_end, payload_json, quality, available_at
           FROM events_bucket
           WHERE scope_id=? AND series=? AND interval=?""" + extra + " ORDER BY bucket_start",
        params,
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["t"] = row["bucket_start"]
        payload["bucket_end"] = row["bucket_end"]
        payload["quality"] = row["quality"]
        payload["available_at"] = row["available_at"]
        out.append(payload)
    return out


def apply_snapshot(db, snapshot: dict, *, notify: bool) -> dict:
    """Сохраняет снимок. Исчезающая активная ситуация закрывается, а не стирается."""
    symbol = snapshot["symbol"]
    now = int(snapshot["evaluated_at"])
    db.conn.execute(
        "INSERT INTO events_snapshot (symbol, evaluated_at, payload_json) VALUES (?,?,?)",
        (symbol, now, _dump(snapshot)),
    )
    current = {
        (item["rule_id"], item["episode_id"]): item
        for item in snapshot.get("situations") or []
    }
    previous = db.conn.execute(
        """SELECT rule_id, episode_id, state, payload_json FROM events_situation
           WHERE symbol=? AND rule_version=?""",
        (symbol, RULE_VERSION),
    ).fetchall()
    prev_map = {(row["rule_id"], row["episode_id"]): row for row in previous}
    journal = 0
    for key, item in current.items():
        old = prev_map.get(key)
        old_state = old["state"] if old else None
        new_state = item["state"]
        if old_state != new_state:
            transition = "appeared" if old_state is None else f"{old_state}->{new_state}"
            journal += _journal(
                db, symbol, item["rule_id"], item["episode_id"], transition, now, item, notify,
                snapshot,
            )
        db.conn.execute(
            """INSERT INTO events_situation (
                   symbol, rule_id, rule_version, episode_id, state, payload_json, updated_at
               ) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (symbol, rule_id, rule_version, episode_id) DO UPDATE SET
                   state=excluded.state,
                   payload_json=excluded.payload_json,
                   updated_at=excluded.updated_at
            """,
            (symbol, item["rule_id"], RULE_VERSION, item["episode_id"], new_state,
             _dump(item), now),
        )
    for key, row in prev_map.items():
        if key in current or row["state"] in {"resolved", "invalidated", "expired"}:
            continue
        rule_id, episode_id = key
        db.conn.execute(
            """UPDATE events_situation SET state='resolved', updated_at=?
               WHERE symbol=? AND rule_id=? AND rule_version=? AND episode_id=?""",
            (now, symbol, rule_id, RULE_VERSION, episode_id),
        )
        journal += _journal(
            db, symbol, rule_id, episode_id, f"{row['state']}->resolved", now,
            {"title": "Ситуация закрыта"}, notify, snapshot,
        )
    for gate in snapshot.get("strategy_gates") or []:
        db.conn.execute(
            """INSERT INTO events_gate (symbol, setup, status, evaluated_at, payload_json)
               VALUES (?,?,?,?,?)""",
            (symbol, gate["setup"], gate["status"], now, _dump(gate)),
        )
    if snapshot.get("morning_digest_due"):
        day = str(now // DAY_MS)
        _journal(
            db, symbol, "MORNING_DIGEST", day, "digest", now,
            {"market_line": snapshot.get("market_line"),
             "service_line": snapshot.get("service_line")},
            notify, snapshot,
        )
    db._commit()
    return {"journal": journal}


def _journal(db, symbol, rule_id, episode_id, transition, now, payload, notify, snapshot) -> int:
    event_key = _key(rule_id, RULE_VERSION, symbol, episode_id, transition, str(now // DAY_MS))
    inserted = db.conn.execute(
        """INSERT OR IGNORE INTO events_journal (
               event_key, symbol, rule_id, transition, occurred_at, payload_json
           ) VALUES (?,?,?,?,?,?)""",
        (event_key, symbol, rule_id, transition, now, _dump(payload)),
    )
    if inserted.rowcount != 1:
        return 0
    if transition in {"appeared", "digest"} or "->" in transition:
        status = _delivery_status(notify, snapshot, transition, rule_id)
        delivery_key = _key(event_key, "owner", "telegram", status)
        db.conn.execute(
            """INSERT OR IGNORE INTO events_outbox (
                   delivery_key, event_key, status, payload_json, created_at
               ) VALUES (?,?,?,?,?)""",
            (delivery_key, event_key, status, _dump({
                "market_line": snapshot.get("market_line"),
                "service_line": snapshot.get("service_line"),
                "transition": transition,
                "rule_id": rule_id,
            }), now),
        )
    return 1


def _delivery_status(notify: bool, snapshot: dict, transition: str, rule_id: str) -> str:
    if not notify:
        return "shadow"
    # Тихих часов нет (спека §2, §9): любое опубликованное изменение — pending.
    return "pending"


def latest_snapshot(db, symbol: str) -> dict | None:
    row = db.conn.execute(
        """SELECT payload_json FROM events_snapshot
           WHERE symbol=? ORDER BY evaluated_at DESC, id DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload_json"])


def snapshot_as_of(db, symbol: str, at_ms: int) -> dict | None:
    row = db.conn.execute(
        """SELECT payload_json FROM events_snapshot
           WHERE symbol=? AND evaluated_at<=?
           ORDER BY evaluated_at DESC, id DESC LIMIT 1""",
        (symbol, at_ms),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload_json"])


def list_journal(db, symbol: str, limit: int = 50) -> list[dict]:
    rows = db.conn.execute(
        """SELECT event_key, rule_id, transition, occurred_at, payload_json
           FROM events_journal WHERE symbol=?
           ORDER BY occurred_at DESC, id DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    return [
        {
            "event_key": row["event_key"],
            "rule_id": row["rule_id"],
            "transition": row["transition"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def list_situations(db, symbol: str) -> list[dict]:
    rows = db.conn.execute(
        """SELECT rule_id, episode_id, state, payload_json, updated_at
           FROM events_situation WHERE symbol=? AND rule_version=?
           ORDER BY updated_at DESC""",
        (symbol, RULE_VERSION),
    ).fetchall()
    return [
        {
            "rule_id": row["rule_id"],
            "episode_id": row["episode_id"],
            "state": row["state"],
            "updated_at": row["updated_at"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def gate_history(db, symbol: str, setup: str, limit: int = 20) -> list[dict]:
    rows = db.conn.execute(
        """SELECT status, evaluated_at, payload_json FROM events_gate
           WHERE symbol=? AND setup=? ORDER BY evaluated_at DESC, id DESC LIMIT ?""",
        (symbol, setup, limit),
    ).fetchall()
    return [
        {
            "status": row["status"],
            "evaluated_at": row["evaluated_at"],
            "gate": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


# --- Память фактов и публикации v2 (спека 2026-10-11) ---


def _fact_row(row) -> dict:
    return {
        "fact_key": row["fact_key"],
        "rule_id": row["rule_id"],
        "rule_version": row["rule_version"],
        "scope": row["scope"],
        "episode_id": row["episode_id"],
        "parent_key": row["parent_key"],
        "direction": row["direction"],
        "state": row["state"],
        "verification": row["verification"],
        "occurred_at": row["occurred_at"],
        "detected_at": row["detected_at"],
        "available_at": row["available_at"],
        "valid_until": row["valid_until"],
        "time_precision": row["time_precision"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def upsert_fact(db, fact: dict) -> None:
    """Вставка по fact_key; при конфликте обновляются только payload, метки обнаружения
    и verification — occurred_at/valid_until/state неизменны (спека §5, AC05)."""
    db.conn.execute(
        """INSERT INTO events_fact (
               fact_key, rule_id, rule_version, scope, episode_id, parent_key,
               direction, state, verification, occurred_at, detected_at, available_at,
               valid_until, time_precision, payload_json, created_at, updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (fact_key) DO UPDATE SET
               payload_json=excluded.payload_json,
               detected_at=excluded.detected_at,
               available_at=excluded.available_at,
               verification=excluded.verification,
               updated_at=excluded.updated_at
        """,
        (
            fact["fact_key"], fact["rule_id"], fact["rule_version"], fact["scope"],
            fact.get("episode_id"), fact.get("parent_key"), fact.get("direction"),
            fact["state"], fact.get("verification", "unknown"),
            fact["occurred_at"], fact["detected_at"], fact["available_at"],
            fact["valid_until"], fact.get("time_precision", "exact"),
            _dump(fact.get("payload") or {}), fact["created_at"], fact["updated_at"],
        ),
    )
    db._commit()


def get_fact(db, fact_key: str) -> dict | None:
    row = db.conn.execute(
        "SELECT * FROM events_fact WHERE fact_key=?", (fact_key,),
    ).fetchone()
    return _fact_row(row) if row is not None else None


def query_facts(db, *, state: str | None = None,
                active_now_ms: int | None = None) -> list[dict]:
    """active_now_ms задан — только активные факты; граница valid_until исключительная (§5)."""
    sql = "SELECT * FROM events_fact"
    params: list = []
    if active_now_ms is not None:
        sql += " WHERE state IN ('provisional','confirmed') AND valid_until > ? AND occurred_at<=? AND available_at<=?"
        params.extend((active_now_ms, active_now_ms, active_now_ms))
    elif state is not None:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY occurred_at, fact_key"
    rows = db.conn.execute(sql, params).fetchall()
    return [_fact_row(row) for row in rows]


def facts_expired_due(db, now_ms: int) -> list[dict]:
    """Активные факты, у которых срок уже истёк: valid_until <= now_ms."""
    rows = db.conn.execute(
        """SELECT * FROM events_fact
           WHERE state IN ('provisional','confirmed') AND valid_until <= ?
           ORDER BY valid_until, fact_key""",
        (now_ms,),
    ).fetchall()
    return [_fact_row(row) for row in rows]


def set_fact_state(db, fact_key: str, state: str, at_ms: int) -> None:
    db.conn.execute(
        "UPDATE events_fact SET state=?, updated_at=? WHERE fact_key=?",
        (state, at_ms, fact_key),
    )
    db._commit()


def set_fact_direction(db, fact_key: str, direction: str | None, at_ms: int) -> None:
    db.conn.execute(
        "UPDATE events_fact SET direction=?, updated_at=? WHERE fact_key=?",
        (direction, at_ms, fact_key),
    )
    db._commit()


def add_fact_transition(db, *, fact_key: str, transition: str,
                        from_state: str | None, to_state: str,
                        reason: str | None, at_ms: int,
                        payload: dict | None = None) -> None:
    db.conn.execute(
        """INSERT INTO events_fact_transition (
               fact_key, transition, from_state, to_state, reason, at_ms, payload_json
           ) VALUES (?,?,?,?,?,?,?)""",
        (fact_key, transition, from_state, to_state, reason, at_ms,
         _dump(payload or {})),
    )
    db._commit()


def fact_transitions_since(db, since_ms: int) -> list[dict]:
    rows = db.conn.execute(
        """SELECT * FROM events_fact_transition
           WHERE at_ms >= ? ORDER BY at_ms, id""",
        (since_ms,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "fact_key": row["fact_key"],
            "transition": row["transition"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "reason": row["reason"],
            "at_ms": row["at_ms"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def insert_assessment(db, assessment: dict) -> None:
    """Снимок оценки целиком в payload_json; ключи верхнего уровня дублируют колонки."""
    db.conn.execute(
        """INSERT INTO events_assessment (
               assessment_id, evaluated_at, available_at, payload_json
           ) VALUES (?,?,?,?)""",
        (assessment["assessment_id"], assessment["evaluated_at"],
         assessment["available_at"], _dump(assessment)),
    )
    db._commit()


def latest_assessment(db) -> dict | None:
    row = db.conn.execute(
        "SELECT payload_json FROM events_assessment ORDER BY evaluated_at DESC LIMIT 1",
    ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None


def assessment_as_of(db, captured_ms: int) -> dict | None:
    """«Известно тогда» (§8.2): последняя оценка с available_at <= captured_ms."""
    row = db.conn.execute(
        """SELECT payload_json FROM events_assessment
           WHERE available_at <= ? ORDER BY evaluated_at DESC LIMIT 1""",
        (captured_ms,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None


def get_assessment(db, assessment_id: str) -> dict | None:
    row = db.conn.execute(
        "SELECT payload_json FROM events_assessment WHERE assessment_id=?",
        (assessment_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row is not None else None


def put_basket(db, *, version: str, day_start_ms: int, payload: dict) -> None:
    """Состав корзины неизменен в рамках версии: повтор по той же версии игнорируется."""
    db.conn.execute(
        """INSERT OR IGNORE INTO events_basket (version, day_start_ms, payload_json)
           VALUES (?,?,?)""",
        (version, day_start_ms, _dump(payload)),
    )
    db._commit()


def latest_basket(db) -> dict | None:
    return basket_at(db, 9_223_372_036_854_775_807)


def basket_at(db, at_ms: int) -> dict | None:
    rows = db.conn.execute(
        """SELECT * FROM events_basket WHERE day_start_ms=(
               SELECT MAX(day_start_ms) FROM events_basket WHERE day_start_ms<=?
           ) ORDER BY version""",
        (at_ms,),
    ).fetchall()
    if not rows:
        return None
    # A failed empty bootstrap must not hide the recovered basket for that day.
    row = max(rows, key=lambda r: bool(json.loads(r["payload_json"]).get("members")))
    return {**json.loads(row["payload_json"]), "version": row["version"], "day_start_ms": row["day_start_ms"]}


def set_source_state(db, *, source: str, quality: str, detail: str | None,
                     at_ms: int, payload: dict | None = None) -> None:
    db.conn.execute(
        """INSERT INTO events_source_state (source, quality, detail, updated_at, payload_json)
           VALUES (?,?,?,?,?)
           ON CONFLICT (source) DO UPDATE SET
               quality=excluded.quality,
               detail=excluded.detail,
               updated_at=excluded.updated_at,
               payload_json=excluded.payload_json
        """,
        (source, quality, detail, at_ms, _dump(payload or {})),
    )
    db._commit()


def get_source_states(db) -> dict[str, dict]:
    rows = db.conn.execute("SELECT * FROM events_source_state").fetchall()
    return {
        row["source"]: {
            "quality": row["quality"],
            "detail": row["detail"],
            "updated_at": row["updated_at"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    }


def upsert_metric(db, *, series: str, bucket_start: int, payload: dict) -> None:
    db.conn.execute(
        """INSERT INTO events_metric (series, bucket_start, payload_json)
           VALUES (?,?,?) ON CONFLICT(series,bucket_start)
           DO UPDATE SET payload_json=excluded.payload_json""",
        (series, bucket_start, _dump(payload)),
    )
    db._commit()


def metric_series(db, series: str, *, since_ms: int = 0) -> list[dict]:
    rows = db.conn.execute(
        """SELECT bucket_start, payload_json FROM events_metric
           WHERE series=? AND bucket_start >= ? ORDER BY bucket_start""",
        (series, since_ms),
    ).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["bucket_start"] = row["bucket_start"]
        out.append(payload)
    return out


def upsert_metrics(db, *, series: str, rows: list[dict]) -> None:
    """Write a calendar history chunk in one database batch."""
    values = [(series, row["bucket_start"], _dump(row)) for row in rows]
    if values:
        with db.batch_writes():
            db.conn.executemany(
                """INSERT INTO events_metric (series, bucket_start, payload_json)
                   VALUES (?,?,?) ON CONFLICT(series,bucket_start)
                   DO UPDATE SET payload_json=excluded.payload_json""", values,
            )


def _bundle_row(row) -> dict:
    return {
        "id": row["id"],
        "opened_at": row["opened_at"],
        "closes_at": row["closes_at"],
        "status": row["status"],
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]),
        "published_at": row["published_at"],
    }


def open_bundle(db) -> dict | None:
    """Открытый пакет объединения (60s); одновременно существует не более одного."""
    row = db.conn.execute(
        """SELECT * FROM events_bundle WHERE status='open'
           ORDER BY opened_at DESC, id DESC LIMIT 1""",
    ).fetchone()
    return _bundle_row(row) if row is not None else None


def create_bundle(db, *, opened_at: int, closes_at: int, kind: str = "changes") -> int:
    cur = db.conn.execute(
        """INSERT INTO events_bundle (opened_at, closes_at, status, kind)
           VALUES (?,?, 'open', ?)""",
        (opened_at, closes_at, kind),
    )
    db._commit()
    return cur.lastrowid


def set_bundle_status(db, bundle_id: int, status: str, *,
                      published_at: int | None = None,
                      payload: dict | None = None) -> None:
    sets = ["status=?"]
    params: list = [status]
    if published_at is not None:
        sets.append("published_at=?")
        params.append(published_at)
    if payload is not None:
        sets.append("payload_json=?")
        params.append(_dump(payload))
    params.append(bundle_id)
    db.conn.execute(
        f"UPDATE events_bundle SET {', '.join(sets)} WHERE id=?", params,
    )
    db._commit()


def get_bundle(db, bundle_id: int) -> dict | None:
    row = db.conn.execute(
        "SELECT * FROM events_bundle WHERE id=?", (bundle_id,),
    ).fetchone()
    return _bundle_row(row) if row is not None else None


def list_bundles(db, *, limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM events_bundle"
    params: list = []
    if status is not None:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.conn.execute(sql, params).fetchall()
    return [_bundle_row(row) for row in rows]


def add_publication(db, *, pub_key: str, bundle_id: int, kind: str,
                    assessment_id: str | None, window_ms: int | None,
                    payload: dict, created_at: int) -> bool:
    """Якорь идемпотентности (§9): повтор того же pub_key не создаёт вторую строку."""
    inserted = db.conn.execute(
        """INSERT OR IGNORE INTO events_publication (
               pub_key, bundle_id, kind, assessment_id, window_ms, payload_json, created_at
           ) VALUES (?,?,?,?,?,?,?)""",
        (pub_key, bundle_id, kind, assessment_id, window_ms, _dump(payload), created_at),
    )
    db._commit()
    return inserted.rowcount == 1


def bundle_publications(db, bundle_id: int) -> list[dict]:
    rows = db.conn.execute(
        """SELECT * FROM events_publication WHERE bundle_id=? ORDER BY id""",
        (bundle_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "pub_key": row["pub_key"],
            "bundle_id": row["bundle_id"],
            "kind": row["kind"],
            "assessment_id": row["assessment_id"],
            "window_ms": row["window_ms"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_publication(db, pub_id: int) -> dict | None:
    """Публикация по id (finish-колбэк outbox получает publication id)."""
    row = db.conn.execute(
        "SELECT * FROM events_publication WHERE id=?", (pub_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "pub_key": row["pub_key"],
        "bundle_id": row["bundle_id"],
        "kind": row["kind"],
        "assessment_id": row["assessment_id"],
        "window_ms": row["window_ms"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    }


def upsert_delivery(db, *, bundle_id: int, recipient: str, channel: str,
                    status: str, at_ms: int, detail: str | None = None,
                    delivered_at: int | None = None) -> None:
    """Доставка идентифицируется пакетом, получателем и каналом; статус — не часть ключа."""
    db.conn.execute(
        """INSERT INTO events_delivery (
               bundle_id, recipient, channel, status, detail, created_at, delivered_at
           ) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (bundle_id, recipient, channel) DO UPDATE SET
               status=excluded.status,
               detail=excluded.detail,
               delivered_at=excluded.delivered_at
        """,
        (bundle_id, recipient, channel, status, detail, at_ms, delivered_at),
    )
    db._commit()


def list_deliveries(db, *, bundle_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM events_delivery"
    params: list = []
    if bundle_id is not None:
        sql += " WHERE bundle_id=?"
        params.append(bundle_id)
    sql += " ORDER BY id"
    rows = db.conn.execute(sql, params).fetchall()
    return [
        {
            "id": row["id"],
            "bundle_id": row["bundle_id"],
            "recipient": row["recipient"],
            "channel": row["channel"],
            "status": row["status"],
            "detail": row["detail"],
            "created_at": row["created_at"],
            "delivered_at": row["delivered_at"],
        }
        for row in rows
    ]
