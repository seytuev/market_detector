"""HTTP API окна LTF (LTF-спека §3, §12): список/фильтры, карточка, слои
графика, таблица Entry Zones с dist-формулами, журнал, ручное завершение."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
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


@pytest.fixture()
def seeded(db, client, instrument_id):
    """Наблюдение active (D1 bear) со сценарием/диапазоном/зонами и
    закрытое наблюдение (W1 bull) для вкладки истории."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000, status=ZoneStatus.ACTIVE,
    ))
    obs1 = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=z1, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc1 = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs1.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    se = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc1.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=110.0,
        break_candle_open_time=T0 + 50, occurred_at=T0 + 51, detected_at=T0 + 51,
        level_key="bos:primary:hl:1:110.0",
    ))
    db.update_ltf_scenario(sc1.id, trigger_event_id=se.id)
    p_low = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=90.0, kind="low",
        pivot_at=T0 - 500, candle_open_time=T0 - 500, confirmed_at=T0 - 100,
        role="LL", state="confirmed",
    ))
    p_high = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=110.0, kind="high",
        pivot_at=T0 - 400, candle_open_time=T0 - 400, confirmed_at=T0 - 50,
        role="LH", state="confirmed",
    ))
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=instrument_id, price=88.0, kind="low",
        pivot_at=T0 + 900, candle_open_time=T0 + 900, confirmed_at=None,
        state="candidate",  # §3.5: кандидат — не сигнал
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc1.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, anchor_low_pivot_id=p_low, anchor_high_pivot_id=p_high,
        available_at=T0 + 60,
    ))

    def zone_row(type_, lower, upper, state, eligible, overlap, ver=1):
        ez = db.insert_ltf_entry_zone(LtfEntryZone(
            id=None, instrument_id=instrument_id, type=type_,
            direction=Direction.BEAR, lower=lower, upper=upper,
            formed_at=T0 + 10, confirmed_at=T0 + 20,
        ))
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc1.id, entry_zone_id=ez.id, range_version=ver,
            eligible=eligible, overlap=overlap, state=state,
            added_at=T0 + 60, updated_at=T0 + 60,
        ))
        return ez

    ez_fvg = zone_row("FVG", 98.0, 102.0, "fresh", True, "partial")
    ez_ob = zone_row("OB", 100.0, 101.0, "fresh", True, "full")
    ez_bsl = zone_row("BSL", 115.0, 115.0, "out_of_range", False, "none")
    # строка старой версии диапазона (§7: история сохраняется)
    zone_row("FVG", 98.0, 102.0, "tested", True, "partial", ver=0)
    db.update_ltf_entry_zone(ez_fvg.id, first_test_at=T0 + 70)
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=ez_bsl.id, scenario_id=sc1.id, level=115.0,
        touch_at=T0 + 80, candle_open_time=T0 + 80,
    ))
    db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs1.id, scenario_id=sc1.id, kind="bos",
        payload={"break_level": 110.0}, occurred_at=T0 + 51,
        detected_at=T0 + 51, dedupe_key="bos:1:1",
    ))
    db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs1.id, scenario_id=sc1.id, kind="touch",
        payload={"entry_zone_id": ez_fvg.id}, occurred_at=T0 + 70,
        detected_at=T0 + 70, dedupe_key="touch:1:1",
    ))

    # закрытое наблюдение (история): W1 bull, сценарий отменён по HTF
    z2 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="W1", lower=50.0, upper=60.0,
        formed_at=T0 - 20_000, confirmed_at=T0 - 19_000, status=ZoneStatus.CONVERTED,
    ))
    obs2 = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=z2, zone_version=1,
        cycle_id=1, direction=Direction.BULL, state="closed_by_parent",
        activated_at=T0 - 5_000,
    ))
    db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs2.id, direction=Direction.BULL,
        trigger="SMS", stage="primary", state="cancelled",
        cancellation_reason="HTF_INVALIDATED", cancelled_at=T0 - 1_000,
    ))
    return {"obs1": obs1, "sc1": sc1, "obs2": obs2, "ez_fvg": ez_fvg,
            "ez_bsl": ez_bsl, "z1": z1, "z2": z2}


