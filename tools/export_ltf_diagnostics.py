"""Экспорт диагностики LTF-наблюдения: почему BOS/SMS (не) зафиксирован.

Read-only прогон детектора структуры (§6) по боевой БД: свечи H1 и
материализованные pivots читаются через `file:...?mode=ro`, detect_breaks
исполняется в памяти с ScanTrace (см. app/models_ltf.py) — БД не меняется.

Два прогона:
- prod: точная реплика вызова engine._maybe_open_scenario
  (stop_on_cancellation=True) — показывает, что видела прод-логика;
- full: stop_on_cancellation=False — машина стороны до конца истории,
  чтобы ответить «что решил алгоритм на уровне X», даже если прод-скан
  был обрезан обратным сломом.

Пример:
    .venv/Scripts/python tools/export_ltf_diagnostics.py \
        --observation-id 1 --focus-from 2026-09-10 --focus-to 2026-09-18 \
        --out data/diag/obs1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.ltf import detect_breaks
from app.engine.ltf.pivots import PivotCandidate
from app.models import Candle, Direction
from app.models_ltf import ScanTrace, TraceEntry

DAY_MS = 86_400_000
CONTEXT_DAYS = 30  # как cfg.ltf_history_days: контекст до activated_at (§4)


def _ms(s: str, end_of_day: bool = False) -> int:
    """ISO-дата/дата-время → ms UTC; date-only с end_of_day — конец суток."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    if end_of_day and "T" not in s and " " not in s:
        ms += DAY_MS - 1
    return ms


