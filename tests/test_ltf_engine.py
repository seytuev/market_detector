"""§13: сквозные сценарии LtfEngine — BOS/SMS, диапазон, зоны, отмена,
replay (приёмка п.12, 15, 17–20)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.engine.ltf import LtfEngine
from app.models import Direction, Zone, ZoneStatus, ZoneType
from tests.conftest import H1_MS
from tests.test_ltf_breaks import (
    SERIES_B_HL,
    SERIES_G_CLOSES,
    SERIES_G_HL,
    SERIES_N2_CLOSES,
    SERIES_N2_HL,
    SERIES_N_CLOSES,
    SERIES_N_HL,
    _series,
)

T0 = 1_780_000_000_000

# Полный медвежий поток: восходящая структура → первичный BOS (idx14) →
# откат трогает FVG (idx18) → диапазон v1 и BSL-якорь (idx21) → новая опора,
# диапазон v2 (idx23) → касание BSL + обратный слом на одном закрытии (idx24)
SERIES_H_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11),
    (14.0, 12.9), (12.8, 11.2), (11.0, 10.0), (10.2, 9.2), (8.8, 7.8),
    (8.2, 7.9), (8.4, 8.0), (8.3, 7.95), (9.5, 8.1),
    (8.6, 7.7), (8.4, 7.6), (8.2, 7.7), (8.1, 7.75), (8.2, 7.7),
    (9.8, 7.8),
]
SERIES_H_CLOSES = {10: 13.2, 11: 11.5, 12: 10.5, 13: 9.4, 14: 7.9, 20: 7.5,
                   24: 9.7}

# п.18: пара LH→LL подтверждается ровно на закрытии слома (break-свеча idx20 —
# третья правая для LL1): диапазон и зоны готовы сразу → одно событие
SERIES_M_HL = [
    (10, 9.5), (11, 10), (12, 10.5), (13, 11), (12, 10.5), (11, 10), (12, 9),
    (13, 9.5), (14, 10), (15, 11),
    (13.8, 12.5), (13.2, 12.0), (13.6, 12.3), (13.9, 12.5), (13.4, 12.2),
    (13.0, 11.5), (12.5, 10.8), (11.8, 8.4), (11.2, 9.2), (11.5, 9.4),
    (10.8, 8.6),
]
SERIES_M_CLOSES = {10: 13.2, 11: 12.6, 12: 13.0, 13: 13.3, 14: 12.8, 15: 12.2,
                   16: 11.5, 17: 10.5, 18: 10.8, 19: 11.0, 20: 8.65}


# Продолжение серии H после отмены (idx24): новая медвежья нога —
# HH 9.9 (idx30), HL 7.7 (idx27), слом 7.7 закрытием idx34
SERIES_H2_HL = [
    (9.4, 8.4), (9.0, 8.1), (8.7, 7.7), (9.0, 8.2), (9.4, 8.6),
    (9.9, 9.0), (9.5, 8.7), (9.1, 8.4), (8.8, 8.1), (8.5, 7.5),
    (8.2, 7.4), (7.9, 7.1),
]
SERIES_H2_CLOSES = {**SERIES_H_CLOSES, 34: 7.6}


def _setup(db: Database, instrument_id: int, direction = Direction.BEAR):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=direction, timeframe="D1", lower=9.0, upper=10.0,
        formed_at=T0 - 10_000_000, confirmed_at=T0 - 9_000_000,
        status=ZoneStatus.ACTIVE,
    ))
    return zid


def _feed(db: Database, engine: LtfEngine, instrument_id: int, candles, upto: int):
    for c in candles[: upto + 1]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)


def _events(db: Database, obs_id: int):
    return sorted(db.list_ltf_events(observation_id=obs_id, limit=1000),
                  key=lambda e: (e.occurred_at, e.id))


def test_full_flow_bos_range_entries_cancellation(db: Database, cfg, instrument_id: int):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    zone = db.get_zone(zid)
    obs = engine.on_htf_zone_touched(instrument_id, zone, occurred_at=T0)
    # идемпотентность запуска от повторного HTF-события (§4)
    assert engine.on_htf_zone_touched(instrument_id, zone, T0 + 1).id == obs.id

    _feed(db, engine, instrument_id, candles, 14)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 1
    sc = scenarios[0]
    assert sc.trigger == "BOS" and sc.stage == "primary"
    assert sc.state == "range_pending"          # пары LH→LL ещё нет (§7)
    assert db.get_ltf_observation(obs.id).state == "active"
    bos = _events(db, obs.id)
    assert [e.kind for e in bos] == ["bos"]
    assert bos[0].payload["range_pending"] is True
    # причинное движение слома (§8.1) — в payload события для уведомления:
    # bear-нога от последнего high-pivot (HH 15.0, idx9) до свечи слома idx14
    mv = bos[0].payload["movement"]
    assert mv["start_price"] == 15.0
    assert mv["end_price"] == 9.0              # последний low-pivot до слома (idx6)
    assert mv["start_at"] == candles[9].open_time
    assert mv["end_at"] == candles[14].open_time
    assert mv["candles"] == 6                  # idx9..idx14 включительно
    assert mv["provenance_status"] == "ok"
    # зоны причинного движения: FVG/OB/BSL; FVG, сформированный ПОСЛЕ слома,
    # не добавлен (§8.1)
    zones = db.list_ltf_entry_zones(instrument_id=instrument_id)
    bounds = {(z.type, z.lower, z.upper) for z in zones}
    assert len(zones) == 4                   # 3 FVG движения + BSL-пик
    assert ("FVG", 8.2, 9.2) not in bounds   # сформирован после слома
    assert ("BSL", 15.0, 15.0) in bounds

    # откат idx18 трогает FVG [8.8;10.0] до готовности диапазона (п.10/п.12)
    _feed(db, engine, instrument_id, candles, 18)
    fvg_b = [z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
             if (z.type, z.lower, z.upper) == ("FVG", 8.8, 10.0)][0]
    assert db.get_ltf_entry_zone(fvg_b.id).validity == "tested"
    assert [e.kind for e in _events(db, obs.id)] == ["bos", "touch"]

    # idx21: LH 9.5 подтверждён, но связанной пары ещё нет — последний LL (idx14)
    # старше LH; диапазон ждёт нового подтверждённого LL (§7, п.6)
    _feed(db, engine, instrument_id, candles, 21)
    assert db.get_ltf_scenario(sc.id).state == "range_pending"
    assert [e.kind for e in _events(db, obs.id)] == ["bos", "touch"]

    # idx23: подтверждённый LL2 7.6 → связанная пара LH(9.5)→LL(7.6):
    # диапазон v1, сценарий в monitoring_entries, якорь пары — BSL-кандидат (§7),
    # одно дополнение entries_ready (§11.2)
    _feed(db, engine, instrument_id, candles, 23)
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "monitoring_entries"
    rng = db.get_current_ltf_range(sc.id)
    assert (rng.version, rng.lower, rng.upper) == (1, 7.6, 9.5)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["bos", "touch", "entries_ready"]
    ready = evs[-1]
    # ТЗ §3: FVG [8.8;10.0], протестированный откатом idx18 на ~58% (< 90%),
    # допустим к повторному выбору — входит в готовый список вместе с якорем
    assert [e["type"] for e in ready.payload["entries"]] == ["FVG", "BSL"]
    assert ready.payload["entries"][0]["lower"] == 8.8   # повторный выбор (ТЗ §3)
    assert ready.payload["entries"][1]["lower"] == 9.5   # якорь пары (§7)
    assert ready.payload["range"]["version"] == 1

    # idx24: касание BSL 9.5 (снятие без возврата — failed) и обратный слом
    # на одном закрытии: журнал хранит оба факта, «готов вход» не публикуется (п.19)
    _feed(db, engine, instrument_id, candles, 24)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == [
        "bos", "touch", "entries_ready", "touch", "sweep_failed", "cancellation",
    ]
    cancel = evs[-1]
    assert cancel.payload["reason"] == "reverse_bos"
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "cancelled" and sc.cancellation_reason == "reverse_bos"
    # родитель валиден — наблюдение ждёт нового подтверждения (§6.5, п.15)
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"
    # контр-HTF входов не создано: единственный сценарий, направление bear
    assert len(db.list_ltf_scenarios(observation_id=obs.id)) == 1
    assert db.list_ltf_scenarios(observation_id=obs.id)[0].direction == Direction.BEAR
    # liquidity-тест BSL 9.5: High>K, Close>K → failed
    tests = db.list_ltf_liquidity_tests(scenario_id=sc.id)
    assert len(tests) == 1 and tests[0].state == "failed"
    assert tests[0].close_price == 9.7
    # обратный слом записан в журнал структурных событий
    struct = db.list_ltf_structure_events(sc.id)
    assert {(e.kind, e.direction) for e in struct} == {
        ("BOS", Direction.BEAR), ("BOS", Direction.BULL),
    }


def test_waiting_observation_not_blocked_by_reverse_break(db: Database, cfg, instrument_id: int):
    """§6.5: встречный слом отменяет АКТИВНЫЙ сценарий; ждущему наблюдению
    отменять нечего — бычий BOS в окне не блокирует открытие по медвежьему."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_N_HL, SERIES_N_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    db.insert_candles(candles)
    engine.replay_observation(obs.id)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 1
    sc = scenarios[0]
    assert sc.direction == Direction.BEAR and sc.trigger == "BOS"
    bear_events = [e for e in db.list_ltf_structure_events(sc.id)
                   if e.direction == Direction.BEAR]
    assert [(e.kind, e.stage, e.break_level) for e in bear_events] == [
        ("BOS", "primary", 11.5),
    ]
    assert bear_events[0].break_candle_open_time == T0 + 25 * H1_MS
    bos = [e for e in _events(db, obs.id) if e.kind == "bos"]
    assert len(bos) == 1 and bos[0].payload["break_level"] == 11.5