def test_ltf_auth_required(client):
    assert client.get("/api/ltf/observations").status_code == 401
    assert client.get("/api/ltf/observations", headers={"Authorization": "Bearer x"}).status_code == 401


def test_observations_list_and_filters(client, seeded):
    r = client.get("/api/ltf/observations", headers=AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1                     # tab=active по умолчанию
    item = items[0]
    assert item["id"] == seeded["obs1"].id
    assert item["instrument"]["symbol"] == "BTCUSDT"
    assert item["parent_zone"]["type"] == "ob" and item["parent_zone"]["timeframe"] == "D1"
    assert item["scenario"]["trigger"] == "BOS"
    assert item["fresh_entries"] == 2          # FVG + OB свежие
    # вкладки
    history = client.get("/api/ltf/observations?tab=history", headers=AUTH).json()
    assert [o["id"] for o in history] == [seeded["obs2"].id]
    assert len(client.get("/api/ltf/observations?tab=all", headers=AUTH).json()) == 2
    # фильтры §3.2
    assert len(client.get("/api/ltf/observations?tab=all&htf_tf=D1", headers=AUTH).json()) == 1
    assert len(client.get("/api/ltf/observations?tab=all&htf_tf=W1", headers=AUTH).json()) == 1
    assert len(client.get("/api/ltf/observations?tab=all&direction=bear", headers=AUTH).json()) == 1
    assert len(client.get("/api/ltf/observations?tab=all&trigger=SMS", headers=AUTH).json()) == 1
    assert len(client.get("/api/ltf/observations?entry_type=FVG", headers=AUTH).json()) == 1
    assert client.get("/api/ltf/observations?entry_type=SSL", headers=AUTH).json() == []


def test_observation_card(client, seeded):
    obs2 = seeded["obs2"]
    r = client.get(f"/api/ltf/observations/{seeded['obs1'].id}", headers=AUTH)
    assert r.status_code == 200
    card = r.json()
    assert card["observation"]["state"] == "active"
    assert card["parent_zone"]["lower"] == 95.0
    sc = card["active_scenario"]
    assert sc["trigger"] == "BOS" and sc["stage"] == "primary"
    assert sc["break_level"] == 110.0
    assert sc["break_candle_open_time"] == T0 + 50
    assert sc["range"]["lower"] == 90.0 and sc["range"]["version"] == 1
    # исходные экстремумы с датами: pivot_at ≠ confirmed_at (§3.5)
    assert sc["anchors"]["low"]["price"] == 90.0
    assert sc["anchors"]["low"]["pivot_at"] == T0 - 500
    assert sc["anchors"]["low"]["confirmed_at"] == T0 - 100
    assert sc["fresh_entries"] == 2
    # последняя отмена — по закрытому наблюдению
    card2 = client.get(f"/api/ltf/observations/{obs2.id}", headers=AUTH).json()
    assert card2["last_cancellation"]["reason"] == "HTF_INVALIDATED"
    # ТЗ «LTF Current Setup» §8/§4.4: отменённый сценарий не отдаётся как
    # active_scenario — он доступен только в истории (scenarios/журнал)
    assert card2["active_scenario"] is None
    assert card2["awaiting_new_scenario"] is False  # наблюдение закрыто
    assert any(s["state"] == "cancelled" for s in card2["scenarios"])
    assert client.get("/api/ltf/observations/999", headers=AUTH).status_code == 404


def test_observation_chart_layers(client, seeded):
    r = client.get(f"/api/ltf/observations/{seeded['obs1'].id}/chart", headers=AUTH)
    assert r.status_code == 200
    chart = r.json()
    assert chart["timeframe"] == "H1"
    assert chart["parent_zone"]["lower"] == 95.0
    assert chart["parent_zone"]["display_from"] == T0 - 10_000
    # pivots: кандидат отдельно виден (state), координаты в ms
    states = {p["state"] for p in chart["pivots"]}
    assert states == {"confirmed", "candidate"}
    se = chart["structure_events"][0]
    assert se["kind"] == "BOS" and se["break_level"] == 110.0
    assert se["break_candle_open_time"] == T0 + 50  # линия заканчивается на свече закрытия
    assert chart["ranges"][0]["current"] is True
    # полные границы зон, без обрезки до половины (§8.5)
    fvg = next(e for e in chart["entries"] if e["type"] == "FVG")
    assert (fvg["lower"], fvg["upper"]) == (98.0, 102.0)
    assert fvg["entry_zone_id"] == fvg["id"]
    assert chart["liquidity_tests"][0]["state"] == "awaiting_close"


def test_entries_table_with_dist(client, seeded):
    sc1 = seeded["sc1"]
    r = client.get(f"/api/ltf/scenarios/{sc1.id}/entries?price=95", headers=AUTH)
    assert r.status_code == 200
    rows = {row["type"]: row for row in r.json()["entries"]}
    # ТЗ §10: по умолчанию — только подходящие (reason ok); строка v0 скрыта
    assert set(rows) == {"FVG", "OB"}
    fvg = rows["FVG"]
    assert (fvg["lower"], fvg["upper"], fvg["mid"]) == (98.0, 102.0, 100.0)
    assert fvg["half"] == "premium" and fvg["partial"] is True
    # §3.4: dist_abs = max(L−P, 0, P−U); dist_pct = 100×dist_abs/P
    assert fvg["dist_abs"] == 3.0
    assert fvg["dist_pct"] == pytest.approx(100 * 3 / 95)
    assert fvg["state"] == "fresh"
    assert fvg["reason"] == "ok"
    assert fvg["max_test_depth"] == 0.0
    assert fvg["first_test_at"] == T0 + 70
    # исключённые — отдельным представлением с причиной (п.11)
    excluded = client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?view=excluded&price=95",
        headers=AUTH,
    ).json()["entries"]
    assert [row["type"] for row in excluded] == ["BSL"]
    bsl = excluded[0]
    assert bsl["dist_abs"] == 20.0                # уровень: abs(P−K)
    assert bsl["half"] == "none"                  # выше диапазона
    assert bsl["reason"] == "outside_pd"          # fallback из state до миграции
    assert bsl["liquidity_state"] == "awaiting_close"  # ≠ «подтверждено» (§3.4)
    # без price — dist пустые, не выдумываем
    rows2 = client.get(f"/api/ltf/scenarios/{sc1.id}/entries", headers=AUTH).json()["entries"]
    assert rows2[0]["dist_abs"] is None
    # include_all: строки всех версий среди подходящих (v0 tested → ok)
    rows3 = client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?include_all=true", headers=AUTH
    ).json()["entries"]
    assert len(rows3) == 3
    assert any(r["range_version"] == 0 and r["outdated"] for r in rows3)
    # history: все строки всех версий, включая исключённые (история причин)
    rows4 = client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?view=history", headers=AUTH
    ).json()["entries"]
    assert len(rows4) == 4
    assert any(r["range_version"] == 0 and r["outdated"] for r in rows4)
    assert {r["reason"] for r in rows4} == {"ok", "outside_pd"}
    assert client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?view=bogus", headers=AUTH
    ).status_code == 400
    assert client.get("/api/ltf/scenarios/999/entries", headers=AUTH).status_code == 404


