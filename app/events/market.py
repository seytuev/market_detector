"""Единый расчёт направления крипторынка (§6, §10 ТЗ 2026-10-11).

Чистый модуль: без БД, сети и конфигурации — runner собирает MarketInput,
здесь только голосование семей, зависимости и контракт снимка оценки.
Направление — контекст рынка по событиям и времени, не сигнал сделки.
Степень перевеса голосов — «Согласованность факторов», не вероятность (§6).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .rules import ENGINE_RULE_VERSION
from .revalidation import MODEL_VERSION

_EVENT_CLASSES = ("market_event",)
_TIME_CLASSES = ("time_condition", "combination")

# Ранг качества обязательных источников: хуже — больше (§10).
_QUALITY_RANK = {
    "ok": 0, "partial": 1, "stale": 2,
    "unavailable": 3, "auth_error": 3, "capability_error": 3,
}
_QUALITY_LABEL = {0: "ok", 1: "partial", 2: "stale", 3: "unavailable"}

_MARKET_LINES = {
    "long": "↑ LONG",
    "short": "↓ SHORT",
    "neutral": "Нейтрально",
    "mixed": "Противоречивый контекст",
    "unknown": "Направление неизвестно",
}
_LINE_LOW_COVERAGE = "Крипторынок: направление неизвестно. Доступен фон BTC"

_ARROWS = {
    "long": "up", "short": "down", "neutral": "flat",
    "mixed": "conflict", "unknown": "unknown",
}

_SIDE_RU = {"long": "LONG", "short": "SHORT"}


@dataclass
class MarketInput:
    now_ms: int
    basket: dict | None            # latest_basket payload (version, day_start_ms, source, members, exclusions)
    metrics: dict | None           # basket_metrics; None — ценовых данных нет вовсе
    liq: dict | None               # метрики эпизода ликвидаций; None — данных нет
    facts: list                    # активные факты памяти (словари store._fact_row)
    time_ctx: dict                 # московский временной контекст (runner, timeutil)
    stats: dict | None             # stats.frequency/insufficient
    source_states: dict            # source → {quality, detail, ...}
    registry: list                 # RuleSpec для оценки (caller применяет overrides)
    coverage_threshold: float = 0.8
    memory_hours: int = 240


def evaluate_market(inp: MarketInput) -> dict:
    """Один снимок оценки; assessment_id заполняет вызывающий (§10)."""
    unknown_deps: list[str] = []

    # --- Голоса фактов: внутри семьи один голос по приоритету правила (§6) ---
    events_votes: list[dict] = []
    time_votes: list[dict] = []
    registry = {r.rule_id: r for r in inp.registry}
    history_unverified = False
    for fact in inp.facts:
        if (fact.get("scope") != "crypto_market" or fact.get("state") != "confirmed"
                or any(fact.get(k, 0) > inp.now_ms for k in ("occurred_at", "available_at"))
                or fact.get("valid_until", inp.now_ms + 1) <= inp.now_ms):
            continue
        payload = fact.get("payload") or {}
        if (not payload.get("superseded_by") and fact.get("direction") in ("long", "short")
                and (payload.get("current_check") or {}).get("status") == "unverified"):
            history_unverified = True
        if (payload.get("superseded_by") or payload.get("context_invalidated")
                or (payload.get("current_check") or {}).get("status") == "unverified"
                or payload.get("phase_at", 0) > inp.now_ms):
            continue
        direction = fact.get("direction")
        if direction not in ("long", "short"):
            continue  # LIQ_FACT и сервисные факты не голосуют
        effective_rule = (fact.get("payload") or {}).get("recovery_rule") or fact.get("rule_id") or ""
        if effective_rule == "LIQ_PRESSURE" and direction == "long":
            effective_rule = "LIQ_RECOVERY"
        rule = registry.get(effective_rule)
        if (rule is None or not rule.enabled or rule.rule_class != "market_event"
                or rule.direction not in ("long", "short") or rule.direction != direction):
            continue
        family = rule.family if rule else (fact.get("rule_id") or "unknown")
        priority = rule.priority if rule else 0
        rclass = rule.rule_class if rule else "market_event"
        group = "time" if rclass in _TIME_CLASSES else "events"
        vote = {
            "group": group, "side": direction, "sign": 1 if direction == "long" else -1,
            "rule_id": effective_rule, "family": family, "priority": priority,
            "occurred_at": fact.get("occurred_at"), "fact": fact, "rule": rule,
            "phase_at": (fact.get("payload") or {}).get("phase_at") or fact.get("occurred_at"),
        }
        (time_votes if group == "time" else events_votes).append(vote)

    # --- Голоса временных правил: только enabled+validated, §13.1 ---
    weekday = inp.time_ctx.get("weekday")
    hour = inp.time_ctx.get("hour_msk")
    basket_history_ok = any(d.get("sign") is not None for d in inp.time_ctx.get("completed_days") or [])
    for rule in inp.registry:
        if rule.rule_class not in _TIME_CLASSES:
            continue
        if not rule.enabled or rule.confirmation != "validated":
            continue  # неподтверждённые методики не голосуют
        if rule.direction not in ("long", "short"):
            continue
        if "basket_history" in rule.dependencies and not basket_history_ok:
            # правило молчит, группа остаётся вычислимой
            if "basket_history" not in unknown_deps:
                unknown_deps.append("basket_history")
            continue
        votes = False
        if rule.rule_id == "TIME_WEEKDAY":
            votes = weekday in rule.thresholds.get("weekdays", [weekday])
        elif rule.rule_id == "SEQ_WEEK_START":
            seq = inp.time_ctx.get("week_seq")
            votes = bool(seq and seq.get("condition_met"))
        elif rule.rule_id == "TIME_WINDOW":
            votes = hour in rule.thresholds.get("hours_msk", list(range(24)))
        if votes:
            time_votes.append({
                "group": "time", "side": rule.direction,
                "sign": 1 if rule.direction == "long" else -1,
                "rule_id": rule.rule_id, "family": rule.family,
                "priority": rule.priority, "occurred_at": None,
                "fact": None, "rule": rule,
            })

    events_votes = _family_votes(events_votes)
    time_votes = _family_votes(time_votes)
    all_votes = events_votes + time_votes

    # --- Обязательные зависимости группы событий (§3) ---
    coverage = None
    price_ok = False
    if inp.metrics is not None:
        cov = inp.metrics.get("coverage") or {}
        ratio = float(cov.get("ratio", 0.0) or 0.0)
        price_ok = (ratio >= inp.coverage_threshold and cov.get("total", 0) > 0
                    and isinstance(inp.metrics.get("median_return"), (int, float))
                    and math.isfinite(inp.metrics["median_return"]))
        coverage = {
            "ok": cov.get("ok", 0), "total": cov.get("total", 0),
            "ratio": ratio, "sufficient": price_ok,
        }
    if not price_ok:
        unknown_deps.insert(0, "price")
    liq_ok = bool(inp.liq and inp.liq.get("available") and inp.liq.get("baseline_ok", True))
    for source in ("price", "liquidations"):
        quality = (inp.source_states.get(source) or {}).get("quality")
        if quality not in ("ok", "partial"):
            if source == "price":
                price_ok = False
            else:
                liq_ok = False
            if source not in unknown_deps:
                unknown_deps.append(source)
    if inp.liq and not inp.liq.get("baseline_ok", True):
        unknown_deps.append("liquidations_baseline")
    if not liq_ok:
        if "liquidations" not in unknown_deps:
            unknown_deps.insert(1 if not price_ok else 0, "liquidations")
    if not price_ok and "price" not in unknown_deps:
        unknown_deps.insert(0, "price")
    if history_unverified:
        unknown_deps.append("event_history")

    events_contra = _contradictory(events_votes)
    time_contra = _contradictory(time_votes)
    events_direction = (
        "unknown" if not (price_ok and liq_ok) or history_unverified else _group_direction(events_votes)
    )
    time_direction = _group_direction(time_votes)  # календарь всегда доступен

    # --- Итог: те же семьи обеих групп (§6) ---
    contradictory = _contradictory(all_votes)
    if events_direction == "unknown" or time_direction == "unknown":
        direction = "unknown"
    else:
        direction = _group_direction(all_votes)

    quality = _quality(inp.source_states)
    if quality == "ok" and (not (price_ok and liq_ok) or history_unverified):
        quality = "partial"
    if coverage:
        coverage["sufficient"] = price_ok
    quality_by_dependency = {
        src: (st or {}).get("quality") for src, st in (inp.source_states or {}).items()
    }

    market_line = _MARKET_LINES[direction]
    if direction == "unknown" and _low_coverage_with_data(inp, coverage):
        market_line = _LINE_LOW_COVERAGE

    counted_votes = (events_votes if events_direction != "unknown" else []) + time_votes
    n_long = sum(1 for v in counted_votes if v["sign"] > 0)
    n_short = sum(1 for v in counted_votes if v["sign"] < 0)
    selected_keys = {v["fact"]["fact_key"] for v in all_votes if v.get("fact")}
    active_facts = []
    for fact in inp.facts:
        item = _fact_out(fact)
        selected = fact.get("fact_key") in selected_keys
        item["used_for_direction"] = selected and events_direction != "unknown"
        item["context_status"] = (
            "invalidated" if (fact.get("payload") or {}).get("context_invalidated") else
            "awaiting_confirmation" if (fact.get("payload") or {}).get("pressure_released") else
            "unverified" if ((fact.get("payload") or {}).get("current_check") or {}).get("status") == "unverified" else
            "non_directional" if fact.get("direction") not in ("long", "short") else
            "unverified" if events_direction == "unknown" else
            "driving" if selected else "background"
        )
        active_facts.append(item)
    status_rank = {"driving": 0, "unverified": 1, "invalidated": 1,
                   "awaiting_confirmation": 1, "background": 2, "non_directional": 3}
    active_facts.sort(key=lambda f: (status_rank[f["context_status"]],
                                   -(f.get("phase_at") or f.get("occurred_at") or 0)))

    return {
        "assessment_id": "",
        "scope": "crypto_market",
        "timezone": "Europe/Moscow",
        "evaluated_at": inp.now_ms,
        "available_at": inp.now_ms,
        "rule_version": ENGINE_RULE_VERSION,
        "basket_version": (inp.basket or {}).get("version"),
        "direction": direction,
        "events_direction": events_direction,
        "time_direction": time_direction,
        "contradictory": contradictory,
        "quality": quality,
        "quality_by_dependency": quality_by_dependency,
        "coverage": coverage,
        "market_line": market_line,
        "subtitle": "Направление по событиям и времени",
        "conflict_note": (
            "Контекст противоречивый"
            if contradictory and direction in ("long", "short") else None
        ),
        "arrow": _ARROWS[direction],
        "reasons": _reasons(all_votes, direction, contradictory, inp.time_ctx),
        "unknown_dependencies": unknown_deps,
        "active_facts": active_facts,
        "liquidations": {k: (inp.liq or {}).get(k) for k in (
            "window", "coverage", "baseline_ok", "baseline_samples", "episode",
        )},
        "memory_policy": {"hours": inp.memory_hours, "expiry_is_model_limit": True},
        "methodology": {
            "id": MODEL_VERSION,
            "label": "Экспериментальная модель корзины · Binance · окна 4ч",
            "study_scope": "Исследование: BTC, дневные ликвидации на 10 биржах",
            "limitations": [
                "Числа исследования BTC не являются вероятностями для корзины монет.",
                "OI не участвует в общей оценке направления этой модели.",
                "История восстанавливается по текущему составу корзины; срок памяти — настройка модели.",
                "Восстановление снимается при закрытиях H1 ниже основания каскада; это проектный критерий корзины.",
            ],
        },
        "time_rules_active": any(r.enabled and r.confirmation == "validated"
                                 and r.direction in ("long", "short")
                                 for r in inp.registry if r.rule_class in _TIME_CLASSES),
        "time_context": inp.time_ctx,
        "statistics": inp.stats,
        "btc_background": None,
        "factor_agreement": {
            "long": n_long, "short": n_short,
            "label": "Согласованность факторов",
        },
        "next_review_at": _next_review(inp),
    }


def _family_votes(votes: list[dict]) -> list[dict]:
    """One vote per family; a later phase supersedes an older interpretation."""
    by_family: dict[str, dict] = {}
    for vote in votes:
        cur = by_family.get(vote["family"])
        key = (vote.get("phase_at") or vote.get("occurred_at") or 0, vote["priority"])
        if cur is None or key > (cur.get("phase_at") or cur.get("occurred_at") or 0, cur["priority"]):
            by_family[vote["family"]] = vote
    return list(by_family.values())


def _group_direction(votes: list[dict]) -> str:
    if not votes:
        return "neutral"
    total = sum(v["sign"] for v in votes)
    if total > 0:
        return "long"
    if total < 0:
        return "short"
    return "mixed" if _contradictory(votes) else "neutral"


def _contradictory(votes: list[dict]) -> bool:
    signs = {v["sign"] for v in votes}
    return 1 in signs and -1 in signs


def _quality(source_states: dict) -> str:
    """Худшая из обязательных (цена, ликвидации); unknown — если не считалось."""
    known = []
    for src in ("price", "liquidations"):
        q = ((source_states or {}).get(src) or {}).get("quality")
        if q is not None:
            known.append(_QUALITY_RANK.get(q, 3))
    if not known:
        return "unknown"
    return _QUALITY_LABEL[max(known)]


def _low_coverage_with_data(inp: MarketInput, coverage: dict | None) -> bool:
    """§3: частичное покрытие корзины — явная подпись, BTC-фон отдельно."""
    if coverage is None or coverage.get("sufficient"):
        return False
    if not coverage.get("ok"):
        return False
    members = (inp.basket or {}).get("members") or []
    return bool(members)


def _reasons(votes: list[dict], direction: str,
             contradictory: bool, time_ctx: dict) -> list[dict]:
    """Не более двух причин; при конфликте — обе стороны (§8.1, AC11)."""
    if not votes or direction in ("unknown", "neutral"):
        return []
    if contradictory:
        picked = []
        for side in ("long", "short"):
            vote = _pick(votes, side)
            if vote is not None:
                picked.append(vote)
    else:
        side = direction if direction in ("long", "short") else None
        picked = sorted(
            (v for v in votes if v["side"] == side),
            key=lambda v: (v["group"] != "events", -(v.get("occurred_at") or 0)),
        )[:2]
    return [
        {
            "group": v["group"], "side": v["side"], "rule_id": v["rule_id"],
            "text": _reason_text(v, time_ctx),
        }
        for v in picked[:2]
    ]


def _pick(votes: list[dict], side: str) -> dict | None:
    cand = [v for v in votes if v["side"] == side]
    if not cand:
        return None
    cand.sort(key=lambda v: (v["group"] != "events", -(v.get("occurred_at") or 0)))
    return cand[0]


def _reason_text(vote: dict, time_ctx: dict) -> str:
    side = _SIDE_RU.get(vote["side"], vote["side"])
    if vote["group"] == "events":
        fact = vote.get("fact") or {}
        rule = vote.get("rule")
        base = (fact.get("payload") or {}).get("summary") or (
            rule.explanation if rule else "активный факт"
        )
        return f"События: {base}"
    if vote["rule_id"] == "TIME_WEEKDAY":
        name = time_ctx.get("weekday_name") or ""
        return f"Время: {name} МСК; календарное правило поддерживает {side}"
    if vote["rule_id"] == "TIME_WINDOW":
        hour = time_ctx.get("hour_msk")
        return f"Время: {hour}:00 МСК; внутридневное правило поддерживает {side}"
    if vote["rule_id"] == "SEQ_WEEK_START":
        return (
            "Время: завершённые понедельник и вторник бычьи; "
            f"недельная комбинация поддерживает {side}"
        )
    return f"Время: правило {vote['rule_id']} поддерживает {side}"


def _fact_out(fact: dict) -> dict:
    return {
        "fact_key": fact.get("fact_key"),
        "rule_id": fact.get("rule_id"),
        "effective_rule_id": (fact.get("payload") or {}).get("recovery_rule") or fact.get("rule_id"),
        "direction": fact.get("direction"),
        "state": fact.get("state"),
        "occurred_at": fact.get("occurred_at"),
        "detected_at": fact.get("detected_at"),
        "available_at": fact.get("available_at"),
        "first_detected_at": fact.get("created_at") or fact.get("detected_at"),
        "valid_until": fact.get("valid_until"),
        "time_precision": fact.get("time_precision"),
        "phase_at": (fact.get("payload") or {}).get("phase_at"),
        "episode_id": fact.get("episode_id"),
        "coverage": (fact.get("payload") or {}).get("coverage"),
        "current_check": (fact.get("payload") or {}).get("current_check"),
        "invalidation_reason": (fact.get("payload") or {}).get("invalidation_reason"),
        "invalidated_at": (fact.get("payload") or {}).get("invalidated_at"),
        "summary": (fact.get("payload") or {}).get("summary"),
    }


def _next_review(inp: MarketInput) -> int | None:
    candidates = []
    next_day = inp.time_ctx.get("next_day_start_ms")
    if next_day and next_day > inp.now_ms:
        candidates.append(next_day)
    candidates.extend(inp.time_ctx[k] for k in ("next_h1_close_ms", "next_liquidation_close_ms")
                      if inp.time_ctx.get(k, 0) > inp.now_ms)
    candidates.extend(
        f["valid_until"] for f in inp.facts if f.get("valid_until", 0) > inp.now_ms
    )
    return min(candidates) if candidates else None
