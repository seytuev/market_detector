"""ТЗ «LTF Current Setup» §6/§7/§14 — read model текущей ситуации:
/api/ltf/instruments (п.01, этапы, агрегатные счётчики),
/api/ltf/instruments/{id}/current (п.02, п.03, п.18, п.19),
POST select-context (§7)."""
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


def _observation(db, instrument_id, zone_id, direction, state, activated_at):
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id, zone_version=1,
        cycle_id=1, direction=direction, state=state, activated_at=activated_at,
    ))


@pytest.fixture()
def seeded(db, client, instrument_id):
    """Активное наблюдение (D1 bear 95–105) со сценарием/диапазоном v1 и
    зонами: FVG+OB подходящие (reason ok), BSL outside_pd, строка v0 —
    история. Плюс закрытое наблюдение (W1) того же инструмента."""
    z1 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0 - 10_000, confirmed_at=T0 - 9_000, status=ZoneStatus.ACTIVE,
    ))
    obs1 = _observation(db, instrument_id, z1, Direction.BEAR, "active", T0)
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
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc1.id, version=1, lower=90.0, upper=110.0,
        mid=100.0, anchor_low_pivot_id=p_low, anchor_high_pivot_id=p_high,
        available_at=T0 + 60,
    ))

    def zone_row(type_, lower, upper, state, reason, ver=1):
        ez = db.insert_ltf_entry_zone(LtfEntryZone(
            id=None, instrument_id=instrument_id, type=type_,
            direction=Direction.BEAR, lower=lower, upper=upper,
            formed_at=T0 + 10, confirmed_at=T0 + 20,
        ))
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc1.id, entry_zone_id=ez.id, range_version=ver,
            eligible=state == "fresh", overlap="partial", state=state,
            reason=reason, added_at=T0 + 60, updated_at=T0 + 60,
        ))
        return ez

    ez_fvg = zone_row("FVG", 98.0, 102.0, "fresh", "ok")
    zone_row("OB", 100.0, 101.0, "fresh", "ok")
    zone_row("BSL", 115.0, 115.0, "out_of_range", "outside_pd")
    zone_row("FVG", 98.0, 102.0, "tested", "ok", ver=0)  # история версий

    # закрытое наблюдение того же инструмента (история контекстов)
    z2 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="W1", lower=50.0, upper=60.0,
        formed_at=T0 - 20_000, confirmed_at=T0 - 19_000,
        status=ZoneStatus.CONVERTED,
    ))
    obs2 = _observation(db, instrument_id, z2, Direction.BULL,
                        "closed_by_parent", T0 - 5_000)
    return {"obs1": obs1, "sc1": sc1, "obs2": obs2, "z1": z1,
            "ez_fvg": ez_fvg}


def _make_live(db, instrument_id, price: float) -> None:
    """Свежая закрытая H1 и котировка → data_state ok."""
    now = now_ms()
    db.insert_candles([make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )])
    db.set_quote(instrument_id, price, now)


# ------------------------------ список активов (§4.2) ------------------------------

def test_instruments_one_row_per_instrument(client, db, seeded, instrument_id):
    """п.01: десять Observation → одна строка инструмента в левой панели."""
    for i in range(8):  # к двум из seeded — ещё 8 наблюдений
        z = db.insert_zone(Zone(
            id=None, instrument_id=instrument_id, type=ZoneType.OB,
            direction=Direction.BEAR, timeframe="D1", lower=90 + i,
            upper=100 + i, formed_at=T0 - 30_000, confirmed_at=T0 - 29_000,
            status=ZoneStatus.ACTIVE,
        ))
        _observation(db, instrument_id, z, Direction.BEAR,
                     "active" if i % 2 else "closed_stale", T0 + i)
    rows = client.get("/api/ltf/instruments", headers=AUTH).json()
    mine = [r for r in rows if r["instrument"]["id"] == instrument_id]
    assert len(mine) == 1
    row = mine[0]
    assert row["instrument"]["symbol"] == "BTCUSDT"
    assert row["instrument"]["venue"] == "binance"
    assert row["instrument"]["market_type"] == "spot"
    # счётчики: подходящие reason==ok текущей версии; контексты — активные
    assert row["eligible_count"] == 2                    # FVG + OB
    assert row["contexts_count"] == 1 + 4                # obs1 + активные i%2
    assert row["htf_context"] == {"type": "ob", "timeframe": "D1"}
    assert row["last_event_at"] is not None
    # нет H1-свечей — «Недостаточно данных», а не «нет сетапа» (§14)
    assert row["stage"] == "Недостаточно данных"
    assert row["direction"] is None
    assert row["data_state"]["state"] == "data_pending"
    # со свежими данными — реальный этап и направление сценария
    _make_live(db, instrument_id, 96.0)  # вне зон входа
    row = next(
        r for r in client.get("/api/ltf/instruments", headers=AUTH).json()
        if r["instrument"]["id"] == instrument_id
    )
    assert row["stage"] == "Возврат в Premium"
    assert row["direction"] == "bear"
    assert row["data_state"]["state"] == "ok"