def test_reopen_after_cancellation(db: Database, cfg, instrument_id: int):
    """§6.5: после отмены наблюдение ждёт нового подтверждения — новый
    медвежий слом открывает НОВЫЙ сценарий (исторический обратный слом
    не должен обрезать opening-скан)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL + SERIES_H2_HL, SERIES_H2_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 24)
    sc1 = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert db.get_ltf_scenario(sc1.id).state == "cancelled"
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"

    _feed(db, engine, instrument_id, candles, 34)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 2
    sc2 = scenarios[1]
    assert sc2.direction == Direction.BEAR and sc2.trigger == "BOS"
    bear_events = [e for e in db.list_ltf_structure_events(sc2.id)
                   if e.direction == Direction.BEAR]
    assert [(e.kind, e.stage, e.break_level) for e in bear_events] == [
        ("BOS", "primary", 7.7),
    ]
    assert bear_events[0].break_candle_open_time == T0 + 34 * H1_MS
    assert [e.kind for e in _events(db, obs.id)].count("bos") == 2


def test_cancellation_only_from_post_trigger_reverse_break(db: Database, cfg,
                                                           instrument_id: int):
    """§6.5: обратный слом, случившийся ДО открытия сценария (bull BOS 13.2
    idx15, secondary 13.5 idx21), отменой не является; отменяет слом ПОСЛЕ
    триггера (bull BOS 12.9 на idx39)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_N2_HL, SERIES_N2_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 25)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.trigger == "BOS" and sc.direction == Direction.BEAR

    # исторические бычьи сломы (idx15, idx21) раньше триггера — не отмена
    _feed(db, engine, instrument_id, candles, 33)
    assert db.get_ltf_scenario(sc.id).state != "cancelled"
    assert db.get_ltf_observation(obs.id).state == "active"

    # bull BOS 12.9 на idx39 — после триггера: отмена, наблюдение ждёт
    _feed(db, engine, instrument_id, candles, 39)
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "cancelled" and sc.cancellation_reason == "reverse_bos"
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"
    cancel = [e for e in _events(db, obs.id) if e.kind == "cancellation"][-1]
    assert cancel.payload["reason"] == "reverse_bos"
    assert cancel.payload["break_level"] == 12.9
    assert cancel.payload["break_candle_open_time"] == T0 + 39 * H1_MS
    # обратный слом записан в журнал структурных событий сценария
    struct = db.list_ltf_structure_events(sc.id)
    assert ("BOS", Direction.BULL, 12.9) in {
        (e.kind, e.direction, e.break_level) for e in struct
    }


