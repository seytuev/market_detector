"""ТЗ 07.10.2026 §13.5, T20–T21: единые критерии допуска для таблицы,
счётчиков, Telegram и графика; снятые SSL/BSL — только в истории."""
from __future__ import annotations

from app.config import Settings
from app.db import Database
from app.models import (
    Direction,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import (
    LtfEntryZone,
    LtfLiquidityTest,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
)
from app.services.overview import observation_chart_layers


def test_taken_level_not_relevant_for_htf():
    """T21: снятый SSL/BSL не актуален — рабочие выборки (таблица/счётчики/
    Telegram/график) строятся на is_currently_relevant."""
    db = Database(":memory:")
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zid = db.insert_zone(
        Zone(None, iid, ZoneType.SSL, Direction.BULL, "D1",
             lower=2600.15, upper=2600.15, formed_at=now_ms() - 10_000,
             confirmed_at=now_ms() - 9_000, status=ZoneStatus.TAKEN,
             market_validity="invalid", entry_eligible=False,
             created_at=now_ms())
    )
    zone = db.get_zone(zid)
    assert not zone.is_currently_relevant()
    assert not zone.entry_eligible


def test_swept_level_excluded_from_chart_working_layer():
    """T20/T21: уровень с подтверждённым снятием не в рабочем слое entries
    графика — только в исключённых (история)."""
    db = Database(":memory:")
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zid = db.insert_zone(
        Zone(None, iid, ZoneType.OB, Direction.BEAR, "D1",
             lower=3000.0, upper=3100.0, formed_at=now_ms() - 10_000,
             confirmed_at=now_ms() - 9_000, status=ZoneStatus.ACTIVE,
             created_at=now_ms())
    )
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=iid, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active",
        activated_at=now_ms() - 8_000,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        None, obs.id, Direction.BEAR, "BOS", "primary",
        state="monitoring_entries", created_at=now_ms(), updated_at=now_ms(),
    ))
    rng_id = db.insert_ltf_range(LtfRange(
        None, sc.id, 1, lower=2800.0, upper=3200.0, mid=3000.0,
        available_at=now_ms(),
    ))
    rng = db.get_current_ltf_range(sc.id)
    swept = db.insert_ltf_entry_zone(LtfEntryZone(
        None, iid, "BSL", Direction.BEAR, 3150.0, 3150.0,
        formed_at=now_ms() - 5_000,
    ))
    # движок при подтверждении снятия пишет привязку invalid/swept_level
    db.upsert_ltf_scenario_entry(LtfScenarioEntry(
        None, sc.id, swept.id, rng.version, eligible=False, overlap="full",
        state="invalid", reason="swept_level",
    ))
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        None, swept.id, sc.id, 3150.0, touch_at=now_ms() - 1_000,
        candle_open_time=now_ms() - 1_000, state="confirmed",
    ))
    layers = observation_chart_layers(db, Settings(), obs.id)
    ids = {e["entry_zone_id"] for e in layers["entries"]}
    assert swept.id not in ids, "снятый уровень не должен быть рабочим входом"


def test_disabled_entry_type_not_delivered():
    """T20: отключённый тип зоны не даёт входовых уведомлений и не виден
    в рабочем списке (фильтр ltf_entry_types единый для расчёта/API/графика)."""
    from app.config import DetectorConfig
    from app.engine.ltf.eligibility import REASON_TYPE_DISABLED, evaluate_entry

    db = Database(":memory:")
    iid = db.upsert_instrument(
        Instrument(None, "ETH", "binance", "spot", "ETHUSDT", "USDT")
    )
    zone = LtfEntryZone(None, iid, "FVG", Direction.BEAR, 100.0, 105.0,
                        formed_at=now_ms())
    cfg = DetectorConfig()
    cfg.ltf_entry_types = "OB,BSL,SSL"  # FVG выключен
    out = evaluate_entry(zone, Direction.BEAR, cfg, None)
    assert out.reason == REASON_TYPE_DISABLED

    # и снятый уровень — терминальный reason, не воскресает (T03/T21)
    from app.models_ltf import LtfLiquidityTest

    lvl = LtfEntryZone(None, iid, "BSL", Direction.BEAR, 3150.0, 3150.0,
                       formed_at=now_ms())
    lvl.id = 7
    test = LtfLiquidityTest(None, 7, 1, 3150.0, touch_at=now_ms(),
                            candle_open_time=now_ms(), state="confirmed")
    out2 = evaluate_entry(lvl, Direction.BEAR, DetectorConfig(), None,
                          liquidity_tests=[test])
    assert out2.reason == "swept_level"
