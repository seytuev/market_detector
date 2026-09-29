"""ТЗ «Единый движок» §8/§9: воспроизводимая диагностика датированных примеров.

Для каждого примера прогоняет общий движок (Scanner.replay) на свечах боевой
БД в изолированной in-memory БД и выдаёт: обнаружен/не обнаружен; выбранные
свечи; границы; подтверждение; актуальность; точную причину исключения.

Без исключений по asset/date/price/id: движок общий, примеры — только
контрольные точки отчёта. Исходная БД открывается read-only, ничего не пишется.

Запуск: .venv/Scripts/python.exe tools/diag_unified_examples.py
Результат: data/diag/unified_examples.json + .md
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DetectorConfig
from app.db import Database
from app.engine.fvg import scan_fvgs
from app.engine.orderblock import find_base, is_external
from app.engine.scanner import Scanner
from app.models import Candle, Instrument, ZoneType

DB_PATH = Path("data/htf_zones.db")
OUT_JSON = Path("data/diag/unified_examples.json")
OUT_MD = Path("data/diag/unified_examples.md")

DAY = 86_400_000


def ms(date: str) -> int:
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# Контрольные примеры ТЗ §8/§9 (координаты владельца; округление только для
# чтения — в БД хранятся исходные значения)
CASES = [
    {"zone_id": 5727348, "tf": "D1", "direction": "bear",
     "range": (86103.35155313351, 90639.98271117167),
     "base": ("2026-01-25", "2026-01-28")},
    {"zone_id": 5727350, "tf": "D1", "direction": "bear",
     "range": (98995.69675749319, 107618.01212534061),
     "base": ("2025-11-05", "2025-11-11")},
    {"zone_id": 5727349, "tf": "D1", "direction": "bear",
     "range": (94583.3283106267, 97886.03967302453),
     "base": ("2026-01-14", "2026-01-14")},
    {"zone_id": 5727351, "tf": "D1", "direction": "bear",
     "range": (80113.6639373297, 81976.50269754768),
     "base": ("2026-09-19", "2026-09-20")},
    {"zone_id": 5727352, "tf": "D1", "direction": "bear",
     "range": (75017.17904632153, 79654.07305177112),
     "base": ("2026-09-14", "2026-09-15")},
    {"zone_id": 5726949, "tf": "W1", "direction": "bull",
     "range": (58946.0, 66498.0),
     "base": ("2024-09-23", "2024-09-30"),
     "comment": "ТЗ §9: FVG владельца — 14.10.2024; тест 21.10.2024 → внутренний SSL"},
]


def load_fresh_db(src_path: Path, instrument_id: int) -> tuple[Database, int]:
    """Свечи инструмента из боевой БД (read-only) → чистая in-memory БД."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    db = Database(":memory:")
    ins_row = src.execute("SELECT * FROM instrument WHERE id=?", (instrument_id,)).fetchone()
    iid = db.upsert_instrument(Instrument(
        id=None, asset=ins_row["asset"], venue=ins_row["venue"],
        market_type=ins_row["market_type"], symbol=ins_row["symbol"],
        quote_asset=ins_row["quote_asset"], precision=ins_row["precision"],
    ))
    for tf in ("D1", "W1"):
        rows = src.execute(
            "SELECT * FROM candle WHERE instrument_id=? AND timeframe=? ORDER BY open_time",
            (instrument_id, tf),
        ).fetchall()
        db.insert_candles([
            Candle(instrument_id=iid, timeframe=r["timeframe"], open_time=r["open_time"],
                   close_time=r["close_time"], open=r["open"], high=r["high"],
                   low=r["low"], close=r["close"], closed=bool(r["closed"]),
                   source=r["source"])
            for r in rows
        ])
    src.close()
    return db, iid


