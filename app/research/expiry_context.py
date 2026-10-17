"""Time-dependent context from the owner's expiry study, separate from model scores."""
from __future__ import annotations

from datetime import datetime, timezone
import calendar

from .catalysts import Catalysts, DAY

STUDY_NOTE = (
    "Источник статистики: исследование, предоставленное пользователем. "
    "Период и размер выборки не указаны; показатели не пересчитаны на данных сервиса. "
    "Это исторические средние и частоты, не вероятность текущей сделки. "
    "Применение ограничено месячными и квартальными экспирациями BTC/ETH; "
    "направление всей корзины рынка из них не выводится."
)
STATS = {
    "BTC": [
        "Дневной диапазон: 3.82% против 4.61% в обычную пятницу (t = −2.7).",
        "Квартальные: диапазон 3.65% против 3.90% у месячных; дополнительная волатильность не выявлена.",
        "Пятница open→close: −0.72%, рост в 35% случаев; обычная: +0.18%, рост в 53% (t = −2.6).",
        "00:00–08:00 UTC: −0.50% против +0.11%; 08:00–16:00 UTC: −0.03%.",
        "Четверг перед экспирацией: +0.25% против −0.29% у обычного четверга.",
        "Понедельник после: +0.36% против +0.35%; рост 52% против 54% — отдельного эффекта нет.",
        "Дрейф к расчёту: 0.85% против 1.00% в обычную пятницу; всплеск в 08:00 не подтверждён.",
    ],
    "ETH": [
        "Дневной диапазон: 5.03% против 5.79% в обычную пятницу (t = −1.8).",
        "Пятница open→close: −0.55%, рост в 44% случаев (t = −1.3); тенденция слабее BTC.",
        "Числовой оценки ETH для четверга, отдельных часов и понедельника в переданном исследовании нет.",
    ],
}


def expiry_context(event, at, *, day_view=False):
    """Recognize verified instrument identity, including already stored provider rows."""
    if (event.get("kind") != "expiry" or event.get("precision") != "exact"
            or event.get("review_status") != "accepted"
            or event.get("stage") in {"cancelled", "denied"}):
        return None
    expires = event.get("starts_at")
    if not isinstance(expires, int):
        return None
    currency = next((currency for currency in STATS for source in event.get("sources", [])
                     if source.get("namespace") == "deribit"
                     and source.get("external_id") == f"{currency}:option:{expires}"), None)
    if currency is None:
        return None
    dt = datetime.fromtimestamp(expires / 1000, timezone.utc)
    monthly = dt.weekday() == 4 and (dt.day + 7 > calendar.monthrange(dt.year, dt.month)[1])
    standard_time = (dt.hour, dt.minute, dt.second, dt.microsecond) == (8, 0, 0, 0)
    cycle = ("quarterly" if dt.month in {3, 6, 9, 12} else "monthly") if monthly else ("weekly" if dt.weekday() == 4 else "daily")
    applicable = monthly and standard_time
    start = expires // DAY * DAY
    today = at // DAY * DAY
    side, active = "neutral", False
    if not applicable:
        phase, title = "outside_study", "Статистика месячных экспираций здесь не применяется"
        note = "Для ежедневной, недельной или нестандартной по времени экспирации отдельная выборка не предоставлена."
    elif today == start - DAY:
        phase, title = "thursday", "Четверг перед экспирацией: слабый бычий фон BTC"
        side = "long" if currency == "BTC" else "neutral"
        active = currency == "BTC"
        note = "Исторически BTC +0.25% против −0.29% у обычного четверга. Для ETH отдельной оценки нет. Основания для шорта только по календарю нет."
    elif today == start and (at < expires or day_view):
        phase = "expiry_day" if day_view else "before_08"
        title = "Экспирационная пятница: историческое давление до 08:00 UTC"
        side, active = "short", True
        note = ("Для BTC среднее движение 00:00–08:00 UTC −0.50%; у ETH дневная медвежья тенденция слабее, почасовой оценки нет. "
                "Ожидаемый режим — подавление диапазона / пиннинг, без подтверждённого всплеска в 08:00 UTC (11:00 МСК). "
                "После расчёта направленный фактор исчерпан.")
    elif today == start:
        phase, title = "after_08", "Расчёт прошёл: направленный фактор исчерпан"
        note = "После 08:00 UTC (11:00 МСК) у BTC сегмент 08–16 близок к нулю: −0.03%. Дневной диапазон исторически подавлен, всплеск от экспирации не подтверждён."
    elif start < today <= start + 3 * DAY:
        phase = "monday" if today == start + 3 * DAY else "weekend"
        title = "Эффект экспирации не переносится на следующую неделю"
        note = "Понедельник BTC +0.36% против обычных +0.35%. Календарного основания держать экспирационный сценарий через выходные нет."
    else:
        phase, title = "upcoming" if at < expires else "past", "Экспирация вне текущего окна влияния"
        note = "Исторический сценарий относится к четвергу и пятнице до 08:00 UTC. Сейчас направленный фактор не активен."
    return {"currency": currency, "cycle": cycle, "expires_at": expires, "phase": phase,
            "title": title, "note": note, "side": side, "active": active,
            "study_applicable": applicable, "statistics": STATS[currency] if applicable else [],
            "study_note": STUDY_NOTE, "evaluated_at": at, "day_view": day_view}


def market_expiries(store, *, at=None, day_view=False):
    """Only dates actually observed in the catalog; no inferred future schedule."""
    moment = store.clock() if at is None else at
    items, after = [], None
    while True:
        page = Catalysts(store).list(kind="expiry", review_status="accepted", limit=100,
                                    from_at=moment - 4 * DAY, to_at=moment + 62 * DAY, after=after)
        for event in page["items"]:
            context = expiry_context(event, moment, day_view=day_view)
            if context and context["study_applicable"]:
                items.append({**context, "catalyst_id": event["id"]})
        after = page["next_cursor"]
        if not after:
            break
    # Prefer the current expiry window over the next month's date, separately per asset.
    chosen = []
    for currency in STATS:
        candidates = [item for item in items if item["currency"] == currency]
        candidates.sort(key=lambda item: (item["phase"] in {"upcoming", "past"}, abs(item["expires_at"] - moment)))
        if candidates:
            item = candidates[0]
            state = store.state("deribit:" + currency)
            last = state.get("last_success_at")
            item["catalog_quality"] = "unavailable" if not last else "stale" if store.clock() - last > DAY or state.get("error_code") else "ok"
            item["catalog_checked_at"] = last
            chosen.append(item)
    return {"items": chosen, "evaluated_at": moment, "day_view": day_view,
            "empty_reason": "В сохранённом каталоге нет подтверждённых месячных экспираций BTC/ETH для этого периода." if not chosen else None}
