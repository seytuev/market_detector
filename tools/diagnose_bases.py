"""T15 (ТЗ 06.10.2026 §8): воспроизводимая диагностика спорных баз №550/549/131.

Для каждой зоны из живой БД (read-only) выгружает:
- границы, базу (source_candles), статус, evidence (candle_decisions, stop);
- контекстные свечи вокруг базы и фактический выход (departure/FVG);
- все FVG того же ТФ в окне базы (самостоятельные движения, §8);
- пересчёт find_base текущим алгоритмом по сохранённым свечам — совпадает
  ли восстановленная база с записанной, и какое согласованное условие
  выполнено/нарушено.

Никаких hardcode-исправлений по ID/цене: только отчёт. Новые пороги длины/
ширины базы НЕ вводятся (ТЗ §8: из отклонений без пояснения формулу не
вывести — спорный пример оформляется данными для калибровки с владельцем).

Запуск: .venv/Scripts/python.exe tools/diagnose_bases.py [zone_id ...]
Вывод: stdout + data/diag/base_diagnostics_<ts>.txt
"""
from __future__ import annotations

import datetime
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_detector_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.engine.fvg import scan_fvgs  # noqa: E402
from app.engine.orderblock import find_base, is_external  # noqa: E402
from app.models import TIMEFRAME_MINUTES  # noqa: E402

LIVE = ROOT / "data" / "htf_zones.db"

# ТЗ 06.10.2026 §18: спорные базы по характеристикам экспорта (zone_id
# пересоздаются между выгрузками — идентификация по инструменту/ТФ/
# направлению/границам, а не по ID, §1 ТЗ)
TZ_CASES = [
    {"export_zone_id": 550, "symbol": "SOLUSDT", "timeframe": "W1",
     "direction": "bear", "lower": 168.88, "upper": 295.83,
     "note": "10 недель, 168.88–295.83 — отклонено «Че попало вообще»"},
    {"export_zone_id": 549, "symbol": "SOLUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 175.26, "upper": 228.95,
     "note": "отклонено без комментария"},
    {"export_zone_id": 131, "symbol": "BTCUSDT", "timeframe": "D1",
     "direction": "bull", "lower": 102000.0, "upper": 122550.0,
     "note": "отклонено без комментария"},
]


def find_by_case(db: Database, case: dict):
    """Зона по характеристикам экспорта (допуск по границам 0.01%)."""
    tol = 1e-4
    for z in db.get_zones():
        ins = db.get_instrument(z.instrument_id)
        if ins is None or ins.symbol != case["symbol"]:
            continue
        if z.timeframe != case["timeframe"] or z.direction.value != case["direction"]:
            continue
        if abs(z.lower - case["lower"]) <= case["lower"] * tol and \
                abs(z.upper - case["upper"]) <= case["upper"] * tol:
            return z
    return None


def _ts(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)\
        .strftime("%Y-%m-%d %H:%M")