def test_sms_opens_scenario_standalone(db: Database, cfg, instrument_id: int):
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_B_HL, {18: 10.9}, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 1
    sc = scenarios[0]
    assert sc.trigger == "SMS"                   # SMS самостоятелен (§6.3, п.3)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["sms"]
    assert evs[0].payload["range_pending"] is True
    se = db.list_ltf_structure_events(sc.id)[0]
    assert se.kind == "SMS" and se.break_level == 11


def test_bos_with_range_ready_same_close_single_event(db: Database, cfg,
                                                      instrument_id: int):
    """п.18: диапазон и зоны готовы на закрытии слома — одно объединённое
    событие вместо bos + range_ready/entries_ready."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_M_HL, SERIES_M_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == ["bos"]
    combined = evs[0]
    assert combined.payload["range_pending"] is False
    assert (combined.payload["range"]["lower"],
            combined.payload["range"]["upper"]) == (8.4, 13.9)   # LH→LL
    # якорь LH=13.9 — свежая BSL-зона в Premium, в том же сообщении
    assert [e["lower"] for e in combined.payload["entries"]] == [13.9]
    # диапазон готов сразу: сценарий в monitoring_entries без range_pending-фазы
    assert sc.state == "monitoring_entries"
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1]


def test_replay_is_idempotent(db: Database, cfg, instrument_id: int):
    """п.20: повторный replay не дублирует события/зоны/диапазоны и не
    воскрешает tested-зоны."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 23)

    def counts():
        return {
            "events": len(db.list_ltf_events(observation_id=obs.id, limit=1000)),
            "zones": len(db.list_ltf_entry_zones(instrument_id=instrument_id)),
            "pivots": len(db.list_ltf_pivots(instrument_id)),
            "ranges": len(db.list_ltf_ranges(1)),
            "struct": len(db.list_ltf_structure_events(1)),
            "tests": len(db.list_ltf_liquidity_tests(scenario_id=1)),
        }

    # снимок состояний привязок: replay не должен ничего переписать
    def entry_states():
        return sorted(
            (e.range_version, e.entry_zone_id, e.state)
            for e in db.list_ltf_scenario_entries(1)
        )

    before = counts()
    states = entry_states()
    res = engine.replay_observation(obs.id)
    assert res.events == []                # все события поглощены дедупом
    assert counts() == before
    assert entry_states() == states        # tested-зоны не воскресли

    # догоняем live и replay после отмены — тоже без дублей
    _feed(db, engine, instrument_id, candles, 24)
    before = counts()
    res = engine.replay_observation(obs.id)
    assert res.events == []
    assert counts() == before
    # восстановленные события replay помечены delayed и не удвоились
    assert len(db.list_ltf_events(observation_id=obs.id, limit=1000)) == 6


