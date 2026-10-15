"""Incremental BTC history for calendar samples; no network work in GET routes."""
from __future__ import annotations

import json
import logging

from . import sources, store
from .calendar import BACKFILL_DAYS, BTC_HISTORY_START, BTC_SERIES, CALENDAR_VERSION, daily_prices
from .mathutil import DAY_MS
from .timeutil import msk_day_start_ms
from .validation import hourly_bars

log = logging.getLogger(__name__)
STATE_KEY = "events:calendar_history"


def update_calendar_history(db, settings, fetcher: dict, bars_by_symbol: dict, *, now: int) -> None:
    """Keep daily OHLC summaries, fetch at most one bounded backfill chunk per cycle.

    Custom fetchers never cause an implicit network request. A caller can supply
    calendar_prices(settings, pairs, start_ms, end_ms) to exercise history loading.
    """
    today = msk_day_start_ms(now)
    saved = {r["bucket_start"]: r for r in store.metric_series(db, BTC_SERIES)
             if r.get("method_version") == CALENDAR_VERSION}
    raw = db.get_meta(STATE_KEY)
    state = json.loads(raw) if raw else {}

    def save(rows):
        changed = []
        for row in rows:
            if row["bucket_start"] < BTC_HISTORY_START:
                continue
            # Existing complete OHLC must not lose its range to return-only input.
            previous = saved.get(row["bucket_start"])
            if previous and previous.get("range") is not None and row.get("range") is None:
                continue
            if previous != row:
                changed.append(row)
        store.upsert_metrics(db, series=BTC_SERIES, rows=changed)
        saved.update((r["bucket_start"], r) for r in changed)

    save(daily_prices(bars_by_symbol.get("BTC", []), now=now))
    fn = fetcher.get("calendar_prices")
    network_allowed = fn is not None or not fetcher
    error = None
    # Refresh the latest two days if basket prices did not supply complete OHLC.
    fresh = all((saved.get(today - d * DAY_MS) or {}).get("range") is not None for d in (1, 2))
    cursor = min(int(state.get("backfill_before") or today), today)
    ranges = []
    if not fresh and cursor != today:
        ranges.append((max(BTC_HISTORY_START, today - 3 * DAY_MS), today))
    if cursor > BTC_HISTORY_START:
        ranges.append((max(BTC_HISTORY_START, cursor - BACKFILL_DAYS * DAY_MS), cursor))
    else:
        gaps = [t for t in range(BTC_HISTORY_START, today, DAY_MS) if t not in saved]
        if gaps:
            after = int(state.get("repair_after") or 0)
            repair = next((t for t in gaps if t > after), gaps[0])
            ranges.append((repair, repair + DAY_MS))
            state["repair_after"] = repair  # rotate even if a provider gap is permanent
    if network_allowed:
        for start, end in ranges:
            if start >= end:
                continue
            try:
                if fn:
                    by_pair, failed = fn(settings, ["BTCUSDT"], start, end)
                else:
                    by_pair, failed = sources.fetch_prices(settings, ["BTCUSDT"], start_ms=start, end_ms=end)
                valid = hourly_bars(by_pair.get("BTCUSDT", [])).values()
                rows = daily_prices([b for b in valid if start <= b["start"] < end], now=end)
                if "BTCUSDT" in failed or not rows:
                    raise ValueError("Нет полных дней BTCUSDT в запрошенном интервале")
                save(rows)
                # Advance only a successful backfill; recent refresh cannot skip history.
                if end == cursor:
                    cursor = start
            except Exception as exc:
                log.warning("events: история календаря BTC: %s", exc)
                error = "Не удалось обновить историю BTC. Сохранённые полные дни доступны; загрузка будет повторена."
                break
    count = len(saved)
    missing = max(0, (today - cursor) // DAY_MS - sum(cursor <= t < today for t in saved))
    state = {"quality": "partial" if error else "backfilling" if cursor > BTC_HISTORY_START else "partial" if missing else "ok",
             "detail": error, "updated_at": now, "backfill_before": cursor,
             "history_start": BTC_HISTORY_START, "days": count,
             "period": {"from": min(saved), "to": max(saved)} if saved else None,
             "remaining_days": max(0, (cursor - BTC_HISTORY_START) // DAY_MS),
             "missing_days": missing, "repair_after": state.get("repair_after"),
             "method_version": CALENDAR_VERSION}
    db.set_meta(STATE_KEY, json.dumps(state, ensure_ascii=False))
