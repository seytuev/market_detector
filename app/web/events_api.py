"""Чтение событий. Страница не ходит в Coinglass сама."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from ..events.evaluate import RULES, relation_to_h1
from ..events.mathutil import RULE_VERSION
from ..events.runner import run_cycle
from ..events.store import (
    gate_history,
    latest_snapshot,
    list_journal,
    list_situations,
    snapshot_as_of,
)


def register_events_routes(app: FastAPI, db, settings, require_auth) -> None:
    @app.get("/api/events/overview", dependencies=[Depends(require_auth)])
    def overview() -> dict[str, Any]:
        snap = latest_snapshot(db, "BTC")
        health = _health(db)
        if snap is None:
            return {
                "ready": False,
                "empty_reason": "Оценка ещё не выполнялась",
                "market_line": "Оценка ещё не выполнялась",
                "service_line": _service_from_health(health),
                "strategy_gates": [],
                "situations": [],
                "health": health,
                "rule_version": RULE_VERSION,
                "study_percents_on_card": False,
            }
        return _public(snap, health)

    @app.get("/api/events/health", dependencies=[Depends(require_auth)])
    def health() -> dict[str, Any]:
        return _health(db)

    @app.get("/api/events/journal", dependencies=[Depends(require_auth)])
    def journal(limit: int = 50) -> dict[str, Any]:
        return {"items": list_journal(db, "BTC", min(limit, 200))}

    @app.get("/api/events/calendar", dependencies=[Depends(require_auth)])
    def calendar() -> dict[str, Any]:
        snap = latest_snapshot(db, "BTC")
        if snap is None:
            return {"ready": False, "empty_reason": "Оценка ещё не выполнялась"}
        return snap.get("calendar") or {}

    @app.get("/api/events/rules", dependencies=[Depends(require_auth)])
    def rules() -> dict[str, Any]:
        return {"rules": RULES, "note": "Проценты источника на карточку не выводятся"}

    @app.get("/api/events/derivatives", dependencies=[Depends(require_auth)])
    def derivatives() -> dict[str, Any]:
        snap = latest_snapshot(db, "BTC")
        if snap is None:
            return {"ready": False, "empty_reason": "Оценка ещё не выполнялась"}
        return {
            "liquidation": snap.get("liquidation"),
            "oi_coin": snap.get("oi_coin"),
            "oi_usd": snap.get("oi_usd"),
            "oi_range30": snap.get("oi_range30"),
            "funding": snap.get("funding"),
            "oi02": snap.get("oi02"),
            "evaluated_at": snap.get("evaluated_at"),
            "price_source": snap.get("price_source"),
        }

    @app.get("/api/events/situations", dependencies=[Depends(require_auth)])
    def situations() -> dict[str, Any]:
        return {"items": list_situations(db, "BTC")}

    @app.get("/api/events/situations/{setup}/gate-history",
             dependencies=[Depends(require_auth)])
    def history(setup: str) -> dict[str, Any]:
        return {"setup": setup, "items": gate_history(db, "BTC", setup)}

    @app.get("/api/ltf/events/{event_id}/context", dependencies=[Depends(require_auth)])
    def ltf_context(event_id: int, mode: str = "at_event") -> dict[str, Any]:
        row = db.conn.execute(
            "SELECT id, scenario_id, occurred_at, detected_at FROM ltf_event WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Событие H1 не найдено")
        direction = None
        if row["scenario_id"] is not None:
            scenario = db.conn.execute(
                "SELECT direction FROM ltf_scenario WHERE id=?",
                (row["scenario_id"],),
            ).fetchone()
            if scenario is not None:
                direction = scenario["direction"]
        if mode == "now":
            snap = latest_snapshot(db, "BTC")
            assumed = False
        else:
            snap = snapshot_as_of(db, "BTC", int(row["detected_at"]))
            assumed = False
            if snap is None:
                return {
                    "mode": "at_event",
                    "context_relation": "недостаточно данных",
                    "reason": "Нет снимка, известного к моменту события",
                    "availability_assumed": False,
                    "effect_on_h1_structure": "none",
                }
        relation = relation_to_h1(snap, direction)
        return {
            "mode": "now" if mode == "now" else "at_event",
            "context_relation": relation,
            "market_line": None if snap is None else snap.get("market_line"),
            "service_line": None if snap is None else snap.get("service_line"),
            "strategy_gates": [] if snap is None else snap.get("strategy_gates"),
            "evaluated_at": None if snap is None else snap.get("evaluated_at"),
            "availability_assumed": assumed,
            "effect_on_h1_structure": "none",
        }

    @app.post("/api/events/refresh", dependencies=[Depends(require_auth)])
    def refresh() -> dict[str, Any]:
        result = run_cycle(db, settings)
        public = {k: v for k, v in result.items() if k != "health"}
        health = result.get("health") or {}
        public["quality"] = health.get("quality") or result.get("quality")
        public["detail"] = health.get("detail") or result.get("detail")
        return public


def _health(db) -> dict:
    raw = db.get_meta("events:health")
    if not raw:
        return {"quality": "unknown", "detail": "Проверка ещё не запускалась"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"quality": "unknown"}


def _service_from_health(health: dict) -> str:
    quality = health.get("quality")
    if quality == "auth_error":
        return "Ключ Coinglass не принят. Производные данные неизвестны."
    if quality == "capability_error":
        return "Тариф не отдаёт запрошенный интервал. Замена на другой таймфрейм не делается."
    if quality == "unavailable":
        return "Coinglass сейчас недоступен. Цена и календарь от этого не останавливаются."
    return "Производные данные ещё не загружены."


def _public(snap: dict, health: dict) -> dict:
    gates = snap.get("strategy_gates") or []
    return {
        "ready": True,
        "symbol": snap.get("symbol"),
        "rule_version": snap.get("rule_version"),
        "evaluated_at": snap.get("evaluated_at"),
        "price_source": snap.get("price_source"),
        "market_line": snap.get("market_line"),
        "service_line": snap.get("service_line"),
        "calendar": snap.get("calendar"),
        "streak": snap.get("streak"),
        "liquidation": snap.get("liquidation"),
        "oi_coin": snap.get("oi_coin"),
        "oi_usd": snap.get("oi_usd"),
        "oi_range30": snap.get("oi_range30"),
        "funding": snap.get("funding"),
        "oi02": snap.get("oi02"),
        "wide": snap.get("wide"),
        "strategy_gates": gates,
        "situations": snap.get("situations") or [],
        "risk_multiplier": None,
        "risk_note": snap.get("risk_note"),
        "study_percents_on_card": False,
        "effect_on_h1_structure": "none",
        "health": health,
        "quiet_hours": snap.get("quiet_hours"),
    }
