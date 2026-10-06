"""ТЗ переработки L01: единая функция окончательного решения о пригодности.

Приёмка:
- актуальный OB с глубиной теста ровно 90% остаётся актуальным, но не
  подходит для нового входа;
- подтверждение геометрии не оживляет завершённую (invalid) зону;
- снятый уровень (swept_level) не возвращается при новой версии диапазона;
- один и тот же набор допустимых идентификаторов используется в карточке
  (/current), счётчике (SQL list_ltf_eligible_zones), графике (/chart),
  таблице (/entries?view=eligible) и движке уведомлений (_entry_candidates);
- контекстное исключение §18 — единственный путь допуска вне обычного
  правила и помечается admission_basis == "context_exception".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.engine.ltf.eligibility import (
    ADMISSION_CONTEXT,
    ADMISSION_RULE,
    REASON_INVALID,
    REASON_OK,
    REASON_OUTSIDE_PD,
    REASON_SWEPT_LEVEL,
    REASON_TESTED_TOO_DEEP,
    admitted_scenario_entries,
    evaluate_final,
)
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from app.web.api import create_app
from app.web.ltf_api import _fresh_entries

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
T0 = 1_780_000_000_000


def _entry(state: str, reason: str, eligible: bool = True,
           overlap: str = "full", ver: int = 1) -> LtfScenarioEntry:
    return LtfScenarioEntry(
        id=1, scenario_id=1, entry_zone_id=1, range_version=ver,
        eligible=eligible, overlap=overlap, state=state, reason=reason,
        added_at=T0, updated_at=T0,
    )


def _zone(type_: str = "OB", validity: str = "fresh",
          depth: float = 0.0) -> LtfEntryZone:
    return LtfEntryZone(
        id=1, instrument_id=1, type=type_, direction=Direction.BEAR,
        lower=100.0, upper=104.0, formed_at=T0, confirmed_at=T0 + 1000,
        validity=validity, max_test_depth=depth,
    )


# ------------------------- evaluate_final (unit) -------------------------

def test_rule_admission_basis():
    fe = evaluate_final(_entry("fresh", REASON_OK), _zone(),
                        allow_outside=False)
    assert fe.eligible_now is True
    assert fe.admission_basis == ADMISSION_RULE
    assert fe.blocking_reasons == ()
    assert fe.primary_reason == REASON_OK
    assert fe.spatial_overlap == "full"


def test_ob_tested_exactly_90pct_stays_valid_but_not_eligible():
    # приёмка: глубина ровно 90% — рыночная актуальность сохраняется
    # (validity == "tested", не invalid), но для нового входа не подходит
    zone = _zone(validity="tested", depth=0.9)
    fe = evaluate_final(
        _entry("tested", REASON_TESTED_TOO_DEEP, overlap="full"),
        zone, allow_outside=True,
    )
    assert zone.validity == "tested"           # актуальность не снята
    assert fe.eligible_now is False
    assert fe.blocking_reasons == (REASON_TESTED_TOO_DEEP,)
    assert fe.admission_basis is None


def test_invalid_zone_not_revived_by_geometry_or_context():
    # приёмка: подтверждение геометрии (eligible=True на новой версии
    # диапазона) не оживляет завершённую зону; контекст тоже не помогает
    fe = evaluate_final(
        _entry("invalid", REASON_INVALID, eligible=True, ver=2),
        _zone(validity="invalid"), allow_outside=True,
    )
    assert fe.eligible_now is False
    assert fe.blocking_reasons == (REASON_INVALID,)


def test_swept_level_not_revived_by_new_range_version():
    # приёмка п.17: снятый BSL/SSL не воскресает от новой версии диапазона
    fe = evaluate_final(
        _entry("tested", REASON_SWEPT_LEVEL, ver=2),
        _zone(type_="BSL"), allow_outside=True,
    )
    assert fe.eligible_now is False
    assert fe.blocking_reasons == (REASON_SWEPT_LEVEL,)


def test_context_exception_only_for_fvg_with_complete_context():
    entry = _entry("out_of_range", REASON_OUTSIDE_PD, eligible=False,
                   overlap="none")
    fe = evaluate_final(entry, _zone(type_="FVG"), allow_outside=True)
    assert fe.eligible_now is True
    assert fe.admission_basis == ADMISSION_CONTEXT
    assert fe.blocking_reasons == ()
    # без полного контекста — нет допуска
    fe_no = evaluate_final(entry, _zone(type_="FVG"), allow_outside=False)
    assert fe_no.eligible_now is False
    assert fe_no.blocking_reasons == (REASON_OUTSIDE_PD,)
    # и только для FVG: OB вне половины не допускается контекстом
    fe_ob = evaluate_final(entry, _zone(type_="OB"), allow_outside=True)
    assert fe_ob.eligible_now is False


# ------------------------- согласованность проекций -------------------------

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
    return TestClient(
        create_app(db, settings, ltf_engine=LtfEngine(db, settings.detector))
    )


@pytest.fixture()
def seeded(db, client, instrument_id):
    """Сценарий monitoring_entries с набором привязок: допущенная OB,
    контекстно допускаемая FVG вне Premium, слишком глубокий тест OB,
    снятый BSL — всё на текущей версии диапазона."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000,
        status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=z1, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, anchor_low_pivot_id=None, anchor_high_pivot_id=None,
        available_at=T0 + 60,
    ))

    def zone_row(type_, lower, upper, state, reason, eligible, overlap):
        ez = db.insert_ltf_entry_zone(LtfEntryZone(
            id=None, instrument_id=instrument_id, type=type_,
            direction=Direction.BEAR, lower=lower, upper=upper,
            formed_at=T0 + 10, confirmed_at=T0 + 20,
        ))
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
            eligible=eligible, overlap=overlap, state=state, reason=reason,
            added_at=T0 + 60, updated_at=T0 + 60,
        ))
        return ez

    ez_ok = zone_row("OB", 100.0, 104.0, "fresh", REASON_OK, True, "full")
    ez_fvg = zone_row("FVG", 80.0, 85.0, "out_of_range", REASON_OUTSIDE_PD,
                      False, "none")
    ez_deep = zone_row("OB", 101.0, 103.0, "tested", REASON_TESTED_TOO_DEEP,
                       True, "full")
    ez_swept = zone_row("BSL", 115.0, 115.0, "tested", REASON_SWEPT_LEVEL,
                        False, "none")
    return {"obs": obs, "sc": sc, "ok": ez_ok, "fvg": ez_fvg,
            "deep": ez_deep, "swept": ez_swept}


