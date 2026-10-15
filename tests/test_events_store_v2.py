"""Хранилище v2 (спека §§4, 9, 10): факты, оценки, корзина, пакеты, доставки, источники."""
from __future__ import annotations

from app.db import Database
from app.events import store
from app.events.store import _delivery_status
import pytest

T0 = 1_800_000_000_000
HOUR_MS = 3_600_000


def _db() -> Database:
    return Database(":memory:")


def test_bulk_buckets_match_scalar_updates_and_rollback(db):
    from decimal import Decimal
    rows = [{"start": T0 + i * HOUR_MS,
             "payload": {"open": Decimal("100.10"), "close": i + 101},
             "quality": "ok"} for i in range(50)]
    rows.append({**rows[3], "payload": {"open": 110, "close": 111}, "quality": "partial"})
    for row in rows:
        store.upsert_bucket(db, scope_id="scalar", series="price", interval="1h",
                            bucket_start=row["start"], payload=row["payload"],
                            quality=row["quality"], available_at=T0 + HOUR_MS)
    db._commit()
    store.upsert_buckets(db, scope_id="bulk", series="price", interval="1h",
                         buckets=iter(rows), available_at=T0 + HOUR_MS)
    expected = store.load_buckets(db, "scalar", "price", "1h")
    assert store.load_buckets(db, "bulk", "price", "1h") == expected
    with pytest.raises(RuntimeError, match="cancel sample"):
        with db.batch_writes():
            store.upsert_buckets(db, scope_id="bulk", series="price", interval="1h",
                                 buckets=[{**rows[0], "payload": {"close": 999}}],
                                 available_at=T0 + 2 * HOUR_MS)
            raise RuntimeError("cancel sample")
    assert store.load_buckets(db, "bulk", "price", "1h") == expected


def _fact(key: str = "k1", **overrides) -> dict:
    fact = {
        "fact_key": key,
        "rule_id": "LIQ_CASCADE",
        "rule_version": "events-time-2.0",
        "scope": "crypto_market",
        "episode_id": "ep-1",
        "parent_key": None,
        "direction": "short",
        "state": "provisional",
        "verification": "unknown",
        "occurred_at": T0,
        "detected_at": T0 + 1_000,
        "available_at": T0 + 500,
        "valid_until": T0 + 240 * HOUR_MS,
        "time_precision": "exact",
        "payload": {"sum": 10},
        "created_at": T0 + 1_000,
        "updated_at": T0 + 1_000,
    }
    fact.update(overrides)
    return fact


def test_upsert_fact_roundtrip_and_conflict_keeps_lifetime():
    db = _db()
    store.upsert_fact(db, _fact())
    got = store.get_fact(db, "k1")
    assert got["occurred_at"] == T0
    assert got["payload"] == {"sum": 10}
    assert got["direction"] == "short"
    assert store.get_fact(db, "missing") is None

    # конфликт: обновляются только payload/detected_at/available_at/verification/updated_at
    store.upsert_fact(db, _fact(
        state="invalidated", occurred_at=T0 + 999, valid_until=T0 + 999,
        direction="long", payload={"sum": 20}, detected_at=T0 + 2_000,
        available_at=T0 + 2_000, verification="ok", updated_at=T0 + 2_000,
    ))
    got = store.get_fact(db, "k1")
    assert got["state"] == "provisional"
    assert got["occurred_at"] == T0
    assert got["valid_until"] == T0 + 240 * HOUR_MS
    assert got["direction"] == "short"
    assert got["payload"] == {"sum": 20}
    assert got["detected_at"] == T0 + 2_000
    assert got["available_at"] == T0 + 2_000
    assert got["verification"] == "ok"


def test_query_facts_filters_and_exclusive_boundary():
    db = _db()
    store.upsert_fact(db, _fact("a", occurred_at=T0))
    store.upsert_fact(db, _fact("b", state="confirmed", occurred_at=T0 + HOUR_MS))
    store.upsert_fact(db, _fact("c", state="invalidated", occurred_at=T0 + 2 * HOUR_MS))
    store.upsert_fact(db, _fact("d", state="expired", occurred_at=T0 + 3 * HOUR_MS))

    assert [f["fact_key"] for f in store.query_facts(db)] == ["a", "b", "c", "d"]
    assert [f["fact_key"] for f in store.query_facts(db, state="confirmed")] == ["b"]
    active = store.query_facts(db, active_now_ms=T0 + 10 * HOUR_MS)
    assert [f["fact_key"] for f in active] == ["a", "b"]
    # исключительная граница valid_until (§5)
    boundary = T0 + 240 * HOUR_MS
    assert [f["fact_key"] for f in store.query_facts(db, active_now_ms=boundary - 1)] == ["a", "b"]
    assert store.query_facts(db, active_now_ms=boundary) == []