def test_journal(client, seeded):
    sc1 = seeded["sc1"]
    r = client.get(f"/api/ltf/scenarios/{sc1.id}/journal", headers=AUTH)
    assert r.status_code == 200
    journal = r.json()["events"]
    assert [e["kind"] for e in journal] == ["bos", "touch"]  # по occurred_at
    assert journal[0]["payload"]["break_level"] == 110.0
    assert journal[0]["delivered"] is False and journal[0]["delayed"] is False
    assert client.get("/api/ltf/scenarios/999/journal", headers=AUTH).status_code == 404


def test_observation_journal_without_active_scenario(client, db, seeded):
    """Журнал по наблюдению: события отменённых сценариев не пропадают (§6.5)."""
    obs1, obs2 = seeded["obs1"], seeded["obs2"]
    db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs2.id, scenario_id=None, kind="cancellation",
        payload={"reason": "HTF_INVALIDATED"}, occurred_at=T0 - 900,
        detected_at=T0 - 900, dedupe_key="cancel:obs2",
    ))
    r = client.get(f"/api/ltf/observations/{obs2.id}/journal", headers=AUTH)
    assert r.status_code == 200
    kinds = [e["kind"] for e in r.json()["events"]]
    assert kinds == ["cancellation"]
    # то же наблюдение через сценарийный маршрут даёт тот же merged-набор
    both = client.get(f"/api/ltf/observations/{obs1.id}/journal", headers=AUTH).json()["events"]
    assert [e["kind"] for e in both] == ["bos", "touch"]
    assert client.get("/api/ltf/observations/999/journal", headers=AUTH).status_code == 404


