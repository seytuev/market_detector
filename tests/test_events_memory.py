"""Жизненный цикл фактов памяти (спека §5, AC05–AC07): срок, состояния, поздние записи."""
from __future__ import annotations

from app.db import Database
from app.events import store
from app.events.memory import (
    MEMORY_HOURS_DEFAULT,
    begin_fact,
    child_fact,
    confirm_fact,
    expire_due,
    invalidate_fact,
    make_fact_key,
    reinterpret_fact,
)

T0 = 1_800_000_000_000
HOUR_MS = 3_600_000
MEMORY_MS = MEMORY_HOURS_DEFAULT * HOUR_MS


def _db() -> Database:
    return Database(":memory:")


def _begin(db, **kwargs):
    base = dict(
        rule_id="LIQ_CASCADE", rule_version="events-time-2.0",
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=T0, detected_at=T0 + 1_000, available_at=T0 + 500,
    )
    base.update(kwargs)
    return begin_fact(db, **base)


def test_fact_active_until_exclusive_240h_boundary():
    """AC05: факт активен до occurred_at+240h-1ms и исключается точно на границе."""
    db = _db()
    fact, created = _begin(db)
    assert created is True
    assert fact["valid_until"] == T0 + MEMORY_MS
    assert fact["state"] == "provisional"

    active = store.query_facts(db, active_now_ms=T0 + MEMORY_MS - 1)
    assert [f["fact_key"] for f in active] == [fact["fact_key"]]
    # граница исключительная: при now >= valid_until факт не участвует
    assert store.query_facts(db, active_now_ms=T0 + MEMORY_MS) == []
    # но до перевода в expired он виден как просроченный для expire_due
    assert store.facts_expired_due(db, T0 + MEMORY_MS - 1) == []
    assert len(store.facts_expired_due(db, T0 + MEMORY_MS)) == 1


def test_repeat_begin_fact_does_not_extend_memory():
    """AC05: повторный опрос с тем же ключом не продлевает срок и возвращает created=False."""
    db = _db()
    fact, _ = _begin(db, payload={"sum": 100})
    later = T0 + 2 * 24 * HOUR_MS
    again, created = _begin(
        db, detected_at=later, available_at=later, payload={"sum": 130},
        verification="ok",
    )
    assert created is False
    assert again["fact_key"] == fact["fact_key"]
    assert again["occurred_at"] == T0
    assert again["valid_until"] == T0 + MEMORY_MS
    assert again["state"] == "provisional"
    # обновляются только payload и метки обнаружения
    assert again["payload"] == {"sum": 130}
    assert again["detected_at"] == later
    assert again["available_at"] == later
    assert again["verification"] == "ok"
    # переход 'appeared' записан один раз
    transitions = store.fact_transitions_since(db, 0)
    assert [t["transition"] for t in transitions] == ["appeared"]


def test_child_fact_has_own_lifetime_and_parent_untouched():
    """AC06: дочерний факт со своим сроком; родитель не переписывается."""
    db = _db()
    parent, _ = _begin(db, payload={"mark": "parent"})
    child_at = T0 + 3 * 24 * HOUR_MS
    child, created = child_fact(
        db, parent["fact_key"],
        rule_id="LIQ_CASCADE", rule_version="events-time-2.0",
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=child_at, detected_at=child_at, available_at=child_at,
        payload={"mark": "child"},
    )
    assert created is True
    assert child["fact_key"] != parent["fact_key"]
    assert child["parent_key"] == parent["fact_key"]
    assert child["occurred_at"] == child_at
    assert child["valid_until"] == child_at + MEMORY_MS

    # позднее обновление ребёнка не трогает родителя
    begin_fact(
        db, rule_id="LIQ_CASCADE", rule_version="events-time-2.0",
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=child_at, detected_at=child_at + 5_000,
        available_at=child_at + 5_000, occurrence=str(child_at),
        parent_key=parent["fact_key"], payload={"mark": "child-2"},
    )
    parent_after = store.get_fact(db, parent["fact_key"])
    assert parent_after["occurred_at"] == T0
    assert parent_after["valid_until"] == T0 + MEMORY_MS
    assert parent_after["payload"] == {"mark": "parent"}
    assert parent_after["detected_at"] == T0 + 1_000


