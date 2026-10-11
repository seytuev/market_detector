"""Этап 10: уведомления и UI по §9 (правила v1).

R26: события текущей активации (OB 90% в активирующем визите) проходят
отбор доставки; исторически отработанные — подавляются. R27: зона вне PD
не блокируется. §9: смысловые context_transition на переходах машины —
эмиссия, дедуп, delayed при catchup. R46: текст исправления.
"""
from __future__ import annotations

import json

from app.engine.ltf.engine import LtfEngine, LtfTickResult
from app.models import (
    Direction,
    Event,
    EventKind,
    Instrument,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import LtfScenario
from app.notify.ltf_queue import LtfDispatcher
from app.notify.queue import EventDispatcher, EventView
from app.notify.telegram import LogSender
from app.services.htf_context import (
    _confirm_episode,
    context_state_v2,
    note_source_outcome,
    open_episode,
    record_interaction,
)
from tests.conftest import H1_MS
from tests.test_audit_fixes import make_entry, make_scenario
from tests.test_ltf_notify import RecSender

H = 3_600_000
D = 24 * H
T = 1_780_012_800_000


def _zone(db, iid, direction, serial=0, **fields):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.OB, direction=direction,
        timeframe="D1", lower=90, upper=110, formed_at=T - H + serial,
        confirmed_at=T - 1, status=ZoneStatus.ACTIVE,
    ))
    if fields:
        db.update_zone(zid, **fields)
    return db.get_zone(zid)


def _view(db, zone, kind=EventKind.TOUCH):
    ins = db.get_instrument(zone.instrument_id)
    ev = Event(
        id=None, zone_id=zone.id, cycle_id=zone.cycle_id, kind=kind,
        occurred_at=now_ms(), detected_at=now_ms(), price=95.0,
    )
    return EventView(event=ev, zone=zone, instrument=ins)


# --------------------------------------------------------------------- #
# R26: текущая активация не подавляется глубиной; историческая — да
# --------------------------------------------------------------------- #

def test_r26_current_activation_not_suppressed_by_depth(db, cfg, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, entry_eligible=False)
    disp = EventDispatcher(db, cfg, LogSender())
    # нет открытой активации — отработанность ДО неё: подавление, как раньше
    assert disp._entry_stale(_view(db, zone)) is True
    # открытая активация (90% накоплено в текущем визите) — не подавляется
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, now_ms())
    assert db.get_open_zone_activation(zone.id, zone.cycle_id) is not None
    assert disp._entry_stale(_view(db, zone)) is False


# --------------------------------------------------------------------- #
# §9: context_transition на переходах машины (эмиссия, дедуп, delayed)
# --------------------------------------------------------------------- #

def _touch(db, cfg, iid, zone, at, evidence=None):
    """Контакт через движок: наблюдение + активация + источник эпизода."""
    return LtfEngine(db, cfg).on_htf_zone_touched(
        iid, zone, at, evidence=evidence,
    )


