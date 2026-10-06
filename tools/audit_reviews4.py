"""Аудиты по ТЗ 07.10.2026 (reviews 4): T06, T12, T16, T10.

Разделы:
1. T06: аудит 29 manual_only — полный поиск подтверждающего FVG: восстановление
   базы и выхода, ВСЕ допустимые последовательные тройки причинного движения
   (не одна tested_fvg_triple), проверка геометрии/времени/внешнего условия
   (bull L_fvg >= U_ob, bear U_fvg <= L_ob — равенство краёв допустимо, §5).
   Найденное подтверждение либо конкретная причина отсутствия/недостаточности.
2. T12: четыре пробоя (№30, 32, 605, 758) — первая закрытая свеча пробоя
   либо явно insufficient history; дата комментария не подставляется без OHLC.
3. T16: девять type-отказов без комментариев — база, выход, проверенные FVG,
   признаки §8; конкретные данные для объяснения, без hardcode по ID/цене.
4. T10: эталон ETH №342/№340 — выставление ссылки preferred_entry_zone
   (--apply); широкая зона не удаляется.

Запуск: .venv/Scripts/python.exe tools/audit_reviews4.py [--apply]
Живая БД не трогается (scratch-копия); --apply влияет только на ссылку
preferred_entry (раздел 4) и пишет в scratch. Для живой БД: --apply --live.
"""
from __future__ import annotations

import datetime
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_detector_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.engine.breaker import first_close_beyond  # noqa: E402
from app.engine.fvg import scan_fvgs  # noqa: E402
from app.engine.lifecycle import base_end_ms  # noqa: E402
from app.models import (  # noqa: E402
    Direction,
    TIMEFRAME_MINUTES,
    close_boundary_ms,
)

LIVE = ROOT / "data" / "htf_zones.db"
FIXTURE = ROOT / "tests" / "fixtures" / "reviews4_export_2026_10_07.json"

BROKEN_CASES = {30, 32, 605, 758}
TYPE_REJECTED = {550, 19, 457, 735, 482, 221, 526, 260, 532}
# эталон §7: №342 предпочтительна для входа, широкая №340 — контекстная
PREFERRED = (342, 340)


def _ts(ms) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)\
        .strftime("%Y-%m-%d %H:%M")


def find_by_attrs(db: Database, symbol: str, tf: str, direction: str,
                  lower: float, upper: float):
    tol = 1e-4
    for z in db.get_zones():
        ins = db.get_instrument(z.instrument_id)
        if ins is None or ins.symbol != symbol:
            continue
        if z.timeframe != tf or z.direction.value != direction:
            continue
        if abs(z.lower - lower) <= lower * tol and abs(z.upper - upper) <= upper * tol:
            return z
    return None


def fvg_audit(db: Database, z, candles, out) -> None:
    """Полный поиск подтверждающего FVG причинного движения (§5 ТЗ)."""
    tf_ms = TIMEFRAME_MINUTES[z.timeframe] * 60_000
    be = base_end_ms(z)
    if be is None or not candles:
        out.append("  недостаточно данных (нет base_end/свечей)")
        return
    out.append(f"  база: {len(z.source_candles)} свечей, конец {_ts(be)}; "
               f"выход: departed_at={_ts(z.evidence.get('departed_at'))}")
    admissible, overlapping = [], []
    for f in scan_fvgs(candles, z.timeframe):
        if f.direction != z.direction:
            continue
        if f.confirmed_at <= be:
            continue  # тройка закрылась до завершения базы — не выход
        # внешнее условие §5: bull L_fvg >= U_ob; bear U_fvg <= L_ob
        if z.direction == Direction.BULL:
            (admissible if f.lower >= z.upper else overlapping).append(f)
        else:
            (admissible if f.upper <= z.lower else overlapping).append(f)
    admissible.sort(key=lambda f: f.confirmed_at)
    overlapping.sort(key=lambda f: f.confirmed_at)
    if admissible:
        f = admissible[0]
        out.append(f"  НАЙДЕН внешний FVG: [{f.lower}, {f.upper}] тройка "
                   f"{_ts(f.candle_open_times[0])}…{_ts(f.candle_open_times[2])}, "
                   f"доступен {_ts(f.confirmed_at)} (всего допустимых: {len(admissible)})")
        if len(admissible) > 1:
            out.append(f"  прочие допустимые: {len(admissible) - 1} — хранить "
                       "использованный и остальные отдельно (§7 ТЗ 06.10)")
    else:
        if overlapping:
            f = overlapping[0]
            out.append(f"  подтверждение НЕ найдено: {len(overlapping)} FVG "
                       f"того же направления после базы ПЕРЕКРЫВАЮТ её — "
                       f"первый [{f.lower}, {f.upper}] "
                       f"{_ts(f.candle_open_times[0])}… (§5: диагностировать "
                       "именно перекрытие, не отменять требование скрыто)")
        else:
            out.append("  подтверждение НЕ найдено: FVG нужного направления "
                       "после завершения базы в данных нет (missing evidence — "
                       "не доказательство отсутствия FVG на рынке, §5)")


