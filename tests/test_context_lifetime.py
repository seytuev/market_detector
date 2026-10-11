"""Этап 6: время жизни контекста — 60 дней от опоры ноги (правила v1 §8).

R24: старые таймеры (168ч ожидания, 14д автоархивации) не закрывают
торговый контекст. R39: 60 дней истекли, обратного слома нет → продление
(horizon_extended), контекст активен. Якорь leg_extreme/leg_start (конфиг,
provisional §12.1), обновление якоря при новом экстремуме ноги, пробел
данных → data_quality_note, автоархивация — техническая очистка.
"""
from __future__ import annotations

from dataclasses import replace

from app.config import DetectorConfig
from app.engine.ltf.engine import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfObservation, LtfScenario
from app.services.htf_context import (
    _apply_leg_anchor,
    _check_horizon,
    _confirm_episode,
    horizon_ms,
    latest_leg,
    load_episode,
    record_interaction,
    save_leg,
)
from tests.conftest import H1_MS, make_candle, make_h1_candles

H = 3_600_000
DAY = 24 * H
T = 1_780_012_800_000


def _zone(db, iid, direction, serial=0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    return db.get_zone(zid)


def _confirmed_episode(db, cfg, iid, direction=Direction.BEAR,
                       protected_open=T, extreme_open=T + 10 * H):
    """Подтверждённый эпизод (context_active) с ногой: исходная опора на
    protected_open, последний экстремум на extreme_open."""
    zone = _zone(db, iid, direction)
    ep = record_interaction(db, cfg, iid, zone, T)
    _confirm_episode(db, ep, 0, T + H, [], [], None, direction)
    ep = load_episode(db, ep["id"])
    kind = "hh" if direction == Direction.BEAR else "ll"
    price = 110.0 if direction == Direction.BEAR else 90.0
    db.conn.execute(
        """UPDATE htf_context_episode
           SET protected_price=?, protected_candle_open=?, protected_kind=?
           WHERE id=?""",
        (price, protected_open, kind, ep["id"]),
    )
    db.conn.commit()
    low, high = (90.0, 110.0)
    save_leg(db, ep["id"], low, high, 100.0, "confirmed", T + H,
             extreme_open, True, 0.01)
    return load_episode(db, ep["id"])


def _fill_h1(db, iid, start_open, end_close):
    """Непрерывная серия закрытых H1 от start_open до end_close."""
    n = int((end_close + 1 - start_open) // H1_MS)
    db.insert_candles(make_h1_candles([(10.0, 10.5, 9.5, 10.0)] * n,
                                      start_open, instrument_id=iid))


# --------------------------------------------------------------------- #
# Якорь ноги: формула, обновление
# --------------------------------------------------------------------- #

def test_anchor_leg_extreme_default(db, cfg, instrument_id):
    ep = _confirmed_episode(db, cfg, instrument_id,
                            protected_open=T, extreme_open=T + 10 * H)
    _apply_leg_anchor(db, cfg, ep["id"], T + 2 * H)
    ep = load_episode(db, ep["id"])
    # leg_extreme (provisional default): время последнего экстремума ноги
    assert ep["leg_anchor_at"] == T + 10 * H
    assert ep["leg_anchor_available_at"] == T + 2 * H
    assert ep["expires_at"] == T + 10 * H + horizon_ms(cfg)
    assert horizon_ms(cfg) == 60 * DAY


def test_anchor_leg_start_config(db, instrument_id):
    cfg = DetectorConfig(htf_context_leg_anchor="leg_start")
    ep = _confirmed_episode(db, cfg, instrument_id,
                            protected_open=T, extreme_open=T + 10 * H)
    _apply_leg_anchor(db, cfg, ep["id"], T + 2 * H)
    ep = load_episode(db, ep["id"])
    # leg_start: начало ноги (исходная опора), не последняя активация
    assert ep["leg_anchor_at"] == T
    assert ep["expires_at"] == T + horizon_ms(cfg)


def test_anchor_updates_on_new_leg_extreme(db, cfg, instrument_id):
    ep = _confirmed_episode(db, cfg, instrument_id, extreme_open=T + 10 * H)
    # разморозим ногу — замороженная по правилу не растёт
    db.conn.execute("UPDATE htf_context_leg SET frozen=0 WHERE episode_id=?",
                    (ep["id"],))
    db.conn.commit()
    _apply_leg_anchor(db, cfg, ep["id"], T + 2 * H)
    assert load_episode(db, ep["id"])["leg_anchor_at"] == T + 10 * H
    # продолжение движения: новый наблюдаемый экстремум — новая версия ноги
    save_leg(db, ep["id"], 90.0, 115.0, 102.5, "confirmed", T + 21 * H,
             T + 20 * H, False, 0.01)
    assert latest_leg(db, ep["id"])["version"] == 2
    _apply_leg_anchor(db, cfg, ep["id"], T + 21 * H)
    ep = load_episode(db, ep["id"])
    assert ep["leg_anchor_at"] == T + 20 * H
    assert ep["leg_anchor_available_at"] == T + 21 * H
    assert ep["expires_at"] == T + 20 * H + horizon_ms(cfg)


# --------------------------------------------------------------------- #
# R39/R24: контрольная дата — продление, не закрытие (оба направления)
# --------------------------------------------------------------------- #

def _check_expired_context(db, cfg, iid, direction):
    ep = _confirmed_episode(db, cfg, iid, direction=direction,
                            protected_open=T, extreme_open=T)
    _apply_leg_anchor(db, cfg, ep["id"], T + H)
    ep = load_episode(db, ep["id"])
    assert ep["expires_at"] == T + horizon_ms(cfg)
    # свечи покрывают весь срок; контрольная дата прошла, обратного слома нет
    end = T + horizon_ms(cfg) + DAY
    _fill_h1(db, iid, T, end)
    candle = make_candle(end - DAY, 10, 10.5, 9.5, 10, timeframe="H1",
                         instrument_id=iid)
    candle = replace(candle, close_time=end)
    _check_horizon(db, cfg, iid, candle)
    return load_episode(db, ep["id"])


def test_r39_bear_context_extended_not_closed(db, cfg, instrument_id):
    ep = _check_expired_context(db, cfg, instrument_id, Direction.BEAR)
    assert ep["state_v2"] == "context_active"
    assert ep["state"] == "confirmed"
    assert ep["horizon_extended"] == 1
    assert ep["expires_at"] == T + 2 * horizon_ms(cfg)


def test_r39_bull_context_extended_not_closed(db, cfg, instrument_id):
    ep = _check_expired_context(db, cfg, instrument_id, Direction.BULL)
    assert ep["state_v2"] == "context_active"
    assert ep["horizon_extended"] == 1
    assert ep["expires_at"] == T + 2 * horizon_ms(cfg)


def test_r24_expiry_beyond_168h_keeps_confirmed_context(db, cfg, instrument_id):
    # 168 часов ожидания прошли давно (контекст подтверждён), горизонт не
    # достигнут — эпизод жив и не «expired»
    ep = _confirmed_episode(db, cfg, instrument_id)
    _apply_leg_anchor(db, cfg, ep["id"], T + H)
    end = T + 200 * H1_MS
    _fill_h1(db, instrument_id, T, end)
    candle = replace(
        make_candle(end - H1_MS, 10, 10.5, 9.5, 10, timeframe="H1",
                    instrument_id=instrument_id),
        close_time=end,
    )
    _check_horizon(db, cfg, instrument_id, candle)
    ep = load_episode(db, ep["id"])
    assert ep["state"] == "confirmed"
    assert ep["state_v2"] == "context_active"
    assert ep["horizon_extended"] == 0


# --------------------------------------------------------------------- #
# Пробел данных → пометка качества, контекст сохранён
# --------------------------------------------------------------------- #

def test_gap_keeps_context_with_data_quality_note(db, cfg, instrument_id):
    ep = _confirmed_episode(db, cfg, instrument_id, extreme_open=T)
    _apply_leg_anchor(db, cfg, ep["id"], T + H)
    end = T + horizon_ms(cfg) + DAY
    # данные только за первые 10 дней окна — полнота проверки слома не
    # подтверждена
    _fill_h1(db, instrument_id, T, T + 10 * DAY)
    candle = replace(
        make_candle(end - DAY, 10, 10.5, 9.5, 10, timeframe="H1",
                    instrument_id=instrument_id),
        close_time=end,
    )
    _check_horizon(db, cfg, instrument_id, candle)
    ep = load_episode(db, ep["id"])
    assert ep["state_v2"] == "context_active"
    assert ep["horizon_extended"] == 1
    assert ep["data_quality_note"]


# --------------------------------------------------------------------- #
# Автоархивация — техническая очистка (R24)
# --------------------------------------------------------------------- #

def test_archive_keeps_active_scenario_and_activation(db, cfg, instrument_id):
    engine = LtfEngine(db, cfg)
    zone = _zone(db, instrument_id, Direction.BEAR)
    obs = engine.on_htf_zone_touched(instrument_id, zone, T)
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=T, updated_at=T,
    ))
    stale_at = T + 30 * DAY
    # активный сценарий — автоархивация не отменяет его и не архивирует
    assert engine.archive_stale_observations(stale_at) == []
    assert db.get_ltf_scenario(sc.id).state == "monitoring_entries"
    assert db.get_ltf_observation(obs.id).state != "closed_stale"
    # сценарий завершился вручную — архивация наблюдения техническая:
    # активация и эпизод не трогаются
    engine.close_scenario_manually(sc.id)
    db.update_ltf_observation(obs.id, updated_at=T)  # имитация давней неактивности
    assert engine.archive_stale_observations(stale_at) == [obs.id]
    assert db.get_open_zone_activation(zone.id, zone.cycle_id) is not None
    # новый рыночный факт восстанавливает наблюдение без новой активации
    before = len(db.list_zone_activations(zone.id))
    obs2 = engine.on_htf_zone_touched(instrument_id, zone, stale_at + H)
    assert obs2 is not None and obs2.id == obs.id
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"
    assert len(db.list_zone_activations(zone.id)) == before


def test_config_fields_present_and_valid():
    cfg = DetectorConfig()
    assert cfg.htf_context_horizon_days == 60
    assert cfg.htf_context_leg_anchor == "leg_extreme"
    assert DetectorConfig(htf_context_leg_anchor="leg_start").htf_context_leg_anchor == "leg_start"