def test_opposing_pending_emits_message_and_dedupes(db, cfg, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    engine = LtfEngine(db, cfg)
    engine.on_htf_zone_touched(instrument_id, bear, T)
    ep_a = open_episode(db, instrument_id)
    _confirm_episode(db, ep_a, 0, T + H, [], [], None, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, bull, T + 2 * H)
    events = [
        e for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
    ]
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["fact"] == "opposing_pending"
    assert "Основной контекст вниз" in ev.payload["text"]
    assert "встречный HTF вверх" in ev.payload["text"]
    # повторный контакт той же зоны — второго сообщения нет
    engine.on_htf_zone_touched(instrument_id, bull, T + 2 * H)
    assert len([
        e for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
    ]) == 1
    # переход произошёл давно (T — далёкое прошлое) — delayed, не в доставку
    assert ev.delayed is True
    assert ev.processing_mode == "catchup"


def test_opposing_confirmed_message(db, cfg, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    engine = LtfEngine(db, cfg)
    engine.on_htf_zone_touched(instrument_id, bear, T)
    ep_a = open_episode(db, instrument_id)
    _confirm_episode(db, ep_a, 0, T + H, [], [], None, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, bull, T + 2 * H)
    ep_b = open_episode(db, instrument_id)
    _confirm_episode(db, ep_b, 0, T + 3 * H, [], [], None, Direction.BULL,
                     cfg=cfg)
    facts = {
        e.payload["fact"]
        for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
    }
    assert "opposing_confirmed" in facts
    ev = next(
        e for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
        and e.payload["fact"] == "opposing_confirmed"
    )
    assert "Встречный bullish-сценарий подтверждён" in ev.payload["text"]
    assert "bearish-контекст сохранён" in ev.payload["text"]


def test_breached_waiting_and_resumed_messages(db, cfg, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, serial=0)
    bull = _zone(db, instrument_id, Direction.BULL, serial=1)
    engine = LtfEngine(db, cfg)
    engine.on_htf_zone_touched(instrument_id, bear, T)
    ep_a = open_episode(db, instrument_id)
    _confirm_episode(db, ep_a, 0, T + H, [], [], None, Direction.BEAR)
    engine.on_htf_zone_touched(instrument_id, bull, T + 2 * H)
    ep_b = open_episode(db, instrument_id)
    _confirm_episode(db, ep_b, 0, T + 3 * H, [], [], None, Direction.BULL,
                     cfg=cfg)
    # факт (а): проход B своим ТФ — ожидание обратного слома
    db.update_zone(bull.id, market_validity="invalid",
                   evidence={"invalidated_at": T + 4 * H})
    note_source_outcome(db, db.get_zone(bull.id), T + 4 * H, cfg=cfg)
    ev = next(
        e for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
        and e.payload["fact"] == "opposing_source_breached_waiting_return"
    )
    assert "пройден закрытием D1" in ev.payload["text"]
    assert "возврата вниз-сценария ожидается обратный BOS/SMS H1" in ev.payload["text"]
    # факт (б): обратный слом — возврат подтверждён
    from app.services.htf_context import _maybe_resume, _save_episode_evidence, load_episode
    ep = load_episode(db, ep_b["id"])
    evidence = json.loads(ep["evidence"] or "{}")
    evidence["reverse_break"] = {"occurred_at": T + 5 * H,
                                 "available_at": T + 5 * H,
                                 "kind": "BOS", "direction": "bear"}
    _save_episode_evidence(db, ep_b["id"], evidence)
    _maybe_resume(db, ep_b["id"], cfg=cfg)
    ev2 = next(
        e for e in db.list_ltf_events_for_instrument(instrument_id)
        if e.kind == "context_transition"
        and e.payload["fact"] == "context_resumed"
    )
    assert "восстановлен" in ev2.payload["text"]
    proj = context_state_v2(db, instrument_id)
    assert proj["state_v2"] == "context_resumed"


def test_fvg_filled_current_activation_message(db, cfg, instrument_id):
    _, sc = make_scenario(db, instrument_id)
    z, e = make_entry(db, instrument_id, sc)
    db.update_ltf_entry_zone(z.id, type="FVG")
    from app.models import Candle
    candle = Candle(instrument_id=instrument_id, timeframe="H1", open_time=T,
                    close_time=T + H - 1, open=101, high=104.5, low=99.0,
                    close=100, closed=True)
    obs = db.get_ltf_observation(sc.observation_id)
    zone = db.get_ltf_entry_zone(z.id)
    LtfEngine(db, cfg)._on_entry_touched(
        obs, sc, e, zone, candle, [], candle.close_time, "live", 0,
        LtfTickResult())
    ev = next(
        e for e in db.list_ltf_events(observation_id=obs.id)
        if e.kind == "context_transition"
    )
    assert ev.payload["fact"] == "fvg_filled_current"
    assert "заполнен; наблюдение реакции H1 продолжается" in ev.payload["text"]


# --------------------------------------------------------------------- #
# Доставка: live context_transition уходит, R46 — финальный текст
# --------------------------------------------------------------------- #

async def test_live_transition_delivered(db, cfg, instrument_id):
    from app.services.htf_context import emit_context_transition
    now = now_ms()
    zone = _zone(db, instrument_id, Direction.BULL)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, zone, now)
    ev = emit_context_transition(
        db, cfg, instrument_id, fact="opposing_pending",
        text="Основной контекст вниз. Достигнут встречный HTF вверх; ожидаем BOS/SMS H1",
        occurred_at=now, dedupe_key="ctxtr:test:live",
        zone=zone,
    )
    assert ev is not None and ev.delayed is False
    sender = RecSender()
    disp = LtfDispatcher(db, cfg, sender)
    assert await disp.deliver([ev]) == 1
    assert sender.texts
    assert "Основной контекст вниз" in sender.texts[0]


def test_r46_correction_text_has_reason_and_fact(db, cfg, instrument_id):
    obs, sc = make_scenario(db, instrument_id)
    engine = LtfEngine(db, cfg)
    revisions = [{
        "open_time": T + H, "previous": [1, 2, 3, 4],
        "current": [1, 2, 3, 5], "detected_at": T + H,
    }]
    db.update_ltf_scenario(sc.id, state="cancelled",
                           cancellation_reason="reverse_bos",
                           cancelled_at=T + H, updated_at=T + H)
    engine._emit_correction_events(
        instrument_id, revisions, {sc.id: "monitoring_entries"},
        T + H + 1_000, cfg.ltf_live_grace_seconds * 1000, LtfTickResult(),
    )
    ev = next(e for e in db.list_ltf_events(observation_id=obs.id)
              if e.kind == "correction")
    text = ev.payload["text"]
    assert "Исправление данных" in text
    assert "ревизия закрытых свечей" in text
    assert "Исходный факт" in text
    assert ev.payload["reason"] == "candle_revision"