def break_audit(db: Database, z, candles, out) -> None:
    scan_from = z.confirmed_at or base_end_ms(z)
    if scan_from is None or not candles:
        out.append("  insufficient history (нет свечей)")
        return
    last_boundary = close_boundary_ms(candles[-1].open_time, z.timeframe)
    first = first_close_beyond(candles, z, scan_from, last_boundary)
    # ближайшее к пробою закрытие окна — доказательство при расхождении
    window = [c for c in candles if c.closed
              and scan_from < close_boundary_ms(c.open_time, z.timeframe)]
    if first is None:
        if not window:
            out.append("  insufficient history: закрытых свечей после "
                       "формирования в данных нет — актуальность не доказана (§14)")
            return
        if z.direction == Direction.BULL:
            ext = min(window, key=lambda c: c.close)
            cmp_note = f"min Close={ext.close} ({_ts(ext.open_time)}) vs L={z.lower}"
        else:
            ext = max(window, key=lambda c: c.close)
            cmp_note = f"max Close={ext.close} ({_ts(ext.open_time)}) vs U={z.upper}"
        out.append(f"  пробоя по строгому правилу в данных НЕТ "
                   f"(история окна полная: {_ts(window[0].open_time)} … "
                   f"{_ts(window[-1].open_time)}): {cmp_note} — тень и "
                   "Close=границе пробоем не являются (T14); расхождение с "
                   "комментарием сохранено, дата комментария не подставлена")
        return
    fb = close_boundary_ms(first.open_time, z.timeframe)
    out.append(f"  первая свеча пробоя: open {_ts(first.open_time)}, "
               f"O={first.open} H={first.high} L={first.low} C={first.close}, "
               f"граница {_ts(fb)}; в БД display_until={_ts(z.display_until)} "
               f"end_reason={z.end_reason}")


