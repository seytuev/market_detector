"""Чистый расчёт направления рынка (спека §6, §10): голоса семей, зависимости,
контракт снимка. AC02–AC04, AC08, AC11–AC13."""
from __future__ import annotations

from dataclasses import replace

from app.events.market import MarketInput, evaluate_market
from app.events.rules import get_rule, registry_for_eval
from app.events.stats import insufficient
from app.events.timeutil import WEEKDAY_RU

DAY = 86_400_000
HOUR = 3_600_000
T0 = 1_800_000_000_000


def _time_ctx(weekday: int = 3, hour: int = 14, *, with_history: bool = True,
              week_seq: dict | None = None) -> dict:
    return {
        "weekday": weekday,
        "weekday_name": WEEKDAY_RU[weekday],
        "hour_msk": hour,
        "day_start_ms": T0,
        "next_day_start_ms": T0 + DAY,
        "completed_days": [
            {
                "day_start_ms": T0 - i * DAY,
                "weekday": (weekday - i) % 7,
                "median_return": 0.1,
                "sign": 1,
            }
            for i in range(1, 8)
        ] if with_history else [],
        "week_seq": week_seq,
    }


def _fact(rule_id: str, direction: str | None, *, occurred: int = T0,
          summary: str | None = None) -> dict:
    return {
        "fact_key": f"k-{rule_id}-{occurred}",
        "rule_id": rule_id,
        "rule_version": "events-time-2.0",
        "scope": "crypto_market",
        "episode_id": "ep-1",
        "parent_key": None,
        "direction": direction,
        "state": "confirmed",
        "verification": "ok",
        "occurred_at": occurred,
        "detected_at": occurred + 1_000,
        "available_at": occurred + 500,
        "valid_until": occurred + 240 * HOUR,
        "time_precision": "window",
        "payload": {"summary": summary} if summary else {},
        "created_at": occurred,
        "updated_at": occurred,
    }


def _metrics(ratio: float = 1.0, ok: int = 20, total: int = 20,
             median_return: float | None = 0.01, breadth: float | None = 0.6) -> dict:
    return {
        "window_ms": DAY,
        "coverage": {"ok": ok, "total": total, "ratio": ratio},
        "median_return": median_return,
        "breadth_up": breadth,
        "members": [],
    }


def _liq(available: bool = True) -> dict:
    return {
        "available": available,
        "baseline_ok": True,
        "window": {"start": T0, "end": T0 + 14_400_000, "closed": True},
        "sum_long_usd": 100.0,
        "sum_short_usd": 40.0,
        "long_share": 0.71,
        "multiple": 1.1,
        "coverage": {"ok": 3, "total": 3},
        "episode": {"active": False, "phase": None, "started_at": None},
        "provisional": None,
    }


def _states(price: str = "ok", liq: str = "ok") -> dict:
    return {
        "price": {"quality": price},
        "liquidations": {"quality": liq},
        "calendar": {"quality": "ok"},
    }


def _basket() -> dict:
    return {
        "version": "b1",
        "day_start_ms": T0,
        "source": "static",
        "members": [{"symbol": "BTC", "cmc_id": 0, "rank": 1, "pair": "BTCUSDT"}],
        "exclusions": [],
    }


def _registry_with(*replacements) -> list:
    by_id = {r.rule_id: r for r in replacements}
    return [by_id.get(r.rule_id, r) for r in registry_for_eval()]


def _weekday_rule(direction: str, weekdays: list[int]):
    return replace(
        get_rule("TIME_WEEKDAY"), enabled=True, confirmation="validated",
        direction=direction, thresholds={"weekdays": weekdays},
    )


def _inp(**overrides) -> MarketInput:
    base = dict(
        now_ms=T0 + 12 * HOUR,
        basket=_basket(),
        metrics=_metrics(),
        liq=_liq(),
        facts=[],
        time_ctx=_time_ctx(),
        stats=None,
        source_states=_states(),
        registry=registry_for_eval(),
    )
    base.update(overrides)
    return MarketInput(**base)


def test_ac02_liq_fact_alone_never_assigns_short():
    # Известен только объём ликвидаций — факт без направления (§6, строка 3)
    out = evaluate_market(_inp(facts=[_fact("LIQ_FACT", None)]))
    assert out["direction"] == "neutral"
    assert out["events_direction"] == "neutral"
    assert out["market_line"] == "Нейтрально"
    assert out["factor_agreement"] == {
        "long": 0, "short": 0, "label": "Согласованность факторов",
    }