def test_facts_expired_due_boundary():
    db = _db()
    store.upsert_fact(db, _fact("a"))
    store.upsert_fact(db, _fact("b", state="confirmed"))
    store.upsert_fact(db, _fact("c", state="invalidated"))
    boundary = T0 + 240 * HOUR_MS
    assert store.facts_expired_due(db, boundary - 1) == []
    due = store.facts_expired_due(db, boundary)
    assert [f["fact_key"] for f in due] == ["a", "b"]


def test_fact_state_direction_and_transitions():
    db = _db()
    store.upsert_fact(db, _fact("a"))
    store.set_fact_state(db, "a", "confirmed", T0 + 5_000)
    store.set_fact_direction(db, "a", None, T0 + 6_000)
    got = store.get_fact(db, "a")
    assert got["state"] == "confirmed"
    assert got["direction"] is None
    assert got["updated_at"] == T0 + 6_000

    store.add_fact_transition(db, fact_key="a", transition="appeared",
                              from_state=None, to_state="provisional",
                              reason=None, at_ms=T0 + 1_000)
    store.add_fact_transition(db, fact_key="a", transition="confirmed",
                              from_state="provisional", to_state="confirmed",
                              reason="закрытие окна", at_ms=T0 + 5_000,
                              payload={"rule": "LIQ"})
    store.add_fact_transition(db, fact_key="b", transition="appeared",
                              from_state=None, to_state="provisional",
                              reason=None, at_ms=T0 + 3_000)
    transitions = store.fact_transitions_since(db, T0 + 2_000)
    assert [t["transition"] for t in transitions] == ["appeared", "confirmed"]
    assert [t["at_ms"] for t in transitions] == [T0 + 3_000, T0 + 5_000]
    assert transitions[1]["reason"] == "закрытие окна"
    assert transitions[1]["payload"] == {"rule": "LIQ"}


def test_assessments_latest_and_as_of_respect_available_at():
    """§8.2/AC15: «Известно тогда» не видит оценку, доступную позже момента просмотра."""
    db = _db()
    older = {"assessment_id": "a1", "evaluated_at": T0, "available_at": T0 + 100,
             "direction": "long"}
    later_eval_later_available = {
        "assessment_id": "a2", "evaluated_at": T0 + 1_000,
        "available_at": T0 + 60_000, "direction": "short",
    }
    store.insert_assessment(db, older)
    store.insert_assessment(db, later_eval_later_available)

    assert store.latest_assessment(db)["assessment_id"] == "a2"
    # оценка a2 сформирована раньше, но доступна позже: в раннем срезе её нет
    assert store.assessment_as_of(db, T0 + 1_000)["assessment_id"] == "a1"
    assert store.assessment_as_of(db, T0 + 60_000)["assessment_id"] == "a2"
    assert store.assessment_as_of(db, T0 - 1) is None
    assert store.get_assessment(db, "a1")["direction"] == "long"
    assert store.get_assessment(db, "nope") is None


def test_basket_immutable_per_version_and_latest_by_day():
    db = _db()
    store.put_basket(db, version="b-1", day_start_ms=T0,
                     payload={"members": ["BTC", "ETH"]})
    # повтор той же версии игнорируется: состав неизменен
    store.put_basket(db, version="b-1", day_start_ms=T0,
                     payload={"members": ["BTC"]})
    store.put_basket(db, version="b-2", day_start_ms=T0 + 86_400_000,
                     payload={"members": ["BTC", "ETH", "SOL"]})
    latest = store.latest_basket(db)
    assert latest["version"] == "b-2"
    assert latest["day_start_ms"] == T0 + 86_400_000
    assert latest["members"] == ["BTC", "ETH", "SOL"]


def test_source_states_roundtrip_and_overwrite():
    db = _db()
    store.set_source_state(db, source="price", quality="ok", detail=None,
                           at_ms=T0, payload={"age_ms": 5})
    store.set_source_state(db, source="liquidations", quality="stale",
                           detail="нет данных 3 цикла", at_ms=T0)
    states = store.get_source_states(db)
    assert states["price"]["quality"] == "ok"
    assert states["price"]["payload"] == {"age_ms": 5}
    assert states["liquidations"]["detail"] == "нет данных 3 цикла"

    store.set_source_state(db, source="price", quality="auth_error",
                           detail="HTTP 401", at_ms=T0 + 1_000)
    states = store.get_source_states(db)
    assert len(states) == 2
    assert states["price"]["quality"] == "auth_error"
    assert states["price"]["updated_at"] == T0 + 1_000


