"""§13 «один снимок для всего LTF-интерфейса»: левая карточка актива
(/api/ltf/instruments), правая панель и таблица
(/api/ltf/instruments/{id}/current) считают подходящие зоны одним циклом
допуска (evaluate_final), по одному сценарию выбранного контекста и отдают
один state_version. Регрессия: «Зоны: 0» / «Нет подходящих зон» /
«Подходящие зоны 1» одновременно у одного инструмента."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms
from app.models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfPivot,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
    LtfStructureEvent,
)
from app.web.api import create_app
from tests.conftest import make_candle

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T0 = 1_780_000_000_000


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
    return TestClient(create_app(db, settings, ltf_engine=LtfEngine(db, settings.detector)))


def _make_live(db, instrument_id, price: float) -> None:
    now = now_ms()
    c = make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )
    db.insert_candles([c])
    db.set_quote(instrument_id, price, now)
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))


def _scenario_with_range(db, instrument_id, zone_id, direction,
                         activated_at, created_at):
    """Активное наблюдение + сценарий monitoring_entries с диапазоном v1
    90–110 (bear) / 80–100 (bull не используется)."""
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id,
        zone_version=1, cycle_id=1, direction=direction, state="active",
        activated_at=activated_at,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=created_at, updated_at=created_at,
    ))
    se = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=direction, break_level=110.0,
        break_candle_open_time=created_at + 50,
        occurred_at=created_at + 51, detected_at=created_at + 51,
        level_key=f"bos:primary:hl:{sc.id}:110.0",
    ))
    db.update_ltf_scenario(sc.id, trigger_event_id=se.id)
    p_low = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=90.0, kind="low",
        pivot_at=activated_at - 500, candle_open_time=activated_at - 500,
        confirmed_at=activated_at - 100, role="LL", state="confirmed",
    ))
    p_high = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=110.0, kind="high",
        pivot_at=activated_at - 400, candle_open_time=activated_at - 400,
        confirmed_at=activated_at - 50, role="LH", state="confirmed",
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, anchor_low_pivot_id=p_low, anchor_high_pivot_id=p_high,
        available_at=created_at + 60,
    ))
    return obs, sc


def _entry(db, instrument_id, scenario_id, type_, lower, upper, state,
           reason, ver=1, eligible=None):
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type=type_,
        direction=Direction.BEAR, lower=lower, upper=upper,
        formed_at=T0 + 10, confirmed_at=T0 + 20,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=scenario_id, entry_zone_id=ez.id,
        range_version=ver,
        eligible=state in ("fresh", "tested") if eligible is None else eligible,
        overlap="partial", state=state, reason=reason,
        added_at=T0 + 60, updated_at=T0 + 60,
    ))
    return ez


@pytest.fixture()
def seeded(db, instrument_id):
    """Сценарий с диапазоном v1 и зонами: FVG fresh/ok и OB tested/ok
    (повторно допущенная tested — источник прежнего расхождения 0/1),
    BSL outside_pd, строка v0 — история."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000, status=ZoneStatus.ACTIVE,
    ))
    obs1, sc1 = _scenario_with_range(
        db, instrument_id, z1, Direction.BEAR, T0, T0 + 100,
    )
    ez_fvg = _entry(db, instrument_id, sc1.id, "FVG", 98.0, 102.0,
                    "fresh", "ok")
    ez_ob = _entry(db, instrument_id, sc1.id, "OB", 100.0, 101.0,
                   "tested", "ok")
    _entry(db, instrument_id, sc1.id, "BSL", 115.0, 115.0,
           "out_of_range", "outside_pd")
    _entry(db, instrument_id, sc1.id, "FVG", 98.0, 102.0, "fresh", "ok", ver=0)
    return {"obs1": obs1, "sc1": sc1, "z1": z1,
            "ez_fvg": ez_fvg, "ez_ob": ez_ob}


def _overview(client, instrument_id):
    body = client.get("/api/ltf/instruments", headers=AUTH).json()
    row = next(
        r for r in body["instruments"]
        if r["instrument"]["id"] == instrument_id
    )
    return body, row


def _current(client, instrument_id):
    return client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()


