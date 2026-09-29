"""ТЗ «LTF Current Setup» §16.2 (предлагаемый режим, выключен по умолчанию):
предварительный диапазон — временный конец текущего движения до подтверждения
опоры тремя правыми свечами; чистый read-only слой (приёмка п.21)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import (
    LtfEngine,
    PivotCandidate,
    current_range,
    provisional_range,
)
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfObservation, LtfPivot, LtfScenario
from app.web.api import create_app
from tests.conftest import H1_MS, make_h1_candles

T0 = 1_780_000_000_000
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def mkp(
    kind: str, price: float, role: str, idx: int,
    confirmed_idx: int | None = None, pid: int | None = None,
) -> PivotCandidate:
    t = T0 + idx * H1_MS
    conf = T0 + (confirmed_idx if confirmed_idx is not None else idx + 3) * H1_MS
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=t, candle_open_time=t,
        confirmed_at=conf, left=3, right=3, role=role, pivot_id=pid,
    )


def _candles(low_idx: int, low_price: float, n: int = 14,
             instrument_id: int = 1) -> list:
    """Свечи T0..T0+n: ровные бары, на свече low_idx — экстремум low_price."""
    bars = []
    for i in range(n):
        low = low_price if i == low_idx else 10.0
        bars.append((10.5, 11.0, low, 10.5))
    return make_h1_candles(bars, T0, instrument_id)


# ------------------------- чистая функция (юнит) -------------------------


def test_provisional_bear_current_movement_end():
    """Медвежий сценарий: начало — последний подтверждённый LH текущего
    движения (не произвольный максимум окна), конец — неподтверждённый
    минимум движения; range_status=provisional."""
    pivots = [
        mkp("high", 15, "LH", 1, pid=11),
        mkp("low", 9, "LL", 2, pid=12),
        mkp("high", 14, "LH", 6, confirmed_idx=9, pid=13),
    ]
    candles = _candles(low_idx=10, low_price=8.0)
    now = T0 + 11 * H1_MS + 1  # после экстремума — 1 правая свеча (< 3)
    draft = provisional_range(pivots, candles, Direction.BEAR, now)
    assert draft is not None
    assert (draft.lower, draft.upper, draft.mid) == (8.0, 14.0, 11.0)
    # подтверждённая сторона — якорь LH; неподтверждённый конец — без якоря
    assert draft.anchor_high_ref == 13
    assert draft.anchor_low_ref is None
    ev = draft.evidence
    assert ev["range_status"] == "provisional"
    assert ev["ref_pivot_at"] == T0 + 6 * H1_MS
    assert ev["extreme_candle_open_time"] == T0 + 10 * H1_MS
    # подтверждённый расчёт не меняется — основной остаётся прежний
    rng = current_range(pivots, Direction.BEAR, now)
    assert (rng.lower, rng.upper) == (9.0, 15.0)


def test_provisional_bull_current_movement_end():
    """Бычий сценарий — зеркально: начало HL, конец — неподтверждённый
    максимум движения."""
    pivots = [
        mkp("low", 10, "HL", 1, pid=11),
        mkp("high", 15, "HH", 2, pid=12),
        mkp("low", 11, "HL", 6, confirmed_idx=9, pid=13),
    ]
    bars = []
    for i in range(14):
        high = 16.0 if i == 10 else 12.0
        bars.append((10.5, high, 10.0, 10.5))
    candles = make_h1_candles(bars, T0, 1)
    now = T0 + 11 * H1_MS + 1
    draft = provisional_range(pivots, candles, Direction.BULL, now)
    assert draft is not None
    assert (draft.lower, draft.upper) == (11.0, 16.0)
    assert draft.anchor_low_ref == 13
    assert draft.anchor_high_ref is None


def test_provisional_none_without_structural_ref():
    """Нет подтверждённого LH/HL движения — предварительного диапазона нет
    (произвольный экстремум окна не подставляется, §16.2)."""
    candles = _candles(low_idx=10, low_price=8.0)
    now = T0 + 11 * H1_MS + 1
    assert provisional_range([], candles, Direction.BEAR, now) is None
    # HH — не опора медвежьего движения
    pivots = [mkp("high", 15, "HH", 1)]
    assert provisional_range(pivots, candles, Direction.BEAR, now) is None


def test_provisional_none_when_extreme_confirmed():
    """Экстремум, уже подтверждённый тремя правыми свечами, покрывает
    обычный расчёт — предварительный слой не дублирует его."""
    pivots = [
        mkp("high", 14, "LH", 6, confirmed_idx=9, pid=13),
        mkp("low", 8, "LL", 10, confirmed_idx=13, pid=14),
    ]
    candles = _candles(low_idx=10, low_price=8.0)
    now = T0 + 13 * H1_MS + 1
    assert provisional_range(pivots, candles, Direction.BEAR, now) is None
    # после подтверждения опор — обычный пересчёт подхватывает пару (§16.2)
    rng = current_range(pivots, Direction.BEAR, now)
    assert (rng.lower, rng.upper) == (8.0, 14.0)


def test_provisional_none_on_invalid_geometry():
    """R_high <= R_low — диапазон не строится (как в подтверждённом)."""
    pivots = [mkp("high", 7.0, "LH", 6, confirmed_idx=9, pid=13)]
    candles = _candles(low_idx=10, low_price=8.0)  # минимум выше LH
    now = T0 + 11 * H1_MS + 1
    assert provisional_range(pivots, candles, Direction.BEAR, now) is None


def test_provisional_disabled_by_default_in_config():
    from app.config import DetectorConfig
    assert DetectorConfig().ltf_provisional_range_enabled is False


# ------------------------- read-only (приёмка п.21) -------------------------


@pytest.fixture()
def api_settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, api_settings) -> TestClient:
    return TestClient(
        create_app(db, api_settings,
                   ltf_engine=LtfEngine(db, api_settings.detector))
    )


def _seed_scenario(db: Database, instrument_id: int):
    """Активное наблюдение (OB D1 bear) со сценарием без подтверждённого
    диапазона; pivots LH(14)/LL(9) и свечи с неподтверждённым минимумом 8."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000,
        status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="range_pending",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=14.0, kind="high",
        pivot_at=T0 + 6 * H1_MS, candle_open_time=T0 + 6 * H1_MS,
        confirmed_at=T0 + 9 * H1_MS, role="LH", state="confirmed",
    ))
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=9.0, kind="low",
        pivot_at=T0 + 2 * H1_MS, candle_open_time=T0 + 2 * H1_MS,
        confirmed_at=T0 + 5 * H1_MS, role="LL", state="confirmed",
    ))
    db.insert_candles(_candles(low_idx=10, low_price=8.0,
                               instrument_id=instrument_id))
    return sc