def _role_log_rows(db: Database) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM ltf_pivot_role_log").fetchone()[0]


def test_replay_does_not_churn_role_log(db: Database, cfg, instrument_id: int):
    """§13/п.20: replay не переписывает промежуточные роли — role_log растёт
    только от реальных пересмотров на голове, не от повторных прогонов."""
    engine = LtfEngine(db, cfg)
    # SERIES_B: pivot idx12 по ходу истории HL → internal_low (§6.3) —
    # без head-only записи каждый replay писал бы оба перехода заново
    candles = _series(SERIES_B_HL, {18: 10.9}, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    roles_final = {p.id: p.role for p in db.list_ltf_pivots(instrument_id)}

    n0 = _role_log_rows(db)
    engine.replay_observation(obs.id)
    assert _role_log_rows(db) == n0          # роли сошлись на голове — без записей
    assert {p.id: p.role for p in db.list_ltf_pivots(instrument_id)} == roles_final
    engine.replay_observation(obs.id)
    assert _role_log_rows(db) == n0


def _fabricate_cancellation(db: Database, sc, candles, idx: int) -> None:
    """Вариант истории: сценарий отменён обратным сломом со свечи idx
    (как после replay с иными ролями — прод-инцидент 09-18)."""
    from app.models_ltf import LtfStructureEvent
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=Direction.BULL, break_level=12.6,
        break_candle_open_time=candles[idx].open_time,
        occurred_at=candles[idx].close_time, detected_at=candles[idx].close_time,
        level_key="bos:primary:LH:test:12.6",
    ))
    db.update_ltf_scenario(sc.id, state="cancelled",
                           cancellation_reason="reverse_bos",
                           cancelled_at=candles[idx].close_time)
    db.update_ltf_observation(sc.observation_id, state="waiting_structure")