def _ts(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_candles(con: sqlite3.Connection, instrument_id: int,
                  start_ms: int, end_ms: Optional[int]) -> list[Candle]:
    q = ("SELECT * FROM candle WHERE instrument_id=? AND timeframe='H1'"
         " AND open_time>=?")
    args: list[Any] = [instrument_id, start_ms]
    if end_ms is not None:
        q += " AND open_time<=?"
        args.append(end_ms)
    q += " ORDER BY open_time"
    return [
        Candle(
            instrument_id=r["instrument_id"], timeframe=r["timeframe"],
            open_time=r["open_time"], close_time=r["close_time"],
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            closed=bool(r["closed"]), source=r["source"],
        )
        for r in con.execute(q, args)
    ]


def _load_pivots(con: sqlite3.Connection, instrument_id: int) -> list[PivotCandidate]:
    return [
        PivotCandidate(
            instrument_id=r["instrument_id"], price=r["price"], kind=r["kind"],
            pivot_at=r["pivot_at"], candle_open_time=r["candle_open_time"],
            confirmed_at=r["confirmed_at"] or 0, left=r["left"], right=r["right"],
            state=r["state"], pivot_id=r["id"], role=r["role"],
        )
        for r in con.execute(
            "SELECT * FROM ltf_pivot WHERE instrument_id=? ORDER BY pivot_at",
            (instrument_id,),
        )
    ]


def _pivot_flag(role: str) -> str:
    if role in ("HL", "LH"):
        return "reference"
    if role.startswith("internal"):
        return "internal"
    if role in ("HH", "LL"):
        return "anchor"
    return "none"


def _pivot_dict(p: PivotCandidate) -> dict[str, Any]:
    return {
        "id": p.pivot_id, "kind": p.kind, "price": p.price,
        "pivot_at": p.pivot_at, "pivot_time": _ts(p.pivot_at),
        "confirmed_at": p.confirmed_at, "confirmed_time": _ts(p.confirmed_at),
        "role": p.role, "state": p.state, "flag": _pivot_flag(p.role),
    }


def _event_dict(e) -> dict[str, Any]:
    d = asdict(e)
    d["direction"] = e.direction.value
    d["break_candle_time"] = _ts(e.break_candle_open_time)
    d["occurred_time"] = _ts(e.occurred_at)
    return d


def _entry_dict(e: TraceEntry) -> dict[str, Any]:
    d = e.to_dict()
    d["candle_time"] = _ts(e.candle_open_time)
    return d


def _side_entries(trace: ScanTrace, side: str, check: str) -> list[TraceEntry]:
    return [e for e in trace.entries
            if e.direction == side and e.check == check]


def _find_pivot(pivots: list[PivotCandidate], ref: Optional[int]) -> Optional[PivotCandidate]:
    if ref is None:
        return None
    return next(
        (p for p in pivots if p.pivot_id == ref or p.pivot_at == ref), None
    )


def _fmt_state(e: TraceEntry) -> str:
    def f(v: Optional[float]) -> str:
        return "-" if v is None else f"{v:g}"
    return (f"anchor={f(e.anchor_price)} ref={f(e.ref_price)} "
            f"internal={f(e.internal_price)} pullback={f(e.pullback_price)} "
            f"first={f(e.first_price)} pb2={f(e.pullback2_price)}")


def _decision_cell(entries: list[TraceEntry], check: str) -> str:
    e = next((x for x in entries if x.check == check), None)
    if e is None:
        return "·"
    if e.decision == "accept":
        return f"ACCEPT {e.level_price:g}"
    return e.reason or e.decision


def build_markdown(
    obs: sqlite3.Row, zone: Optional[sqlite3.Row], direction: Direction,
    candles: list[Candle], pivots: list[PivotCandidate],
    prod_scan, full_scan, focus_from: int, focus_to: int,
    since_ms: int, now_ms: int,
) -> str:
    bear = direction == Direction.BEAR
    tgt = direction.value
    rev_side = "bull" if bear else "bear"
    ref_role = "HL" if bear else "LH"
    anchor_role = "HH" if bear else "LL"
    internal_role = "internal_low" if bear else "internal_high"
    trace: ScanTrace = full_scan.trace
    by_id = {p.pivot_id: p for p in pivots}
    L: list[str] = []

    def pivot_line(p: Optional[PivotCandidate]) -> str:
        if p is None:
            return "нет"
        return (f"#{p.pivot_id} {p.kind} {p.price:g} ({p.role}), "
                f"pivot_at {_ts(p.pivot_at)}, confirmed {_ts(p.confirmed_at)}")

    L.append(f"# LTF-диагностика наблюдения #{obs['id']}")
    L.append("")
    L.append(f"- инструмент: {obs['instrument_id']}, направление: **{tgt}**, "
             f"состояние: `{obs['state']}`")
    if zone is not None:
        L.append(f"- HTF-зона: #{zone['id']} {zone['type']} {zone['timeframe']} "
                 f"{zone['lower']:g}–{zone['upper']:g} ({zone['status']})")
    L.append(f"- activated_at (since_ms): {_ts(since_ms)}; "
             f"окно: {_ts(candles[0].open_time)} — {_ts(candles[-1].open_time)} "
             f"({len(candles)} свечей); now_ms = {_ts(now_ms)} "
             f"(close последней свечи)")
    L.append(f"- pivots в БД: {len(pivots)} "
             f"(confirmed: {sum(1 for p in pivots if p.state == 'confirmed')})")

    # --- prod-прогон: что видела прод-логика ---
    L.append("")
    L.append("## Prod-прогон (stop_on_cancellation=True, как в engine)")
    L.append("")
    if prod_scan.cancellation is not None:
        c = prod_scan.cancellation.event
        L.append(f"Скан остановлен обратным сломом **{prod_scan.cancellation.pattern}**: "
                 f"{c.kind} {c.stage} уровень {c.break_level:g} на свече "
                 f"{_ts(c.break_candle_open_time)} — все свечи после неё машина "
                 f"стороны {tgt} не оценивала (skip=after_cancellation).")
    else:
        L.append("Обратного слома не было; скан прошёл всё окно.")
    L.append(f"События в направлении {tgt}: "
             f"{[(e.kind, e.stage, e.break_level, _ts(e.break_candle_open_time)) for e in prod_scan.events] or 'нет'}")

    # --- (a) опорный уровень: последний первичный слом (в фокусе, иначе окна) ---
    L.append("")
    L.append(f"## (a) Опорный {ref_role} последнего {anchor_role} перед движением")
    L.append("")
    prim_all = _side_entries(trace, tgt, "primary_bos")
    prim_accepts = [e for e in prim_all if e.decision == "accept"]
    focus_accepts = [e for e in prim_accepts
                     if focus_from <= e.candle_open_time <= focus_to]
    chosen = focus_accepts or prim_accepts
    e0 = chosen[-1] if chosen else None
    ref_id: Optional[int] = None
    ref_price: Optional[float] = None
    if e0 is not None:
        ref_id, ref_price = e0.ref_pivot_id, e0.ref_price
        L.append(f"- якорь ({anchor_role}): {pivot_line(by_id.get(e0.anchor_pivot_id))}")
        L.append(f"- опорный ({ref_role}): {pivot_line(by_id.get(ref_id))}")
        L.append(f"- слом принят на свече {_ts(e0.candle_open_time)}, "
                 f"уровень {e0.level_price:g}")
    else:
        # якорь/опора из последнего снапшота, где они установлены
        settled = [e for e in prim_all if e.anchor_pivot_id is not None]
        if settled:
            last = settled[-1]
            ref_id, ref_price = last.ref_pivot_id, last.ref_price
            L.append(f"- якорь ({anchor_role}): "
                     f"{pivot_line(by_id.get(last.anchor_pivot_id))}")
            L.append(f"- опорный ({ref_role}): "
                     f"{pivot_line(by_id.get(ref_id))}")
            L.append("- слом НЕ принят ни на одной свече")
        else:
            L.append(f"- {anchor_role} якорь так и не был установлен")

    # --- (b) первая свеча за уровнем (пока этот ref активен в машине) ---
    L.append("")
    L.append(f"## (b) Первая свеча с закрытием за опорным уровнем")
    L.append("")
    if ref_price is None:
        L.append(f"Опорный уровень ({ref_role}) не установлен — вопрос неприменим.")
    else:
        candle_by_open = {c.open_time: c for c in candles}
        hit: Optional[tuple[Candle, TraceEntry]] = None
        for e in prim_all:
            if e.ref_pivot_id != ref_id:
                continue  # другая опора — другой режим машины
            c = candle_by_open.get(e.candle_open_time)
            if c is None:
                continue
            if (c.close < e.ref_price) if bear else (c.close > e.ref_price):
                hit = (c, e)
                break
        if hit is None:
            L.append(f"Ни одна закрытая свеча не закрылась за {ref_price:g}, "
                     f"пока опора #{ref_id} была активна.")
        else:
            c0, pe = hit
            L.append(f"- свеча **{_ts(c0.open_time)}**, close={c0.close:g} "
                     f"(уровень {pe.ref_price:g}, опора #{ref_id})")
            L.append(f"- решение primary_bos: **{pe.decision}**"
                     + (f" reason=`{pe.reason}`" if pe.reason else ""))
            L.append(f"- снапшот: {_fmt_state(pe)}")
            pc = prod_scan.cancellation
            if pc is not None and pc.event.occurred_at <= c0.close_time:
                L.append(f"- прод-скан до этой свечи не дошёл: обрезан "
                         f"{pc.pattern} на {_ts(pc.event.break_candle_open_time)}; "
                         f"решение видно только в full-прогоне")

    # --- (c) внутренний экстремум / SMS ---
    L.append("")
    L.append(f"## (c) SMS: внутренний экстремум ({internal_role})")
    L.append("")
    internals = [p for p in pivots
                 if p.role == internal_role and p.pivot_at >= since_ms]
    sms_entries = [e for e in _side_entries(trace, tgt, "sms")
                   if e.candle_open_time >= since_ms]
    sms_accepts = [e for e in sms_entries if e.decision == "accept"]
    absorb_of = {a.pivot_ref: a for a in trace.absorptions if a.direction == tgt}
    prim_by_candle = {e.candle_open_time: e for e in prim_all}
    candle_by_close = {c.close_time: c for c in candles}
    if internals:
        for p in internals:
            a = absorb_of.get(p.pivot_id)
            ref_at: Optional[float] = None
            if a is not None:
                c_abs = candle_by_close.get(a.candle_close_time)
                e_abs = (prim_by_candle.get(c_abs.open_time)
                         if c_abs is not None else None)
                ref_at = e_abs.ref_price if e_abs is not None else None
            adopted = any(e.internal_pivot_id == p.pivot_id
                          for e in trace.entries)
            geo = ""
            if ref_at is not None:
                ok = (p.price > ref_at) if bear else (p.price < ref_at)
                geo = (f"; опора на момент поглощения {ref_at:g} → "
                       f"геометрия SMS {'есть' if ok else 'НЕТ'}")
            L.append(f"- {pivot_line(p)}; машина приняла как internal: "
                     f"{'да' if adopted else 'нет'}{geo}")
    else:
        L.append(f"- pivots с ролью `{internal_role}` в окне нет")
    reasons = Counter(e.reason for e in sms_entries if e.decision == "reject")
    L.append(f"- проверок sms в окне: {len(sms_entries)}, "
             f"accept: {len(sms_accepts)}, "
             f"reject reasons: {dict(reasons) or '{}'}")
    for e in sms_accepts:
        L.append(f"- SMS accept: уровень {e.level_price:g} на свече "
                 f"{_ts(e.candle_open_time)} (accompanying см. JSON)")

    # --- (d) таблица по свечам фокусного окна ---
    L.append("")
    L.append(f"## (d) Трасса по свечам: {_ts(focus_from)} — {_ts(focus_to)} "
             f"(сторона {tgt})")
    L.append("")
    L.append("| свеча | close | primary_bos | sms | secondary_bos | состояние |")
    L.append("|---|---|---|---|---|---|")
    for c in candles:
        if not (focus_from <= c.open_time <= focus_to):
            continue
        ent = [e for e in trace.entries_for(c.open_time) if e.direction == tgt]
        if not ent:
            continue
        state_src = next((e for e in ent if e.check == "primary_bos"), ent[0])
        L.append(f"| {_ts(c.open_time)} | {c.close:g} "
                 f"| {_decision_cell(ent, 'primary_bos')} "
                 f"| {_decision_cell(ent, 'sms')} "
                 f"| {_decision_cell(ent, 'secondary_bos')} "
                 f"| {_fmt_state(state_src)} |")

    # --- (e) обратная сторона ---
    L.append("")
    L.append(f"## (e) Обратная сторона ({rev_side}) — кандидаты на отмену")
    L.append("")
    rev_accepts = [e for e in trace.entries
                   if e.direction == rev_side and e.decision == "accept"]
    if rev_accepts:
        for e in rev_accepts:
            L.append(f"- {e.level_kind} {e.level_stage} уровень {e.level_price:g} "
                     f"на свече {_ts(e.candle_open_time)}")
    else:
        L.append("- обратных сломов не зафиксировано")
    rev_all = [e for e in trace.entries
               if e.direction == rev_side and e.check == "primary_bos"]
    if rev_all:
        L.append(f"- финальное состояние: {_fmt_state(rev_all[-1])}")
    if full_scan.cancellation is not None:
        c = full_scan.cancellation.event
        L.append(f"- первый обратный слом окна: {full_scan.cancellation.pattern} "
                 f"на {_ts(c.break_candle_open_time)} (уровень {c.break_level:g})")

    # --- поглощения в фокусе ---
    L.append("")
    L.append("## Поглощения pivots в фокусном окне")
    L.append("")
    L.append("| pivot | kind | role | price | поглощён на закрытии |")
    L.append("|---|---|---|---|---|")
    seen: set[tuple[int, str]] = set()
    for a in trace.absorptions:
        key = (a.pivot_ref, a.direction)
        if key in seen or a.direction != tgt:
            continue
        p = _find_pivot(pivots, a.pivot_ref)
        if p is None or not (focus_from <= p.pivot_at <= focus_to):
            continue
        seen.add(key)
        L.append(f"| #{a.pivot_ref} | {a.kind} | {a.role} | {a.price:g} "
                 f"| {_ts(a.candle_close_time)} |")
    L.append("")
    return "\n".join(L)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/htf_zones.db")
    ap.add_argument("--observation-id", type=int, required=True)
    ap.add_argument("--from", dest="from_", default=None,
                    help="ISO-дата начала окна (по умолчанию activated_at - 30д)")
    ap.add_argument("--to", dest="to", default=None,
                    help="ISO-дата конца окна (по умолчанию последняя свеча)")
    ap.add_argument("--focus-from", default=None,
                    help="ISO-дата начала фокусного окна таблицы (по умолчанию: to - 8д)")
    ap.add_argument("--focus-to", default=None)
    ap.add_argument("--out", required=True,
                    help="базовый путь: пишутся <out>.json и <out>.md")
    args = ap.parse_args()

    con = _connect_ro(args.db)
    obs = con.execute("SELECT * FROM ltf_observation WHERE id=?",
                      (args.observation_id,)).fetchone()
    if obs is None:
        sys.exit(f"наблюдение #{args.observation_id} не найдено")
    zone = con.execute("SELECT * FROM zone WHERE id=?",
                       (obs["zone_id"],)).fetchone()
    since_ms = obs["activated_at"]
    start = min(_ms(args.from_) if args.from_ else 1 << 62,
                since_ms - CONTEXT_DAYS * DAY_MS)
    end = _ms(args.to, end_of_day=True) if args.to else None
    candles = _load_candles(con, obs["instrument_id"], start, end)
    if not candles:
        sys.exit("нет свечей H1 в окне")
    pivots_all = _load_pivots(con, obs["instrument_id"])
    con.close()

    now_ms = candles[-1].close_time
    # как engine._maybe_open_scenario: доступные pivots на now, since=activated_at
    avail = [p for p in pivots_all
             if p.state == "confirmed" and p.confirmed_at
             and p.confirmed_at <= now_ms]
    direction = Direction(obs["direction"])

    prod_trace = ScanTrace()
    prod_scan = detect_breaks(avail, candles, direction, now_ms,
                              since_ms=since_ms, trace=prod_trace)
    full_trace = ScanTrace()
    full_scan = detect_breaks(avail, candles, direction, now_ms,
                              since_ms=since_ms, trace=full_trace,
                              stop_on_cancellation=False)

    focus_from = _ms(args.focus_from) if args.focus_from else (
        (end or candles[-1].open_time) - 8 * DAY_MS)
    focus_to = (_ms(args.focus_to, end_of_day=True) if args.focus_to
                else candles[-1].open_time)

    report = {
        "observation": {k: obs[k] for k in obs.keys()},
        "zone": ({k: zone[k] for k in zone.keys()
                  if k != "evidence"} if zone is not None else None),
        "params": {
            "direction": direction.value, "since_ms": since_ms,
            "since_time": _ts(since_ms), "now_ms": now_ms,
            "now_time": _ts(now_ms), "window_from": _ts(candles[0].open_time),
            "window_to": _ts(candles[-1].open_time),
            "focus_from": _ts(focus_from), "focus_to": _ts(focus_to),
        },
        "candles": [
            {"open_time": c.open_time, "time": _ts(c.open_time),
             "open": c.open, "high": c.high, "low": c.low, "close": c.close,
             "closed": c.closed}
            for c in candles
        ],
        "pivots": [_pivot_dict(p) for p in pivots_all],
        "prod_run": {
            "events": [_event_dict(e) for e in prod_scan.events],
            "cancellation": (
                {"pattern": prod_scan.cancellation.pattern,
                 "event": _event_dict(prod_scan.cancellation.event)}
                if prod_scan.cancellation else None),
            "trace_entries": [_entry_dict(e) for e in prod_trace.entries],
            "absorptions": [a.to_dict() for a in prod_trace.absorptions],
        },
        "full_run": {
            "events": [_event_dict(e) for e in full_scan.events],
            "cancellation": (
                {"pattern": full_scan.cancellation.pattern,
                 "event": _event_dict(full_scan.cancellation.event)}
                if full_scan.cancellation else None),
            "trace_entries": [_entry_dict(e) for e in full_trace.entries],
            "absorptions": [a.to_dict() for a in full_trace.absorptions],
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = build_markdown(obs, zone, direction, candles, pivots_all,
                        prod_scan, full_scan, focus_from, focus_to,
                        since_ms, now_ms)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(f"JSON: {out.with_suffix('.json')}")
    print(f"MD:   {out.with_suffix('.md')}")
    print(f"prod events: {[(e.kind, e.stage, e.break_level) for e in prod_scan.events]}")
    print(f"full events: {[(e.kind, e.stage, e.break_level) for e in full_scan.events]}")
    if prod_scan.cancellation:
        print(f"prod scan остановлен: {prod_scan.cancellation.pattern} на "
              f"{_ts(prod_scan.cancellation.event.break_candle_open_time)}")


if __name__ == "__main__":
    main()
