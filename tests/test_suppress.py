"""Подавление 120 ч (§8) — приёмка §13 №5, №6, №12."""
from __future__ import annotations

import pytest

from app.config import DetectorConfig
from app.db import Database
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.notify.suppress import mark_delivered, should_notify

HOUR = 3_600_000


def _make_db(path: str = ":memory:") -> tuple[Database, int]:
    db = Database(path)
    ins_id = db.upsert_instrument(
        Instrument(None, "BTC", "binance", "spot", "BTCUSDT", "USDT")
    )
    zone_id = db.insert_zone(
        Zone(
            None, ins_id, ZoneType.OB, Direction.BULL, "W1",
            lower=65000.0, upper=66000.0, formed_at=1, confirmed_at=2,
            status=ZoneStatus.ACTIVE, created_at=now_ms(),
        )
    )
    return db, zone_id


def _event(zone_id: int, kind: EventKind, occurred_at: int | None = None) -> Event:
    t = occurred_at if occurred_at is not None else now_ms()
    return Event(None, zone_id, 1, kind, occurred_at=t, detected_at=t, price=65500.0)


@pytest.fixture
def cfg() -> DetectorConfig:
    return DetectorConfig()


def test_first_signal_passes(cfg):
    db, zone_id = _make_db()
    assert should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)


def test_repeat_same_depth_silent_before_120h(cfg):
    """§13 №6: повтор на той же глубине до 120 часов молчит."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 1 * HOUR)
    assert not should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)


def test_after_120h_new_visit_may_notify(cfg):
    """§13 №6: после 120 часов новый заход может уведомить."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 121 * HOUR)
    assert should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)


def test_suppression_counts_from_delivery_not_event(cfg):
    """§8: отсчёт 120 ч — от успешной доставки, не от момента события."""
    db, zone_id = _make_db()
    old_event = _event(zone_id, EventKind.TOUCH, occurred_at=now_ms() - 200 * HOUR)
    mark_delivered(db, old_event, now_ms() - 1 * HOUR)  # доставлен недавно
    assert not should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)


def test_depth50_not_blocked_by_recent_touch(cfg):
    """§8: первое достижение 50% не блокируется недавним уведомлением о касании."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 1 * HOUR)
    assert should_notify(db, _event(zone_id, EventKind.DEPTH_50), cfg)


def test_depth90_not_suppressed(cfg):
    """DEPTH_90 — новый порог вне SUPPRESSED_KINDS, подавлению не подлежит."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.DEPTH_90), now_ms() - 1 * HOUR)
    assert should_notify(db, _event(zone_id, EventKind.DEPTH_90), cfg)


def test_breaker_created_not_suppressed(cfg):
    """Структурные события (BREAKER_CREATED) не подавляются."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.BREAKER_CREATED), now_ms() - 1 * HOUR)
    assert should_notify(db, _event(zone_id, EventKind.BREAKER_CREATED), cfg)


def test_mute_silences_zone_until_deadline(cfg):
    """§9: «Отложить»/«Отключить» глушит зону до срока, включая все виды."""
    db, zone_id = _make_db()
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 200 * HOUR)
    db.set_mute(zone_id, 1, now_ms() + 1 * HOUR)
    assert not should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)
    # mute — на всю зону, а не только на подавляемые виды
    assert not should_notify(db, _event(zone_id, EventKind.DEPTH_90), cfg)
    # срок вышел — уведомления снова идут (само истечение событие не создаёт)
    db.set_mute(zone_id, 1, now_ms() - 1 * HOUR)
    assert should_notify(db, _event(zone_id, EventKind.TOUCH), cfg)


def test_restart_preserves_suppression(tmp_path, cfg):
    """§13 №12: перезапуск процесса не сбрасывает сроки подавления."""
    path = str(tmp_path / "htf.db")
    db, zone_id = _make_db(path)
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 1 * HOUR)
    db.close()

    db2 = Database(path)  # «перезапуск»: новый объект по тому же файлу
    assert not should_notify(db2, _event(zone_id, EventKind.TOUCH), cfg)
    # а 50% после перезапуска по-прежнему не блокируется касанием
    assert should_notify(db2, _event(zone_id, EventKind.DEPTH_50), cfg)
    db2.close()


def test_restart_preserves_mute(tmp_path, cfg):
    """§13 №5/№12: ручная пауза переживает перезапуск."""
    path = str(tmp_path / "htf.db")
    db, zone_id = _make_db(path)
    mark_delivered(db, _event(zone_id, EventKind.TOUCH), now_ms() - 1 * HOUR)
    db.set_mute(zone_id, 1, now_ms() + 10 * HOUR)
    db.close()

    db2 = Database(path)
    assert not should_notify(db2, _event(zone_id, EventKind.TOUCH), cfg)
    db2.close()