def _pivot(db, iid, price, kind, role, t):
    return db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=iid, price=price, kind=kind,
        pivot_at=t, candle_open_time=t, confirmed_at=t + 100,
        role=role, state="confirmed",
    ))


def test_expected_levels(client, db, seeded):
    """Ожидаемые BOS/SMS из подтверждённых pivots (§6.1–§6.4)."""
    obs1, obs2 = seeded["obs1"], seeded["obs2"]
    iid = obs1.instrument_id
    # bear: опорный HL перед последним HH; SMS — внутренний минимум выше HL
    hl = _pivot(db, iid, 90.0, "low", "HL", T0 + 1000)
    _pivot(db, iid, 100.0, "high", "HH", T0 + 1200)
    il = _pivot(db, iid, 95.0, "low", "internal_low", T0 + 1400)
    card = client.get(f"/api/ltf/observations/{obs1.id}", headers=AUTH).json()
    exp = card["expected"]
    assert exp["bos"]["direction"] == "bear"
    assert exp["bos"]["level"] == 90.0
    assert exp["bos"]["ref_pivot"]["id"] == hl
    assert exp["bos"]["ref_pivot"]["pivot_at"] == T0 + 1000
    assert exp["sms"]["level"] == 95.0
    assert exp["sms"]["internal_pivot"]["id"] == il
    chart = client.get(f"/api/ltf/observations/{obs1.id}/chart", headers=AUTH).json()
    assert chart["expected"]["bos"]["level"] == 90.0
    # bull зеркально: опорный LH перед последним LL, internal_high ниже LH
    _pivot(db, iid, 108.0, "high", "LH", T0 + 2000)
    _pivot(db, iid, 88.0, "low", "LL", T0 + 2200)
    ih = _pivot(db, iid, 93.0, "high", "internal_high", T0 + 2400)
    exp2 = client.get(f"/api/ltf/observations/{obs2.id}", headers=AUTH).json()["expected"]
    assert exp2["bos"]["direction"] == "bull"
    assert exp2["bos"]["level"] == 108.0
    assert exp2["sms"]["level"] == 93.0
    assert exp2["sms"]["internal_pivot"]["id"] == ih


def test_expected_levels_empty_without_anchor(client, seeded):
    """Нет подтверждённого HH/LL — якорной структуры нет, уровни null."""
    r = client.get(f"/api/ltf/observations/{seeded['obs2'].id}", headers=AUTH)
    assert r.json()["expected"] == {"bos": None, "sms": None}


def test_expected_levels_hidden_after_break(client, db, seeded):
    """Свершившийся слом — не «ожидаемый»: уровень, пробитый закрытием H1,
    скрывается; тень без закрепления уровни сохраняет (§6: строгое закрытие)."""
    obs1 = seeded["obs1"]
    iid = obs1.instrument_id
    _pivot(db, iid, 90.0, "low", "HL", T0 + 1000)       # опорный HL, conf T0+1100
    _pivot(db, iid, 100.0, "high", "HH", T0 + 1200)
    _pivot(db, iid, 95.0, "low", "internal_low", T0 + 1400)
    # тень ниже HL, но закрытие выше обоих уровней — слома нет
    db.insert_candles([make_candle(
        T0 + 1500, 97.0, 98.0, 85.0, 96.0, timeframe="H1", instrument_id=iid,
    )])
    exp = client.get(f"/api/ltf/observations/{obs1.id}", headers=AUTH).json()["expected"]
    assert exp["bos"]["level"] == 90.0
    assert exp["sms"]["level"] == 95.0
    # закрытие H1 строго ниже HL — BOS свершился (и SMS: 88 < 95)
    db.insert_candles([make_candle(
        T0 + 2000, 92.0, 93.0, 86.0, 88.0, timeframe="H1", instrument_id=iid,
    )])
    exp = client.get(f"/api/ltf/observations/{obs1.id}", headers=AUTH).json()["expected"]
    assert exp == {"bos": None, "sms": None}
    chart = client.get(f"/api/ltf/observations/{obs1.id}/chart", headers=AUTH).json()
    assert chart["expected"] == {"bos": None, "sms": None}