def test_replay_skips_opening_inside_cancelled_window(db: Database, cfg,
                                                      instrument_id: int):
    """Повторный replay не открывает дубль сценария внутри окна [триггер;
    отмена) существующего — даже с другим level_key (иная опора после
    пересчёта ролей, прод-инцидент 09-18)."""
    engine = LtfEngine(db, cfg)
    # SERIES_G: две медвежьи ноги (primary 9 @idx12, primary 8.4 @idx34)
    candles = _series(SERIES_G_HL, SERIES_G_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    # имитация варианта истории: продолжение 8.4 первый прогон не записал
    # (иные роли), а отмена пришла ПОЗЖЕ второй ноги (idx40 > idx34)
    db.conn.execute(
        "DELETE FROM ltf_structure_event WHERE scenario_id=? AND break_level=8.4",
        (sc.id,),
    )
    _fabricate_cancellation(db, sc, candles, 40)
    n_events = len(db.list_ltf_structure_events(sc.id))

    engine.replay_observation(obs.id)
    # окно [idx12; idx40) накрывает слом idx34 — дубль-сценарий не открывается,
    # и событие 8.4 не воскресает
    assert len(db.list_ltf_scenarios(observation_id=obs.id)) == 1
    assert len(db.list_ltf_structure_events(sc.id)) == n_events


def test_replay_opens_leg_after_cancelled_window(db: Database, cfg,
                                                 instrument_id: int):
    """Зеркальный контроль: первичный слом ПОСЛЕ конца окна отмены открывает
    новый сценарий — окно не должно блокировать легитимное переоткрытие (§6.5)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_G_HL, SERIES_G_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    db.conn.execute(
        "DELETE FROM ltf_structure_event WHERE scenario_id=? AND break_level=8.4",
        (sc.id,),
    )
    # отмена ДО второй ноги (idx30 < idx34) — окно закрыто к моменту слома
    _fabricate_cancellation(db, sc, candles, 30)

    engine.replay_observation(obs.id)
    scenarios = db.list_ltf_scenarios(observation_id=obs.id)
    assert len(scenarios) == 2
    sc2 = scenarios[1]
    assert sc2.direction == Direction.BEAR and sc2.trigger == "BOS"
    ev2 = [e for e in db.list_ltf_structure_events(sc2.id)
           if e.direction == Direction.BEAR]
    assert (ev2[0].kind, ev2[0].stage, ev2[0].break_level) == ("BOS", "primary", 8.4)


def test_replay_does_not_add_range_versions(db: Database, cfg, instrument_id: int):
    """§13/п.20: replay по активному сценарию не дописывает исторические
    геометрии диапазона как новые версии (текущая версия на голове новее)."""
    from app.models_ltf import LtfRange

    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_M_HL, SERIES_M_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, len(candles) - 1)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "monitoring_entries"
    v1 = db.get_current_ltf_range(sc.id)
    # имитация расхождения головы с историей: позднее в БД осела иная геометрия
    db.insert_ltf_range(LtfRange(
        id=None, scenario_id=sc.id, version=v1.version + 1,
        lower=v1.lower, upper=v1.upper - 0.5, mid=(v1.lower + v1.upper - 0.5) / 2,
        available_at=v1.available_at + H1_MS, prev_version_id=v1.id,
    ))
    n0 = len(db.list_ltf_ranges(sc.id))
    engine.replay_observation(obs.id)
    assert len(db.list_ltf_ranges(sc.id)) == n0
    engine.replay_observation(obs.id)
    assert len(db.list_ltf_ranges(sc.id)) == n0


def test_htf_parent_invalidation(db: Database, cfg, instrument_id: int):
    """§4/п.15: достоверная инвалидация родителя → HTF_INVALIDATED."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 16)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    assert sc.state == "range_pending"
    # OB конвертирован в Breaker — подтверждённый пробой в обратную сторону
    db.update_zone(zid, status=ZoneStatus.CONVERTED,
                   end_reason="converted_to_breaker (§6/§15.6)")
    assert engine.check_parent_validity(db.get_zone(zid)) is True
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "cancelled"
    assert sc.cancellation_reason == "HTF_INVALIDATED"
    assert db.get_ltf_observation(obs.id).state == "closed_by_parent"
    evs = _events(db, obs.id)
    assert evs[-1].kind == "cancellation"
    assert evs[-1].payload["reason"] == "HTF_INVALIDATED"


def test_parent_90pct_test_not_invalidation(db: Database, cfg, instrument_id: int):
    """п.17: тест родителя на 90% не закрывает наблюдение (is_valid=true)."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 16)
    # зона ACTIVE, но был глубокий тест 92% — наблюдение живо
    vid = db.open_visit_for(zid, 1)
    assert vid is None
    from app.models import Visit
    vid = db.open_visit(Visit(id=None, zone_id=zid, cycle_id=1, entered_at=T0))
    db.update_visit_depth(vid, 0.92)
    assert engine.check_parent_validity(db.get_zone(zid)) is False
    assert db.get_ltf_observation(obs.id).state == "active"
    assert db.list_ltf_scenarios(observation_id=obs.id)[0].state == "range_pending"


def test_manual_close_keeps_parent(db: Database, cfg, instrument_id: int):
    """§12: ручное завершение сценария не удаляет HTF-зону; наблюдение ждёт."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 16)
    sc = db.list_ltf_scenarios(observation_id=obs.id)[0]
    engine.close_scenario_manually(sc.id)
    sc = db.get_ltf_scenario(sc.id)
    assert sc.state == "closed" and sc.cancellation_reason == "manual"
    assert db.get_ltf_observation(obs.id).state == "waiting_structure"
    assert db.get_zone(zid).status == ZoneStatus.ACTIVE
    assert _events(db, obs.id)[-1].payload["reason"] == "manual"
    # повторное закрытие — no-op (дедуп)
    engine.close_scenario_manually(sc.id)
    assert len([e for e in _events(db, obs.id) if e.kind == "cancellation"]) == 1


# --------------------------------------------------------------------- #
# ТЗ «LTF Current Setup»: дедуп якорей/версий, guard отмены, ltf_entry_types
# --------------------------------------------------------------------- #

def _mk_scenario(db: Database, instrument_id: int):
    """Наблюдение + активный медвежий сценарий без свечной истории —
    для прямых вызовов _update_range/_build_entries с явными pivots."""
    from app.models_ltf import LtfObservation, LtfScenario
    zid = _setup(db, instrument_id)
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="range_pending",
    ))
    return obs, sc


def _mkp(kind: str, price: float, role: str, idx: int, pid: int,
         confirmed_idx: int | None = None):
    from app.engine.ltf import PivotCandidate
    t = T0 + idx * H1_MS
    conf = T0 + (confirmed_idx if confirmed_idx is not None else idx + 3) * H1_MS
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=t, candle_open_time=t,
        confirmed_at=conf, left=3, right=3, role=role, pivot_id=pid,
        state="confirmed",
    )


_PAIR = lambda: [_mkp("high", 15.0, "LH", 1, 11), _mkp("low", 9.0, "LL", 2, 12)]