def _complete_context(db, seeded):
    for fact in ("counter_sweep", "htf_fvg50"):
        db.insert_ltf_event(LtfEvent(
            id=None, observation_id=seeded["obs"].id,
            scenario_id=seeded["sc"].id, kind="context_update",
            payload={"fact": fact}, occurred_at=T0 + 70, detected_at=T0 + 70,
            dedupe_key=f"ctx:{fact}:1",
        ))


def _projection_ids(db, client, seeded, instrument_id):
    """Наборы допустимых entry_zone_id из всех проекций (L01)."""
    sc = seeded["sc"]
    engine = LtfEngine(db, Settings().detector)
    canonical = {
        z.id for _, z, _ in admitted_scenario_entries(db, sc.id)
    }
    engine_ids = {
        z.id for _, z, _out in engine._entry_candidates(sc)
    }
    fresh_ids = {z.id for _, z in _fresh_entries(db, sc.id)}
    r = client.get(
        f"/api/ltf/scenarios/{sc.id}/entries?view=eligible", headers=AUTH
    )
    entries_ids = {row["entry_zone_id"] for row in r.json()}
    r = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    )
    body = r.json()
    current_ids = {row["entry_zone_id"] for row in body["eligible_entries"]}
    count = body["counts"]["eligible"]
    r = client.get(
        f"/api/ltf/observations/{seeded['obs'].id}/chart", headers=AUTH
    )
    chart_ids = {row["entry_zone_id"] for row in r.json()["entries"]}
    sql_ids = {
        row["entry_zone_id"] for row in db.list_ltf_eligible_zones()
        if row["scenario_id"] == sc.id
    }
    return {
        "canonical": canonical, "engine": engine_ids, "fresh": fresh_ids,
        "entries_view": entries_ids, "current": current_ids,
        "current_count": count, "chart": chart_ids, "sql": sql_ids,
    }


def test_single_admitted_set_across_projections(db, client, seeded,
                                                instrument_id):
    ids = _projection_ids(db, client, seeded, instrument_id)
    # без контекста: только OB по обычному правилу; глубокий тест и снятый
    # уровень не подходят нигде
    expected = {seeded["ok"].id}
    assert ids["canonical"] == expected
    for name in ("engine", "fresh", "entries_view", "current", "chart",
                 "sql"):
        assert ids[name] == expected, name
    assert ids["current_count"] == 1


def test_single_admitted_set_with_context_exception(db, client, seeded,
                                                    instrument_id):
    _complete_context(db, seeded)
    ids = _projection_ids(db, client, seeded, instrument_id)
    # полный контекст §18: FVG вне Premium допускается во всех проекциях;
    # swept_level и tested_too_deep не возвращаются
    expected = {seeded["ok"].id, seeded["fvg"].id}
    assert ids["canonical"] == expected
    for name in ("engine", "fresh", "entries_view", "current", "chart",
                 "sql"):
        assert ids[name] == expected, name
    assert ids["current_count"] == 2
    # контекстный допуск явно помечен в карточке
    r = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    )
    rows = {row["entry_zone_id"]: row for row in r.json()["eligible_entries"]}
    assert rows[seeded["fvg"].id]["admission_basis"] == ADMISSION_CONTEXT
    assert rows[seeded["fvg"].id]["outside_premium"] is True
    assert rows[seeded["ok"].id]["admission_basis"] == ADMISSION_RULE
