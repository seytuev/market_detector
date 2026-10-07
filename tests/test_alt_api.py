"""HTTP API окна «Альткоины» (ТЗ 07.10.2026 §16–§18): ранжирование §17,
фильтры, деталь с опорами/целями/K, статус прогона, ручной пересчёт,
математика ширины (+100%/−50%) и текст неположительного K."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models_alt import (
    AltAsset,
    AltCandle,
    AltEntryOpportunity,
    AltEvent,
    AltFrozenRange,
    AltInstrumentSource,
    AltRangeCandidate,
    AltRun,
    AltSetup,
    AltState,
    AltStructureEvent,
)
from app.web.api import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DAY = 86_400_000
T0 = 1_780_000_000_000  # граница суток (кратна DAY)


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


def _asset(db: Database, cmc_id: int, symbol: str, rank: int) -> AltAsset:
    return db.upsert_alt_asset(AltAsset(
        id=None, cmc_id=cmc_id, symbol=symbol, name=symbol, cmc_rank=rank,
        mapping_status="mapped",
    ))


def _source(db: Database, asset_id: int, venue: str = "bybit") -> AltInstrumentSource:
    return db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset_id, venue=venue,
        symbol="AAAUSDT", quote="USDT",
        earliest_available_ms=T0, last_closed_ms=T0 + 400 * DAY,
        history_scope="full", source_version=1,
    ))


def _candles(db: Database, source_id: int, ath: float = 10.0, low: float = 1.5):
    """ATH на первой свече, минимум после него, затем боковик."""
    rows = [AltCandle(source_id=source_id, open_time=T0, open=ath, high=ath,
                      low=ath * 0.99, close=ath * 0.995)]
    for i in range(1, 401):
        ot = T0 + i * DAY
        if i == 1:  # свеча минимума после ATH
            rows.append(AltCandle(source_id=source_id, open_time=ot, open=2.0,
                                  high=2.1, low=low, close=1.8))
        else:
            px = 2.0 + (i % 10) * 0.01
            rows.append(AltCandle(source_id=source_id, open_time=ot, open=px,
                                  high=px * 1.02, low=px * 0.98, close=px))
    db.insert_alt_candles(rows)


def _candidate(db: Database, asset_id: int, state: str,
               lower: float = 1.0, upper: float = 2.0,
               n_days: int = 120) -> AltRangeCandidate:
    return db.insert_alt_range_candidate(AltRangeCandidate(
        id=None, asset_id=asset_id, origin_key=f"{T0}:{T0 + DAY}",
        start_anchor_open_time=T0 + DAY, rebound_anchor_open_time=T0 + 5 * DAY,
        lower=lower, upper=upper, width=upper - lower, mid=(lower + upper) / 2,
        n_days=n_days, version=1, state=state, metrics_json="{}",
        first_seen_ms=T0, updated_ms=T0,
    ))


def _setup(db: Database, asset: AltAsset, source: AltInstrumentSource,
           state: str, *, flags: dict | None = None,
           cancel_price: float | None = 0.0, cancel_reachable: bool = True,
           breakout: bool = False, terminated_ms: int | None = None,
           targets: list | None = None) -> AltSetup:
    cand = _candidate(db, asset.id, AltState.MATURE.value)
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=cand.id, lower=cand.lower, upper=cand.upper,
        width=cand.width, mid=cand.mid,
        start_anchor_open_time=cand.start_anchor_open_time,
        rebound_anchor_open_time=cand.rebound_anchor_open_time,
        included_candles=101, mature_at_ms=T0 + 101 * DAY,
        classifier_version="v1", range_version=1,
    ))
    setup, created = db.insert_alt_setup(AltSetup(
        id=None, asset_id=asset.id, source_id=source.id, range_id=frozen.id,
        state=state, flags_json=json.dumps(flags or {}),
        targets_json=json.dumps(targets or []),
        cancel_price=cancel_price, cancel_reachable=cancel_reachable,
        breakout_close=(2.5 if breakout else None),
        breakout_closed_at=(T0 + 200 * DAY if breakout else None),
        retest_deadline_ms=(T0 + 214 * DAY if breakout else None),
        created_ms=T0, updated_ms=T0, terminated_ms=terminated_ms,
    ))
    assert created
    return setup


@pytest.fixture()
def seeded(db, client):
    """Сетапы во всех стадиях §17 + terminal + forming-кандидат + прогон."""
    assets = {}
    sources = {}
    for i, (cmc, sym, rank) in enumerate([
            (101, "NEW", 20), (102, "RET", 30), (103, "CNF", 40),
            (104, "MAT", 50), (105, "FRM", 60), (106, "REV", 70),
            (107, "HIS", 80), (108, "NOD", 90)]):
        a = _asset(db, cmc, sym, rank)
        assets[sym] = a
        sources[sym] = _source(db, a.id)
        _candles(db, sources[sym].id)

    s_new = _setup(db, assets["NEW"], sources["NEW"],
                   AltState.ACTIVE_CONFIRMED.value,
                   flags={"structure_event": True, "entry_a_confirmed": True,
                          "target_snapshot": {"bases": ["bos"], "as_of": T0}},
                   targets=[{"tp": 1, "price": 3.0}])
    s_ret = _setup(db, assets["RET"], sources["RET"],
                   AltState.ACTIVE_CONFIRMED.value,
                   flags={"breakout_confirmed": True,
                          "structure_event": True}, breakout=True,
                   targets=[{"tp": 1, "price": 3.0}])
    s_cnf = _setup(db, assets["CNF"], sources["CNF"],
                   AltState.ACTIVE_CONFIRMED.value,
                   flags={"structure_event": True},
                   targets=[{"tp": 1, "price": 3.0}])
    s_mat = _setup(db, assets["MAT"], sources["MAT"], AltState.MATURE.value)
    s_rev = _setup(db, assets["REV"], sources["REV"],
                   AltState.REVIEW_REQUIRED.value)
    s_his = _setup(db, assets["HIS"], sources["HIS"],
                   AltState.CANCELLED.value, terminated_ms=T0 + 300 * DAY)
    # структурная опора для расстояния «ожидание BOS» у CNF (у MAT — нет)
    db.insert_alt_structure_event(AltStructureEvent(
        id=None, setup_id=s_cnf.id, kind="BOS", level_price=2.1,
        close_price=2.2, candle_open_time=T0 + 150 * DAY, anchors_json="{}",
    ))
    _candidate(db, assets["FRM"].id, AltState.FORMING.value, n_days=80)

    # последний успешный прогон + новый вход A, детектированный в его окне
    run = db.insert_alt_run(AltRun(
        id=None, started_ms=T0 + 400 * DAY, as_of_ms=T0 + 400 * DAY,
        status="ok", finished_ms=T0 + 400 * DAY + 60_000,
        processed=7, errors=0,
        summary_json=json.dumps({"per_asset": [
            {"asset_id": assets["NOD"].id, "symbol": "NOD", "cmc_rank": 90,
             "status": "error", "reason": "adapter timeout"},
        ]}),
    ))
    ev, created = db.insert_alt_event(AltEvent(
        id=None, setup_id=s_new.id, event_type="entry_a",
        source_event_id=f"entry_a:{s_new.id}", payload_json="{}",
        event_time_ms=T0 + 399 * DAY, detected_at_ms=T0 + 400 * DAY + 30_000,
        created_ms=T0 + 400 * DAY + 30_000,
    ))
    assert created
    return {
        "assets": assets, "run": run,
        "new": s_new, "ret": s_ret, "cnf": s_cnf, "mat": s_mat,
        "rev": s_rev, "his": s_his,
    }


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

def test_alt_auth_required(client):
    assert client.get("/api/alt/setups").status_code == 401
    assert client.get("/api/alt/setup/1").status_code == 401
    assert client.get("/api/alt/run-status").status_code == 401
    assert client.post("/api/alt/recalc").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/api/alt/setups", headers=bad).status_code == 401
    assert client.post("/api/alt/recalc", headers=bad).status_code == 401


def test_recalc_foreign_origin_rejected(client):
    headers = {**AUTH, "Origin": "https://evil.example"}
    assert client.post("/api/alt/recalc", headers=headers).status_code == 403


def test_recalc_no_runner_503(client):
    r = client.post("/api/alt/recalc", headers=AUTH)
    assert r.status_code == 503


def test_recalc_with_runner(db, settings):
    calls = []

    class FakeRunner:
        async def run_daily(self, trigger: str = "schedule"):
            calls.append(trigger)
            return {"status": "ok"}

    c = TestClient(create_app(db, settings, alt_runner=FakeRunner()))
    r = c.post("/api/alt/recalc", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["trigger"] == "manual"
    # фоновая задача отрабатывает в цикле TestClient при следующем запросе
    c.get("/api/alt/run-status", headers=AUTH)
    assert calls == ["manual"]


# ---------------------------------------------------------------------------
# Ранжирование (§17)
# ---------------------------------------------------------------------------

def test_ranking_stage_order(client, seeded):
    rows = client.get("/api/alt/setups", headers=AUTH).json()["rows"]
    syms = [r["asset"]["symbol"] for r in rows]
    # 1) новый вход run → 2) ожидание ретеста → 3) прочие подтверждённые →
    # 4) зрелые → 5) формирующиеся; review/история — вне «подходящих»
    assert syms == ["NEW", "RET", "CNF", "MAT", "FRM"]
    stages = {r["asset"]["symbol"]: r["stage"] for r in rows}
    assert stages["NEW"] == 1 and stages["RET"] == 2 and stages["CNF"] == 3
    assert stages["MAT"] == 4 and stages["FRM"] == 5


def test_null_distance_after_known(client, seeded):
    rows = client.get("/api/alt/setups?bucket=mature", headers=AUTH).json()["rows"]
    assert [r["asset"]["symbol"] for r in rows] == ["MAT"]
    # MAT: активная опора неизвестна → distance null; CNF (bucket eligible)
    # имеет структурную опору → distance посчитано и не равно 0
    assert rows[0]["distance_pct"] is None
    eligible = client.get("/api/alt/setups", headers=AUTH).json()["rows"]
    cnf = next(r for r in eligible if r["asset"]["symbol"] == "CNF")
    assert cnf["distance_pct"] is not None and cnf["distance_pct"] > 0
    # RET: цена внутри [M,U] боковика 1.96..2.05 → расстояние ретеста 0
    ret = next(r for r in eligible if r["asset"]["symbol"] == "RET")
    assert ret["distance_pct"] == 0.0


def test_history_bucket_not_mixed(client, seeded):
    default = client.get("/api/alt/setups", headers=AUTH).json()["rows"]
    assert "HIS" not in [r["asset"]["symbol"] for r in default]
    history = client.get("/api/alt/setups?bucket=history", headers=AUTH).json()["rows"]
    assert [r["asset"]["symbol"] for r in history] == ["HIS"]
    assert history[0]["terminal"] is True


def test_review_and_nodata_bucket(client, seeded):
    review = client.get("/api/alt/setups?bucket=review", headers=AUTH).json()["rows"]
    syms = {r["asset"]["symbol"] for r in review}
    assert "REV" in syms
    # NOD: ошибка источника в последнем run → «нет данных», причина видна
    nod = next(r for r in review if r["asset"]["symbol"] == "NOD")
    assert nod["reason"] == "adapter timeout"


def test_secondary_filters(client, seeded):
    rows = client.get("/api/alt/setups?rank_max=50", headers=AUTH).json()["rows"]
    assert {r["asset"]["symbol"] for r in rows} == {"NEW", "RET", "CNF", "MAT"}
    rows = client.get("/api/alt/setups?venue=binance", headers=AUTH).json()["rows"]
    assert rows == []
    rows = client.get("/api/alt/setups?structure=breakout", headers=AUTH).json()["rows"]
    assert [r["asset"]["symbol"] for r in rows] == ["RET"]
    rows = client.get("/api/alt/setups?bucket=all&age_min=100", headers=AUTH).json()
    assert all(r["age_days"] is None or r["age_days"] >= 100
               for r in rows["rows"])
    bad = client.get("/api/alt/setups?bucket=nope", headers=AUTH)
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# Математика таблицы (§16)
# ---------------------------------------------------------------------------

def test_width_pct_both_signs(client, seeded):
    # L=1, U=2 → вверх +100%, снижение −50% (не одинаковые проценты)
    rows = client.get("/api/alt/setups?bucket=mature", headers=AUTH).json()["rows"]
    mat = rows[0]
    assert mat["range"]["lower"] == 1.0 and mat["range"]["upper"] == 2.0
    assert mat["width_up_pct"] == pytest.approx(100.0)
    assert mat["width_down_pct"] == pytest.approx(50.0)


def test_k_nonpositive_text(client, seeded):
    # K = 2L − U = 0 → недостижим, текст ТЗ дословно, сетап не обрезан
    rows = client.get("/api/alt/setups?bucket=mature", headers=AUTH).json()["rows"]
    cancel = rows[0]["cancel"]
    assert cancel["price"] == 0.0
    assert cancel["reachable"] is False
    assert cancel["nonpositive_text"] == (
        "По выбранной формуле ценовой уровень отмены неположительный"
    )
    assert cancel["mode"] == "wick_on_closed_d1"
    assert "проектная настройка" in cancel["mode_note"]


def test_k_positive_reachable(client, db, settings):
    a = _asset(db, 201, "POS", 15)
    src = _source(db, a.id)
    _candles(db, src.id)
    cand = _candidate(db, a.id, AltState.MATURE.value, lower=2.0, upper=3.0)
    frozen = db.insert_alt_frozen_range(AltFrozenRange(
        id=None, range_id=cand.id, lower=2.0, upper=3.0, width=1.0, mid=2.5,
        start_anchor_open_time=cand.start_anchor_open_time,
        rebound_anchor_open_time=cand.rebound_anchor_open_time,
        included_candles=101, mature_at_ms=T0 + 101 * DAY,
    ))
    setup, _ = db.insert_alt_setup(AltSetup(
        id=None, asset_id=a.id, source_id=src.id, range_id=frozen.id,
        state=AltState.MATURE.value, cancel_price=1.0, cancel_reachable=True,
        created_ms=T0, updated_ms=T0,
    ))
    c = TestClient(create_app(db, settings))
    detail = c.get(f"/api/alt/setup/{setup.id}", headers=AUTH).json()
    assert detail["cancel"]["reachable"] is True
    assert detail["cancel"]["nonpositive_text"] is None


# ---------------------------------------------------------------------------
# Деталь (график + «Почему найдено»)
# ---------------------------------------------------------------------------

def test_detail_shape(client, seeded):
    sid = seeded["ret"].id
    d = client.get(f"/api/alt/setup/{sid}", headers=AUTH).json()
    assert d["setup_id"] == sid
    # опоры с временем доступности (правило 3+3)
    assert d["anchors"]["start"]["open_time"] == T0 + DAY
    assert d["anchors"]["start"]["available_at_ms"] > d["anchors"]["start"]["open_time"]
    # frozen range, цели, K, breakout, deadline
    assert d["frozen_range"]["lower"] == 1.0 and d["frozen_range"]["upper"] == 2.0
    assert d["targets"][0]["tp"] == 1 and d["targets"][0]["price"] == 3.0
    assert d["cancel"]["price"] == 0.0
    assert d["breakout"]["retest_deadline_ms"] == T0 + 214 * DAY
    # свечи: предшествующее падение от ATH + диапазон, серия одного источника
    assert d["candles"]
    assert d["candles"][0]["open_time"] <= d["ath"]["ath_open_time"]
    # ATH из сохранённых свечей того же source_id
    assert d["ath"]["ath_price"] == 10.0
    assert d["ath"]["p_min"] == 1.5
    # версии и формулы для панели «Почему найдено»
    assert d["versions"]["rule"] and d["versions"]["classifier"] == "v1"
    assert "2L − U" in d["formulas"]["cancel"]
    assert client.get("/api/alt/setup/9999", headers=AUTH).status_code == 404


def test_candidate_detail(client, seeded):
    rows = client.get("/api/alt/setups?bucket=forming", headers=AUTH).json()["rows"]
    cid = rows[0]["candidate_id"]
    d = client.get(f"/api/alt/candidate/{cid}", headers=AUTH).json()
    assert d["kind"] == "candidate"
    assert d["range"]["lower"] == 1.0
    assert d["candles"] and d["anchors"]["start"]
    assert d["targets"] == [] and d["cancel"] is None
    assert client.get("/api/alt/candidate/9999", headers=AUTH).status_code == 404


# ---------------------------------------------------------------------------
# Статус прогона (§18)
# ---------------------------------------------------------------------------

def test_run_status(client, seeded):
    st = client.get("/api/alt/run-status", headers=AUTH).json()
    assert st["running"] is False
    assert st["last_run"]["status"] == "ok"
    assert st["last_run"]["processed"] == 7
    assert st["last_run"]["as_of_ms"] == T0 + 400 * DAY
    assert st["last_run"]["skipped"][0]["reason"] == "adapter timeout"
    assert st["next_run_ms"] > 0 and st["job_time_msk"] == "03:15"


def test_run_status_empty(client):
    st = client.get("/api/alt/run-status", headers=AUTH).json()
    assert st["last_run"] is None and st["universe"] is None
    assert st["next_run_ms"] > 0


def test_detail_keeps_history_and_links_confirmation(client, db, seeded):
    """Новые поля совместимы: старые массивы на месте, связь точная, не по цене."""
    sid = seeded["cnf"].id
    candle = T0 + 150 * DAY
    source = f"bos:{sid}:{candle}"
    event, created = db.insert_alt_event(AltEvent(
        id=None, setup_id=sid, event_type="bos_confirmed",
        source_event_id=source, payload_json=json.dumps({"level_price": 9.9}),
        event_time_ms=candle + DAY, detected_at_ms=candle + DAY,
        created_ms=candle + DAY,
    ))
    assert created
    db.update_alt_setup(sid, confirmation_event_id=event.id)
    detail = client.get(f"/api/alt/setup/{sid}", headers=AUTH).json()
    assert any(row["event_type"] == "bos_confirmed" for row in detail["events"])
    assert detail["events"][0]["source_event_id"]
    assert detail["confirmation"]["structure_link"]["status"] == "exact"
    assert detail["confirmation"]["structure_link"]["structure_event_id"]
    assert detail["structure_events"]
    assert detail["candle_history"]["loaded_from_ms"]
    assert detail["candle_history"]["truncated"] is False
    assert "не вся" in detail["candle_history"]["note"] or "не является" in detail["candle_history"]["note"] or "фрагмент" in detail["candle_history"]["note"]

    # Цена в payload другая: связь держится на source_event_id, не на цене.
    assert detail["confirmation"]["payload"]["level_price"] == 9.9
    linked = next(
        row for row in detail["structure_events"]
        if row["id"] == detail["confirmation"]["structure_link"]["structure_event_id"]
    )
    assert linked["level_price"] != 9.9

    db.update_alt_setup(sid, confirmation_event_id=None)
    other, _ = db.insert_alt_event(AltEvent(
        id=None, setup_id=sid, event_type="sms_confirmed",
        source_event_id=f"sms:{sid}:{candle + DAY}",
        payload_json="{}", event_time_ms=candle + 2 * DAY,
        detected_at_ms=candle + 2 * DAY, created_ms=candle + 2 * DAY,
    ))
    db.update_alt_setup(sid, confirmation_event_id=other.id)
    unknown = client.get(f"/api/alt/setup/{sid}", headers=AUTH).json()
    assert unknown["confirmation"]["structure_link"]["status"] == "unknown"
    assert len(unknown["events"]) >= len(detail["events"])


def test_venues_survive_empty_filter_and_last_event(client, seeded):
    empty = client.get(
        "/api/alt/setups?bucket=eligible&venue=no-such-venue", headers=AUTH
    ).json()
    assert empty["rows"] == []
    assert "bybit" in empty["venues"]
    fresh = client.get("/api/alt/setups?bucket=new_entries", headers=AUTH).json()
    assert fresh["rows"][0]["last_event"]["event_type"] == "entry_a"
    assert fresh["rows"][0]["last_event"]["label_ru"] == "Вход A"