def test_range_anchor_level_not_duplicated(db: Database, cfg, instrument_id: int):
    """п.09: смена диапазона с прежним опорным pivot не создаёт новый
    BSL/SSL; повторная обработка — тоже. Дедуп по evidence['pivot_ref'],
    а не по цене (§11): другой pivot с той же ценой — другая зона."""
    from app.engine.ltf import LtfTickResult
    engine = LtfEngine(db, cfg)
    obs, sc = _mk_scenario(db, instrument_id)
    res = LtfTickResult()
    now = T0 + 10 * H1_MS
    engine._update_range(sc, [], now, res, avail=_PAIR())
    bsl = [z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
           if z.type == "BSL"]
    assert len(bsl) == 1 and bsl[0].lower == 15.0
    assert bsl[0].evidence["pivot_ref"] == 11
    # повторная обработка тех же опор — ни версии, ни нового уровня
    assert engine._update_range(sc, [], now, res, avail=_PAIR()) is None
    # новый LL при прежнем LH → диапазон v2, но новый BSL не создаётся
    ps2 = _PAIR() + [_mkp("low", 8.0, "LL", 6, 13, confirmed_idx=9)]
    r2 = engine._update_range(sc, [], T0 + 12 * H1_MS, res, avail=ps2)
    assert r2 is not None and r2.version == 2
    bsl2 = [z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
            if z.type == "BSL"]
    assert [z.id for z in bsl2] == [bsl[0].id]
    # ДРУГОЙ pivot с той же ценой 15.0 — отдельная зона (цена ≠ идентичность)
    ps3 = ps2 + [
        _mkp("high", 15.0, "LH", 8, 21, confirmed_idx=11),
        _mkp("low", 7.0, "LL", 9, 22, confirmed_idx=12),
    ]
    r3 = engine._update_range(sc, [], T0 + 13 * H1_MS, res, avail=ps3)
    assert r3 is not None and r3.version == 3
    bsl3 = [z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
            if z.type == "BSL"]
    assert len(bsl3) == 2
    assert {z.evidence["pivot_ref"] for z in bsl3} == {11, 21}
    assert {z.lower for z in bsl3} == {15.0}   # цены совпадают, происхождения — нет


def test_swept_bsl_not_resurrected_on_range_update(db: Database, cfg,
                                                   instrument_id: int):
    """п.17: BSL с подтверждённым sweep не воскресает при новой версии
    диапазона — reason swept_level, в кандидаты уведомлений не попадает."""
    from app.engine.ltf import LtfTickResult
    from app.models_ltf import LtfLiquidityTest
    engine = LtfEngine(db, cfg)
    obs, sc = _mk_scenario(db, instrument_id)
    res = LtfTickResult()
    now = T0 + 10 * H1_MS
    engine._update_range(sc, [], now, res, avail=_PAIR())
    bsl = [z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
           if z.type == "BSL"][0]
    e1 = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == bsl.id][0]
    assert (e1.reason, e1.state) == ("ok", "fresh")
    # подтверждённое снятие уровня (touch + возврат закрытия)
    db.update_ltf_entry_zone(bsl.id, validity="tested", first_test_at=T0 + 100)
    db.insert_ltf_liquidity_test(LtfLiquidityTest(
        id=None, entry_zone_id=bsl.id, scenario_id=sc.id, level=15.0,
        touch_at=T0 + 100, candle_open_time=T0 + 100, state="confirmed",
        close_price=14.5, sweep_at=T0 + 200, resolved_at=T0 + 200,
    ))
    # новая версия диапазона: снятый уровень не возвращается в fresh
    ps2 = _PAIR() + [_mkp("low", 8.0, "LL", 6, 13, confirmed_idx=9)]
    engine._update_range(sc, [], T0 + 12 * H1_MS, res, avail=ps2)
    e2 = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == bsl.id and e.range_version == 2][0]
    assert (e2.reason, e2.state) == ("swept_level", "tested")
    assert all(z.id != bsl.id for _, z, _ in engine._entry_candidates(
        db.get_ltf_scenario(sc.id)))
    # и повторное включение типа его не воскрешает
    cfg.ltf_entry_types = "OB,FVG,SSL"
    engine.reclassify_active_entries()
    cfg.ltf_entry_types = "OB,FVG,BSL,SSL"
    engine.reclassify_active_entries()
    e2b = [e for e in db.list_ltf_scenario_entries(sc.id)
           if e.entry_zone_id == bsl.id and e.range_version == 2][0]
    assert (e2b.reason, e2b.state) == ("swept_level", "tested")


