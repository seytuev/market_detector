"""One presentation model for market notification caption and branded image.

Intermediate journal transitions are not simultaneous current market signals.
Only the bundle's final assessment supplies the current direction and reasons.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..notify.formatting import fmt_time_msk

STATUS = {
    "long": ("↑", "Перевес в сторону роста", "перевес в сторону роста", "Рост"),
    "short": ("↓", "Перевес в сторону снижения", "перевес в сторону снижения", "Снижение"),
    "neutral": ("↔", "Направленного перевеса нет", "направленного перевеса нет", "Без перевеса"),
    "mixed": ("⇄", "Противоречивый контекст", "противоречивый контекст", "Противоречие"),
    "unknown": ("?", "Направление неизвестно", "направление неизвестно", "Нет данных"),
}
QUALITY = {"ok": "Данные доступны", "partial": "Данные неполные", "stale": "Данные устарели",
           "unavailable": "Данные недоступны", "auth_error": "Нет доступа к источнику",
           "capability_error": "Источник не поддерживает запрос"}
DEPENDENCIES = {"price": "цены", "liquidations": "ликвидации", "liquidations_baseline": "история ликвидаций",
                "oi": "открытый интерес", "open_interest": "открытый интерес", "funding": "ставки финансирования",
                "event_history": "история события", "basket_history": "история корзины"}


def _short(text, limit=130):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def _reason(reason):
    known = {
        "LIQ_RECOVERY": "После каскада цена и ширина рынка восстанавливаются.",
        "LIQ_PRESSURE": "Ликвидации сопровождаются снижением цены и ширины рынка.",
    }
    expected = {"LIQ_RECOVERY": "long", "LIQ_PRESSURE": "short"}
    text = (known.get(reason.get("rule_id")) if expected.get(reason.get("rule_id")) == reason.get("side") else None)
    text = text or re.sub(r"^(События|Время):\s*", "", str(reason.get("text") or ""))
    text = text.replace("(пороги проектные)", "").strip()
    if reason.get("side") in ("long", "short"):
        text = ("За рост: " if reason["side"] == "long" else "За снижение: ") + text
    return _short(text)


def _current_reasons(assessment, pubs):
    if assessment.get("direction") == "unknown":
        unknown = assessment.get("unknown_dependencies") or []
        return ["Для общей оценки недостаточно проверяемых данных."] + ([
            "Не учтено: " + ", ".join(DEPENDENCIES.get(str(x), "непроверенная зависимость") for x in unknown[:3]) + "."
        ] if unknown else [])
    reasons = assessment.get("reasons") or []
    if assessment.get("contradictory"):
        # Preserve both sides even if multiple first reasons share one side.
        reasons = [next((r for r in reasons if r.get("side") == side), None) for side in ("long", "short")]
    items = [text for r in reasons if r for text in [_reason(r)] if text]
    changed = {(p.get("payload") or {}).get("fact_key") for p in pubs}
    if not items:
        for fact in assessment.get("active_facts") or []:
            if fact.get("fact_key") not in changed:
                continue
            status = fact.get("context_status")
            if status == "awaiting_confirmation":
                items.append("Прежнее давление снято; новое направление пока не подтверждено.")
            elif status == "invalidated":
                items.append("Направленная трактовка прежнего события отменена.")
            elif status == "unverified":
                items.append("Текущее влияние прежнего события пока нельзя проверить.")
            elif status == "non_directional":
                items.append(_short(fact.get("summary") or "Объём ликвидаций сохранён; сам по себе он не задаёт направление."))
    if not items:
        items.append({
            "neutral": "Учтённые условия не дают направленного перевеса.",
            "mixed": "Факторы роста и снижения противоречат друг другу.",
            "unknown": "Для общей оценки недостаточно проверяемых данных.",
        }.get(assessment.get("direction"), "Подробные основания оценки доступны в журнале."))
    unknown = assessment.get("unknown_dependencies") or []
    if unknown and not assessment.get("contradictory"):
        items.append("Не учтено: " + ", ".join(DEPENDENCIES.get(str(x), "непроверенная зависимость") for x in unknown[:3]) + ".")
    return list(dict.fromkeys(items))[:2]


def build_notice(assessment: dict | None, pubs: list[dict], bundle: dict, *, base_url="", mode="changes") -> dict:
    assessment = assessment or {}
    direction = assessment.get("direction")
    if direction not in STATUS:
        direction = "unknown"
    icon, title, caption_title, status = STATUS[direction]
    main = [p for p in pubs if p.get("kind") == "direction" and not (p.get("payload") or {}).get("group")]
    first = (main[0].get("payload") or {}).get("from") if main else None
    if first in STATUS and first != direction:
        change = f"{STATUS[first][3]} → {status}"
        change_title = "Направление изменилось"
    else:
        change = status
        change_title = "Направление сохранено" if main else "Оценка обновлена"
        kinds = {p.get("kind") for p in pubs}
        if not main:
            if kinds & {"source_degraded", "source_recovered"}:
                change_title = "Состояние источников изменилось"
            elif "coverage" in kinds:
                change_title = "Покрытие данных изменилось"
            elif "stats" in kinds:
                change_title = "Историческая статистика обновлена"
            elif kinds == {"fact_late_record"}:
                change_title = "История событий дополнена"
            elif any(str(k).startswith("fact_") for k in kinds):
                change_title = "Контекст событий обновлён"
            elif "time_boundary" in kinds:
                change_title = "Новый календарный день МСК"
    payload = bundle.get("payload") or {}
    if mode == "digest":
        minutes = max(1, int(payload.get("gap_ms") or 0) // 60_000)
        duration = f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"
        change_title, change = "После перерыва · последняя сохранённая оценка", f"Без новой оценки: {duration}"
    elif mode == "updated":
        change_title, change = "Сводка актуализирована", "Последняя сохранённая оценка"
    coverage = assessment.get("coverage") or {}
    ok, total = coverage.get("ok"), coverage.get("total")
    coverage_text = f"{ok}/{total}" if isinstance(ok, int) and isinstance(total, int) and total > 0 else "Нет данных"
    as_of = assessment.get("evaluated_at")
    stamp = fmt_time_msk(as_of) if as_of is not None else "Время оценки не получено"
    assessment_id = assessment.get("assessment_id")
    url = f"{base_url.rstrip('/')}/events.html?assessment={quote(str(assessment_id), safe='')}" if base_url and assessment_id else ""
    journal_url = f"{base_url.rstrip('/')}/?kind=market&scope=context#journal" if base_url else ""
    reasons = _current_reasons(assessment, pubs)
    if mode == "digest":
        counts = payload.get("counts") or {}
        count = sum(int(v) for v in counts.values() if isinstance(v, int) and v > 0)
        reasons = ([f"Изменений в журнале: {count}." if count else "Изменений в журнале не зафиксировано.",
                    "Дождитесь новой оценки: сохранённое направление могло измениться."])
    return {"direction": direction, "icon": icon, "title": title, "caption_title": caption_title,
            "status": status, "mode": mode, "change_title": change_title, "change": change,
            "reasons": reasons, "as_of": as_of, "stamp": stamp,
            "coverage": coverage_text, "quality": QUALITY.get(assessment.get("quality"), "Качество не определено"),
            "events": STATUS.get(assessment.get("events_direction"), STATUS["unknown"])[3],
            "time": "Не влияет" if assessment.get("time_rules_active") is False else STATUS.get(assessment.get("time_direction"), STATUS["unknown"])[3],
            "assessment_id": assessment_id, "url": url, "journal_url": journal_url,
            "experimental": bool(assessment.get("methodology")),
            "unknown": [DEPENDENCIES.get(str(x), "непроверенная зависимость") for x in assessment.get("unknown_dependencies") or []]}


def notice_text(notice: dict, *, include_url=True) -> str:
    lines = [f"{notice['icon']} Рынок: {notice['caption_title']}", "STV Market Researcher", "",
             f"{notice['change_title']}: {notice['change']}", ""]
    lines.extend("• " + reason for reason in notice["reasons"])
    if notice["mode"] == "digest" and notice["unknown"]:
        lines.append("Не учтено: " + ", ".join(notice["unknown"][:3]))
    lines.extend(["", f"Оценка на: {notice['stamp']}", f"{notice['quality']} · покрытие {notice['coverage']}"])
    if notice["experimental"]:
        lines.append("Экспериментальная модель корзины")
    if notice["mode"] == "updated":
        lines.append("Исходные изменения устарели до отправки; показана новая сводка.")
    if include_url and notice["url"]:
        lines.extend(["", notice["url"]])
    return "\n".join(lines)
