"""Чтение событий. Страница не ходит в Coinglass сама."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from ..events.evaluate import RULES, market_direction, relation_to_h1
from ..events.mathutil import RULE_VERSION
from ..events.rules import ENGINE_RULE_VERSION, RULE_REGISTRY
from ..events.runner import run_cycle
from ..events.calendar import BTC_SERIES, CALENDAR_VERSION, calendar_statistics
from ..events.calendar_history import STATE_KEY as CALENDAR_HISTORY_KEY
from ..events.revalidation import MODEL_VERSION
from ..events.timeutil import CONTEXT_STALE_AFTER_MS, MSK, msk_day_start_ms
from ..events.store import (
    assessment_as_of,
    bundle_publications,
    gate_history,
    get_assessment,
    get_source_states,
    latest_assessment,
    latest_snapshot,
    metric_series,
    list_bundles,
    list_deliveries,
    list_journal,
    list_situations,
    snapshot_as_of,
)
from ..models import now_ms

# Старые BTC-снимки сохранены как прежняя методика (§11.8): подпись явная.
LEGACY_METHODOLOGY = "legacy BTC / прежняя методика"

_DEFAULT_CYCLE_SECONDS = 60


def register_events_routes(app: FastAPI, db, settings, require_auth) -> None:
    @app.get("/api/events/overview", dependencies=[Depends(require_auth)])
    def overview() -> dict[str, Any]:
        snap = latest_snapshot(db, "BTC")
        health = _health(db)
        market = latest_assessment(db)
        age, stale = _market_timing(market, health)
        if snap is None:
            return {
                "ready": market is not None,
                "empty_reason": None if market else "Оценка ещё не выполнялась",
                "market_line": market["market_line"] if market else "Оценка ещё не выполнялась",
                "service_line": _service_from_health(health),
                "direction_note": market_direction(None),
                "strategy_gates": [],
                "situations": [],
                "health": health,
                "rule_version": RULE_VERSION,
                "study_percents_on_card": False,
                "market": market,
                "market_age_seconds": age,
                "market_stale": stale,
                "legacy_methodology": None,
            }
        payload = _public(snap, health)
        # Рыночная область — рядом; legacy market_line (BTC) не трогаем (§10, AC04)
        payload["market"] = market
        payload["market_age_seconds"] = age
        payload["market_stale"] = stale
        payload["legacy_methodology"] = LEGACY_METHODOLOGY
        return payload

    @app.get("/api/events/health", dependencies=[Depends(require_auth)])
    def health() -> dict[str, Any]:
        return _health_payload(db)

    @app.get("/api/events/journal", dependencies=[Depends(require_auth)])
    def journal(limit: int = 50) -> dict[str, Any]:
        return {
            "items": list_journal(db, "BTC", min(limit, 200)),
            "bundles": _bundles(db),
        }

    @app.get("/api/events/calendar", dependencies=[Depends(require_auth)])
    def calendar(day: str | None = None, scope: str = "BTC") -> dict[str, Any]:
        if scope not in ("BTC", "basket"):
            raise HTTPException(status_code=400, detail="Область календаря: BTC или basket")
        now = now_ms()
        target = msk_day_start_ms(now)
        if day is not None:
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=MSK)
                if parsed.strftime("%Y-%m-%d") != day or not 2017 <= parsed.year <= 2100:
                    raise ValueError("invalid calendar date")
                target = int(parsed.timestamp() * 1000)
            except ValueError:
                raise HTTPException(status_code=400, detail="Дата календаря: ГГГГ-ММ-ДД, годы 2017–2100")
        snap = latest_snapshot(db, "BTC")
        if snap is None:
            payload = {"ready": False, "empty_reason": "Оценка ещё не выполнялась"}
        else:
            payload = dict(snap.get("calendar") or {})
        market = _market_calendar(latest_assessment(db)) or {"time_context": {}, "upcoming": []}
        if scope == "BTC":
            rows = [r for r in metric_series(db, BTC_SERIES) if r.get("method_version") == CALENDAR_VERSION]
            raw_state = db.get_meta(CALENDAR_HISTORY_KEY)
            history_state = json.loads(raw_state) if raw_state else {"quality": "unavailable", "days": len(rows)}
        else:
            rows = [{**r, "return": r.get("median_return"), "range": None}
                    for r in metric_series(db, "basket_day_return")
                    if r.get("historical_composition_known") and r.get("method_version") == MODEL_VERSION]
            history_state = {"quality": "accumulating", "days": sum(bool(r.get("complete")) for r in rows)}
        forecast = calendar_statistics(rows, target=target, now=now, min_n=settings.events_stats_min_n)
        forecast.update({"scope": scope, "scope_label": "BTCUSDT · Binance Spot" if scope == "BTC" else "Медиана исторической корзины рынка",
                         "history": history_state})
        market["time_context"] = {**market["time_context"], "completed_days": forecast.pop("completed_days")}
        market["forecast"] = forecast
        payload["market"] = market
        return payload

    @app.get("/api/events/rules", dependencies=[Depends(require_auth)])
    def rules() -> dict[str, Any]:
        market = latest_assessment(db)
        return {
            "rules": RULES,
            "note": "Проценты источника на карточку не выводятся",
            "rule_version": ENGINE_RULE_VERSION,
            "registry": [asdict(rule) for rule in RULE_REGISTRY],
            "statistics": None if market is None else market.get("statistics"),
        }

    @app.get("/api/events/market/history", dependencies=[Depends(require_auth)])
    def market_history(at: str | None = None) -> dict[str, Any]:
        captured = _parse_at(at)
        return {
            "captured_at": captured,
            # «Известно тогда» (§8.2): available_at <= captured, без будущих данных
            "known_then": assessment_as_of(db, captured),
            "current": latest_assessment(db),
        }

    @app.get("/api/events/market/assessment/{assessment_id}",
             dependencies=[Depends(require_auth)])
    def market_assessment(assessment_id: str) -> dict[str, Any]:
        assessment = get_assessment(db, assessment_id)
        if assessment is None:
            raise HTTPException(status_code=404, detail="Оценка не найдена")
        return assessment

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


def _health_payload(db) -> dict:
    """Доступность по зависимостям (§10): возраст каждого источника отдельно."""
    health = _health(db)
    now = now_ms()
    payload = dict(health)
    sources = health.get("sources") or get_source_states(db)
    with_age = {}
    for name, state in sources.items():
        entry = dict(state or {})
        updated = entry.get("updated_at")
        entry["age_seconds"] = (
            None if updated is None else (now - int(updated)) // 1000
        )
        with_age[name] = entry
    payload["sources"] = with_age
    payload["evaluated_at"] = health.get("evaluated_at")
    payload["cycle_seconds"] = health.get("cycle_seconds") or _DEFAULT_CYCLE_SECONDS
    return payload


def _market_timing(market: dict | None, health: dict) -> tuple[int | None, bool | None]:
    """Возраст оценки: устаревание после четырёх часов, независимо от опроса."""
    if market is None:
        return None, None
    elapsed_ms = now_ms() - int(market["evaluated_at"])
    elapsed = elapsed_ms // 1000
    age = max(0, elapsed)
    cycle = health.get("cycle_seconds") or _DEFAULT_CYCLE_SECONDS
    return age, elapsed_ms > CONTEXT_STALE_AFTER_MS or elapsed < -int(cycle)


def _bundles(db, limit: int = 50) -> list[dict]:
    """Пакеты публикаций: обнаружение, публикация и доставка — раздельно (AC15)."""
    out = []
    for bundle in list_bundles(db, limit=limit):
        publications = []
        for pub in bundle_publications(db, bundle["id"]):
            payload = pub.get("payload") or {}
            publications.append({
                "kind": pub.get("kind"),
                "text": payload.get("text"),
                "rule_id": payload.get("rule_id"),
                "created_at": pub.get("created_at"),
                "window_ms": pub.get("window_ms"),
                "assessment_id": pub.get("assessment_id"),
            })
        deliveries = [
            {
                "channel": delivery.get("channel"),
                "recipient": delivery.get("recipient"),
                "status": delivery.get("status"),
                "delivered_at": delivery.get("delivered_at"),
                "detail": delivery.get("detail"),
            }
            for delivery in list_deliveries(db, bundle_id=bundle["id"])
        ]
        out.append({
            "id": bundle["id"],
            "kind": bundle["kind"],
            "status": bundle["status"],
            "opened_at": bundle["opened_at"],
            "closes_at": bundle["closes_at"],
            "published_at": bundle["published_at"],
            "publications": publications,
            "deliveries": deliveries,
        })
    return out


def _market_calendar(market: dict | None) -> dict | None:
    """Реальные будущие переключения из снимка: граница дня МСК и истечения
    активных фактов. Будущие ликвидации не прогнозируются (§8.2)."""
    if market is None:
        return None
    time_ctx = market.get("time_context") or {}
    upcoming = []
    for key, kind in (("next_h1_close_ms", "h1_close"),
                      ("next_liquidation_close_ms", "liquidation_window_close")):
        if time_ctx.get(key) is not None:
            upcoming.append({"kind": kind, "at": time_ctx[key]})
    next_day = time_ctx.get("next_day_start_ms")
    if next_day is not None:
        upcoming.append({
            "at": int(next_day),
            "kind": "msk_day_boundary",
            "label": "Смена дня МСК",
        })
    for fact in market.get("active_facts") or []:
        valid_until = fact.get("valid_until")
        if valid_until is None:
            continue
        upcoming.append({
            "at": int(valid_until),
            "kind": "fact_expiry",
            "label": "Событие больше не учитывается",
            "fact_key": fact.get("fact_key"),
        })
    upcoming.sort(key=lambda item: item["at"])
    return {
        "time_context": time_ctx,
        "next_review_at": market.get("next_review_at"),
        "upcoming": upcoming,
    }


def _parse_at(at: str | None) -> int:
    if at is None:
        raise HTTPException(
            status_code=400,
            detail="Параметр at обязателен: метка времени, мс",
        )
    try:
        return int(str(at).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Параметр at должен быть целым числом миллисекунд",
        )


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
        # сторона контекста событий — отдельно от направления графика
        "direction_note": market_direction(snap),
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
