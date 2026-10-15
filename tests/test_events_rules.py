"""Реестр правил: уникальность, доступ, переопределения enabled, defaults."""
from __future__ import annotations

from app.events.rules import (
    ENGINE_RULE_VERSION,
    RULE_REGISTRY,
    RuleSpec,
    get_rule,
    registry_for_eval,
)


def test_rule_ids_unique():
    ids = [r.rule_id for r in RULE_REGISTRY]
    assert len(ids) == len(set(ids))


def test_all_rules_carry_engine_version():
    assert all(r.version == ENGINE_RULE_VERSION for r in RULE_REGISTRY)


def test_get_rule():
    rule = get_rule("LIQ_PRESSURE")
    assert isinstance(rule, RuleSpec)
    assert rule.rule_class == "market_event"
    assert rule.scope == "crypto_market"
    assert rule.direction == "short"
    assert rule.family == "liq"
    assert get_rule("NO_SUCH_RULE") is None


def test_liq_rules_enabled_by_default():
    assert get_rule("LIQ_PRESSURE").enabled is True
    assert get_rule("LIQ_RECOVERY").enabled is True
    assert get_rule("LIQ_FACT").enabled is True
    assert get_rule("LIQ_FACT").direction == "none"
    assert get_rule("LIQ_PRESSURE").priority > get_rule("LIQ_RECOVERY").priority


def test_unvalidated_time_rules_disabled_by_default():
    for rule_id in ("TIME_WEEKDAY", "TIME_WINDOW", "SEQ_WEEK_START"):
        rule = get_rule(rule_id)
        assert rule.enabled is False
        assert rule.confirmation == "unvalidated"
        assert rule.direction == "conditional"


def test_liq_thresholds_are_project_defaults():
    expected = {
        "long_share": 0.60,
        "episode_multiple": 1.5,
        "baseline_days": 30,
        "baseline_min_samples": 14,
        "fade_ratio": 0.5,
        "recovery_windows": 3,
        "window_ms": 4 * 3_600_000,
    }
    assert get_rule("LIQ_PRESSURE").thresholds == expected
    assert get_rule("LIQ_RECOVERY").thresholds == expected
    assert get_rule("LIQ_FACT").thresholds == {}


def test_registry_for_eval_default_matches_registry():
    assert registry_for_eval() == list(RULE_REGISTRY)
    assert registry_for_eval(None) == list(RULE_REGISTRY)


def test_registry_for_eval_honors_overrides():
    overridden = {r.rule_id: r.enabled for r in registry_for_eval(
        {"TIME_WEEKDAY": True, "LIQ_PRESSURE": False}
    )}
    assert overridden["TIME_WEEKDAY"] is True
    assert overridden["LIQ_PRESSURE"] is False
    assert overridden["LIQ_RECOVERY"] is True
    # Реестр-источник не мутирует.
    assert get_rule("TIME_WEEKDAY").enabled is False
    assert get_rule("LIQ_PRESSURE").enabled is True
