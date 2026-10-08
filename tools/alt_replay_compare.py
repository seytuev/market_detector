"""Этап 6: изолированный replay-сравнитель v1/v2 на эталонах (ТЗ 07.10.2026,
§5/§8; fixtures tests/fixtures/alt_v2/).

Для каждого тикера эталона прогоняет v1 (app/alt/engine.py AltEngine) и v2
(app/alt/engine_v2.py AltEngineV2) в ОТДЕЛЬНЫХ in-memory БД (без уведомлений,
без живой БД), сопоставляет результат с markup.json (допустимые интервалы
L/U, окна дат, роли эпизодов) и пишет отчёт:

- data/diag/alt_replay_compare_<date>.json — полные данные прогонов и матчинга;
- data/diag/alt_replay_compare_<date>.md — краткая сводка по тикерам:
  совпадения/пропуски/ложные базы, сдвиги границ и времён доступности,
  выбор актуального эпизода (R-08), сверка базы v1 с v2.

Конфиг — репозиторные дефолты (load_alt_config(), ENV HTF_ALT_* применимы).
Запуск: .venv/Scripts/python.exe tools/alt_replay_compare.py
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.alt.engine import AltEngine  # noqa: E402
from app.alt.engine_v2 import AltEngineV2  # noqa: E402
from app.config import AltConfig, load_alt_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.models_alt import (  # noqa: E402
    AltAsset,
    AltCandle,
    AltInstrumentSource,
)
from app.alt.replay_match import (  # noqa: E402
    intervals_overlap,
    published_episode,
    selection_matches,
)
from app.services.alt_overview import select_current_episode  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "alt_v2"
OUT_DIR = ROOT / "data" / "diag"
DAY_MS = 86_400_000
# допуск совпадения границы с интервалом разметки (погрешность чтения
# разметки со скриншотов и медианных линий кластеров)
MATCH_EPS_REL = 0.02
# допуск окна для base_end у окна breakout разметки (дней)
BASE_END_WINDOW_DAYS = 20
# даты разметки ориентировочные; год сдвига этим допуском не проходит
TIME_PAD_DAYS = 90


def date_to_ms(s: str) -> int:
    dt = datetime.datetime.strptime(s, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    return int(dt.timestamp() * 1000)


def ms_to_date(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).date().isoformat()


def load_markup() -> dict:
    return json.loads((FIXTURES / "markup.json").read_text(encoding="utf-8"))


def load_ohlc(ticker: str) -> list[AltCandle]:
    rows = json.loads(
        (FIXTURES / "ohlc" / f"{ticker}.json").read_text(encoding="utf-8")
    )
    return [
        AltCandle(source_id=1, open_time=r["open_time"], open=r["open"],
                  high=r["high"], low=r["low"], close=r["close"])
        for r in rows
    ]


def fresh_db(ticker: str, earliest_ms: int) -> Database:
    db = Database(":memory:")
    db.upsert_alt_asset(AltAsset(id=None, cmc_id=1, symbol=ticker, name=ticker))
    db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=1, venue="binance", symbol=f"{ticker}USDT",
        earliest_available_ms=earliest_ms, history_scope="full",
    ))
    return db


def episode_span_ms(ep: dict) -> tuple[int | None, int | None]:
    """Интервал эпизода. Открытая база тянется вперёд, но не на годы вне старта."""
    start_raw = ep.get("base_start") or ep.get("anchor")
    end_raw = ep.get("base_end") or ep.get("accompaniment_end")
    start = date_to_ms(start_raw) if start_raw else None
    end = date_to_ms(end_raw) if end_raw else None
    if start is not None and end is None:
        end = start + 800 * DAY_MS
    return start, end


def in_interval(x: float, iv: dict, eps_rel: float = MATCH_EPS_REL) -> bool:
    lo, hi = iv["min"], iv["max"]
    return lo * (1 - eps_rel) <= x <= hi * (1 + eps_rel)


def run_ticker(ticker: str, markup_eps: list[dict], cfg: AltConfig) -> dict:
    candles = load_ohlc(ticker)
    as_of_ms = candles[-1].open_time + DAY_MS

    # --- v1 (изолированная БД; движок v1 не изменён) ---
    db1 = fresh_db(ticker, candles[0].open_time)
    s1 = AltEngine(db1, cfg).process_asset_history(1, 1, candles)
    v1_frozen = []
    for r in db1.conn.execute("SELECT * FROM alt_frozen_range").fetchall():
        v1_frozen.append({
            "id": r["id"], "range_id": r["range_id"],
            "lower": r["lower"], "upper": r["upper"],
            "mature_at": ms_to_date(r["mature_at_ms"]),
            "start": ms_to_date(r["start_anchor_open_time"]),
        })
    v1_setups = [
        {"id": r["id"], "state": r["state"], "range_id": r["range_id"]}
        for r in db1.conn.execute("SELECT * FROM alt_setup").fetchall()
    ]
    db1.close()

    # --- v2 (изолированная БД) ---
    db2 = fresh_db(ticker, candles[0].open_time)
    s2 = AltEngineV2(db2, cfg).process_asset_history(1, 1, candles)
    episodes = []
    for e in db2.list_alt_range_episodes(1):
        q = json.loads(e.quality_json)
        episodes.append({
            "id": e.id, "state": e.state, "origin_key": e.origin_key,
            "anchor": ms_to_date(e.anchor_start_open_time),
            "base_start": ms_to_date(e.base_start_open_time),
            "base_end": ms_to_date(e.base_end_open_time),
            "base_end_reason": e.base_end_reason,
            "base_end_confirmed": ms_to_date(e.base_end_confirmed_at_ms),
            "accompaniment_end": ms_to_date(e.accompaniment_end_open_time),
            "lower": e.lower, "upper": e.upper,
            "wick_low": e.wick_low, "wick_high": e.wick_high,
            "lifecycle_kinds": [x["kind"] for x in q.get("lifecycle", [])],
            "sweeps": [
                {
                    "state": sw.state, "start": ms_to_date(sw.start_open_time),
                    "min_price": sw.min_price,
                    "end": ms_to_date(sw.end_open_time),
                    "return_confirmed": bool(sw.return_confirmed),
                }
                for sw in db2.list_alt_sweep_episodes(e.id)
            ],
        })
    last_close = candles[-1].close
    qualified = [
        e for e in db2.list_alt_range_episodes(1)
    ]
    sel, sel_reason, sel_alts = select_current_episode(
        qualified, last_close, as_of_ms
    )
    db2.close()

    # --- матчинг против разметки ---
    markup_match = []
    used_episode_ids: set[int] = set()
    for me in markup_eps:
        role = me["role"]
        rec: dict = {"markup_id": me["id"], "role": role, "matched": False}
        if role == "sweep":
            # вынос разметки против эпизодов выносов v2: минимум в зоне L и
            # пересечение по времени
            w_start = date_to_ms(me["start"]) - 45 * DAY_MS
            w_end = date_to_ms(me["end"]) + 45 * DAY_MS
            hits = []
            for ep in episodes:
                for sw in ep["sweeps"]:
                    sw_start = date_to_ms(sw["start"])
                    if (
                        in_interval(sw["min_price"], me["L"])
                        and w_start <= sw_start <= w_end
                    ):
                        hits.append({"episode_id": ep["id"], **sw})
            rec["sweep_hits"] = hits
            rec["matched"] = bool(hits)
        elif role == "late_consolidation_candidate":
            rec["note"] = "контрольный кандидат: не должен расширять старую базу"
            rec["matched"] = None
        else:
            window_lo = date_to_ms(me["start"]) - TIME_PAD_DAYS * DAY_MS
            window_hi = date_to_ms(me["end"]) + TIME_PAD_DAYS * DAY_MS
            cands = []
            for ep in episodes:
                if ep["id"] in used_episode_ids or not published_episode(ep):
                    continue
                if not (
                    in_interval(ep["lower"], me["L"])
                    and in_interval(ep["upper"], me["U"])
                ):
                    continue
                span = episode_span_ms(ep)
                if not intervals_overlap(span[0], span[1], window_lo, window_hi):
                    continue
                cands.append(ep)
            if cands:
                markup_start = date_to_ms(me["start"])
                best = min(
                    cands,
                    key=lambda ep: abs(
                        (episode_span_ms(ep)[0] or 0) - markup_start
                    ),
                )
                used_episode_ids.add(best["id"])
                rec["matched"] = True
                rec["episode_id"] = best["id"]
                rec["state"] = best["state"]
                rec["v2_L"] = best["lower"]
                rec["v2_U"] = best["upper"]
                rec["boundary_shift"] = {
                    "L": best["lower"] - (me["L"]["min"] + me["L"]["max"]) / 2,
                    "U": best["upper"] - (me["U"]["min"] + me["U"]["max"]) / 2,
                }
                brk = next(
                    (ev for ev in me.get("events", [])
                     if ev["type"] == "breakout"), None
                )
                if brk is not None:
                    be_ms = (
                        date_to_ms(best["base_end"])
                        if best["base_end"] else None
                    )
                    w_lo = (date_to_ms(brk["date_from"])
                            - BASE_END_WINDOW_DAYS * DAY_MS)
                    w_hi = (date_to_ms(brk["date_to"])
                            + BASE_END_WINDOW_DAYS * DAY_MS)
                    rec["base_end_check"] = {
                        "reason": best["base_end_reason"],
                        "base_end": best["base_end"],
                        "window": [brk["date_from"], brk["date_to"]],
                        "ok": bool(
                            best["base_end_reason"] == "breakout_confirmed"
                            and be_ms is not None and w_lo <= be_ms <= w_hi
                        ),
                    }
            else:
                # диагностика ближайшего по границам эпизода
                frozen = [ep for ep in episodes if published_episode(ep)]
                if frozen:
                    mid_l = (me["L"]["min"] + me["L"]["max"]) / 2
                    mid_u = (me["U"]["min"] + me["U"]["max"]) / 2
                    nearest = min(
                        frozen,
                        key=lambda ep: abs(ep["lower"] - mid_l)
                        + abs(ep["upper"] - mid_u),
                    )
                    rec["nearest"] = {
                        "episode_id": nearest["id"], "state": nearest["state"],
                        "L": nearest["lower"], "U": nearest["upper"],
                        "anchor": nearest["anchor"],
                    }
        markup_match.append(rec)

    unmarked_episodes = [
        {
            "episode_id": ep["id"], "state": ep["state"],
            "L": ep["lower"], "U": ep["upper"], "anchor": ep["anchor"],
        }
        for ep in episodes
        if published_episode(ep)
        and not any(
            me["role"] in ("historical_base", "current_base")
            and in_interval(ep["lower"], me["L"])
            and in_interval(ep["upper"], me["U"])
            and intervals_overlap(
                *episode_span_ms(ep),
                date_to_ms(me["start"]) - TIME_PAD_DAYS * DAY_MS,
                date_to_ms(me["end"]) + TIME_PAD_DAYS * DAY_MS,
            )
            for me in markup_eps
        )
    ]
    current_marks = [
        m for m in markup_match if m["role"] == "current_base"
    ]
    for mark in current_marks:
        matched_id = mark.get("episode_id") if mark.get("matched") else None
        mark["selection_ok"] = selection_matches(
            sel.id if sel else None, matched_id,
        )
    selection_ok = (
        all(m["selection_ok"] for m in current_marks)
        if current_marks else None
    )

    return {
        "ticker": ticker,
        "candles": len(candles),
        "v1": {
            "state": s1["state"],
            "frozen_ranges": v1_frozen,
            "setups": v1_setups,
        },
        "v2": {
            "state": s2["state"],
            "candidates_total": len(s2["candidates"]),
            "episodes": episodes,
            "selection": {
                "episode_id": sel.id if sel else None,
                "reason": sel_reason,
                "alternatives": [e.id for e in sel_alts],
                "selection_ok": selection_ok,
            },
        },
        "markup_match": markup_match,
        "unmarked_episodes": unmarked_episodes,
    }


def render_md(results: list[dict], cfg: AltConfig, as_of: str) -> str:
    lines = [
        "# Alt v1/v2 replay compare (этап 6)",
        "",
        f"as_of разметки: {as_of}. Конфиг — репозиторные дефолты "
        f"(load_alt_config). Допуск границ ±{MATCH_EPS_REL:.0%}, окно "
        f"base_end ±{BASE_END_WINDOW_DAYS}д.",
        "",
        "v2-параметры: "
        f"sweep_max_days={cfg.v2_sweep_max_days}, "
        f"split_days={cfg.v2_candidate_split_days}, "
        f"reaction_gap={cfg.v2_min_reaction_gap_days}, "
        f"cluster_atr_mult={cfg.v2_cluster_atr_mult}, "
        f"cluster_pct={cfg.v2_cluster_pct}, "
        f"max_days_since_reaction={cfg.v2_max_days_since_reaction}, "
        f"min_reactions={cfg.v2_min_reactions}.",
        "",
    ]
    base_hit = base_miss = sweep_hit = sweep_miss = 0
    body: list[str] = []
    for r in results:
        body.append(f"## {r['ticker']} (candles={r['candles']})")
        body.append("")
        body.append(
            f"- v1: state={r['v1']['state']}, frozen="
            + (", ".join(
                f"{fr['lower']:.6g}/{fr['upper']:.6g}@{fr['mature_at']}"
                for fr in r["v1"]["frozen_ranges"]
            ) or "—")
        )
        body.append(
            f"- v2: state={r['v2']['state']}, "
            f"episodes={len(r['v2']['episodes'])}, "
            f"candidates={r['v2']['candidates_total']}, "
            f"selection={r['v2']['selection']['episode_id']}"
            f" ({r['v2']['selection']['reason']}"
            f", selection_ok={r['v2']['selection']['selection_ok']})"
        )
        for m in r["markup_match"]:
            if m["role"] == "sweep":
                if m.get("matched"):
                    sweep_hit += 1
                    body.append(
                        f"  - MATCH sweep {m['markup_id']} ← "
                        + ", ".join(
                            f"ep#{h['episode_id']} min={h['min_price']:.6g} "
                            f"{h['start']}..{h['end']} [{h['state']}]"
                            for h in m["sweep_hits"]
                        )
                    )
                else:
                    sweep_miss += 1
                    body.append(f"  - MISS sweep {m['markup_id']}")
            elif m["matched"] is True:
                base_hit += 1
                extra = ""
                if "base_end_check" in m:
                    c = m["base_end_check"]
                    extra = (f", base_end {c['base_end']} "
                             f"({'OK' if c['ok'] else 'MISMATCH'} "
                             f"окно {c['window'][0]}..{c['window'][1]})")
                body.append(
                    f"  - MATCH {m['markup_id']} ← ep#{m['episode_id']} "
                    f"[{m['state']}] L={m['v2_L']:.6g} U={m['v2_U']:.6g}"
                    f" (сдвиг L {m['boundary_shift']['L']:+.4g}, "
                    f"U {m['boundary_shift']['U']:+.4g}){extra}"
                )
            elif m["matched"] is False:
                base_miss += 1
                near = m.get("nearest")
                tail = (f" (ближайший: ep#{near['episode_id']} "
                        f"{near['L']:.6g}/{near['U']:.6g} @{near['anchor']})"
                        if near else "")
                body.append(f"  - MISS {m['markup_id']}{tail}")
            else:
                body.append(f"  - NOTE {m['markup_id']}: {m['note']}")
        if r["unmarked_episodes"]:
            body.append(
                f"  - НЕРАЗМЕЧЕННЫЕ (не ошибка сами по себе): "
                + ", ".join(
                    f"ep#{b['episode_id']}[{b['state']}] "
                    f"{b['L']:.6g}/{b['U']:.6g}@{b['anchor']}"
                    for b in r["unmarked_episodes"]
                )
            )
        body.append("")
    lines.append(
        f"**Итого: баз совпало {base_hit}, пропущено {base_miss}; "
        f"выносов покрыто {sweep_hit}, не покрыто {sweep_miss}**"
    )
    lines.append("")
    lines.extend(body)
    return "\n".join(lines) + "\n"


def main() -> None:
    cfg = load_alt_config()
    markup = load_markup()
    results = []
    for ticker, entry in markup["tickers"].items():
        results.append(run_ticker(ticker, entry["episodes"], cfg))
        r = results[-1]
        hits = sum(1 for m in r["markup_match"] if m.get("matched"))
        miss = sum(1 for m in r["markup_match"] if m.get("matched") is False)
        print(f"{ticker}: match={hits} miss={miss} "
              f"unmarked={len(r['unmarked_episodes'])} "
              f"selection_ok={r['v2']['selection']['selection_ok']} "
              f"episodes={len(r['v2']['episodes'])}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Необязательный суффикс, чтобы не затирать снимок до правки измерителя.
    stamp = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    js_path = OUT_DIR / f"alt_replay_compare_{stamp}.json"
    md_path = OUT_DIR / f"alt_replay_compare_{stamp}.md"
    js_path.write_text(
        json.dumps({
            "generated": datetime.datetime.now(
                tz=datetime.timezone.utc).isoformat(),
            "match_eps_rel": MATCH_EPS_REL,
            "base_end_window_days": BASE_END_WINDOW_DAYS,
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(
        render_md(results, cfg, markup["as_of"]), encoding="utf-8"
    )
    print(f"\nJSON: {js_path}\nMD:   {md_path}")


if __name__ == "__main__":
    main()