def test_instruments_counts_per_instrument(client, db, seeded, instrument_id):
    """Агрегатный запрос счётчиков корректно разносит зоны по инструментам."""
    from app.models import Instrument
    iid2 = db.upsert_instrument(Instrument(
        id=None, asset="ETH", venue="binance", market_type="spot",
        symbol="ETHUSDT", quote_asset="USDT",
    ))
    z = db.insert_zone(Zone(
        id=None, instrument_id=iid2, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=10.0, upper=20.0,
        formed_at=T0, confirmed_at=T0 + 100, status=ZoneStatus.ACTIVE,
    ))
    obs = _observation(db, iid2, z, Direction.BEAR, "active", T0)
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=iid2, type="OB", direction=Direction.BEAR,
        lower=15.0, upper=16.0, formed_at=T0 + 10, confirmed_at=T0 + 20,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=0,
        eligible=True, overlap="full", state="fresh", reason="ok",
        added_at=T0 + 60, updated_at=T0 + 60,
    ))
    rows = {
        r["instrument"]["symbol"]: r
        for r in client.get("/api/ltf/instruments", headers=AUTH).json()
    }
    assert rows["BTCUSDT"]["eligible_count"] == 2
    assert rows["ETHUSDT"]["eligible_count"] == 1
    assert rows["ETHUSDT"]["contexts_count"] == 1


# ------------------------------ /current (§14) ------------------------------

def test_current_price_above_zone_is_not_inside(client, db, seeded, instrument_id):
    """п.02: цена выше U родительской зоны → is_price_inside_now=false и
    «above», а не «цена в зоне» (пример снимка: 84038 > 81272.62)."""
    _make_live(db, instrument_id, 110.0)  # выше OB 95–105
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["data_state"]["state"] == "ok"
    ctx = next(c for c in cur["contexts"]
               if c["observation_id"] == seeded["obs1"].id)
    assert ctx["is_price_inside_now"] is False
    assert ctx["price_position"] == "above"
    assert ctx["parent_validity"] == "active"
    # историческое касание — отдельным полем, inside=true не удерживает
    assert ctx["last_touch_at"] == T0
    # цена внутри FVG 98–102? 110 выше — этап «Возврат в Premium», не «в зоне»
    assert cur["stage"] == "Возврат в Premium"
    # котировка внутри подходящей зоны → «Цена в Entry Zone»
    _make_live(db, instrument_id, 100.0)
    cur2 = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur2["stage"] == "Цена в Entry Zone"
    ctx2 = next(c for c in cur2["contexts"]
                if c["observation_id"] == seeded["obs1"].id)
    assert ctx2["is_price_inside_now"] is True
    assert ctx2["price_position"] == "inside"


def test_current_stale_quote_hides_inside(client, db, seeded, instrument_id):
    """§6: устаревшая котировка — is_price_inside_now=null + stale,
    положение цены актуальным не заявляется."""
    now = now_ms()
    db.insert_candles([make_candle(
        now - 30 * 60_000, 100, 101, 99, 100,
        timeframe="H1", instrument_id=instrument_id,
    )])
    db.set_quote(instrument_id, 100.0, now - 10 * 3_600_000)  # 10ч назад
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    ds = cur["data_state"]
    assert ds["state"] == "stale" and ds["reason"] == "quote_stale"
    assert ds["quote_stale"] is True and ds["quote_age_s"] is not None
    ctx = cur["contexts"][0]
    assert ctx["is_price_inside_now"] is None
    assert ctx["price_position"] is None


def test_current_scenario_lifecycle(client, db, seeded, instrument_id):
    """п.03/п.18: отмена → current_scenario=null и ожидание нового; reconnect
    (повторный запрос) отменённый не возвращает; новое событие → новый id."""
    _make_live(db, instrument_id, 100.0)
    sc1 = seeded["sc1"]
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["current_scenario"]["id"] == sc1.id
    assert cur["scenario_waiting"] is None
    # ручная отмена
    r = client.post(f"/api/ltf/scenarios/{sc1.id}/close", headers=AUTH)
    assert r.status_code == 200
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["current_scenario"] is None
    assert cur["scenario_waiting"]["status"] == "awaiting_new_scenario"
    assert cur["scenario_waiting"]["last_cancellation"]["reason"] == "manual"
    assert cur["counts"] == {"eligible": 0, "excluded": 0, "historical": 0}
    assert cur["stage"] == "Ждём BOS/SMS"
    # reconnect: повторный снимок — отменённый сценарий текущим не удерживается
    again = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert again["current_scenario"] is None
    # карточка наблюдения тоже не отдаёт отменённый как активный
    card = client.get(f"/api/ltf/observations/{seeded['obs1'].id}",
                      headers=AUTH).json()
    assert card["active_scenario"] is None
    assert card["awaiting_new_scenario"] is True
    # новое согласованное событие → новый scenario_id (§8)
    sc2 = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=seeded["obs1"].id, direction=Direction.BEAR,
        trigger="SMS", stage="primary", state="range_pending",
        created_at=T0 + 200, updated_at=T0 + 200,
    ))
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["current_scenario"]["id"] == sc2.id != sc1.id
    assert cur["scenario_waiting"] is None
    assert cur["stage"] == "Ждём диапазон"