def test_close_scenario(client, db, seeded):
    sc1 = seeded["sc1"]
    r = client.post(f"/api/ltf/scenarios/{sc1.id}/close", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "closed"
    assert r.json()["cancellation_reason"] == "manual"
    # родительская HTF-зона не тронута (§12)
    assert db.get_zone(seeded["z1"]).status == ZoneStatus.ACTIVE
    # наблюдение снова ждёт подтверждения
    assert db.get_ltf_observation(seeded["obs1"].id).state == "waiting_structure"
    # повторное закрытие — 409, несуществующий — 404
    assert client.post(f"/api/ltf/scenarios/{sc1.id}/close", headers=AUTH).status_code == 409
    assert client.post("/api/ltf/scenarios/999/close", headers=AUTH).status_code == 404


def test_labels_include_ltf_dictionaries(client):
    labels = client.get("/api/labels", headers=AUTH).json()
    assert labels["ltf_entry_types"]["BSL"] == "BSL"
    assert labels["ltf_observation_states"]["waiting_structure"]
    assert labels["ltf_scenario_states"]["range_pending"]
    assert labels["ltf_cancellation_reasons"]["reverse_bos"]
    assert labels["ltf_event_kinds"]["entries_ready"]


def test_settings_ltf_fields_roundtrip(client):
    r = client.get("/api/settings", headers=AUTH)
    assert r.status_code == 200
    detector = r.json()["detector"]
    assert detector["ltf_enabled"] is True
    assert detector["ltf_poll_seconds"] == 300
    r = client.post("/api/settings", headers=AUTH,
                    json={"ltf_enabled": False, "ltf_structure_left": 4})
    assert r.status_code == 200
    assert set(r.json()["applied"]) == {"ltf_enabled", "ltf_structure_left"}
    detector = client.get("/api/settings", headers=AUTH).json()["detector"]
    assert detector["ltf_enabled"] is False
    assert detector["ltf_structure_left"] == 4
    assert detector["ltf_range_right"] == 3  # фиксированные 3 правые (§5.1)


def test_ltf_analyze_flag_roundtrip(client, instrument_id):
    """Галочка «Анализировать» пишется в инструмент и читается обратно."""
    ins = client.get("/api/instruments", headers=AUTH).json()
    row = next(i for i in ins if i["id"] == instrument_id)
    assert row["ltf_analyze"] is True  # включён по умолчанию
    r = client.post(
        f"/api/instruments/{instrument_id}/ltf-analyze",
        headers=AUTH, json={"analyze": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ltf_analyze"] is False
    again = client.get("/api/instruments", headers=AUTH).json()
    assert next(i for i in again if i["id"] == instrument_id)["ltf_analyze"] is False
    r = client.post(
        f"/api/instruments/{instrument_id}/ltf-analyze",
        headers=AUTH, json={"analyze": True},
    )
    assert r.json()["ltf_analyze"] is True


def test_history_tab_includes_closed_stale(client, db, seeded):
    db.update_ltf_observation(seeded["obs1"].id, state="closed_stale")
    history = client.get("/api/ltf/observations?tab=history", headers=AUTH).json()
    states = {o["id"]: o["state"] for o in history}
    assert states[seeded["obs1"].id] == "closed_stale"
    assert states[seeded["obs2"].id] == "closed_by_parent"
    # из активной вкладки архивное исчезло
    assert client.get("/api/ltf/observations", headers=AUTH).json() == []


def test_chart_structure_events_only_current_scenario(client, db, seeded):
    """События отменённых сценариев на слой графика не попадают."""
    obs1, sc1 = seeded["obs1"], seeded["sc1"]
    old = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs1.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="cancelled",
        cancelled_at=T0 + 40, created_at=T0 + 30, updated_at=T0 + 40,
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=old.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=108.0,
        break_candle_open_time=T0 + 30, occurred_at=T0 + 31, detected_at=T0 + 31,
        level_key="bos:primary:hl:1:108.0",
    ))
    chart = client.get(
        f"/api/ltf/observations/{obs1.id}/chart", headers=AUTH
    ).json()
    assert {e["scenario_id"] for e in chart["structure_events"]} == {sc1.id}


def test_chart_entries_fallback_to_max_version(client, db, seeded):
    """Нет строк текущей версии диапазона — показываем строки последней
    версии, для которой они есть, а не весь исторический набор."""
    obs1, sc1 = seeded["obs1"], seeded["sc1"]
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc1.id, version=2, lower=91.0, upper=109.0,
        mid=100.0, available_at=T0 + 90,
    ))
    chart = client.get(
        f"/api/ltf/observations/{obs1.id}/chart", headers=AUTH
    ).json()
    # записей v2 нет — fallback на v1; группировка eligible/excluded (§10):
    # подходящие FVG+OB в рабочем слое, BSL outside_pd — в исключённых,
    # строка v0 не подмешана
    assert len(chart["entries"]) == 2
    assert {e["type"] for e in chart["entries"]} == {"FVG", "OB"}
    assert [e["type"] for e in chart["entries_excluded"]] == ["BSL"]
    assert chart["entries_excluded"][0]["reason"] == "outside_pd"


