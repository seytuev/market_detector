"""Публикации и пакеты (§9): группировка 60с, дедуп pub_key, сводка простоя,
записи доставки, различение expired/invalidated. AC07, AC16–AC18."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.db import Database
from app.events import memory, publish, store
from app.events.rules import ENGINE_RULE_VERSION
from app.events.runner import _set_source

T0 = 1_800_000_000_000
DAY = 86_400_000
HOUR = 3_600_000


@pytest.fixture(autouse=True)
def _unwire():
    publish._WIRED = False
    yield
    publish._WIRED = False


def _db() -> Database:
    return Database(":memory:")


def _settings() -> Settings:
    settings = Settings()
    settings.events_group_seconds = 60
    return settings


def _asmt(*, direction="neutral", events="neutral", time="neutral",
          contradictory=False, weekday=3, coverage=None, stats=None,
          aid="a1", evaluated=T0) -> dict:
    return {
        "assessment_id": aid,
        "evaluated_at": evaluated,
        "available_at": evaluated,
        "direction": direction,
        "events_direction": events,
        "time_direction": time,
        "contradictory": contradictory,
        "coverage": coverage,
        "statistics": stats,
        "time_context": {
            "weekday": weekday,
            "day_start_ms": evaluated - (evaluated % DAY),
        },
        "market_line": "линия",
        "quality": "ok",
        "reasons": [],
        "unknown_dependencies": [],
    }


def test_ac17_changes_within_60s_share_one_bundle_without_extension():
    db, settings = _db(), _settings()
    prev = _asmt(direction="neutral", aid="a0")
    curr = _asmt(direction="long", aid="a1")
    created = publish.process_cycle(
        db, settings, prev=prev, curr=curr, transitions=[], now_ms=T0,
    )
    assert [c["kind"] for c in created] == ["direction"]
    bundle = store.open_bundle(db)
    assert bundle["closes_at"] == T0 + 60_000

    # второе изменение в том же окне — тот же пакет, closes_at не продлевается
    curr2 = _asmt(direction="long", contradictory=True, aid="a2", evaluated=T0 + 5_000)
    created2 = publish.process_cycle(
        db, settings, prev=curr, curr=curr2, transitions=[], now_ms=T0 + 5_000,
    )
    assert [c["kind"] for c in created2] == ["conflict"]
    bundle2 = store.open_bundle(db)
    assert bundle2["id"] == bundle["id"]
    assert bundle2["closes_at"] == T0 + 60_000
    kinds = {p["kind"] for p in store.bundle_publications(db, bundle["id"])}
    assert kinds == {"direction", "conflict"}

    published = publish.flush_due(db, settings, T0 + 60_000)
    assert [b["id"] for b in published] == [bundle["id"]]
    assert published[0]["status"] == "published"
    assert published[0]["published_at"] == T0 + 60_000

    # изменение после закрытия окна — новый пакет
    publish.process_cycle(
        db, settings, prev=curr2,
        curr=_asmt(direction="short", aid="a3", evaluated=T0 + 61_000),
        transitions=[], now_ms=T0 + 61_000,
    )
    bundle3 = store.open_bundle(db)
    assert bundle3 is not None and bundle3["id"] != bundle["id"]


def test_same_change_twice_creates_single_publication():
    db, settings = _db(), _settings()
    prev, curr = _asmt(direction="neutral", aid="a0"), _asmt(direction="long", aid="a1")
    fact, _ = memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=T0, detected_at=T0, available_at=T0,
        payload={"summary": "давление"},
    )
    transitions = store.fact_transitions_since(db, 0)
    first = publish.process_cycle(
        db, settings, prev=prev, curr=curr, transitions=transitions, now_ms=T0,
    )
    again = publish.process_cycle(
        db, settings, prev=prev, curr=curr, transitions=transitions, now_ms=T0 + 1_000,
    )
    assert again == []
    bundle = store.open_bundle(db)
    pubs = store.bundle_publications(db, bundle["id"])
    assert len(pubs) == len(first) == 2  # direction + fact_appeared


def test_plain_recompute_without_changes_publishes_nothing():
    db, settings = _db(), _settings()
    same = _asmt(direction="neutral", aid="a1")
    created = publish.process_cycle(
        db, settings, prev=same, curr=dict(same), transitions=[], now_ms=T0,
    )
    assert created == []
    assert store.open_bundle(db) is None


def test_ac16_flush_deliveries_feed_sent_telegram_unavailable():
    db, settings = _db(), _settings()
    publish.process_cycle(
        db, settings, prev=_asmt(direction="neutral", aid="a0"),
        curr=_asmt(direction="long", aid="a1"), transitions=[], now_ms=T0,
    )
    published = publish.flush_due(db, settings, T0 + 60_000)
    assert len(published) == 1
    deliveries = store.list_deliveries(db, bundle_id=published[0]["id"])
    feed = next(d for d in deliveries if d["channel"] == "feed")
    telegram = next(d for d in deliveries if d["channel"] == "telegram")
    assert feed["status"] == "sent"
    assert feed["delivered_at"] == T0 + 60_000
    assert telegram["status"] == "unavailable"
    assert telegram["detail"] == "Доставка недоступна"
    assert telegram["delivered_at"] is None


def test_ac16_telegram_pending_only_when_wired_and_enabled():
    db, settings = _db(), _settings()
    publish.wire(sender=object(), settings=settings)
    publish.process_cycle(
        db, settings, prev=_asmt(direction="neutral", aid="a0"),
        curr=_asmt(direction="long", aid="a1"), transitions=[], now_ms=T0,
    )
    published = publish.flush_due(db, settings, T0 + 60_000)
    telegram = next(
        d for d in store.list_deliveries(db, bundle_id=published[0]["id"])
        if d["channel"] == "telegram"
    )
    assert telegram["status"] == "pending"
    assert telegram["delivered_at"] is None  # pending — не доставка (AC22)


def test_ac07_expired_and_invalidated_are_distinct():
    db, settings = _db(), _settings()
    f1, _ = memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=T0, detected_at=T0, available_at=T0,
    )
    f2, _ = memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-2", direction="short",
        occurred_at=T0, detected_at=T0, available_at=T0,
    )
    memory.invalidate_fact(db, f2["fact_key"], T0 + 100, "условие сломано")
    expired = memory.expire_due(db, T0 + 240 * HOUR)
    assert [f["fact_key"] for f in expired] == [f1["fact_key"]]

    transitions = store.fact_transitions_since(db, 0)
    curr = _asmt(aid="a1", evaluated=T0 + 240 * HOUR)
    created = publish.process_cycle(
        db, settings, prev=None, curr=curr, transitions=transitions,
        now_ms=T0 + 240 * HOUR,
    )
    by_kind: dict[str, list] = {}
    for c in created:
        by_kind.setdefault(c["kind"], []).append(c["payload"]["text"])
    assert by_kind["fact_expired"] == ["Событие больше не учитывается"]
    assert by_kind["fact_invalidated"] == ["Условие опровергнуто"]
    assert len(by_kind["fact_appeared"]) == 2


def test_source_degrade_and_recover_publish_once():
    db, settings = _db(), _settings()
    # запись состояний — через контракт runner'а, сохраняющий notified_quality
    _set_source(db, "liquidations", "ok", None, T0)
    curr = _asmt(aid="a1")
    assert publish.process_cycle(
        db, settings, prev=None, curr=curr, transitions=[], now_ms=T0,
    ) == []  # baseline без публикации

    _set_source(db, "liquidations", "unavailable", "таймаут", T0 + 1_000)
    created = publish.process_cycle(
        db, settings, prev=None, curr=curr, transitions=[], now_ms=T0 + 1_000,
    )
    assert [c["kind"] for c in created] == ["source_degraded"]
    # повторный опрос с тем же сбоем — без новой публикации
    _set_source(db, "liquidations", "unavailable", "таймаут", T0 + 2_000)
    assert publish.process_cycle(
        db, settings, prev=None, curr=curr, transitions=[], now_ms=T0 + 2_000,
    ) == []
    # восстановление — один раз
    _set_source(db, "liquidations", "ok", None, T0 + 3_000)
    created = publish.process_cycle(
        db, settings, prev=None, curr=curr, transitions=[], now_ms=T0 + 3_000,
    )
    assert [c["kind"] for c in created] == ["source_recovered"]
    # другие поля source_state сохранены
    state = store.get_source_states(db)["liquidations"]
    assert state["payload"]["notified_quality"] == "ok"


def test_stats_change_over_threshold_publishes():
    db, settings = _db(), _settings()
    settings.events_stats_change_pp = 5
    base = {"available": True, "value": 0.50, "n": 120, "text": "… 50% …"}
    prev = _asmt(aid="a0", stats=base)
    small = _asmt(aid="a1", stats={**base, "value": 0.53})
    assert publish.process_cycle(
        db, settings, prev=prev, curr=small, transitions=[], now_ms=T0,
    ) == []  # 3 п.п. — колебание без пересечения порога (§9)
    big = _asmt(aid="a2", stats={**base, "value": 0.56, "text": "… 56% …"})
    created = publish.process_cycle(
        db, settings, prev=prev, curr=big, transitions=[], now_ms=T0,
    )
    assert [c["kind"] for c in created] == ["stats"]


def test_coverage_threshold_crossing_publishes_both_ways():
    db, settings = _db(), _settings()
    cov_ok = {"ok": 16, "total": 20, "ratio": 0.8, "sufficient": True}
    cov_low = {"ok": 10, "total": 20, "ratio": 0.5, "sufficient": False}
    prev = _asmt(aid="a0", coverage=cov_ok)
    curr = _asmt(aid="a1", coverage=cov_low)
    created = publish.process_cycle(
        db, settings, prev=prev, curr=curr, transitions=[], now_ms=T0,
    )
    assert [c["kind"] for c in created] == ["coverage"]
    back = publish.process_cycle(
        db, settings, prev=curr,
        curr=_asmt(aid="a2", coverage=cov_ok, evaluated=T0 + DAY),
        transitions=[], now_ms=T0 + 1_000,
    )
    assert [c["kind"] for c in back] == ["coverage"]


def test_time_boundary_publishes_even_without_direction_change():
    db, settings = _db(), _settings()
    prev = _asmt(direction="neutral", weekday=3, aid="a0")
    curr = _asmt(direction="neutral", weekday=4, aid="a1", evaluated=T0 + DAY)
    created = publish.process_cycle(
        db, settings, prev=prev, curr=curr, transitions=[], now_ms=T0 + DAY,
    )
    assert [c["kind"] for c in created] == ["time_boundary"]
    assert created[0]["payload"]["text"] == "Временной фактор обновился"


def test_ac18_startup_digest_once_after_gap():
    db, settings = _db(), _settings()
    settings.events_digest_gap_seconds = 600
    store.insert_assessment(db, {
        "assessment_id": "old", "evaluated_at": T0, "available_at": T0,
        "direction": "short", "market_line": "↓ SHORT",
        "unknown_dependencies": ["liquidations"],
    })
    fact, _ = memory.begin_fact(
        db, rule_id="LIQ_PRESSURE", rule_version=ENGINE_RULE_VERSION,
        scope="crypto_market", episode_id="ep-1", direction="short",
        occurred_at=T0, detected_at=T0, available_at=T0,
    )
    memory.expire_due(db, T0 + 240 * HOUR)

    now = T0 + 240 * HOUR + 700_000
    publish.startup_digest(db, settings, now)
    bundles = store.list_bundles(db)
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle["kind"] == "downtime_digest"
    assert bundle["status"] == "published"
    assert bundle["payload"]["summary"] == "После простоя: сводка"
    assert bundle["payload"]["counts"]["expired"] == 1
    assert bundle["payload"]["expired_facts"] == [fact["fact_key"]]
    assert bundle["payload"]["unknown_dependencies"] == ["liquidations"]
    # старые события сведены в счётчики, а не переизданы как свежие
    pubs = store.bundle_publications(db, bundle["id"])
    assert [p["kind"] for p in pubs] == ["downtime_digest"]

    publish.startup_digest(db, settings, now + 10_000)
    assert len(store.list_bundles(db)) == 1


def test_startup_digest_no_assessment_or_small_gap_is_noop():
    db, settings = _db(), _settings()
    publish.startup_digest(db, settings, T0)
    assert store.list_bundles(db) == []
    store.insert_assessment(db, {
        "assessment_id": "a", "evaluated_at": T0, "available_at": T0,
        "direction": "neutral",
    })
    publish.startup_digest(db, settings, T0 + 300_000)  # 5 мин < порога 600с
    assert store.list_bundles(db) == []


def test_render_bundle_text_includes_changes_and_link():
    db, settings = _db(), _settings()
    assessment = _asmt(direction="long", aid="a1")
    assessment["market_line"] = "↑ LONG"
    assessment["coverage"] = {"ok": 18, "total": 20, "ratio": 0.9, "sufficient": True}
    store.insert_assessment(db, assessment)
    publish.process_cycle(
        db, settings, prev=_asmt(direction="neutral", aid="a0"),
        curr=assessment, transitions=[], now_ms=T0,
    )
    bundle = store.open_bundle(db)
    publish.flush_due(db, settings, T0 + 60_000)
    text = publish.render_bundle_text(db, bundle["id"], base_url="http://x")
    assert "↑ Рынок: перевес в сторону роста" in text
    assert "Направление изменилось" in text
    assert "Данные оценки: данные доступны · покрытие 18/20" in text
    assert "http://x/events.html#assessment=a1" in text
