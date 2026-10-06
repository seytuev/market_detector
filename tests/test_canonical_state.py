"""T21/T22 (ТЗ 06.10.2026 §4, §13, §14).

Единый canonical state: актуальны только подтверждённые (FVG / явно
только вручную / ручные) И рыночно валидные И незавершённые зоны;
invalidated/candidate не попадают в актуальные списки API/бота/LTF.
Повторный replay не дублирует визиты.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.scanner import Scanner
from app.models import (
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
    now_ms,
)
from app.web.api import create_app
from tests.conftest import make_candle
from tests.test_ltf_engine import SERIES_H_CLOSES, SERIES_H_HL, T0, _feed, _setup
from tests.test_ltf_breaks import _series


def _zone(**kw) -> Zone:
    base = dict(
        id=None, instrument_id=1, type=ZoneType.OB, direction=Direction.BULL,
        timeframe="D1", lower=100.0, upper=110.0, formed_at=1000,
        confirmed_at=2000, status=ZoneStatus.ACTIVE, source="auto",
        source_candles=[1000], evidence={}, created_at=1000,
    )
    base.update(kw)
    return Zone(**base)


@pytest.mark.parametrize("kw,relevant", [
    ({}, True),                                            # active + confirmed
    ({"confirmed_at": None}, False),                       # не подтверждён
    ({"confirmed_at": None,
      "evidence": {"manual_confirmation_only": True}}, True),   # T09: только вручную
    ({"confirmed_at": None, "source": "manual",
      "type": ZoneType.MANUAL}, True),                     # ручная зона владельца
    ({"market_validity": "invalid"}, False),               # пробит (close_beyond)
    ({"display_until": 3000}, False),                      # завершён
    # ТЗ 07.10.2026 §3: candidate — признак очереди ревью, не рынка;
    # подтверждённый кандидат актуален и рисуется сразу
    ({"status": ZoneStatus.CANDIDATE}, True),
    ({"status": ZoneStatus.CANDIDATE, "confirmed_at": None}, False),
    ({"status": ZoneStatus.REJECTED}, False),
    ({"status": ZoneStatus.WEAKENED}, True),               # ослабленная FVG жива
])
def test_is_currently_relevant(kw, relevant):
    assert _zone(**kw).is_currently_relevant() is relevant


# ----- API -----

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


def test_grouped_excludes_invalid_and_candidates(db, client, instrument_id):
    ok = db.insert_zone(_zone(instrument_id=instrument_id))
    db.insert_zone(_zone(instrument_id=instrument_id, lower=200, upper=210,
                         market_validity="invalid"))       # пробит, status ACTIVE
    db.insert_zone(_zone(instrument_id=instrument_id, lower=300, upper=310,
                         status=ZoneStatus.CANDIDATE, confirmed_at=None))
    resp = client.get(f"/api/zones/grouped?instrument_id={instrument_id}",
                      headers=AUTH)
    assert resp.status_code == 200
    ids = [zid for g in resp.json()["groups"] for zid in g["zone_ids"]]
    assert ids == [ok]


def test_candidates_queue_shows_unconfirmed_reason(db, client, instrument_id):
    zid = db.insert_zone(_zone(instrument_id=instrument_id, confirmed_at=None,
                               status=ZoneStatus.CANDIDATE))
    resp = client.get("/api/candidates", headers=AUTH)
    assert resp.status_code == 200
    row = next(r for r in resp.json() if r["id"] == zid)
    assert row["unconfirmed_reason"] == "no_external_fvg"
    assert row["confirmation_state"] == "unconfirmed"


# ----- LTF: невалидный родитель не порождает сценарии (T21) -----

def test_parent_market_invalid_closes_observation(db, cfg, instrument_id):
    """market_validity=invalid (close_beyond без смены status) закрывает
    наблюдение наряду с CONVERTED/ARCHIVED."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 16)
    db.update_zone(zid, market_validity="invalid",
                   end_reason="close_beyond (ТЗ §3)", display_until=T0)
    assert engine.check_parent_validity(db.get_zone(zid)) is True
    assert db.get_ltf_observation(obs.id).state == "closed_by_parent"


# ----- идемпотентность replay: визиты (T22) -----

def test_replay_does_not_duplicate_visits(db, cfg, instrument_id):
    W1 = "W1"
    t0 = 1_730_000_000_000
    w = 604_800_000
    zid = db.insert_zone(_zone(instrument_id=instrument_id, timeframe=W1,
                               formed_at=t0, source_candles=[t0],
                               confirmed_at=t0 + 2 * w))
    candles = [make_candle(t0 + i * w, 112, 115, 111, 113, W1) for i in range(3)]
    # заход в зону и выход обратно
    candles.append(make_candle(t0 + 3 * w, 112, 113, 104, 105, W1))
    candles.append(make_candle(t0 + 4 * w, 111, 113, 110.5, 112, W1))
    db.insert_candles(candles)
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={W1})
    n1 = len(db.get_visits(zid))
    assert n1 >= 1
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={W1})
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={W1})
    assert len(db.get_visits(zid)) == n1