def rejection_audit(db: Database, z, candles, out) -> None:
    width_pct = (z.upper - z.lower) / z.lower * 100 if z.lower else 0.0
    ev = z.evidence
    out.append(f"  база {len(z.source_candles)} свечей, ширина {width_pct:.1f}%, "
               f"anchor={'да' if ev.get('boundary_anchor') else 'нет'}, "
               f"scan_limited={ev.get('base_search_limited', False)}, "
               f"indep_fvg={ev.get('contains_independent_fvg_members', False)}, "
               f"external_fvg={ev.get('external_fvg')}, "
               f"тестов max_depth={z.max_test_depth:.2f}")
    fvg_audit(db, z, candles, out)
    break_audit(db, z, candles, out)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    apply = "--apply" in sys.argv
    live = "--live" in sys.argv
    load_detector_config()
    labels = json.loads(FIXTURE.read_text(encoding="utf-8"))["labels"]
    by_zone = {l["zone_id"]: l for l in labels}

    stamp = int(datetime.datetime.now().timestamp())
    if live and apply:
        backup = ROOT / "data" / "backups" / f"htf_zones_pre_reviews4_{stamp}.db"
        for sfx in ("", "-wal", "-shm"):
            src = Path(str(LIVE) + sfx)
            if src.exists():
                shutil.copy(src, str(backup) + sfx)
        print(f"Бэкап живой БД: {backup}")
        db = Database(str(LIVE))
    else:
        scratch = ROOT / "data" / "diag" / f"audit4_scratch_{stamp}.db"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        for sfx in ("", "-wal", "-shm"):
            src = Path(str(LIVE) + sfx)
            if src.exists():
                shutil.copy(src, str(scratch) + sfx)
        db = Database(str(scratch))

    report: list[str] = [
        f"Аудиты ТЗ 07.10.2026 — {datetime.datetime.now(datetime.timezone.utc).isoformat()}Z\n"
    ]
    cache: dict = {}

    def candles_for(z):
        key = (z.instrument_id, z.timeframe)
        if key not in cache:
            cache[key] = db.get_candles(*key)
        return cache[key]

    try:
        # ---- T06: 29 manual_only ----
        report.append("=" * 70)
        report.append("T06: аудит 29 manual_only — полный поиск подтверждающего FVG")
        report.append("=" * 70)
        found = missing = nozone = 0
        for lb in labels:
            if lb["confirmation_state"] != "manual_only":
                continue
            z = find_by_attrs(db, lb["instrument"], lb["timeframe"],
                              lb["direction"], lb["lower"], lb["upper"])
            report.append(f"№{lb['zone_id']} {lb['instrument']} "
                          f"{lb['timeframe']} {lb['direction']} "
                          f"[{lb['lower']}, {lb['upper']}]")
            if z is None:
                report.append("  зона не найдена в текущей БД (data_incomplete)")
                nozone += 1
                continue
            before = len(report)
            fvg_audit(db, z, candles_for(z), report)
            if "НАЙДЕН внешний FVG" in report[-1] or \
                    any("НАЙДЕН внешний FVG" in line for line in report[before:]):
                found += 1
            else:
                missing += 1
        report.append(f"\nИТОГО manual_only: FVG найден у {found}, "
                      f"не найден/перекрытие у {missing}, "
                      f"вне БД {nozone} (ручные оценки сохраняются, §5)")

        # ---- T12: 4 пробоя ----
        report.append("\n" + "=" * 70)
        report.append("T12: четыре явно отмеченных пробоя")
        report.append("=" * 70)
        for zid in sorted(BROKEN_CASES):
            lb = by_zone[zid]
            z = find_by_attrs(db, lb["instrument"], lb["timeframe"],
                              lb["direction"], lb["lower"], lb["upper"])
            report.append(f"№{zid} {lb['instrument']} {lb['timeframe']} "
                          f"{lb['direction']} [{lb['lower']}, {lb['upper']}] "
                          f"— «{lb['comment']}»")
            if z is None:
                report.append("  зона не найдена в текущей БД (data_incomplete)")
                continue
            break_audit(db, z, candles_for(z), report)

        # ---- T16: 9 type-отказов ----
        report.append("\n" + "=" * 70)
        report.append("T16: девять type-отказов без комментариев — данные для объяснения")
        report.append("=" * 70)
        for zid in sorted(TYPE_REJECTED):
            lb = by_zone[zid]
            z = find_by_attrs(db, lb["instrument"], lb["timeframe"],
                              lb["direction"], lb["lower"], lb["upper"])
            report.append(f"№{zid} {lb['instrument']} {lb['timeframe']} "
                          f"{lb['direction']} [{lb['lower']}, {lb['upper']}]")
            if z is None:
                report.append("  зона не найдена в текущей БД (data_incomplete)")
                continue
            rejection_audit(db, z, candles_for(z), report)

        # ---- T10: preferred №342/№340 ----
        report.append("\n" + "=" * 70)
        report.append("T10: эталон ETH №342/№340 — preferred_entry_zone")
        report.append("=" * 70)
        lb342, lb340 = by_zone[PREFERRED[0]], by_zone[PREFERRED[1]]
        z342 = find_by_attrs(db, lb342["instrument"], lb342["timeframe"],
                             lb342["direction"], lb342["lower"], lb342["upper"])
        z340 = find_by_attrs(db, lb340["instrument"], lb340["timeframe"],
                             lb340["direction"], lb340["lower"], lb340["upper"])
        if z342 is None or z340 is None:
            report.append("  зоны №342/№340 не найдены в текущей БД (data_incomplete)")
        else:
            w340 = z340.upper - z340.lower
            w342 = z342.upper - z342.lower
            report.append(f"  W340={w340:.2f} M340={(z340.lower + z340.upper) / 2:.2f}; "
                          f"W342={w342:.2f} M342={(z342.lower + z342.upper) / 2:.2f}; "
                          f"разница {w340 - w342:.2f}")
            report.append(f"  глубины: №340 max_test_depth={z340.max_test_depth:.3f}, "
                          f"№342 max_test_depth={z342.max_test_depth:.3f} "
                          f"(каждая от собственного W, T11)")
            already = z342.evidence.get("supersedes_for_entry") == z340.id
            if already:
                report.append("  ссылка preferred уже установлена (идемпотентно)")
            elif apply:
                ev = dict(z342.evidence)
                ev["preferred_for_entry"] = True
                ev["supersedes_for_entry"] = z340.id
                ev["entry_preference_comment"] = (
                    "владелец: «лучше брать одну свечу 10 декабря 2025» (review 25, §7 ТЗ)")
                ev["source_candles"] = z342.source_candles
                db.update_zone(z342.id, evidence=ev)
                ev340 = dict(z340.evidence)
                ev340["superseded_for_entry_by"] = z342.id
                ev340["source_candles"] = z340.source_candles
                db.update_zone(z340.id, evidence=ev340)
                report.append("  ссылка preferred_entry УСТАНОВЛЕНА: №342 → замещает №340 "
                              "(родитель сохранён со своей историей)")
            else:
                report.append("  dry-run: ссылка не установлена (запустите с --apply)")
    finally:
        db.close()

    text = "\n".join(report)
    print(text)
    if not (live and apply):
        for p in Path(ROOT / "data" / "diag").glob(f"audit4_scratch_{stamp}.db*"):
            p.unlink()
    out_path = ROOT / "data" / "diag" / f"audit_reviews4_{stamp}.txt"
    out_path.write_text(text, encoding="utf-8")
    print(f"\nОтчёт сохранён: {out_path}")


if __name__ == "__main__":
    main()
