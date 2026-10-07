"""Тесты агрегирующего журнала GET /api/journal (план ребрендинга §6.D)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import (
    Delivery,
    Direction,
    Event,
    EventKind,
    Instrument,
    Review,
    Zone,
    ZoneStatus,
    ZoneType,
)
from app.models_ltf import LtfEntryZone, LtfEvent, LtfObservation, LtfReview
from app.web.api import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = 1_780_000_000_000


@pytest.fixture()
def db() -> Database:
    return Database(":memory:")


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


@pytest.fixture()
def seeded(db, client):
    """Инструмент, HTF-зона+событие, review, LTF-цепочка и доставки."""
    ins_id = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=ins_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=100.0, upper=110.0,
        formed_at=BASE, confirmed_at=BASE, status=ZoneStatus.ACTIVE,
        source="auto", created_at=BASE, evidence={"reason": "test"},
    ))
    db.insert_event(Event(
        id=None, zone_id=zone_id, cycle_id=1, kind=EventKind.TOUCH,
        occurred_at=BASE + 3_000, detected_at=BASE + 3_000, price=105.0,
    ))
    db.add_review(Review(
        id=None, zone_id=zone_id, decision="confirmed", text="ок",
        created_at=BASE + 4_000,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=ins_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=Direction.BULL, activated_at=BASE,
    ))
    db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, kind="bos",
        occurred_at=BASE + 5_000, detected_at=BASE + 5_000,
        dedupe_key="t:bos:1",
    ))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=ins_id, type="FVG", direction=Direction.BULL,
        lower=101.0, upper=102.0, formed_at=BASE,
    ))
    db.add_ltf_review(LtfReview(
        id=None, entry_zone_id=ez.id, scenario_id=None, decision="correct",
        text="верно", created_at=BASE + 6_000,
    ))
    db.record_delivery(Delivery(
        id=None, event_ids=[1, 2], destination="telegram", status="sent",
        idempotency_key="k1", delivered_at=BASE + 2_000,
    ))
    db.record_delivery(Delivery(
        id=None, event_ids=[3], destination="telegram", status="failed",
        idempotency_key="k2", delivered_at=BASE + 1_000, error="timeout",
    ))
    return client


def test_requires_auth(seeded):
    r = seeded.get("/api/journal")
    assert r.status_code == 401


def test_invalid_kind(seeded):
    r = seeded.get("/api/journal", params={"kind": "bogus"}, headers=AUTH)
    assert r.status_code == 400


def test_all_mixed_sorted_desc(seeded):
    r = seeded.get("/api/journal", headers=AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 6
    assert {it["category"] for it in items} == {"market", "decisions", "delivery"}
    ats = [it["at"] for it in items]
    assert ats == sorted(ats, reverse=True)
    for it in items:
        assert it["title"] and it["at"] is not None and it["category"]
        assert it["source"] in ("htf", "ltf")
        assert isinstance(it["ref"], dict)


def test_kind_filters(seeded):
    market = seeded.get("/api/journal", params={"kind": "market"}, headers=AUTH).json()
    assert {it["category"] for it in market} == {"market"}
    assert {it["source"] for it in market} == {"htf", "ltf"}
    htf = next(it for it in market if it["source"] == "htf")
    assert htf["title"] == "BTCUSDT · первое касание"
    assert htf["kind"] == "touch"
    assert htf["ref"]["zone_id"]
    ltf = next(it for it in market if it["source"] == "ltf")
    assert ltf["title"] == "BTCUSDT · слом структуры BOS"
    assert ltf["ref"]["observation_id"]

    decisions = seeded.get(
        "/api/journal", params={"kind": "decisions"}, headers=AUTH).json()
    assert {it["category"] for it in decisions} == {"decisions"}
    htf_r = next(it for it in decisions if it["source"] == "htf")
    assert htf_r["title"] == "BTCUSDT · Решение: размечено верно"
    assert htf_r["ref"]["review_id"]
    ltf_r = next(it for it in decisions if it["source"] == "ltf")
    assert ltf_r["ref"]["entry_zone_id"]

    delivery = seeded.get(
        "/api/journal", params={"kind": "delivery"}, headers=AUTH).json()
    assert {it["category"] for it in delivery} == {"delivery"}
    assert len(delivery) == 2


def test_delivery_error_visible(seeded):
    delivery = seeded.get(
        "/api/journal", params={"kind": "delivery"}, headers=AUTH).json()
    failed = next(it for it in delivery if it["status"] == "failed")
    assert failed["title"] == "Ошибка доставки"
    assert "timeout" in failed["text"]
    assert "telegram" in failed["text"]
    sent = next(it for it in delivery if it["status"] == "sent")
    assert sent["title"] == "Уведомление доставлено"
    assert "событий: 2" in sent["text"]


def test_limit(seeded):
    items = seeded.get(
        "/api/journal", params={"limit": 3}, headers=AUTH).json()
    assert len(items) == 3
    ats = [it["at"] for it in items]
    assert ats == sorted(ats, reverse=True)
    r = seeded.get("/api/journal", params={"limit": 0}, headers=AUTH)
    assert r.status_code == 422
    r = seeded.get("/api/journal", params={"limit": 501}, headers=AUTH)
    assert r.status_code == 422