def diagnose(db: Database, cfg, zone_id: int, out) -> None:
    z = db.get_zone(zone_id)
    if z is None:
        out.append(f"=== зона {zone_id}: НЕ НАЙДЕНА в БД ===\n")
        return
    ins = db.get_instrument(z.instrument_id)
    sym = ins.symbol if ins else z.instrument_id
    out.append(f"=== зона {zone_id}: {sym} {z.timeframe} {z.direction.value} "
               f"[{z.lower}, {z.upper}] status={z.status.value} "
               f"validity={z.market_validity} ===")
    out.append(f"formed_at={_ts(z.formed_at)} confirmed_at="
               f"{_ts(z.confirmed_at) if z.confirmed_at else '—'} "
               f"display_until={_ts(z.display_until) if z.display_until else '—'} "
               f"end_reason={z.end_reason}")
    out.append(f"база ({len(z.source_candles)} свечей): "
               f"{', '.join(_ts(t) for t in z.source_candles)}")
    out.append(f"departed_at={_ts(z.evidence['departed_at']) if z.evidence.get('departed_at') else '—'} "
               f"phase={z.evidence.get('phase', '—')} external_fvg={z.evidence.get('external_fvg')}")

    candles = db.get_candles(z.instrument_id, z.timeframe)
    tf_ms = TIMEFRAME_MINUTES[z.timeframe] * 60_000
    by_ot = {c.open_time: c for c in candles}

    out.append("-- журнал решений по свечам базы (evidence.candle_decisions) --")
    decisions = z.evidence.get("candle_decisions") or []
    if not decisions:
        out.append("(журнал отсутствует — зона записана до ТЗ 06.10.2026 §8; "
                   "пересчёт ниже восстанавливает журнал текущим алгоритмом)")
    for d in decisions:
        c = by_ot.get(d["open_time"])
        ohlc = (f" O={c.open} H={c.high} L={c.low} C={c.close}"
                if c else " (свеча отсутствует в БД)")
        flag = " [FVG-member]" if d.get("independent_fvg_member") else ""
        out.append(f"  {_ts(d['open_time'])} {d['decision']}: {d['reason']}{flag}{ohlc}")
    stop = z.evidence.get("stop")
    if stop:
        out.append(f"  stop: {stop.get('reason')}")

    # контекст: свечи базы + по 3 с каждой стороны
    if z.source_candles:
        first, last = min(z.source_candles), max(z.source_candles)
        ctx = [c for c in candles
               if first - 3 * tf_ms <= c.open_time <= last + 6 * tf_ms]
        out.append("-- контекст (база ± свечи выхода) --")
        for c in ctx:
            mark = "BASE" if c.open_time in z.source_candles else ""
            out.append(f"  {_ts(c.open_time)} O={c.open} H={c.high} "
                       f"L={c.low} C={c.close} {mark}")
    else:
        first = last = z.formed_at

    # фактические FVG того же ТФ в расширенном окне после базы
    out.append("-- FVG того же ТФ в окне (база −12 свечей … база +24 свечи) --")
    win_lo, win_hi = first - 12 * tf_ms, last + 24 * tf_ms
    for f in scan_fvgs(candles, z.timeframe):
        if not (win_lo <= f.candle_open_times[0] <= win_hi):
            continue
        out.append(f"  FVG {f.direction.value} [{f.lower}, {f.upper}] "
                   f"тройка {_ts(f.candle_open_times[0])}…{_ts(f.candle_open_times[2])} "
                   f"confirmed {_ts(f.confirmed_at)}")

    # пересчёт текущим алгоритмом: совпадает ли база
    out.append("-- пересчёт find_base текущим алгоритмом --")
    rel = db.get_relation(z.id)
    recomputed = 0
    for f in scan_fvgs(candles, z.timeframe):
        base = find_base(candles, f, cfg)
        if base is None:
            continue
        if abs(base.lower - z.lower) < 1e-9 and abs(base.upper - z.upper) < 1e-9:
            recomputed += 1
            ext = is_external(base, f)
            out.append(f"  база воспроизводится от FVG {_ts(f.formed_at)}: "
                       f"{len(base.source_candles)} свечей, "
                       f"gap={base.gap_candles}, external={ext}, "
                       f"решений в журнале={len(base.evidence['candle_decisions'])}")
            if base.evidence.get("contains_independent_fvg_members"):
                out.append("  ВНИМАНИЕ: в базу включены свечи самостоятельных "
                           "FVG (§8 — спорный признак, открытая калибровка)")
    if not recomputed:
        out.append("  записанная база НЕ воспроизводится текущим алгоритмом "
                   "на сохранённых свечах (проверить полноту истории/версию правил)")
    width_pct = (z.upper - z.lower) / z.lower * 100 if z.lower else 0.0
    out.append(f"-- сводка: база {len(z.source_candles)} свечей, ширина "
               f"{width_pct:.1f}%, weeks_span="
               f"{(last - first) / tf_ms + 1 if z.source_candles else '—'} --")
    out.append("")


def main() -> None:
    cfg = load_detector_config()
    # живая БД не открывается на запись: работаем на scratch-копии (WAL)
    diag_dir = ROOT / "data" / "diag"
    diag_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(datetime.datetime.now().timestamp())
    scratch = diag_dir / f"base_diag_scratch_{stamp}.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(LIVE) + suffix)
        if src.exists():
            shutil.copy(src, str(scratch) + suffix)
    db = Database(str(scratch))
    out: list[str] = []
    out.append(f"Диагностика спорных баз — {datetime.datetime.now(datetime.timezone.utc).isoformat()}Z")
    out.append(f"БД: {LIVE} (scratch-копия {scratch.name})\n")
    try:
        if len(sys.argv) > 1:
            # явный режим по ID текущей БД
            for zid in [int(a) for a in sys.argv[1:]]:
                diagnose(db, cfg, zid, out)
        else:
            for case in TZ_CASES:
                out.append(f"### кейс экспорта №{case['export_zone_id']}: {case['note']}")
                z = find_by_case(db, case)
                if z is None:
                    out.append(
                        f"зона {case['symbol']} {case['timeframe']} {case['direction']} "
                        f"[{case['lower']}, {case['upper']}] НЕ НАЙДЕНА в текущей БД — "
                        f"спорный пример оформляется по данным экспорта §18, "
                        f"воспроизводимая диагностика требует БД на момент выгрузки "
                        f"(data_incomplete, ТЗ §17)\n")
                    continue
                out.append(f"соответствие в текущей БД: zone_id={z.id}")
                diagnose(db, cfg, z.id, out)
    finally:
        db.close()
    text = "\n".join(out)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    path = diag_dir / f"base_diagnostics_{stamp}.txt"
    path.write_text(text, encoding="utf-8")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(scratch) + suffix)
        if p.exists():
            p.unlink()
    print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
