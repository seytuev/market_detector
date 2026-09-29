"""ТЗ «Единый движок» §10: миграция/восстановление зон после смены правил.

Что делает (идемпотентно, повторный запуск не дублирует уведомления):
1) выбирает OB, завершённые по worked_90 и прежнему правилу H1 first_touch,
   а также отвергнутые из-за ошибочной интерпретации lifecycle-комментария;
2) пересчитывает из свечей: подтверждение, самостоятельные тесты и закрытия
   до текущего момента (replay общего движка — он же строит внутренние
   уровни, max_test_depth, entry_eligible);
3) восстанавливает только OB без последующего валидного пробоя закрытием;
   пробитые остаются завершёнными с причиной close_beyond и событием;
4) не сбрасывает историю доставленных уведомлений (delivery/alert_state/
   ltf_event не трогаются); восстановленные события получают delayed=True;
5) при нехватке свечей зона помечается needs_replay — не принудительно active.

Запуск:
    .venv/Scripts/python.exe tools/restore_unified_zones.py [--db data/htf_zones.db] [--dry-run]
По умолчанию перед записью делается бэкап <db>.bak-unified-<ts>.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_detector_config
from app.db import Database
from app.engine import depth as geom
from app.engine.lifecycle import is_ob_like
from app.engine.scanner import Scanner
from app.models import Direction, Event, EventKind, TIMEFRAME_MINUTES, Zone, ZoneStatus, ZoneType, now_ms

# Признаки lifecycle-комментария, ошибочно записанного как wrong_type/geometry
# (ТЗ §10: review_id 131–134 — нормализация отдельным слоем, авторский текст
# не переписывается)
_LIFECYCLE_HINTS = ("тест", "отработ", "актуальн", "протестирован", "не отработан")


def _find_close_beyond(zone: Zone, candles) -> tuple[int, int, float] | None:
    """Первое закрытие свечи ТФ зоны строго за дальней границей после
    формирования: (candle_open_time, boundary, close). Равенство — не пробой."""
    tf_ms = TIMEFRAME_MINUTES[zone.timeframe] * 60_000
    start = zone.confirmed_at or zone.display_from or zone.formed_at
    for c in candles:
        if c.open_time + tf_ms <= start:
            continue
        if zone.direction == Direction.BULL and c.close < zone.lower:
            return c.open_time, c.open_time + tf_ms, c.close
        if zone.direction == Direction.BEAR and c.close > zone.upper:
            return c.open_time, c.open_time + tf_ms, c.close
    return None


def _reset_zone_stats(db: Database, zone: Zone) -> None:
    """Сброс производных данных зоны перед пересчётом replay: визиты —
    производные от свечей, пересоздаются движком; события и доставки — история,
    не трогаются (идемпотентность по UNIQUE-ключам)."""
    db.conn.execute("DELETE FROM visit WHERE zone_id=?", (zone.id,))
    db.conn.execute("DELETE FROM inner_level WHERE parent_ob_id=?", (zone.id,))
    db.conn.commit()
    db.update_zone(
        zone.id, max_test_depth=0.0, has_tests=False, test_extreme=None,
        entry_eligible=False, breakout_close_at=None, breakout_expired=False,
    )


def collect_targets(db: Database) -> dict[str, list[Zone]]:
    """Цели миграции по ТЗ §10 п.1."""
    worked_90, h1_touch, rejected_lifecycle = [], [], []
    for z in db.get_zones():
        if z.type == ZoneType.OB and z.end_reason and "worked_90" in z.end_reason:
            worked_90.append(z)
        elif z.end_reason and ("h1_first_touch" in z.end_reason
                               or "h1_jumped_through" in z.end_reason):
            h1_touch.append(z)
        elif z.status == ZoneStatus.REJECTED and z.type in (ZoneType.OB, ZoneType.MANUAL):
            # отвергнут по lifecycle-комментарию: оценка с geometry_verdict,
            # но текст/причина — про актуальность/тест, а не про геометрию
            assessments = db.get_assessments(z.id)
            reviews = {r.id: r for r in db.get_reviews(z.id)}
            for a in assessments:
                text = (reviews.get(a.review_id).text if a.review_id in reviews else "")
                hay = f"{a.reason_code} {text}".lower()
                if any(h in hay for h in _LIFECYCLE_HINTS):
                    rejected_lifecycle.append(z)
                    break
    return {"worked_90": worked_90, "h1_first_touch": h1_touch,
            "rejected_lifecycle": rejected_lifecycle}


def _repair_confirming_fvg(db: Database, cfg, zone: Zone, candles, dry: bool) -> dict | None:
    """ТЗ §10 (кейс 164417): связь OB→FVG должна указывать на FVG, реально
    использованный расчётом. Пересчитываем движком: первый внешний FVG,
    чья база совпадает с зоной; при расхождении чиним relation и
    confirmed_at (confirmed_at OB = подтверждение именно этого FVG)."""
    from app.engine.fvg import scan_fvgs
    from app.engine.orderblock import find_base, is_external

    for f in scan_fvgs(candles, zone.timeframe):
        if f.direction != zone.direction:
            continue
        base = find_base(candles, f, cfg)
        if base is None:
            continue
        if (base.lower, base.upper, base.formed_at) != (zone.lower, zone.upper, zone.formed_at):
            continue
        if not is_external(base, f):
            continue
        fvg_zone = next(
            (z for z in db.get_zones(zone.instrument_id, types=[ZoneType.FVG])
             if z.formed_at == f.formed_at and z.timeframe == zone.timeframe
             and z.lower == f.lower and z.upper == f.upper),
            None,
        )
        rel = db.get_relation(zone.id)
        mismatch = (
            (fvg_zone is not None and (rel is None or rel.confirming_fvg_id != fvg_zone.id))
            or zone.confirmed_at != f.confirmed_at
        )
        if not mismatch:
            return None
        fix = {"fvg_formed_at": f.formed_at, "fvg_confirmed_at": f.confirmed_at,
               "fvg_range": [f.lower, f.upper],
               "old_confirmed_at": zone.confirmed_at,
               "old_relation_fvg": rel.confirming_fvg_id if rel else None}
        if not dry:
            if fvg_zone is not None:
                db.conn.execute(
                    """INSERT INTO zone_relation (zone_id, confirming_fvg_id) VALUES (?,?)
                       ON CONFLICT (zone_id) DO UPDATE SET confirming_fvg_id=excluded.confirming_fvg_id""",
                    (zone.id, fvg_zone.id),
                )
                db.conn.commit()
            z = db.get_zone(zone.id)
            ev = dict(z.evidence)
            ev.update({"external_fvg": True, "confirming_fvg_range": [f.lower, f.upper],
                       "confirming_fvg_formed_at": f.formed_at,
                       "relation_repaired": "restore_unified_zones (ТЗ §10)"})
            ev["source_candles"] = z.source_candles
            db.update_zone(zone.id, confirmed_at=f.confirmed_at, evidence=ev)
        return fix
    return None


def restore_zone(db: Database, cfg, zone: Zone, now: int, dry: bool) -> dict:
    """Пересчёт одной зоны из свечей. Восстанавливаем только без валидного
    пробоя; пробитые получают корректную причину и событие закрытия."""
    candles = db.get_candles(zone.instrument_id, zone.timeframe)
    rec: dict = {"zone_id": zone.id, "tf": zone.timeframe,
                 "old_status": zone.status.value, "old_end_reason": zone.end_reason}

    if not candles or candles[0].open_time > zone.formed_at:
        # нехватка свечей — не принудительно active (ТЗ §10 п.6)
        rec["result"] = "needs_replay (нет свечей с formed_at)"
        if not dry:
            db.update_zone(zone.id, needs_replay=True)
        return rec

    breach = _find_close_beyond(zone, candles)
    if breach is not None:
        open_t, boundary, close = breach
        rec["result"] = "broken — не восстановлена"
        rec["breach"] = {"candle_open_time": open_t, "boundary": boundary,
                         "close": close}
        if not dry:
            db.update_zone(zone.id, market_validity="invalid", entry_eligible=False,
                           status=ZoneStatus.ARCHIVED)
            db.conn.execute(
                "UPDATE zone SET display_until=?, end_reason=? WHERE id=?",
                (boundary, "close_beyond (ТЗ §3)", zone.id),
            )
            db.invalidate_zone_cache()
            db.conn.commit()
            # событие пробоя с полным контекстом (ТЗ §10: event_time,
            # candle_open_time, candle_close_time, условие, экстремум)
            ev = Event(
                id=None, zone_id=zone.id, cycle_id=zone.cycle_id,
                kind=EventKind.JUMP_THROUGH, occurred_at=boundary, detected_at=now,
                price=close, delayed=True,
                evidence={
                    "migration": "restore_unified_zones",
                    "condition": ("close < lower" if zone.direction == Direction.BULL
                                  else "close > upper"),
                    "candle_open_time": open_t, "candle_close_time": boundary,
                    "close": close, "lower": zone.lower, "upper": zone.upper,
                },
            )
            db.insert_event(ev)
        return rec

    # пробоя нет — восстановление: снимаем терминальные поля, пересчёт ниже
    rec["result"] = "restored"
    fix = _repair_confirming_fvg(db, cfg, zone, candles, dry)
    if fix:
        rec["confirming_fvg_repaired"] = fix
    if not dry:
        db.update_zone(
            zone.id,
            status=ZoneStatus.ACTIVE if zone.confirmed_at else ZoneStatus.CANDIDATE,
            market_validity="active", display_until=None, end_reason=None,
            breaker_forbidden=False,
        )
        _reset_zone_stats(db, zone)
    return rec


def normalize_manual_zones(db: Database, dry: bool) -> list[int]:
    """ТЗ §7: у ручных зон время создания записи — не доказанное время
    исторического подтверждения. confirmed_at, проставленный старым API
    равным created_at, сбрасывается в NULL (видимость зоны обеспечивается
    anchor_time/display_from/formed_at)."""
    fixed = []
    for z in db.get_zones():
        if z.source != "manual" or z.confirmed_at is None:
            continue
        if z.confirmed_at != z.created_at:
            continue
        fixed.append(z.id)
        if not dry:
            db.update_zone(z.id, confirmed_at=None)
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/htf_zones.db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="data/diag/restore_unified_report.json")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not args.dry_run:
        # бэкап через SQLite backup API — copy2 файла при WAL небезопасен
        import sqlite3
        backup = db_path.with_suffix(f".bak-unified-{int(time.time())}")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(backup))
        src.backup(dst)
        dst.close()
        src.close()
        print(f"бэкап: {backup}")

    db = Database(str(db_path))
    cfg = load_detector_config()
    now = now_ms()

    targets = collect_targets(db)
    manual_fixed = normalize_manual_zones(db, args.dry_run)
    report: dict = {"rule_version": cfg.rule_version, "dry_run": args.dry_run,
                    "manual_confirmed_at_reset": manual_fixed,
                    "targets": {k: [z.id for z in v] for k, v in targets.items()},
                    "zones": []}

    for group, zones in targets.items():
        for z in zones:
            rec = restore_zone(db, cfg, z, now, args.dry_run)
            rec["group"] = group
            report["zones"].append(rec)

    if not args.dry_run:
        # Пересчёт восстановленных зон общим движком: replay идемпотентен —
        # существующие события не дублируются, новые получают occurred_at
        # исторических свечей и delayed=True; доставки не сбрасываются.
        # D1/W1 — replay инструмента по этим ТФ; H1 — точечный прогон
        # track_zone по свечам зоны (полная H1-история × все зоны слишком
        # дорога для миграции).
        from app.engine.lifecycle import track_zone
        scanner = Scanner(db, cfg)
        restored_ids = {r["zone_id"] for r in report["zones"] if r["result"] == "restored"}
        by_instr_tf: dict[int, set[str]] = {}
        for g in targets.values():
            for z in g:
                if z.id in restored_ids:
                    by_instr_tf.setdefault(z.instrument_id, set()).add(z.timeframe)
        for iid, tfs in by_instr_tf.items():
            htf = tfs & {"D1", "W1"}
            if htf:
                scanner.replay_instrument(iid, timeframes=htf)
            for zid in restored_ids:
                z = db.get_zone(zid)
                if z is None or z.instrument_id != iid or z.timeframe not in tfs - htf:
                    continue
                tf_ms = TIMEFRAME_MINUTES[z.timeframe] * 60_000
                prev = None
                for c in db.get_candles(iid, z.timeframe):
                    boundary = c.open_time + tf_ms
                    z = db.get_zone(zid)
                    if z is None or z.status == ZoneStatus.ARCHIVED:
                        break
                    silent = z.type == ZoneType.OB and (
                        z.confirmed_at is None or z.confirmed_at >= boundary)
                    track_zone(db, cfg, z, c.low, c.high, boundary, False,
                               prev, now, entered_at=c.open_time, silent=silent)
                    prev = c.close
        for r in report["zones"]:
            if r["zone_id"] in restored_ids:
                z = db.get_zone(r["zone_id"])
                r["new"] = {
                    "status": z.status.value, "market_validity": z.market_validity,
                    "max_test_depth": z.max_test_depth, "has_tests": z.has_tests,
                    "entry_eligible": z.entry_eligible,
                    "inner_levels": len(db.list_inner_levels(parent_ob_id=z.id)),
                }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["targets"], ensure_ascii=False))
    for r in report["zones"]:
        print(f"zone {r['zone_id']} [{r['group']}]: {r['result']}"
              + (f" -> {r['new']}" if "new" in r else ""))
    print(f"отчёт: {out}")
    db.close()


if __name__ == "__main__":
    main()