def diagnose_case(case: dict, candles: list[Candle], cfg: DetectorConfig) -> dict:
    """Прямой разбор окна примера: FVG → база → external → причина исключения."""
    base_start, base_end = ms(case["base"][0]), ms(case["base"][1]) + DAY - 1
    lo_exp, hi_exp = case["range"]
    window_end = base_end + 90 * DAY
    fvgs = [f for f in scan_fvgs(candles, case["tf"])
            if base_start <= f.formed_at <= window_end
            and f.direction.value == case["direction"]]  # FVG импульса в направлении OB
    attempts = []
    if not fvgs:
        last = candles[-1].open_time if candles else None
        attempts.append({
            "result": ("в окне после базы нет FVG нужного направления "
                       f"(данные ТФ до {iso(last)}; окно до {iso(window_end)})"),
        })
    for f in fvgs[:25]:
        base = find_base(candles, f, cfg)
        rec = {
            "fvg": {"range": [f.lower, f.upper], "formed_at": iso(f.formed_at),
                    "confirmed_at": iso(f.confirmed_at)},
        }
        if base is None:
            rec["result"] = "база не найдена (нет свечи противоположного цвета перед FVG)"
        else:
            overlap = not (base.upper < lo_exp or base.lower > hi_exp)
            rec.update({
                "base_range": [base.lower, base.upper],
                "base_candles": [iso(t) for t in base.source_candles],
                "external": is_external(base, f),
                "gap_candles": base.gap_candles,
                "overlap_with_expected": overlap,
                "boundary_diff": [round(base.lower - lo_exp, 2), round(base.upper - hi_exp, 2)],
            })
            if not is_external(base, f):
                rec["result"] = "FVG не внешний (не подтверждает OB)"
            elif base.gap_candles > cfg.uncalibrated_ob_delay_max_candles:
                rec["result"] = f"отложенный FVG за окном ({base.gap_candles} > {cfg.uncalibrated_ob_delay_max_candles})"
            elif overlap:
                rec["result"] = "ПОДХОДИТ: внешний FVG, база пересекается с диапазоном владельца"
            else:
                rec["result"] = "база вне диапазона владельца"
        attempts.append(rec)
    return attempts