def test_ac02_pressure_fact_gives_short():
    out = evaluate_market(_inp(facts=[_fact(
        "LIQ_PRESSURE", "short",
        summary="Усиление лонг-ликвидаций при падающей цене",
    )]))
    assert out["direction"] == "short"
    assert out["events_direction"] == "short"
    assert out["market_line"] == "↓ SHORT"
    assert out["arrow"] == "down"
    assert out["reasons"][0]["side"] == "short"
    assert "События:" in out["reasons"][0]["text"]


def test_ac02_reinterpreted_fact_gives_long():
    # Интерпретация того же факта изменена на LONG (затухание, LIQ_RECOVERY)
    out = evaluate_market(_inp(facts=[_fact("LIQ_PRESSURE", "long")]))
    assert out["direction"] == "long"
    assert out["market_line"] == "↑ LONG"
    out2 = evaluate_market(_inp(facts=[_fact("LIQ_RECOVERY", "long")]))
    assert out2["direction"] == "long"


def test_family_dedup_single_vote_per_liq_family():
    # Порог и затухание одного эпизода — не два независимых голоса (§6)
    out = evaluate_market(_inp(facts=[
        _fact("LIQ_PRESSURE", "short"),
        _fact("LIQ_RECOVERY", "long"),
    ]))
    assert out["direction"] == "short"  # приоритет LIQ_PRESSURE (100 > 90)
    assert out["factor_agreement"]["short"] == 1
    assert out["factor_agreement"]["long"] == 0
    assert out["contradictory"] is False


def test_ac03_missing_data_is_unknown_not_neutral():
    pressure = _fact("LIQ_PRESSURE", "short")
    out = evaluate_market(_inp(metrics=None, facts=[pressure]))
    assert out["direction"] == "unknown"
    assert out["events_direction"] == "unknown"
    assert "price" in out["unknown_dependencies"]
    # известные компоненты остаются видимыми
    assert [f["fact_key"] for f in out["active_facts"]] == [pressure["fact_key"]]
    assert out["market_line"] == "Направление неизвестно"

    out = evaluate_market(_inp(metrics=_metrics(ratio=0.5, ok=10, total=20)))
    assert out["direction"] == "unknown"
    assert "price" in out["unknown_dependencies"]
    assert out["coverage"]["sufficient"] is False

    out = evaluate_market(_inp(liq=None))
    assert out["direction"] == "unknown"
    assert "liquidations" in out["unknown_dependencies"]

    out = evaluate_market(_inp(liq=_liq(available=False)))
    assert out["direction"] == "unknown"
    assert "liquidations" in out["unknown_dependencies"]
    # вторая группа остаётся вычислимой и показывается
    assert out["time_direction"] == "neutral"


def test_ac04_low_coverage_market_line_names_btc_background():
    out = evaluate_market(_inp(metrics=_metrics(ratio=0.5, ok=1, total=2)))
    assert out["market_line"] == (
        "Крипторынок: направление неизвестно. Доступен фон BTC"
    )
    # Совсем без данных корзины — честная неизвестность без подмены фоном
    out = evaluate_market(_inp(metrics=None))
    assert out["market_line"] == "Направление неизвестно"


def test_ac08_weekday_flips_at_msk_midnight():
    # Валидированное календарное правило SHORT по четвергам (weekday=3)
    registry = _registry_with(_weekday_rule("short", [3]))
    thursday = evaluate_market(_inp(time_ctx=_time_ctx(weekday=3), registry=registry))
    friday = evaluate_market(_inp(time_ctx=_time_ctx(weekday=4), registry=registry))
    assert thursday["direction"] == "short"
    assert thursday["time_direction"] == "short"
    assert friday["direction"] == "neutral"
    assert friday["time_direction"] == "neutral"
    # временной контекст проходит в снимок как есть
    assert thursday["time_context"]["weekday"] == 3
    assert friday["time_context"]["weekday"] == 4


def test_ac11_conflict_both_reasons_and_consistent_arrow():
    registry = _registry_with(_weekday_rule("short", [3]))
    out = evaluate_market(_inp(
        facts=[_fact("LIQ_RECOVERY", "long", summary="восстановление после выноса")],
        registry=registry,
    ))
    assert out["direction"] == "mixed"
    assert out["contradictory"] is True
    assert out["arrow"] == "conflict"
    assert out["market_line"] == "Противоречивый контекст"
    sides = {r["side"] for r in out["reasons"]}
    assert sides == {"long", "short"}
    groups = {r["group"] for r in out["reasons"]}
    assert groups == {"events", "time"}


