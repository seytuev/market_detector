"""Десятидневная память фактов (спека §5): жизненный цикл, переходы, поздние записи.

Срок учёта — 240 часов от occurred_at конкретного факта; граница исключительная.
Повторный опрос срок не продлевает; дочерний факт эпизода получает собственный срок.
invalidated и expired — различные состояния и не подменяют друг друга (AC07).
"""
from __future__ import annotations

import hashlib

from . import store

MEMORY_HOURS_DEFAULT = 240
_HOUR_MS = 3_600_000


def make_fact_key(*, rule_id: str, rule_version: str, scope: str,
                  episode_id: str | None, occurrence: str) -> str:
    """Стабильный ключ факта; день обнаружения в ключ не входит (§9)."""
    raw = "|".join([rule_id, rule_version, scope, episode_id or "", occurrence])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def begin_fact(db, *, rule_id: str, rule_version: str, scope: str,
               episode_id: str | None = None, parent_key: str | None = None,
               direction: str | None = None,
               occurred_at: int, detected_at: int, available_at: int,
               time_precision: str = "exact", payload: dict | None = None,
               state: str = "provisional", verification: str = "unknown",
               memory_hours: int = MEMORY_HOURS_DEFAULT,
               occurrence: str = "") -> tuple[dict, bool]:
    """Регистрация факта. Возвращает (fact, created).

    Существующий ключ: обновляются только payload и метки обнаружения, срок не
    продлевается и состояние не меняется (AC05). Поздний факт (старше срока памяти
    на момент обнаружения) сохраняется как историческая запись expired с переходом
    late_record и payload["late"]=True — не текущий сигнал (§5).
    """
    occurrence = occurrence or str(occurred_at)
    fact_key = make_fact_key(
        rule_id=rule_id, rule_version=rule_version, scope=scope,
        episode_id=episode_id, occurrence=occurrence,
    )
    existing = store.get_fact(db, fact_key)
    if existing is not None:
        store.upsert_fact(db, {
            **existing,
            "payload": payload if payload is not None else existing["payload"],
            "detected_at": detected_at,
            "available_at": available_at,
            "verification": verification,
            "updated_at": detected_at,
        })
        return store.get_fact(db, fact_key), False

    valid_until = occurred_at + memory_hours * _HOUR_MS
    fact_payload = dict(payload or {})
    if valid_until <= detected_at:
        # позднее получение: историческая запись, не текущий сигнал
        state = "expired"
        fact_payload["late"] = True
    fact = {
        "fact_key": fact_key,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "scope": scope,
        "episode_id": episode_id,
        "parent_key": parent_key,
        "direction": direction,
        "state": state,
        "verification": verification,
        "occurred_at": occurred_at,
        "detected_at": detected_at,
        "available_at": available_at,
        "valid_until": valid_until,
        "time_precision": time_precision,
        "payload": fact_payload,
        "created_at": detected_at,
        "updated_at": detected_at,
    }
    store.upsert_fact(db, fact)
    if fact_payload.get("late"):
        store.add_fact_transition(
            db, fact_key=fact_key, transition="late_record", from_state=None,
            to_state="expired", reason="получен после истечения срока памяти",
            at_ms=detected_at,
        )
    else:
        store.add_fact_transition(
            db, fact_key=fact_key, transition="appeared", from_state=None,
            to_state=state, reason=None, at_ms=detected_at,
        )
    return store.get_fact(db, fact_key), True


def confirm_fact(db, fact_key: str, at_ms: int,
                 reason: str | None = None) -> dict | None:
    """provisional → confirmed; для остальных состояний — без изменений."""
    fact = store.get_fact(db, fact_key)
    if fact is None:
        return None
    if fact["state"] != "provisional":
        return fact
    store.set_fact_state(db, fact_key, "confirmed", at_ms)
    store.add_fact_transition(
        db, fact_key=fact_key, transition="confirmed",
        from_state="provisional", to_state="confirmed", reason=reason, at_ms=at_ms,
    )
    return store.get_fact(db, fact_key)


def invalidate_fact(db, fact_key: str, at_ms: int, reason: str) -> dict | None:
    """Опровержение до истечения срока: немедленно исключает факт из расчёта (§5)."""
    fact = store.get_fact(db, fact_key)
    if fact is None:
        return None
    if fact["state"] not in ("provisional", "confirmed"):
        return fact
    store.set_fact_state(db, fact_key, "invalidated", at_ms)
    store.add_fact_transition(
        db, fact_key=fact_key, transition="invalidated",
        from_state=fact["state"], to_state="invalidated", reason=reason, at_ms=at_ms,
    )
    return store.get_fact(db, fact_key)


def reinterpret_fact(db, fact_key: str, *, new_direction: str | None,
                     at_ms: int, reason: str,
                     payload: dict | None = None) -> dict | None:
    """Смена интерпретации: состояние и исходные времена сохраняются (§5)."""
    fact = store.get_fact(db, fact_key)
    if fact is None:
        return None
    store.set_fact_direction(db, fact_key, new_direction, at_ms)
    if payload:
        merged = {**fact["payload"], **payload}
        store.upsert_fact(db, {**fact, "payload": merged, "updated_at": at_ms})
    store.add_fact_transition(
        db, fact_key=fact_key, transition="reinterpreted",
        from_state=fact["state"], to_state=fact["state"], reason=reason, at_ms=at_ms,
        payload={"from_direction": fact["direction"], "to_direction": new_direction},
    )
    return store.get_fact(db, fact_key)


def expire_due(db, now_ms: int) -> list[dict]:
    """Переводит просроченные активные факты в expired («Больше не учитывается»)."""
    expired = []
    for fact in store.facts_expired_due(db, now_ms):
        store.set_fact_state(db, fact["fact_key"], "expired", now_ms)
        store.add_fact_transition(
            db, fact_key=fact["fact_key"], transition="expired",
            from_state=fact["state"], to_state="expired", reason=None, at_ms=now_ms,
        )
        expired.append(store.get_fact(db, fact["fact_key"]))
    return expired


def child_fact(db, parent_key: str, **kwargs) -> tuple[dict, bool]:
    """Дочерний факт эпизода с собственными occurred_at/valid_until (AC06)."""
    return begin_fact(db, parent_key=parent_key, **kwargs)