def test_invalid_entries_excluded(client, db, seeded):
    sc1 = seeded["sc1"]
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=sc1.observation_id, type="FVG",
        direction=Direction.BEAR, lower=96.0, upper=97.0,
        formed_at=T0 + 10, confirmed_at=T0 + 20, validity="invalid",
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc1.id, entry_zone_id=ez.id, range_version=1,
        eligible=False, overlap="none", state="invalid",
        added_at=T0 + 60, updated_at=T0 + 60,
    ))
    rows = client.get(f"/api/ltf/scenarios/{sc1.id}/entries", headers=AUTH).json()["entries"]
    assert all(r["entry_zone_id"] != ez.id for r in rows)
    rows_all = client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?include_all=true", headers=AUTH
    ).json()["entries"]
    assert all(r["entry_zone_id"] != ez.id for r in rows_all)
    chart = client.get(
        f"/api/ltf/observations/{seeded['obs1'].id}/chart", headers=AUTH
    ).json()
    assert all(e["entry_zone_id"] != ez.id for e in chart["entries"])


def test_entries_table_without_current_range_uses_max_version(client, db, seeded):
    """Диапазона ещё нет: фильтр идёт по max range_version строк сценария,
    а не по несуществующей v0."""
    obs1 = seeded["obs1"]
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs1.id, direction=Direction.BEAR,
        trigger="SMS", stage="primary", state="range_pending",
        created_at=T0 + 200, updated_at=T0 + 200,
    ))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=obs1.instrument_id, type="OB",
        direction=Direction.BEAR, lower=99.0, upper=101.0,
        formed_at=T0 + 210, confirmed_at=T0 + 220,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=3,
        eligible=True, overlap="full", state="fresh",
        added_at=T0 + 230, updated_at=T0 + 230,
    ))
    rows = client.get(f"/api/ltf/scenarios/{sc.id}/entries", headers=AUTH).json()["entries"]
    assert [r["entry_zone_id"] for r in rows] == [ez.id]


def test_settings_structure_resync(client, db, seeded):
    # без изменения l/r пересчёта нет
    r = client.post("/api/settings", headers=AUTH, json={"ltf_poll_seconds": 301})
    assert r.status_code == 200
    assert r.json()["structure_resynced"] is False
    # смена профиля: старые pivots удалены (H1-свечей в БД нет — набор пуст)
    assert db.list_ltf_pivots(seeded["obs1"].instrument_id)
    r = client.post("/api/settings", headers=AUTH,
                    json={"ltf_structure_left": 5, "ltf_structure_right": 4})
    body = r.json()
    assert body["structure_resynced"] is True
    assert body["detector"]["ltf_structure_left"] == 5
    assert db.list_ltf_pivots(seeded["obs1"].instrument_id) == []


def test_settings_recalc_job_status(client, db):
    """A05: успешный пересчёт — задание помечается ready с результатом,
    статус виден через GET /api/settings."""
    r = client.post("/api/settings", headers=AUTH, json={"ltf_structure_left": 4})
    assert r.status_code == 200
    assert r.json()["structure_resynced"] is True
    recalc = client.get("/api/settings", headers=AUTH).json()["recalc"]
    assert recalc["status"] == "ready"
    assert recalc["result"]["structure_resynced"] is True
    assert recalc["finished_at"] >= recalc["started_at"]