def test_invalidate_and_expire_are_distinguishable():
    """AC07: invalidated и expired — разные состояния, оба исключены из активных."""
    db = _db()
    inv, _ = _begin(db, episode_id="ep-inv", occurrence="inv")
    exp, _ = _begin(db, episode_id="ep-exp", occurrence="exp",
                    detected_at=T0 + 2_000, available_at=T0 + 2_000)

    invalidated = invalidate_fact(db, inv["fact_key"], T0 + 10_000, "условие опровергнуто")
    assert invalidated["state"] == "invalidated"

    expired = expire_due(db, T0 + MEMORY_MS)
    assert [f["fact_key"] for f in expired] == [exp["fact_key"]]
    assert expired[0]["state"] == "expired"
    # уже invalidated факт не переводится в expired
    assert store.get_fact(db, inv["fact_key"])["state"] == "invalidated"

    assert store.query_facts(db, active_now_ms=T0 + MEMORY_MS) == []
    transitions = store.fact_transitions_since(db, 0)
    kinds = {(t["fact_key"], t["transition"]) for t in transitions}
    assert (inv["fact_key"], "invalidated") in kinds
    assert (exp["fact_key"], "expired") in kinds
    tr_inv = next(t for t in transitions if t["transition"] == "invalidated")
    assert tr_inv["from_state"] == "provisional"
    assert tr_inv["reason"] == "условие опровергнуто"


def test_confirmed_fact_expires_as_expired_not_invalidated():
    db = _db()
    fact, _ = _begin(db)
    confirmed = confirm_fact(db, fact["fact_key"], T0 + 60_000, "закрытие окна")
    assert confirmed["state"] == "confirmed"
    # повторное подтверждение и инвалидация после истечения — без смены состояния
    assert confirm_fact(db, fact["fact_key"], T0 + 120_000)["state"] == "confirmed"
    expire_due(db, T0 + MEMORY_MS)
    assert store.get_fact(db, fact["fact_key"])["state"] == "expired"
    assert invalidate_fact(db, fact["fact_key"], T0 + MEMORY_MS, "поздно")["state"] == "expired"
    transitions = [t["transition"] for t in store.fact_transitions_since(db, 0)]
    assert transitions == ["appeared", "confirmed", "expired"]


def test_late_fact_is_historical_record_not_current_signal():
    """§5: факт старше 240h на момент обнаружения — expired + late_record, не в активных."""
    db = _db()
    detected = T0 + 241 * HOUR_MS
    fact, created = _begin(db, detected_at=detected, available_at=detected)
    assert created is True
    assert fact["state"] == "expired"
    assert fact["payload"]["late"] is True
    assert store.query_facts(db, active_now_ms=detected) == []
    transitions = store.fact_transitions_since(db, 0)
    assert len(transitions) == 1
    assert transitions[0]["transition"] == "late_record"
    assert transitions[0]["to_state"] == "expired"
    # факт ровно на границе срока — ещё не поздний
    edge, _ = _begin(db, episode_id="ep-edge", occurrence="edge",
                     detected_at=T0 + MEMORY_MS, available_at=T0 + MEMORY_MS)
    assert edge["state"] == "expired"
    assert edge["payload"]["late"] is True
    almost, _ = _begin(db, episode_id="ep-ok", occurrence="ok",
                       detected_at=T0 + MEMORY_MS - 1, available_at=T0 + MEMORY_MS - 1)
    assert almost["state"] == "provisional"
    assert "late" not in almost["payload"]


def test_reinterpret_keeps_state_and_original_times():
    """§5: смена интерпретации — переход reinterpreted, состояние и времена сохраняются."""
    db = _db()
    fact, _ = _begin(db, payload={"phase": "cascade"})
    later = T0 + 24 * HOUR_MS
    updated = reinterpret_fact(
        db, fact["fact_key"], new_direction="long", at_ms=later,
        reason="затухание и восстановление ширины", payload={"phase": "recovery"},
    )
    assert updated["direction"] == "long"
    assert updated["state"] == "provisional"
    assert updated["occurred_at"] == T0
    assert updated["valid_until"] == T0 + MEMORY_MS
    assert updated["payload"] == {"phase": "recovery"}
    transitions = store.fact_transitions_since(db, 0)
    assert [t["transition"] for t in transitions] == ["appeared", "reinterpreted"]
    tr = transitions[-1]
    assert tr["reason"] == "затухание и восстановление ширины"
    assert tr["payload"] == {"from_direction": "short", "to_direction": "long"}
    # сброс направления допустим: факт есть, направление неизвестно
    cleared = reinterpret_fact(db, fact["fact_key"], new_direction=None,
                               at_ms=later + 1, reason="контекст потерян")
    assert cleared["direction"] is None


def test_make_fact_key_ignores_detection_day():
    """§9: ключ — правило/версия/scope/эпизод/occurrence; день обнаружения не входит."""
    kwargs = dict(rule_id="R", rule_version="v1", scope="crypto_market",
                  episode_id="ep", occurrence="2026-10-11")
    assert make_fact_key(**kwargs) == make_fact_key(**kwargs)
    assert len(make_fact_key(**kwargs)) == 20
    assert make_fact_key(**kwargs) != make_fact_key(**{**kwargs, "occurrence": "2026-10-12"})
    assert make_fact_key(**kwargs) != make_fact_key(**{**kwargs, "episode_id": None})