def test_ac11_advantage_with_conflict_note():
    window_rule = replace(
        get_rule("TIME_WINDOW"), enabled=True, confirmation="validated",
        direction="short", thresholds={"hours_msk": [14]},
    )
    registry = _registry_with(_weekday_rule("long", [3]), window_rule)
    out = evaluate_market(_inp(
        facts=[_fact("LIQ_RECOVERY", "long")],
        registry=registry,
    ))
    assert out["direction"] == "long"      # 2 семьи long против 1 short
    assert out["contradictory"] is True
    assert out["conflict_note"] == "Контекст противоречивый"
    assert out["arrow"] == "up"
    assert out["market_line"] == "↑ LONG"
    assert {r["side"] for r in out["reasons"]} == {"long", "short"}


def test_ac12_known_neutral_state():
    out = evaluate_market(_inp())
    assert out["direction"] == "neutral"
    assert out["events_direction"] == "neutral"
    assert out["time_direction"] == "neutral"
    assert out["market_line"] == "Нейтрально"
    assert out["arrow"] == "flat"
    assert out["reasons"] == []
    assert out["contradictory"] is False
    assert out["conflict_note"] is None
    assert out["quality"] == "ok"


def test_ac13_insufficient_stats_no_invented_percent():
    stats = insufficient("n<min", n=5)
    out = evaluate_market(_inp(stats=stats))
    assert out["statistics"]["available"] is False
    assert out["statistics"]["value"] is None
    assert out["statistics"]["text"] == "Статистика недостаточна"


def test_basket_history_dependency_silence_keeps_group_evaluable():
    seq_rule = replace(
        get_rule("SEQ_WEEK_START"), enabled=True, confirmation="validated",
        direction="long",
    )
    registry = _registry_with(seq_rule)
    seq = {"mon_sign": 1, "tue_sign": 1, "condition_met": True}
    out = evaluate_market(_inp(
        time_ctx=_time_ctx(with_history=False, week_seq=seq), registry=registry,
    ))
    assert "basket_history" in out["unknown_dependencies"]
    assert out["time_direction"] == "neutral"  # правило молчит, группа считается
    assert out["direction"] == "neutral"
    # с историей корзины та же комбинация голосует
    out = evaluate_market(_inp(
        time_ctx=_time_ctx(week_seq=seq), registry=registry,
    ))
    assert out["time_direction"] == "long"
    assert out["direction"] == "long"


def test_quality_is_worst_of_mandatory_sources():
    out = evaluate_market(_inp(source_states=_states(price="stale")))
    assert out["quality"] == "stale"
    out = evaluate_market(_inp(source_states=_states(liq="auth_error")))
    assert out["quality"] == "unavailable"
    assert out["quality_by_dependency"]["liquidations"] == "auth_error"
    out = evaluate_market(_inp(source_states={}))
    assert out["quality"] == "unknown"


def test_next_review_is_min_of_day_boundary_and_fact_expiry():
    far = T0 + 200 * HOUR
    out = evaluate_market(_inp(facts=[_fact("LIQ_PRESSURE", "short", occurred=T0)]))
    assert out["next_review_at"] == min(T0 + DAY, T0 + 240 * HOUR)
    out = evaluate_market(_inp(time_ctx=_time_ctx()))
    assert out["next_review_at"] == T0 + DAY
    assert far > out["next_review_at"]


def test_contract_keys_exact():
    out = evaluate_market(_inp())
    assert set(out) == {
        "assessment_id", "scope", "timezone", "evaluated_at", "available_at",
        "rule_version", "basket_version", "direction", "events_direction",
        "time_direction", "contradictory", "quality", "quality_by_dependency",
        "coverage", "market_line", "subtitle", "conflict_note", "arrow",
        "reasons", "unknown_dependencies", "active_facts", "time_context",
        "statistics", "btc_background", "factor_agreement", "next_review_at",
    }
    assert out["scope"] == "crypto_market"
    assert out["timezone"] == "Europe/Moscow"
    assert out["rule_version"] == "events-time-2.0"
    assert out["subtitle"] == "Направление по событиям и времени"
    assert out["btc_background"] is None