def test_cancelled_scenario_gets_no_ranges_or_entries(db: Database, cfg,
                                                      instrument_id: int):
    """п.04: отменённый/закрытый сценарий не получает новые версии диапазона
    и привязки Entry Zones при следующих свечах (явный guard); история
    версий при этом сохраняется."""
    from app.engine.ltf import LtfTickResult
    from app.engine.ltf.breaks import StructureEventDraft
    engine = LtfEngine(db, cfg)
    obs, sc = _mk_scenario(db, instrument_id)
    res = LtfTickResult()
    now = T0 + 10 * H1_MS
    engine._update_range(sc, [], now, res, avail=_PAIR())
    assert db.get_current_ltf_range(sc.id).version == 1
    n_entries = len(db.list_ltf_scenario_entries(sc.id))
    db.update_ltf_scenario(sc.id, state="cancelled",
                           cancellation_reason="reverse_bos",
                           cancelled_at=now, updated_at=now)
    sc2 = db.get_ltf_scenario(sc.id)
    # новые «свечи»/опоры: версии и привязки не создаются
    ps2 = _PAIR() + [_mkp("low", 8.0, "LL", 6, 13, confirmed_idx=9)]
    assert engine._update_range(sc2, [], T0 + 12 * H1_MS, res,
                                avail=ps2) is None
    ev = StructureEventDraft(
        kind="BOS", stage="secondary", direction=Direction.BEAR,
        break_level=8.0, break_candle_open_time=T0 + 12 * H1_MS,
        occurred_at=T0 + 13 * H1_MS - 1, detected_at=T0 + 13 * H1_MS - 1,
        level_key="bos:secondary:test",
    )
    engine._build_entries(obs, sc2, ev, 1, ps2, [], T0 + 12 * H1_MS, res)
    assert [r.version for r in db.list_ltf_ranges(sc.id)] == [1]
    assert len(db.list_ltf_scenario_entries(sc.id)) == n_entries
    assert db.list_ltf_movements(sc.id) == []


def test_disabled_entry_type_reclassify(db: Database, cfg, instrument_id: int):
    """п.14: отключение FVG в ltf_entry_types исключает его из пригодности
    и кандидатов новых уведомлений, история сохраняется; повторное
    включение возвращает зону."""
    from app.engine.ltf import LtfTickResult
    from app.models_ltf import LtfEntryZone, LtfScenarioEntry
    engine = LtfEngine(db, cfg)
    obs, sc = _mk_scenario(db, instrument_id)
    res = LtfTickResult()
    now = T0 + 10 * H1_MS
    # диапазон v1 [9;15], Premium [12;15]: FVG [13;14] и OB [12.5;14.5] — ok
    engine._update_range(sc, [], now, res, avail=_PAIR())
    fvg = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="FVG",
        direction=Direction.BEAR, lower=13.0, upper=14.0, formed_at=T0,
        confirmed_at=T0 + 100,
    ))
    ob = db.insert_ltf_entry_zone(LtfEntryZone(
        id=None, instrument_id=instrument_id, type="OB",
        direction=Direction.BEAR, lower=12.5, upper=14.5, formed_at=T0,
        confirmed_at=T0 + 100,
    ))
    for ez in (fvg, ob):
        db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=ez.id, range_version=1,
            eligible=True, overlap="full", state="fresh",
        ))
    # якорь пары — тоже кандидат (BSL 15.0 на границе Premium)
    assert {z.type for _, z, _ in engine._entry_candidates(sc)} == {"FVG", "OB", "BSL"}

    cfg.ltf_entry_types = "OB,BSL,SSL"
    changed = engine.reclassify_active_entries(now_ms=T0 + 11 * H1_MS)
    assert changed == {sc.id: 2}
    fe = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == fvg.id][0]
    assert (fe.reason, fe.state) == ("type_disabled", "out_of_range")
    oe = [e for e in db.list_ltf_scenario_entries(sc.id)
          if e.entry_zone_id == ob.id][0]
    assert (oe.reason, oe.state) == ("ok", "fresh")
    # из кандидатов уведомлений FVG исключён, строка и зона на месте
    assert {z.type for _, z, _ in engine._entry_candidates(
        db.get_ltf_scenario(sc.id))} == {"OB", "BSL"}
    assert db.get_ltf_entry_zone(fvg.id) is not None
    # повторное включение возвращает FVG
    cfg.ltf_entry_types = "FVG,OB,BSL,SSL"
    engine.reclassify_active_entries(now_ms=T0 + 12 * H1_MS)
    fe2 = [e for e in db.list_ltf_scenario_entries(sc.id)
           if e.entry_zone_id == fvg.id][0]
    assert (fe2.reason, fe2.state) == ("ok", "fresh")


# --- F01/A01: происхождение событий (processing_mode/detection_lag_ms) ------


def _feed_until_bos(db: Database, engine: LtfEngine, instrument_id: int,
                    candles, lag_ms: int):
    """Серия H до первичного BOS (idx14); свеча слома обрабатывается
    с заданным лагом обнаружения после её закрытия."""
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    _feed(db, engine, instrument_id, candles, 13)
    c = candles[14]
    db.insert_candles([c])
    result = engine.process_h1_close(instrument_id,
                                     now_ms=c.close_time + lag_ms)
    return obs, result


