"""L05 (несколько HTF-контекстов: основание выбора, конфликт направлений)
и L06 (приоритет внимания списка активов) — read model
/app/services/overview.py + эндпоинты /api/ltf/instruments[/{id}/current].

Новые поля — обратно-совместимые добавления: selected_context_basis,
contexts_conflict (снимок /current), attention/attention_reason (список)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms
from app.models_ltf import (
    LtfObservation,
    LtfPivot,
    LtfRange,
    LtfScenario,
)
from app.services.overview import ATTENTION_ORDER
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


def _zone(db, instrument_id, direction, lower, upper, status=ZoneStatus.ACTIVE,
          offset=0):
    return db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=direction, timeframe="D1", lower=lower, upper=upper,
        formed_at=T0 - 10_000 + offset, confirmed_at=T0 - 9_000 + offset,
        status=status,
    ))


def _scenario_with_range(db, obs, direction=Direction.BEAR,
                         lower=90.0, upper=110.0):
    """Действующий сценарий с подтверждённым диапазоном v1 (без entry-зон)."""
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T0 + 100, updated_at=T0 + 100,
    ))
    p_low = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=obs.instrument_id, price=lower, kind="low",
        pivot_at=T0 - 500, candle_open_time=T0 - 500, confirmed_at=T0 - 100,
        role="LL", state="confirmed",
    ))
    p_high = db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=obs.instrument_id, price=upper, kind="high",
        pivot_at=T0 - 400, candle_open_time=T0 - 400, confirmed_at=T0 - 50,
        role="LH", state="confirmed",
    ))
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=1, lower=lower, upper=upper,
        mid=(lower + upper) / 2, anchor_low_pivot_id=p_low,
        anchor_high_pivot_id=p_high, available_at=T0 + 60,
    ))
    return sc


@pytest.fixture()
def seeded(db, client, instrument_id):
    """Активное наблюдение (D1 bear 95–105) со сценарием и диапазоном v1."""
    z1 = _zone(db, instrument_id, Direction.BEAR, 95.0, 105.0)
    obs1 = _observation(db, instrument_id, z1, Direction.BEAR, "active", T0)
    sc1 = _scenario_with_range(db, obs1)
    return {"obs1": obs1, "sc1": sc1, "z1": z1}


def _make_live(db, instrument_id, price: float) -> None:
    """Свежая закрытая H1 и котировка → data_state ok."""
    now = now_ms()
    c = make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )
    db.insert_candles([c])
    db.set_quote(instrument_id, price, now)
    # F03: курсор обработки на последней закрытой — иначе processing_lag
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))


def _current(client, instrument_id):
    return client.get(
        f"/api/ltf/instruments/{instrument_id}/current", headers=AUTH
    ).json()


def _overview_row(client, instrument_id):
    rows = client.get("/api/ltf/instruments", headers=AUTH).json()
    return next(r for r in rows if r["instrument"]["id"] == instrument_id)


# ------------------------------ L05: основание выбора контекста ------------------------------

def test_basis_manual(client, db, seeded, instrument_id):
    """Ручной выбор → основание manual («Выбран вручную»)."""
    _make_live(db, instrument_id, 100.0)
    z3 = _zone(db, instrument_id, Direction.BULL, 70.0, 75.0, offset=2000)
    obs3 = _observation(db, instrument_id, z3, Direction.BULL,
                        "waiting_structure", T0 + 10)
    r = client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": obs3.id},
    )
    assert r.status_code == 200
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] == obs3.id
    assert cur["selected_context_basis"] == "manual"


def test_basis_manual_stable_on_quote_change(client, db, seeded, instrument_id):
    """L05: ручной выбор не слетает при обновлении котировки — в том числе
    когда цена уходит внутри зоны другого контекста (meta-политика)."""
    z3 = _zone(db, instrument_id, Direction.BULL, 70.0, 75.0, offset=2000)
    obs3 = _observation(db, instrument_id, z3, Direction.BULL,
                        "waiting_structure", T0 + 10)
    client.post(
        f"/api/ltf/instruments/{instrument_id}/select-context",
        headers=AUTH, json={"observation_id": obs3.id},
    )
    for price in (100.0, 74.0, 120.0, 96.0):
        _make_live(db, instrument_id, price)
        cur = _current(client, instrument_id)
        assert cur["selected_context_id"] == obs3.id
        assert cur["selected_context_basis"] == "manual"


def test_basis_price_inside(client, db, seeded, instrument_id):
    """Свежая котировка внутри HTF-зоны контекста без сценария → price_inside."""
    z3 = _zone(db, instrument_id, Direction.BULL, 106.0, 108.0, offset=2000)
    obs3 = _observation(db, instrument_id, z3, Direction.BULL,
                        "waiting_structure", T0 + 10)
    _make_live(db, instrument_id, 107.0)
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] == obs3.id
    assert cur["selected_context_basis"] == "price_inside"


def test_basis_price_inside_not_applied_on_stale(client, db, seeded,
                                                 instrument_id):
    """Stale-котировка: приоритет «цена внутри» не применяется — основание
    отражает фактический fallback (last_scenario), защита сохранена."""
    z3 = _zone(db, instrument_id, Direction.BULL, 106.0, 108.0, offset=2000)
    _observation(db, instrument_id, z3, Direction.BULL,
                 "waiting_structure", T0 + 10)
    now = now_ms()
    db.insert_candles([make_candle(
        now - 30 * 60_000, 100, 101, 99, 100,
        timeframe="H1", instrument_id=instrument_id,
    )])
    db.set_quote(instrument_id, 107.0, now - 10 * 3_600_000)  # 10ч назад
    cur = _current(client, instrument_id)
    assert cur["data_state"]["state"] == "stale"
    assert cur["selected_context_id"] == seeded["obs1"].id
    assert cur["selected_context_basis"] == "last_scenario"


def test_basis_last_scenario(client, db, seeded, instrument_id):
    """Нет свежей котировки и ручного выбора → контекст с последним
    действующим сценарием (last_scenario)."""
    cur = _current(client, instrument_id)  # данных нет вовсе — data_pending
    assert cur["selected_context_id"] == seeded["obs1"].id
    assert cur["selected_context_basis"] == "last_scenario"


def test_basis_last_contact(client, db, instrument_id):
    """Активные контексты без сценариев и без котировки → последний валидный
    контакт (last_contact)."""
    z1 = _zone(db, instrument_id, Direction.BEAR, 95.0, 105.0)
    _observation(db, instrument_id, z1, Direction.BEAR, "waiting_structure", T0)
    z2 = _zone(db, instrument_id, Direction.BEAR, 80.0, 85.0, offset=1000)
    obs2 = _observation(db, instrument_id, z2, Direction.BEAR,
                        "active", T0 + 10)
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] == obs2.id  # последний по активации
    assert cur["selected_context_basis"] == "last_contact"


def test_basis_none_without_contexts(client, instrument_id):
    """Активных контекстов нет → контекст и основание — None."""
    cur = _current(client, instrument_id)
    assert cur["selected_context_id"] is None
    assert cur["selected_context_basis"] is None
    assert cur["contexts_conflict"] is False


# ------------------------------ L05: конфликт направлений ------------------------------

def test_contexts_conflict_mixed(client, db, instrument_id):
    """Активные контексты противоположных направлений → contexts_conflict
    true, direction остаётся «mixed» (семантика сохранена)."""
    z1 = _zone(db, instrument_id, Direction.BEAR, 95.0, 105.0)
    _observation(db, instrument_id, z1, Direction.BEAR, "active", T0)
    z3 = _zone(db, instrument_id, Direction.BULL, 70.0, 75.0, offset=2000)
    _observation(db, instrument_id, z3, Direction.BULL, "active", T0 + 10)
    _make_live(db, instrument_id, 110.0)  # вне обеих зон, данные свежие
    cur = _current(client, instrument_id)
    assert cur["contexts_conflict"] is True
    assert cur["direction"] == "mixed"
    # обе зоны видны в списке контекстов
    dirs = {c["direction"] for c in cur["contexts"]}
    assert dirs == {"bull", "bear"}


def test_contexts_no_conflict_single_direction(client, db, seeded,
                                               instrument_id):
    """Одно направление активных контекстов → конфликта нет."""
    _make_live(db, instrument_id, 110.0)
    cur = _current(client, instrument_id)
    assert cur["contexts_conflict"] is False
    assert cur["direction"] == "bear"


# ------------------------------ L06: приоритет внимания ------------------------------

def test_attention_order_constant():
    """Порядок групп приоритета — единая константа, по убыванию внимания."""
    assert ATTENTION_ORDER == [
        "review", "price_in_zone", "eligible", "awaiting",
        "data_problem", "none",
    ]


def test_attention_review(client, db, seeded, instrument_id):
    """Зона-кандидат (status candidate) → «Требует проверки» — приоритет
    выше «Цена в зоне», даже при свежей котировке внутри HTF-зоны."""
    _make_live(db, instrument_id, 100.0)  # внутри зоны obs1 (95–105)
    _zone(db, instrument_id, Direction.BEAR, 120.0, 130.0,
          status=ZoneStatus.CANDIDATE, offset=3000)
    row = _overview_row(client, instrument_id)
    assert row["attention"] == "review"
    assert row["attention_reason"] == "Требует проверки"


def test_attention_price_in_zone(client, db, seeded, instrument_id):
    """Свежая котировка внутри HTF-зоны выбранного контекста → «Цена в зоне»
    (выше eligible)."""
    _make_live(db, instrument_id, 100.0)  # внутри 95–105
    row = _overview_row(client, instrument_id)
    assert row["attention"] == "price_in_zone"
    assert row["attention_reason"] == "Цена в зоне"


def test_attention_price_in_zone_not_on_stale(client, db, seeded,
                                              instrument_id):
    """Stale-котировка: «Цена в зоне» НЕ присваивается (защита как в
    _select_context) — ниже по приоритету сработает data_problem."""
    now = now_ms()
    db.insert_candles([make_candle(
        now - 30 * 60_000, 100, 101, 99, 100,
        timeframe="H1", instrument_id=instrument_id,
    )])
    db.set_quote(instrument_id, 100.0, now - 10 * 3_600_000)
    row = _overview_row(client, instrument_id)
    assert row["attention"] == "data_problem"
    assert row["data_state"]["state"] == "stale"


def test_attention_eligible(client, db, seeded, instrument_id):
    """Подходящие зоны есть (eligible_count > 0), цена вне HTF-зоны →
    «Есть подходящие зоны»."""
    from app.models_ltf import LtfEntryZone, LtfScenarioEntry
    ez = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG",
        direction=Direction.BEAR, lower=98.0, upper=102.0,
        formed_at=T0 + 10, confirmed_at=T0 + 20,
    ))
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        id=None, scenario_id=seeded["sc1"].id, entry_zone_id=ez.id,
        range_version=1, eligible=True, overlap="partial", state="fresh",
        reason="ok", added_at=T0 + 60, updated_at=T0 + 60,
    ))
    _make_live(db, instrument_id, 110.0)  # выше HTF-зоны 95–105
    row = _overview_row(client, instrument_id)
    assert row["eligible_count"] == 1
    assert row["attention"] == "eligible"
    assert row["attention_reason"] == "Есть подходящие зоны"


def test_attention_awaiting(client, db, instrument_id):
    """Наблюдение активно, сценария нет → «Ожидается структура»."""
    z1 = _zone(db, instrument_id, Direction.BEAR, 95.0, 105.0)
    _observation(db, instrument_id, z1, Direction.BEAR, "active", T0)
    _make_live(db, instrument_id, 110.0)  # вне зоны, данные свежие
    row = _overview_row(client, instrument_id)
    assert row["stage"] == "Ждём BOS/SMS"
    assert row["attention"] == "awaiting"
    assert row["attention_reason"] == "Ожидается структура"


def test_attention_data_problem(client, db, seeded, instrument_id):
    """Данных нет (data_pending), кандидатов/подходящих зон нет →
    «Проблема данных»."""
    row = _overview_row(client, instrument_id)  # без _make_live
    assert row["data_state"]["state"] == "data_pending"
    assert row["attention"] == "data_problem"
    assert row["attention_reason"] == "Проблема данных"


def test_attention_none(client, db, seeded, instrument_id):
    """Ничего из перечисленного: данные свежие, цена вне зоны, подходящих
    зон нет, структура есть (этап «Нет подходящих зон») → none."""
    _make_live(db, instrument_id, 110.0)
    row = _overview_row(client, instrument_id)
    assert row["stage"] == "Нет подходящих зон"
    assert row["eligible_count"] == 0
    assert row["attention"] == "none"
    assert row["attention_reason"] == "—"


# ------------------------------ стабильность снимка (бот) ------------------------------

def test_snapshot_fields_additive(client, db, seeded, instrument_id):
    """Новые поля — добавления; существующие ключи снимка и списка не
    переименованы и не изменили семантику (их читает Telegram-бот)."""
    _make_live(db, instrument_id, 110.0)
    cur = _current(client, instrument_id)
    for key in (
        "instrument", "price", "quote_at", "data_state", "stage", "direction",
        "contexts", "selected_context_id", "current_scenario",
        "scenario_waiting", "range", "eligible_entries", "counts",
        "state_version",
        # новые (L05)
        "selected_context_basis", "contexts_conflict",
    ):
        assert key in cur
    row = _overview_row(client, instrument_id)
    for key in (
        "instrument", "stage", "direction", "htf_context", "last_event_at",
        "eligible_count", "contexts_count", "data_state",
        # новые (L06)
        "attention", "attention_reason",
    ):
        assert key in row