def main(db_path: Path = DB_PATH, out_json: Path = OUT_JSON, out_md: Path = OUT_MD) -> dict:
    cfg = DetectorConfig()
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ins_id = src.execute(
        "SELECT id FROM instrument WHERE symbol='BTCUSDT' AND venue='binance' LIMIT 1"
    ).fetchone()[0]
    src.close()

    db, iid = load_fresh_db(db_path, ins_id)
    scanner = Scanner(db, cfg)
    scanner.replay_instrument(iid)

    all_zones = db.get_zones(iid)
    report: list[dict] = []

    for case in CASES:
        base_start, base_end = ms(case["base"][0]), ms(case["base"][1]) + DAY - 1
        lo_exp, hi_exp = case["range"]
        candles = db.get_candles(iid, case["tf"])

        # кандидаты из прогона движка: OB того же ТФ/направления; разделяем
        # «обнаружен на базе владельца» (formed_at в окне базы) и «только
        # пересечение диапазона» (другая база — не доказывает пример)
        window_matches, overlap_only = [], []
        for z in all_zones:
            if z.type != ZoneType.OB or z.timeframe != case["tf"]:
                continue
            if z.direction.value != case["direction"]:
                continue
            in_window = base_start - 5 * DAY <= z.formed_at <= base_end + 5 * DAY
            overlap = not (z.upper < lo_exp or z.lower > hi_exp)
            if in_window and overlap:
                window_matches.append(z)
            elif overlap:
                overlap_only.append(z)
        found = window_matches
        entry_extra = {
            "overlap_only_zones": [
                {"id": z.id, "range": [z.lower, z.upper], "formed_at": iso(z.formed_at)}
                for z in sorted(overlap_only, key=lambda x: x.formed_at)
            ],
        }

        entry: dict = {
            "zone_id": case["zone_id"], "tf": case["tf"],
            "direction": case["direction"],
            "expected_range": [lo_exp, hi_exp],
            "expected_base": case["base"],
            "detected": bool(found),
            "zones": [
                {
                    "id": z.id, "range": [z.lower, z.upper],
                    "formed_at": iso(z.formed_at), "confirmed_at": iso(z.confirmed_at),
                    "status": z.status.value,
                    "market_validity": z.market_validity,
                    "max_test_depth": z.max_test_depth, "has_tests": z.has_tests,
                    "entry_eligible": z.entry_eligible,
                    "external_fvg": z.evidence.get("external_fvg"),
                    "source_candles": [iso(t) for t in z.source_candles],
                    "boundary_diff_vs_expected": [
                        round(z.lower - lo_exp, 2), round(z.upper - hi_exp, 2)],
                }
                for z in sorted(found, key=lambda x: x.formed_at)
            ],
        }
        if not found:
            entry["exclusion_reason_search"] = diagnose_case(case, candles, cfg)
        entry.update(entry_extra)
        if case.get("comment"):
            entry["comment"] = case["comment"]
        report.append(entry)

    # ТЗ §9: недельная тройка 7/14/21 октября 2024 — проверка реального FVG
    w1 = db.get_candles(iid, "W1")
    trio = [c for c in w1 if c.open_time in (ms("2024-10-07"), ms("2024-10-14"), ms("2024-10-21"))]
    w1_extra: dict = {"trio": [
        {"open": iso(c.open_time), "o": c.open, "h": c.high, "l": c.low, "c": c.close}
        for c in trio
    ]}
    if len(trio) == 3:
        bull_fvg = trio[0].high < trio[2].low
        bear_fvg = trio[0].low > trio[2].high
        w1_extra["bull_fvg_7_14_21"] = bull_fvg
        w1_extra["bear_fvg_7_14_21"] = bear_fvg
        if bull_fvg:
            w1_extra["fvg_range"] = [trio[0].high, trio[2].low]
            # подтверждение — закрытие третьей свечи (21.10 + неделя)
            w1_extra["fvg_confirmed_at"] = iso(trio[2].open_time + 7 * DAY)
        # тест 21.10: минимум недели — кандидат внутреннего SSL (уточнение владельца)
        w1_extra["test_2024_10_21_low"] = trio[2].low
        w1_extra["test_2024_10_21_inside_ob"] = 58946.0 <= trio[2].low <= 66498.0

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rule_version": cfg.rule_version, "cases": report,
               "w1_october_2024": w1_extra}
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    lines = ["# Диагностика примеров ТЗ «Единый движок» §8/§9",
             f"rule_version={cfg.rule_version}; свечи: боевая БД (read-only), "
             "движок: replay на чистой БД", ""]
    for e in report:
        lines.append(f"## zone_id={e['zone_id']} {e['tf']} {e['direction']} "
                     f"{e['expected_range'][0]:.2f}–{e['expected_range'][1]:.2f} "
                     f"(база {e['expected_base'][0]}…{e['expected_base'][1]})")
        lines.append(f"**Обнаружен: {'да' if e['detected'] else 'НЕТ'}**")
        for z in e["zones"]:
            lines.append(
                f"- OB id={z['id']} [{z['range'][0]:.2f}, {z['range'][1]:.2f}] "
                f"formed={z['formed_at']} confirmed={z['confirmed_at']} "
                f"status={z['status']} validity={z['market_validity']} "
                f"max_depth={z['max_test_depth']:.3f} eligible={z['entry_eligible']} "
                f"external_fvg={z['external_fvg']} diff={z['boundary_diff_vs_expected']}"
            )
        if "exclusion_reason_search" in e:
            if e["overlap_only_zones"]:
                lines.append("- Диапазон пересекают OB с другой базой (не доказывают пример):")
                for z in e["overlap_only_zones"]:
                    lines.append(f"  - id={z['id']} [{z['range'][0]:.2f}, {z['range'][1]:.2f}] formed={z['formed_at']}")
            lines.append("- Попытки реконструкции (FVG → база):")
            for a in e["exclusion_reason_search"][:10]:
                if "fvg" not in a:
                    lines.append(f"  - {a['result']}")
                else:
                    lines.append(f"  - FVG {a['fvg']['formed_at']}: {a['result']}"
                                 + (f" base={a.get('base_range')} external={a.get('external')}"
                                    if "base_range" in a else ""))
        lines.append("")
    lines.append("## W1, октябрь 2024 (zone_id=5726949)")
    lines.append("```json")
    lines.append(json.dumps(w1_extra, ensure_ascii=False, indent=2))
    lines.append("```")
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"записано: {out_json}, {out_md}")
    for e in report:
        print(f"zone_id={e['zone_id']}: detected={e['detected']} zones={len(e['zones'])}")
    return payload


if __name__ == "__main__":
    main()
