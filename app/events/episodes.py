"""Closed liquidation windows and their aftermath, independent of polling time."""
from __future__ import annotations

from statistics import median
from .validation import finite_number

DAY_MS = 86_400_000


def liquidation_history(per_symbol: dict, *, now: int, baseline_days: int,
                        thresholds: dict, memory_ms: int,
                        min_coverage: float) -> list[dict]:
    window_ms = int(thresholds["window_ms"])
    # Defensive validation also protects callers replaying old persisted samples.
    per_symbol = {symbol: {t: row for t, row in rows.items()
                          if t % window_ms == 0 and finite_number(row.get("long_usd"))
                          and finite_number(row.get("short_usd"))}
                  for symbol, rows in sorted(per_symbol.items())}
    total = len(per_symbol)
    last = now // window_ms * window_ms - window_ms
    starts = sorted({t for rows in per_symbol.values() for t in rows
                     if now - memory_ms - baseline_days * DAY_MS <= t <= last})
    history = []
    episode = None
    previous = None
    quiet = 0
    was_hot = False
    for start in starts:
        covered = [s for s, rows in per_symbol.items() if start in rows]
        long = sum(float(per_symbol[s][start]["long_usd"]) for s in covered)
        short = sum(float(per_symbol[s][start]["short_usd"]) for s in covered)
        baselines = []
        samples = []
        for symbol in covered:
            values = [float(per_symbol[symbol][start - k * DAY_MS]["long_usd"])
                      for k in range(1, baseline_days + 1)
                      if start - k * DAY_MS in per_symbol[symbol]]
            samples.append(len(values))
            if len(values) >= int(thresholds.get("baseline_min_samples", 14)):
                baselines.append(median(values))
        ratio = len(covered) / total if total else 0.0
        baseline_ok = (bool(covered) and ratio >= min_coverage
                       and len(baselines) == len(covered) and sum(baselines) > 0)
        multiple = long / sum(baselines) if baseline_ok else None
        share = long / (long + short) if long + short > 0 else None
        known = baseline_ok and ratio >= min_coverage
        hot = (known and multiple >= float(thresholds["episode_multiple"])
               and share is not None and share >= float(thresholds["long_share"]))
        # A missing window cannot prove the end of a cascade or its fading.
        contiguous = previous is not None and previous + window_ms == start
        if not contiguous or not known:
            quiet = 0
            was_hot = False
        phase = None
        if hot:
            if (not was_hot or covered != episode["symbols"]
                    or (quiet >= int(thresholds["recovery_windows"])
                        and long >= float(thresholds["fade_ratio"]) * episode["peak_long_usd"])):
                episode = {"started_at": start, "peak_long_usd": long, "symbols": covered}
            else:
                episode["peak_long_usd"] = max(episode["peak_long_usd"], long)
            quiet = (quiet + 1 if contiguous and covered == episode["symbols"]
                     and long < float(thresholds["fade_ratio"]) * episode["peak_long_usd"] else 0)
            phase = ("fading" if quiet >= int(thresholds["recovery_windows"])
                     else "intensifying")
        elif known and episode is not None:
            quiet = (quiet + 1 if contiguous
                     and covered == episode["symbols"]
                     and long < float(thresholds["fade_ratio"]) * episode["peak_long_usd"]
                     else 0)
            phase = ("fading" if quiet >= int(thresholds["recovery_windows"])
                     else "post_cascade")
        if start + window_ms > now - memory_ms:
            history.append({
                "available": ratio >= min_coverage and bool(covered),
                "baseline_ok": baseline_ok,
                "baseline_samples": min(samples) if samples else 0,
                "window": {"start": start, "end": start + window_ms, "closed": True},
                "sum_long_usd": long, "sum_short_usd": short,
                "long_share": share, "multiple": multiple,
                "coverage": {"ok": len(covered), "total": total, "ratio": ratio},
                "episode": {**(episode or {}), "active": hot, "phase": phase,
                            "started_at": (episode or {}).get("started_at"),
                            "quiet_windows": quiet},
            })
        previous, was_hot = start, hot
    return history