def test_current_counts_and_state_version_consistent(client, db, seeded,
                                                     instrument_id):
    """п.19: счётчик/таблица/карточка — один scenario_id/state_version;
    counts согласованы с полями ответа."""
    _make_live(db, instrument_id, 100.0)
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["counts"]["eligible"] == len(cur["eligible_entries"]) == 2
    assert cur["counts"]["excluded"] == 1      # BSL outside_pd
    assert cur["counts"]["historical"] == 1    # строка v0
    assert cur["current_scenario"]["id"] == seeded["sc1"].id
    assert cur["range"]["lower"] == 90.0
    assert cur["range"]["anchors"]["low"]["price"] == 90.0
    # тот же state_version отдаёт карточка наблюдения
    card = client.get(f"/api/ltf/observations/{seeded['obs1'].id}",
                      headers=AUTH).json()
    assert card["state_version"] == cur["state_version"]
    # dist подходящих зон посчитан от свежей котировки
    fvg = next(e for e in cur["eligible_entries"] if e["type"] == "FVG")
    assert fvg["dist_abs"] == 0.0              # цена 100 внутри 98–102
    # last_processed_h1 — из meta-курсора движка (не выдуман)
    assert cur["last_processed_h1"] is None
    assert cur["last_closed_h1"] is not None
    assert cur["quote_at"] is not None


def test_select_context_manual_and_fallback(client, db, seeded, instrument_id):
    """§7: ручной выбор удерживается, пока контекст доступен; ушедший в
    историю — fallback на контекст с последним действующим сценарием."""
    _make_live(db, instrument_id, 100.0)
    z3 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=70.0, upper=75.0,
        formed_at=T0 - 8_000, confirmed_at=T0 - 7_000, status=ZoneStatus.ACTIVE,
    ))
    obs3 = _observation(db, instrument_id, z3, Direction.BULL,
                        "waiting_structure", T0 + 10)
    # ручной выбор
    r = client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": obs3.id},
    )
    assert r.status_code == 200
    assert r.json()["selected_context_id"] == obs3.id
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["selected_context_id"] == obs3.id
    # выбранный контекст без сценария → ожидание BOS/SMS, сценария нет
    assert cur["current_scenario"] is None
    assert cur["stage"] == "Ждём BOS/SMS"
    # контекст ушёл в историю → fallback: контекст с действующим сценарием
    db.update_ltf_observation(obs3.id, state="closed_stale")
    cur = client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    assert cur["selected_context_id"] == seeded["obs1"].id
    assert cur["current_scenario"]["id"] == seeded["sc1"].id
    # ошибки: чужой/несуществующий контекст — 404, исторический — 409
    assert client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": 999},
    ).status_code == 404
    assert client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": obs3.id},
    ).status_code == 409


def test_select_context_prefers_price_inside(client, db, seeded, instrument_id):
    """Автовыбор показывает контекст, чья HTF-зона сейчас содержит цену
    («план в реализации»), даже если сценария у него ещё нет, а у другого
    контекста вне зоны сценарий есть. Среди «внутри» — с действующим
    сценарием; по stale-котировке приоритет не применяется."""
    z3 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=106.0, upper=108.0,
        formed_at=T0 - 8_000, confirmed_at=T0 - 7_000, status=ZoneStatus.ACTIVE,
    ))
    obs3 = _observation(db, instrument_id, z3, Direction.BULL,
                        "waiting_structure", T0 + 10)
    get = lambda: client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()
    # нет свежей котировки → прежняя политика (последний действующий сценарий)
    assert get()["selected_context_id"] == seeded["obs1"].id
    # цена 107 внутри зоны obs3 (106–108) и вне зоны obs1 (95–105) со
    # сценарием → показываем obs3, хотя сценария у него ещё нет
    _make_live(db, instrument_id, 107.0)
    cur = get()
    assert cur["selected_context_id"] == obs3.id
    assert cur["current_scenario"] is None
    # у obs3 появился сценарий; второй контекст «внутри» без сценария
    # активирован позже — всё равно выбирается obs3 (сценарий важнее даты)
    sc3 = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs3.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 200, updated_at=T0 + 200,
    ))
    z4 = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=105.0, upper=110.0,
        formed_at=T0 - 6_000, confirmed_at=T0 - 5_000, status=ZoneStatus.ACTIVE,
    ))
    _observation(db, instrument_id, z4, Direction.BULL, "active", T0 + 20)
    cur = get()
    assert cur["selected_context_id"] == obs3.id
    assert cur["current_scenario"]["id"] == sc3.id


def test_current_unknown_instrument_404(client):
    assert client.get("/api/ltf/instruments/999/current",
                      headers=AUTH).status_code == 404
