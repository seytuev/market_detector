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


def upsert_bucket(db, *, scope_id: str, series: str, interval: str,
                  bucket_start: int, payload: dict, quality: str,
                  available_at: int) -> None:
    interval_ms = DAY_MS if interval == "1d" else None
    if interval_ms is None:
        raise ValueError("в первой версии корзины только 1d")
    db.conn.execute(
        """INSERT INTO events_bucket (
               scope_id, series, interval, bucket_start, bucket_end,
               payload_json, quality, available_at
           ) VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (scope_id, series, interval, bucket_start) DO UPDATE SET
               payload_json=excluded.payload_json,
               quality=excluded.quality,
               available_at=excluded.available_at,
               bucket_end=excluded.bucket_end
        """,
        (
            scope_id, series, interval, bucket_start, bucket_start + interval_ms,
            _dump(payload), quality, available_at,
        ),
    )


def load_buckets(db, scope_id: str, series: str) -> list[dict]:
    rows = db.conn.execute(
        """SELECT bucket_start, bucket_end, payload_json, quality, available_at
           FROM events_bucket
           WHERE scope_id=? AND series=? AND interval='1d'
           ORDER BY bucket_start""",
        (scope_id, series),
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


_CRITICAL_RULES = {
    "CASCADE_OI_RISING_RISK",
    "POST_HIGH_LONG_CASCADE",
}


def _delivery_status(notify: bool, snapshot: dict, transition: str, rule_id: str) -> str:
    if not notify:
        return "shadow"
    # Отмена и предупреждение не ждут тихих часов. Сама отправка здесь не делается.
    critical = (
        rule_id in _CRITICAL_RULES
        or transition.endswith("invalidated")
        or "resolved" in transition
    )
    if snapshot.get("quiet_hours") and not critical:
        return "quiet_hold"
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