def test_single_snapshot_counts_and_version(client, db, seeded, instrument_id):
    """Левая карточка, счётчик и таблица — одно число и один state_version;
    повторно допущенная tested-зона (state='tested', reason='ok') входит
    во все проекции (прежнее расхождение 0/1)."""
    _make_live(db, instrument_id, 96.0)  # вне зон входа
    body, row = _overview(client, instrument_id)
    cur = _current(client, instrument_id)
    assert body["state_version"] == cur["state_version"]
    assert row["eligible_count"] == 2                    # FVG fresh + OB tested
    assert cur["counts"]["eligible"] == len(cur["eligible_entries"]) == 2
    assert row["eligible_count"] == cur["counts"]["eligible"]
    ids = {e["entry_zone_id"] for e in cur["eligible_entries"]}
    assert ids == {seeded["ez_fvg"].id, seeded["ez_ob"].id}
    # SQL-агрегат зеркалит тот же допуск (tested + reason ok)
    sql_ids = {
        r["entry_zone_id"] for r in db.list_ltf_eligible_zones()
        if r["scenario_id"] == seeded["sc1"].id
    }
    assert sql_ids == ids
    # этап — из того же результата допуска: зон две, цена вне них
    assert row["stage"] == cur["stage"] == "Ожидаем возврат в Premium"
    # идентичность снимка (§13): панели читают один контекст/сценарий/диапазон
    assert cur["instrument_id"] == instrument_id
    assert cur["context_id"] == cur["selected_context_id"] == seeded["obs1"].id
    assert cur["scenario_id"] == seeded["sc1"].id
    assert cur["range_version"] == 1
    assert isinstance(cur["as_of"], int)
    assert cur["data_state"]["state"] == "ok"


def test_tested_readmitted_zone_not_lost(client, db, instrument_id):
    """Сценарий, где единственная подходящая зона — tested/ok: раньше
    /current показывал 1, а список активов — 0 (SQL допускал только fresh)."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000, status=ZoneStatus.ACTIVE,
    ))
    _obs, sc = _scenario_with_range(
        db, instrument_id, z1, Direction.BEAR, T0, T0 + 100,
    )
    _entry(db, instrument_id, sc.id, "FVG", 98.0, 102.0, "tested", "ok")
    _make_live(db, instrument_id, 96.0)
    body, row = _overview(client, instrument_id)
    cur = _current(client, instrument_id)
    assert row["eligible_count"] == 1
    assert cur["counts"]["eligible"] == len(cur["eligible_entries"]) == 1
    assert body["state_version"] == cur["state_version"]
    assert row["stage"] == cur["stage"] == "Ожидаем возврат в Premium"


def test_overview_counts_selected_context_only(client, db, seeded,
                                               instrument_id):
    """Scope паритета: зоны других (невыбранных) контекстов инструмента не
    раздувают счётчик — список активов считает тот же сценарий, что /current
    (прежний SQL агрегировал все наблюдения инструмента)."""
    z2 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BEAR, timeframe="W1", lower=120.0, upper=130.0,
        formed_at=T0 - 8_000, confirmed_at=T0 - 7_000, status=ZoneStatus.ACTIVE,
    ))
    _obs2, sc2 = _scenario_with_range(
        db, instrument_id, z2, Direction.BEAR, T0 + 10, T0 + 300,
    )
    for i in range(3):
        _entry(db, instrument_id, sc2.id, "FVG", 95.0 + i, 96.0 + i,
               "fresh", "ok")
    # цена внутри HTF-зоны obs1 (95–105) → выбран obs1, не obs2
    _make_live(db, instrument_id, 96.0)
    body, row = _overview(client, instrument_id)
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] == seeded["obs1"].id
    assert cur["scenario_id"] == seeded["sc1"].id
    assert row["eligible_count"] == cur["counts"]["eligible"] == 2  # не 2+3
    assert body["state_version"] == cur["state_version"]


def test_stage_follows_counts(client, db, seeded, instrument_id):
    """Этап согласован с counts: count>0 → ожидание возврата; count=0 →
    «Нет подходящих зон»; data_pending важнее счётчика (§14)."""
    # нехватка данных важнее count-based этапа, счётчик при этом тот же
    body, row = _overview(client, instrument_id)
    cur = _current(client, instrument_id)
    assert cur["data_state"]["state"] == "data_pending"
    assert row["stage"] == cur["stage"] == "Недостаточно данных"
    assert row["eligible_count"] == cur["counts"]["eligible"] == 2
    # count=0: все зоны текущей версии исключены → «Нет подходящих зон»
    z2 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BEAR, timeframe="W1", lower=120.0, upper=130.0,
        formed_at=T0 - 8_000, confirmed_at=T0 - 7_000, status=ZoneStatus.ACTIVE,
    ))
    _obs2, sc2 = _scenario_with_range(
        db, instrument_id, z2, Direction.BEAR, T0 + 10, T0 + 300,
    )
    _entry(db, instrument_id, sc2.id, "BSL", 115.0, 115.0,
           "out_of_range", "outside_pd")
    # ручной выбор контекста без подходящих зон
    r = client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": _obs2.id},
    )
    assert r.status_code == 200
    _make_live(db, instrument_id, 125.0)  # внутри зоны obs2 (120–130)
    body, row = _overview(client, instrument_id)
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] == _obs2.id
    assert cur["counts"]["eligible"] == len(cur["eligible_entries"]) == 0
    assert row["eligible_count"] == 0
    assert row["stage"] == cur["stage"] == "Нет подходящих зон"
    assert body["state_version"] == cur["state_version"]
