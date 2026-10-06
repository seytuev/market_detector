"""Миграция-пересчёт по ТЗ 06.10.2026 (§14) + отчёт по 24 зонам экспорта.

Режим по умолчанию — DRY-RUN на scratch-копии живой БД (живая не трогается).
--apply  : применить исправления к scratch-копии и показать результат;
           для живой БД используйте --apply --live (создаёт бэкап в
           data/backups/ и пишет в data/htf_zones.db).

Что делает пересчёт (идемпотентно, T22):
1. Испорченные departed_at (не позднее конца базы, §3.1) — явно помечаются
   integrity=inconsistent + departure_evidence_incomplete (T02), без
   сигналов; прежнее значение не удаляется (аудит).
2. Первое закрытие за границей пересчитывается ретросканированием (§6):
   display_until не позднее первой пробойной свечи; более ранняя дата не
   сдвигается вперёд; evidence.invalidated_at и событие OB_INVALIDATED
   дозаполняются с OHLC-доказательствами (T03–T06).
3. breaker_forbidden + пробой → ARCHIVED close_beyond_no_breaker (T20).
4. Отчёт: таблица 24 зон экспорта (фикстура tests/fixtures/
   reviews_export_2026_10_06.json, идентификация по характеристикам —
   zone_id между выгрузками пересоздаются, §1 ТЗ) — старый/новый
   formation/lifecycle/review статус и дата первого пробоя; контрольные
   №291/391/657 — геометрия не изменилась (T24); полнота истории (T19).

Никаких исторических рассылок: инструмент события только помечает delayed,
доставкой не занимается. Повторный запуск не создаёт зон/уровней/событий
дубликатов (дедуп по has_event и first-wins).

Запуск:
  .venv/Scripts/python.exe tools/repair_engine_corrections.py            # dry-run
  .venv/Scripts/python.exe tools/repair_engine_corrections.py --apply    # на копии
  .venv/Scripts/python.exe tools/repair_engine_corrections.py --apply --live
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
from app.engine.lifecycle import base_end_ms, is_ob_like  # noqa: E402
from app.models import (  # noqa: E402
    Event,
    EventKind,
    Direction,
    TIMEFRAME_MINUTES,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
    now_ms,
)

LIVE = ROOT / "data" / "htf_zones.db"
FIXTURE = ROOT / "tests" / "fixtures" / "reviews_export_2026_10_06.json"


def _ts(ms) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)\
        .strftime("%Y-%m-%d")


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


def repair_zone(db: Database, z, candles, run_id: str, apply: bool,
                audit: list) -> None:
    """Один OB: инварианты §3.1/§6. Возвращает запись аудита (если изменено)."""
    tf_ms = TIMEFRAME_MINUTES[z.timeframe] * 60_000
    be = base_end_ms(z)
    scan_from = z.confirmed_at or be
    if scan_from is None or not candles:
        return
    last_boundary = close_boundary_ms(candles[-1].open_time, z.timeframe)
    entry = {"zone_id": z.id, "run_id": run_id, "changes": []}

    # 1) испорченный departed_at (§3.1, T02)
    departed = z.evidence.get("departed_at")
    if is_ob_like(z) and departed is not None and be is not None and departed <= be:
        if z.evidence.get("integrity") != "inconsistent":
            entry["changes"].append({
                "field": "evidence.integrity",
                "old": z.evidence.get("integrity"),
                "new": "inconsistent",
                "reason": f"departed_at {_ts(departed)} не позднее конца базы "
                          f"{_ts(be)} — выход не относится к этой базе (§3.1)",
            })
            if apply:
                ev = dict(z.evidence)
                ev["integrity"] = "inconsistent"
                ev["departure_evidence_incomplete"] = True
                ev["inconsistent_reason"] = entry["changes"][-1]["reason"]
                ev["source_candles"] = z.source_candles
                db.update_zone(z.id, evidence=ev)

    # 2) первое закрытие за границей (§6, T03–T06)
    first = first_close_beyond(candles, z, scan_from, last_boundary)
    if first is None:
        if entry["changes"]:
            audit.append(entry)
        return
    fb = close_boundary_ms(first.open_time, z.timeframe)
    is_converted = (z.end_reason or "").startswith("converted_to_breaker")

    if z.evidence.get("invalidated_at") != fb:
        entry["changes"].append({
            "field": "evidence.invalidated_at",
            "old": _ts(z.evidence.get("invalidated_at")),
            "new": _ts(fb),
            "reason": f"первая пробойная свеча {_ts(first.open_time)} "
                      f"Close={first.close} (OHLC: {first.open}/{first.high}/"
                      f"{first.low}/{first.close}), граница "
                      f"{z.lower if z.direction == Direction.BULL else z.upper}",
        })
        if apply:
            ev = dict(z.evidence)
            ev["invalidated_at"] = fb
            ev["first_invalidating_candle_open_time"] = first.open_time
            ev["source_candles"] = z.source_candles
            db.update_zone(z.id, evidence=ev)

    if not db.has_event(z.id, z.cycle_id, EventKind.OB_INVALIDATED, fb):
        entry["changes"].append({
            "field": "event.OB_INVALIDATED",
            "old": None, "new": _ts(fb),
            "reason": "бэкфилл события первого пробоя (журнал доказательств §14)",
        })
        if apply:
            db.insert_event(Event(
                id=None, zone_id=z.id, cycle_id=z.cycle_id,
                kind=EventKind.OB_INVALIDATED, occurred_at=fb,
                detected_at=now_ms(), price=first.close, delayed=True,
                evidence={
                    "candle_open_time": first.open_time,
                    "open": first.open, "high": first.high,
                    "low": first.low, "close": first.close,
                    "boundary_compared": (
                        z.lower if z.direction == Direction.BULL else z.upper
                    ),
                    "rule_version": z.rule_version, "run_id": run_id,
                    "backfill": "repair_engine_corrections (ТЗ 06.10.2026 §14)",
                },
            ))

    if not is_converted:
        fields: dict = {}
        if z.market_validity != "invalid":
            fields["market_validity"] = "invalid"
            fields["entry_eligible"] = False
        if z.display_until is None or fb < z.display_until:
            fields["display_until"] = fb
            fields["end_reason"] = "close_beyond (ТЗ §3)"
        if fields:
            entry["changes"].append({
                "field": "zone." + "+".join(sorted(fields)),
                "old": {"market_validity": z.market_validity,
                        "display_until": _ts(z.display_until),
                        "end_reason": z.end_reason},
                "new": {"market_validity": "invalid",
                        "display_until": _ts(fields.get("display_until", z.display_until)),
                        "end_reason": fields.get("end_reason", z.end_reason)},
                "reason": "пересчёт первого close_beyond ретросканированием (§6)",
            })
            if apply:
                db.update_zone(z.id, **fields)
        # 3) запрещённый Breaker + пробой → архив (T20)
        z2 = db.get_zone(z.id) if apply else z
        if z2.breaker_forbidden and z2.status not in (
                ZoneStatus.ARCHIVED, ZoneStatus.CONVERTED):
            entry["changes"].append({
                "field": "zone.status",
                "old": z2.status.value, "new": "archived",
                "reason": "breaker_forbidden + пробой — OB неактуален и при "
                          "запрещённой конверсии (T20, §10)",
            })
            if apply:
                db.update_zone(z.id, status=ZoneStatus.ARCHIVED,
                               end_reason="close_beyond_no_breaker (§15.6)")
    if entry["changes"]:
        audit.append(entry)


def zone_report_row(db: Database, label: dict, candles_cache: dict) -> dict:
    z = find_by_attrs(db, label["instrument"], label["timeframe"],
                      label["direction"], label["lower"], label["upper"])
    row = {
        "export_zone_id": label["zone_id"],
        "review_id": label["review_id"],
        "zone": f"{label['instrument']} {label['timeframe']} {label['direction']}",
        "bounds": f"{label['lower']}–{label['upper']}",
        "old": f"{label['status']}/{label['reason']}",
        "comment": label["comment"],
    }
    if z is None:
        row.update(db_zone_id=None, new="не найдена (data_incomplete)",
                   first_break="—", confirmation="—", history="—")
        return row
    key = (z.instrument_id, z.timeframe)
    if key not in candles_cache:
        candles_cache[key] = db.get_candles(*key)
    candles = candles_cache[key]
    scan_from = z.confirmed_at or base_end_ms(z)
    first_break = "—"
    history = "ok"
    if candles and scan_from is not None:
        first = first_close_beyond(
            candles, z, scan_from,
            close_boundary_ms(candles[-1].open_time, z.timeframe))
        if first is not None:
            first_break = (f"{_ts(first.open_time)} → "
                           f"{_ts(close_boundary_ms(first.open_time, z.timeframe))} "
                           f"(Close {first.close})")
        # T19: покрывает ли история формирование
        if z.source_candles and candles[0].open_time > min(z.source_candles):
            history = "data_incomplete (история позже базы)"
    else:
        history = "data_incomplete (нет свечей)"
    row.update(
        db_zone_id=z.id,
        new=f"{z.status.value}/{z.market_validity}"
            f"{('/' + z.end_reason) if z.end_reason else ''}",
        first_break=first_break,
        confirmation=("fvg" if z.confirmed_at else
                      "manual_only" if z.manual_confirmation_only else "unconfirmed"),
        history=history,
    )
    return row


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    apply = "--apply" in sys.argv
    live = "--live" in sys.argv
    load_detector_config()
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    diag_dir = ROOT / "data" / "diag"
    diag_dir.mkdir(parents=True, exist_ok=True)

    if live and apply:
        stamp = int(datetime.datetime.now().timestamp())
        backup = ROOT / "data" / "backups" / f"htf_zones_pre_repair_{stamp}.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(LIVE) + suffix)
            if src.exists():
                shutil.copy(src, str(backup) + suffix)
        print(f"Бэкап живой БД: {backup}")
        db = Database(str(LIVE))
        target = str(LIVE)
    else:
        scratch = diag_dir / f"repair_scratch_{int(datetime.datetime.now().timestamp())}.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(LIVE) + suffix)
            if src.exists():
                shutil.copy(src, str(scratch) + suffix)
        db = Database(str(scratch))
        target = f"{scratch} (копия)"

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"Режим: {mode}; цель: {target}; run_id: {run_id}\n")

    audit: list = []
    candles_cache: dict = {}
    try:
        obs = [z for z in db.get_zones()
               if z.type == ZoneType.OB and z.source == "auto"]
        for z in obs:
            key = (z.instrument_id, z.timeframe)
            if key not in candles_cache:
                candles_cache[key] = db.get_candles(*key)
            repair_zone(db, z, candles_cache[key], run_id, apply, audit)

        # отчёт по 24 зонам экспорта
        labels = json.loads(FIXTURE.read_text(encoding="utf-8"))["labels"]
        rows, seen = [], set()
        for lb in labels:
            if lb["zone_id"] in seen:
                continue
            seen.add(lb["zone_id"])
            rows.append(zone_report_row(db, lb, candles_cache))

        # контрольные №291/391/657: геометрия не изменилась (T24)
        regress = []
        for lb in labels:
            if lb["zone_id"] in (291, 391, 657):
                z = find_by_attrs(db, lb["instrument"], lb["timeframe"],
                                  lb["direction"], lb["lower"], lb["upper"])
                ok = z is not None and abs(z.lower - lb["lower"]) < 1e-6 \
                    and abs(z.upper - lb["upper"]) < 1e-6
                regress.append((lb["zone_id"], ok))
    finally:
        db.close()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"=== Пересчёт: изменений {'внесено' if apply else 'требуется'}: "
          f"{sum(len(e['changes']) for e in audit)} по {len(audit)} зонам ===")
    for e in audit[:50]:
        print(f"  zone {e['zone_id']}")
        for ch in e["changes"]:
            print(f"    {ch['field']}: {ch['old']} → {ch['new']} ({ch['reason']})")
    if len(audit) > 50:
        print(f"  … и ещё {len(audit) - 50} зон (см. audit-файл)")

    audit_path = diag_dir / f"repair_audit_{run_id.replace(':', '')}.json"
    audit_path.write_text(json.dumps(
        {"run_id": run_id, "mode": mode, "audit": audit},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Аудит: {audit_path}\n")

    print("=== Таблица 24 зон экспорта (старый → новый статус, первый пробой) ===")
    for r in rows:
        print(f"№{r['export_zone_id']:<4} {r['zone']:<22} {r['bounds']:<20}")
        print(f"      экспорт: {r['old']} | «{r['comment']}»")
        print(f"      сейчас:  {r['new']} (db id {r['db_zone_id']}) "
              f"| подтверждение: {r['confirmation']} | история: {r['history']}")
        print(f"      первый пробой: {r['first_break']}")
    print("\n=== Контрольные геометрии (T24) ===")
    for zid, ok in regress:
        print(f"  №{zid}: границы {'без изменений' if ok else 'ИЗМЕНИЛИСЬ — ПРОВЕРИТЬ'}")


if __name__ == "__main__":
    main()