def test_settings_recalc_failure_rolls_back(db, settings, tmp_path, monkeypatch):
    """A05: сбой пересчёта — 500, память и файл откачены к прежнему конфигу,
    задание помечается failed."""
    engine = LtfEngine(db, settings.detector)
    client = TestClient(create_app(db, settings, ltf_engine=engine))

    def boom():
        raise RuntimeError("сбой пересчёта")

    monkeypatch.setattr(engine, "resync_structure_params", boom)
    r = client.post("/api/settings", headers=AUTH, json={"ltf_structure_left": 5})
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "settings_recalc_failed"
    # память откачена
    assert settings.detector.ltf_structure_left == 3
    # файл переписан прежним конфигом
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["detector"]["ltf_structure_left"] == 3
    # задание failed, статус виден через GET
    recalc = client.get("/api/settings", headers=AUTH).json()["recalc"]
    assert recalc["status"] == "failed"
    assert "сбой пересчёта" in recalc["error"]


def test_settings_entry_types_reclassify(client, db, seeded):
    """п.14: отключение FVG в ltf_entry_types через /api/settings исключает
    его из пригодности, счётчика, API и графика — с сохранением истории;
    повторное включение возвращает зону (invalid не восстанавливается)."""
    sc1 = seeded["sc1"]
    r = client.post("/api/settings", headers=AUTH,
                    json={"ltf_entry_types": "OB,BSL,SSL"})
    assert r.status_code == 200
    assert r.json()["entries_reclassified"] == {str(sc1.id): 3}
    # FVG переведён в type_disabled на текущей версии, строка v0 не тронута
    entries = db.list_ltf_scenario_entries(sc1.id)
    fvg_v1 = next(e for e in entries
                  if e.entry_zone_id == seeded["ez_fvg"].id
                  and e.range_version == 1)
    assert (fvg_v1.reason, fvg_v1.state) == ("type_disabled", "out_of_range")
    fvg_v0 = next(e for e in entries
                  if e.entry_zone_id == seeded["ez_fvg"].id
                  and e.range_version == 0)
    assert fvg_v0.reason == "" and fvg_v0.state == "tested"  # история цела
    # подходящие и счётчик: только OB
    rows = client.get(f"/api/ltf/scenarios/{sc1.id}/entries",
                      headers=AUTH).json()["entries"]
    assert [row["type"] for row in rows] == ["OB"]
    card = client.get(f"/api/ltf/observations/{seeded['obs1'].id}",
                      headers=AUTH).json()
    assert card["active_scenario"]["fresh_entries"] == 1
    # исключённые с причиной; график — eligible/excluded группами
    excluded = client.get(
        f"/api/ltf/scenarios/{sc1.id}/entries?view=excluded", headers=AUTH
    ).json()["entries"]
    assert {row["type"]: row["reason"] for row in excluded} == {
        "FVG": "type_disabled", "BSL": "outside_pd",
    }
    chart = client.get(
        f"/api/ltf/observations/{seeded['obs1'].id}/chart", headers=AUTH
    ).json()
    assert [e["type"] for e in chart["entries"]] == ["OB"]
    assert {e["type"] for e in chart["entries_excluded"]} == {"FVG", "BSL"}
    # зона не удалена, история версий сохранена
    assert db.get_ltf_entry_zone(seeded["ez_fvg"].id) is not None
    assert len(db.list_ltf_scenario_entries(sc1.id)) == 4
    # повторное включение возвращает FVG в подходящие (без событий)
    r2 = client.post("/api/settings", headers=AUTH,
                     json={"ltf_entry_types": "FVG,OB,BSL,SSL"})
    assert r2.status_code == 200
    fvg_v1 = next(e for e in db.list_ltf_scenario_entries(sc1.id)
                  if e.entry_zone_id == seeded["ez_fvg"].id
                  and e.range_version == 1)
    assert (fvg_v1.reason, fvg_v1.state) == ("ok", "fresh")
    # пересчёт не создал событий/уведомлений
    kinds = [e.kind for e in db.list_ltf_events(observation_id=seeded["obs1"].id)]
    assert sorted(kinds) == ["bos", "touch"]
