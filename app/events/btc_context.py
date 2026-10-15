"""Aligned daily BTC liquidation/OI evidence; never a vote for an altcoin basket."""
from __future__ import annotations

from decimal import Decimal

from .mathutil import DAY_MS, median, percentile
from .validation import finite_number


def btc_liquidation_oi(liquidations: list, oi_coin: list, *, now: int) -> dict:
    def valid(row, keys):
        return (row.get("quality") == "ok" and row["t"] % DAY_MS == 0
                and row["t"] + DAY_MS <= now
                and all(finite_number(row.get(k)) for k in keys))
    liq = {r["t"]: r for r in liquidations if valid(r, ("long_usd", "short_usd"))}
    oi = {r["t"]: r for r in oi_coin if valid(r, ("open", "close"))}
    expected = now // DAY_MS * DAY_MS - DAY_MS
    history = []
    for day in range(expected - 9 * DAY_MS, expected + DAY_MS, DAY_MS):
        row, interest = liq.get(day), oi.get(day)
        if row is None:
            continue
        baseline = [Decimal(str(liq[t]["long_usd"]))
                    for t in range(day - 30 * DAY_MS, day, DAY_MS) if t in liq]
        total = float(row["long_usd"]) + float(row["short_usd"])
        share = float(row["long_usd"]) / total if total > 0 else None
        threshold = percentile(baseline, Decimal("0.9")) if len(baseline) >= 14 else None
        base = median(baseline) if len(baseline) >= 14 else None
        cascade = (float(row["long_usd"]) > float(threshold) and share > 0.55
                   if threshold is not None and share is not None else None)
        change = (float(interest["close"]) / float(interest["open"]) - 1
                  if interest is not None and float(interest["open"]) > 0 else None)
        state = ("unknown" if cascade is None or change is None else
                 "cascade_oi_rising" if cascade and change > 0 else
                 "cascade_oi_falling" if cascade and change < 0 else
                 "cascade_oi_flat" if cascade else "no_cascade")
        history.append({"day_start": day, "day_end": day + DAY_MS, "closed": True,
                        "long_share": share, "long_usd": float(row["long_usd"]),
                        "long_multiple": float(Decimal(str(row["long_usd"])) / base) if base else None,
                        "baseline_samples": len(baseline), "oi_change": change, "state": state})
    current = next((r for r in history if r["day_start"] == expected), None)
    state = (current or {}).get("state", "unknown")
    texts = {
        "cascade_oi_rising": "Крупные лонг-ликвидации совпали с ростом OI. Разгрузка плеча не подтверждается.",
        "cascade_oi_falling": "Крупные лонг-ликвидации совпали со снижением OI. Есть разгрузка плеча; локальный минимум цены требует отдельной проверки.",
        "cascade_oi_flat": "Есть крупные лонг-ликвидации, OI не изменился. Условие разгрузки не подтверждено.",
        "no_cascade": "За последний завершённый день условие крупного каскада не выполнено.",
        "unknown": "Для совместной оценки ликвидаций и OI недостаточно сопоставимых завершённых данных.",
    }
    return {"scope": "BTC", "state": state, "summary": texts[state], "current": current,
            "recent_events": [r for r in history if r["state"].startswith("cascade_")],
            "used_for_market_direction": False,
            "scope_note": "BTC · ликвидации Binance · OI в монетах, агрегат бирж · дни UTC. Области источников отличаются.",
            "method_note": "Сравнение с p90 предыдущих 30 дней, минимум 14 наблюдений — проектная адаптация, не воспроизведение выборки PDF."}