def test_current_provisional_hidden_by_default(db, client, api_settings,
                                               instrument_id):
    """п.21: по умолчанию выключен — поля есть, но provisional_range=None,
    флаг False; снимок не меняется."""
    _seed_scenario(db, instrument_id)
    r = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                   headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["provisional_range_enabled"] is False
    assert body["provisional_range"] is None


def test_current_provisional_read_only_when_enabled(db, client, api_settings,
                                                    instrument_id):
    """п.21: при включении слой возвращается отдельным полем с подписью
    provisional, визуально отличим (пунктир — на фронте), и НЕ создаёт
    подтверждённые версии диапазона, события и зоны (счётчик не меняется)."""
    sc = _seed_scenario(db, instrument_id)
    api_settings.detector.ltf_provisional_range_enabled = True
    r = client.get(f"/api/ltf/instruments/{instrument_id}/current",
                   headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["provisional_range_enabled"] is True
    prov = body["provisional_range"]
    assert prov is not None
    assert prov["range_status"] == "provisional"
    assert prov["proposed_mode"] is True  # §16.2: режим не подтверждён владельцем
    assert (prov["lower"], prov["upper"]) == (8.0, 14.0)
    assert prov["extreme_candle_open_time"] == T0 + 10 * H1_MS
    # read-only: ни одной записи в ltf_range, событий и новых зон;
    # основной счётчик и подтверждённый range не тронуты
    assert db.list_ltf_ranges(sc.id) == []
    assert db.list_ltf_events(scenario_id=sc.id, limit=100) == []
    assert body["range"] is None
    assert body["counts"] == {"eligible": 0, "excluded": 0, "historical": 0}
    assert body["eligible_entries"] == []