@pytest.mark.parametrize("lag_ms", [1_000, 60_000])
def test_a01_bos_within_grace_stays_live(db: Database, cfg, instrument_id: int,
                                         lag_ms: int):
    """A01: BOS, обнаруженный с лагом внутри grace-окна (1 с / 60 с при
    дефолтных 900 с), — live: delayed=False. До фикса любой лаг опроса
    (c.close_time < now) делал событие delayed и оно навсегда подавлялось."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    obs, result = _feed_until_bos(db, engine, instrument_id, candles, lag_ms)
    bos = [e for e in result.events if e.kind == "bos"]
    assert len(bos) == 1
    assert bos[0].processing_mode == "live"
    assert bos[0].detection_lag_ms == lag_ms
    assert bos[0].delayed is False
    # событие доступно ретраю доставки (pending фильтрует только delayed)
    assert [e.id for e in db.pending_ltf_events()] == [bos[0].id]
    # повторный прогон тех же свечей (рестарт) — дублей нет (§11.5)
    again = engine.process_h1_close(instrument_id,
                                    now_ms=candles[14].close_time + lag_ms)
    assert again.events == []
    assert len(db.list_ltf_events(observation_id=obs.id, limit=1000)) == 1


def test_a01_catchup_backlog_suppressed_then_live(db: Database, cfg,
                                                  instrument_id: int):
    """A01: лаг дольше grace-окна — catchup (delayed=True, не доставляется,
    в pending не попадает); следующая живая свеча снова даёт live-события."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    obs, result = _feed_until_bos(db, engine, instrument_id, candles,
                                  lag_ms=901_000)  # > 900 с grace
    bos = [e for e in result.events if e.kind == "bos"]
    assert len(bos) == 1
    assert bos[0].processing_mode == "catchup"
    assert bos[0].detection_lag_ms == 901_000
    assert bos[0].delayed is True
    assert db.pending_ltf_events() == []

    # последующие живые закрытия (lag=0) — события снова live: откат idx18
    # трогает FVG причинного движения (см. test_full_flow)
    for c in candles[15:19]:
        db.insert_candles([c])
        engine.process_h1_close(instrument_id, now_ms=c.close_time)
    touch = [e for e in _events(db, obs.id) if e.kind == "touch"]
    assert len(touch) == 1
    assert touch[0].processing_mode == "live"
    assert touch[0].delayed is False
    assert [e.id for e in db.pending_ltf_events()] == [touch[0].id]


def test_a01_replay_marks_replay_and_stays_suppressed(db: Database, cfg,
                                                      instrument_id: int):
    """A01: replay_observation — processing_mode='replay' (delayed=True):
    события пишутся в журнал, но в доставку и pending не попадают."""
    engine = LtfEngine(db, cfg)
    candles = _series(SERIES_H_HL, SERIES_H_CLOSES, instrument_id)
    zid = _setup(db, instrument_id)
    obs = engine.on_htf_zone_touched(instrument_id, db.get_zone(zid), T0)
    db.insert_candles(candles)
    engine.replay_observation(obs.id)
    evs = _events(db, obs.id)
    assert [e.kind for e in evs] == [
        "bos", "touch", "entries_ready", "touch", "sweep_failed", "cancellation",
    ]
    assert all(e.processing_mode == "replay" for e in evs)
    assert all(e.delayed for e in evs)
    assert all(e.detection_lag_ms >= 0 for e in evs)
    assert db.pending_ltf_events() == []


def test_a01_ltf_event_migration_adds_origin_columns(tmp_path):
    """F01: миграция существующей БД добавляет processing_mode/
    detection_lag_ms; строки до миграции читаются с 'unknown'/0."""
    schema = Path("app/schema.sql").read_text(encoding="utf-8")
    # схема «до миграции»: без новых колонок ltf_event
    old_schema = "\n".join(
        ln for ln in schema.replace("\r\n", "\n").split("\n")
        if "processing_mode" not in ln and "detection_lag_ms" not in ln
        and "F01/A01" not in ln
    )
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(old_schema)
    # строка, записанная старым кодом (только старые колонки)
    raw.execute(
        """INSERT INTO ltf_event
           (observation_id, scenario_id, kind, payload, occurred_at,
            detected_at, dedupe_key, delivered, delayed)
           VALUES (1, NULL, 'bos', '{}', 1000, 1000, 'bos:legacy:1', 0, 1)"""
    )
    raw.commit()
    raw.close()

    mdb = Database(str(path))
    try:
        cols = {
            r["name"]
            for r in mdb.conn.execute("PRAGMA table_info(ltf_event)").fetchall()
        }
        assert {"processing_mode", "detection_lag_ms"} <= cols
        ev = mdb.list_ltf_events(limit=10)[0]
        assert ev.processing_mode == "unknown"
        assert ev.detection_lag_ms == 0
        assert ev.delayed is True           # старое значение сохранено
    finally:
        mdb.close()
