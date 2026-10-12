"""Версионированный реестр правил направления крипторынка (§6 ТЗ).

Каждое правило фиксирует происхождение, зависимости, окно, формулу,
состояние подтверждения и направление. Честность подтверждения (§2, §7):
направления experimental/unvalidated — проектные гипотезы и примеры
владельца, а не доказанные статистические факты; примеры «четверг —
худший день для лонгов» и «бычьи пн+вт» этим ТЗ не установлены.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

ENGINE_RULE_VERSION = "events-time-2.0"

HOUR_MS = 3_600_000

# Проектные defaults (§1): не согласованы владельцем и не являются
# результатами исследования; переиспользованы только в scope/окне,
# для которых заданы.
_LIQ_THRESHOLDS = {
    "long_share": 0.60,
    "episode_multiple": 1.5,
    "baseline_days": 30,
    "fade_ratio": 0.5,
    "recovery_windows": 3,
    "window_ms": 4 * HOUR_MS,
}


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    version: str
    origin: str                # "owner_answer" | "project_default" | "previous_spec"
    rule_class: str            # "market_event" | "time_condition" | "combination" | "service_change"
    scope: str                 # "crypto_market" | "BTC"
    dependencies: tuple[str, ...]
    window: str                # человекочитаемое окно (RU)
    formula: str               # человекочитаемая формула (RU)
    confirmation: str          # "unvalidated" | "experimental" | "validated"
    direction: str             # "long" | "short" | "conditional" | "none"
    explanation: str
    family: str                # корреляционная семья для дедупликации голосов
    priority: int              # внутри семьи побеждает больший
    enabled: bool
    thresholds: dict           # версионированные проектные defaults


RULE_REGISTRY: tuple[RuleSpec, ...] = (
    RuleSpec(
        rule_id="LIQ_PRESSURE",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="market_event",
        scope="crypto_market",
        dependencies=("price", "liquidations"),
        window="Эпизод ликвидаций: окно 4ч; норма — baseline аналогичного окна за 30 дней",
        formula=(
            "доля лонг-ликвидаций >= long_share и объём >= episode_multiple × "
            "нормы окна, цена и ширина корзины продолжают падать"
        ),
        confirmation="experimental",
        direction="short",
        explanation=(
            "Усиление лонг-ликвидаций при падающей цене и ширине рынка — "
            "давление на рынок, SHORT. Пороги проектные, подтверждение "
            "экспериментальное — не доказанный статистический факт."
        ),
        family="liq",
        priority=100,
        enabled=True,
        thresholds=dict(_LIQ_THRESHOLDS),
    ),
    RuleSpec(
        rule_id="LIQ_RECOVERY",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="market_event",
        scope="crypto_market",
        dependencies=("price", "liquidations"),
        window="Затухание эпизода: recovery_windows окон по 4ч после пика каскада",
        formula=(
            "после эпизода лонг-ликвидаций активность ниже fade_ratio от пика "
            "в течение recovery_windows окон, цена и ширина корзины восстанавливаются"
        ),
        confirmation="experimental",
        direction="long",
        explanation=(
            "Затухание каскада лонг-ликвидаций и восстановление цены и ширины "
            "рынка — контекст восстановления, LONG. Критерии «затухает» и "
            "«восстанавливается» — проектные (fade_ratio, recovery_windows), "
            "статистически не подтверждены."
        ),
        family="liq",
        priority=90,
        enabled=True,
        thresholds=dict(_LIQ_THRESHOLDS),
    ),
    RuleSpec(
        rule_id="LIQ_FACT",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="market_event",
        scope="crypto_market",
        dependencies=("liquidations",),
        window="Окно наблюдения ликвидаций (4ч)",
        formula="фиксация объёма и доли лонг-ликвидаций без интерпретации направления",
        confirmation="validated",
        direction="none",
        explanation=(
            "Известен только объём ликвидаций: факт записывается, направление "
            "данного условия не задаётся (§6). Чистый факт, не голосует."
        ),
        family="liq",
        priority=10,
        enabled=True,
        thresholds={},
    ),
    RuleSpec(
        rule_id="TIME_WEEKDAY",
        version=ENGINE_RULE_VERSION,
        origin="owner_answer",
        rule_class="time_condition",
        scope="crypto_market",
        dependencies=("calendar", "basket_history"),
        window="Московский календарный день [00:00, 00:00 следующего дня) МСК",
        formula=(
            "день недели МСК → временное предпочтение LONG/SHORT по методике; "
            "подтверждённой методики пока нет"
        ),
        confirmation="unvalidated",
        direction="conditional",
        explanation=(
            "День недели как временное условие. Пример владельца «четверг — "
            "худший день для лонгов» статистически не подтверждён (§7): правило "
            "не голосует и показано в реестре до появления явной методики."
        ),
        family="weekday",
        priority=50,
        enabled=False,
        thresholds={},
    ),
    RuleSpec(
        rule_id="TIME_WINDOW",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="time_condition",
        scope="crypto_market",
        dependencies=("calendar",),
        window="Внутридневные окна МСК (границы окон не заданы)",
        formula="час МСК внутри зарегистрированного окна → условное предпочтение; окна не заданы",
        confirmation="unvalidated",
        direction="conditional",
        explanation=(
            "Внутридневное временное условие по Москве. Методика и сами окна "
            "не подтверждены; правило отключено и не голосует."
        ),
        family="intraday",
        priority=50,
        enabled=False,
        thresholds={},
    ),
    RuleSpec(
        rule_id="SEQ_WEEK_START",
        version=ENGINE_RULE_VERSION,
        origin="owner_answer",
        rule_class="combination",
        scope="crypto_market",
        dependencies=("calendar", "basket_history"),
        window="Завершённые понедельник и вторник МСК текущей недели",
        formula=(
            "медианная доходность корзины за завершённые пн и вт строго > 0 "
            "→ вход недельной комбинации"
        ),
        confirmation="unvalidated",
        direction="conditional",
        explanation=(
            "Завершённые бычьи понедельник и вторник — вход комбинации недели; "
            "само по себе не задаёт среду/четверг (§6). Методика не подтверждена "
            "владельцем, правило отключено и не голосует."
        ),
        family="week_seq",
        priority=60,
        enabled=False,
        thresholds={},
    ),
    RuleSpec(
        rule_id="SRC_DEGRADED",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="service_change",
        scope="crypto_market",
        dependencies=("price", "liquidations", "oi", "funding"),
        window="От обнаружения деградации до восстановления источника",
        formula="обязательная зависимость недоступна или просрочена → сервисное изменение",
        confirmation="validated",
        direction="none",
        explanation=(
            "Ухудшение данных источника: связанные рыночные правила становятся "
            "неизвестными. Служебное событие не голосует за LONG, SHORT или "
            "нейтральность."
        ),
        family="service",
        priority=10,
        enabled=True,
        thresholds={},
    ),
    RuleSpec(
        rule_id="SRC_RECOVERED",
        version=ENGINE_RULE_VERSION,
        origin="project_default",
        rule_class="service_change",
        scope="crypto_market",
        dependencies=("price", "liquidations", "oi", "funding"),
        window="Момент восстановления источника",
        formula="зависимость снова доступна и свежая → сервисное изменение",
        confirmation="validated",
        direction="none",
        explanation=(
            "Восстановление данных источника; публикуется один раз на "
            "восстановление, не голосует за направление."
        ),
        family="service",
        priority=10,
        enabled=True,
        thresholds={},
    ),
)

_RULES_BY_ID = {r.rule_id: r for r in RULE_REGISTRY}


def get_rule(rule_id: str) -> RuleSpec | None:
    """Правило реестра по rule_id или None."""
    return _RULES_BY_ID.get(rule_id)


def registry_for_eval(enabled_overrides: dict[str, bool] | None = None) -> list[RuleSpec]:
    """Полный реестр для оценки с enabled после переопределений.

    Переопределения меняют только флаг enabled; отключённые правила
    остаются в списке (видимость реестра), голосовать не должны.
    """
    if not enabled_overrides:
        return list(RULE_REGISTRY)
    return [
        replace(r, enabled=enabled_overrides[r.rule_id])
        if r.rule_id in enabled_overrides else r
        for r in RULE_REGISTRY
    ]