def test_metric_upsert_and_series():
    db = _db()
    store.upsert_metric(db, series="basket_day_return", bucket_start=T0,
                        payload={"median": "0.5"})
    store.upsert_metric(db, series="basket_day_return", bucket_start=T0 + HOUR_MS,
                        payload={"median": "-0.2"})
    # повтор бакета перезаписывает
    store.upsert_metric(db, series="basket_day_return", bucket_start=T0,
                        payload={"median": "0.6"})
    series = store.metric_series(db, "basket_day_return")
    assert [m["bucket_start"] for m in series] == [T0, T0 + HOUR_MS]
    assert series[0]["median"] == "0.6"
    since = store.metric_series(db, "basket_day_return", since_ms=T0 + 1)
    assert [m["bucket_start"] for m in since] == [T0 + HOUR_MS]
    assert store.metric_series(db, "unknown_series") == []


def test_bundle_lifecycle_and_publication_idempotency():
    """AC17/§9: один открытый пакет; повтор pub_key не создаёт дубль."""
    db = _db()
    assert store.open_bundle(db) is None
    bundle_id = store.create_bundle(db, opened_at=T0, closes_at=T0 + 60_000)
    opened = store.open_bundle(db)
    assert opened["id"] == bundle_id
    assert opened["status"] == "open"
    assert opened["kind"] == "changes"

    first = store.add_publication(
        db, pub_key="p1", bundle_id=bundle_id, kind="fact_transition",
        assessment_id="a1", window_ms=60_000, payload={"text": "x"}, created_at=T0,
    )
    again = store.add_publication(
        db, pub_key="p1", bundle_id=bundle_id, kind="fact_transition",
        assessment_id="a1", window_ms=60_000, payload={"text": "x"}, created_at=T0,
    )
    assert first is True
    assert again is False
    pubs = store.bundle_publications(db, bundle_id)
    assert len(pubs) == 1
    assert pubs[0]["pub_key"] == "p1"
    assert pubs[0]["payload"] == {"text": "x"}

    store.set_bundle_status(db, bundle_id, "published", published_at=T0 + 60_000,
                            payload={"count": 1})
    assert store.open_bundle(db) is None
    bundle = store.get_bundle(db, bundle_id)
    assert bundle["status"] == "published"
    assert bundle["published_at"] == T0 + 60_000
    assert bundle["payload"] == {"count": 1}
    assert [b["id"] for b in store.list_bundles(db)] == [bundle_id]
    assert store.list_bundles(db, status="open") == []
    assert store.get_bundle(db, 999) is None


def test_delivery_upsert_keeps_single_row_per_recipient_channel():
    """§9: статус доставки — не часть ключа; pending → sent обновляет ту же строку."""
    db = _db()
    bundle_id = store.create_bundle(db, opened_at=T0, closes_at=T0 + 60_000)
    store.upsert_delivery(db, bundle_id=bundle_id, recipient="owner",
                          channel="telegram", status="pending", at_ms=T0)
    store.upsert_delivery(db, bundle_id=bundle_id, recipient="owner",
                          channel="telegram", status="sent", at_ms=T0 + 2_000,
                          delivered_at=T0 + 2_000)
    store.upsert_delivery(db, bundle_id=bundle_id, recipient="owner",
                          channel="feed", status="sent", at_ms=T0 + 1_000,
                          delivered_at=T0 + 1_000)
    deliveries = store.list_deliveries(db, bundle_id=bundle_id)
    assert len(deliveries) == 2
    tg = next(d for d in deliveries if d["channel"] == "telegram")
    assert tg["status"] == "sent"
    assert tg["delivered_at"] == T0 + 2_000
    assert tg["created_at"] == T0
    assert len(store.list_deliveries(db)) == 2


def test_delivery_status_has_no_quiet_hold():
    """§2/§9: тихих часов нет — notify выключен даёт shadow, включён — pending."""
    for notify in (False, True):
        for snapshot in ({}, {"quiet_hours": True}):
            for transition in ("appeared", "provisional->invalidated", "active->resolved"):
                status = _delivery_status(notify, snapshot, transition, "SOME_RULE")
                assert status != "quiet_hold"
                assert status == ("pending" if notify else "shadow")
