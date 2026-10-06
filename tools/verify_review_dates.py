"""T23 (ТЗ 06.10.2026 §12): проверка дат из комментариев владельца.

Даты в комментариях — заявления владельца, а не верифицированные OHLC-
события. Для каждого кейса: полный интервал данных, первая qualifying
свеча (OHLC, open/close_time), сравниваемая граница, строгое сравнение,
совпадение/расхождение с комментарием. Дата пользователя механически не
становится event_time: для W1 сначала проверяется, это open_time недели
пробоя или её close_time; сигнал о закрытии недели доступен на следующей
недельной границе. Неуточнённый год («2 февраля») сохраняется явно.

Запуск: .venv/Scripts/python.exe tools/verify_review_dates.py
Вывод: stdout + data/diag/review_dates_<ts>.txt
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
from app.engine.breaker import first_close_beyond  # noqa: E402
from app.engine.lifecycle import base_end_ms  # noqa: E402
from app.models import TIMEFRAME_MINUTES, Direction, close_boundary_ms  # noqa: E402

LIVE = ROOT / "data" / "htf_zones.db"
DAY = 86_400_000


def _ts(ms) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)\
        .strftime("%Y-%m-%d %H:%M")


def _ms(iso: str) -> int:
    dt = datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


# ТЗ §12: кейсы по характеристикам экспорта §18 (IDs между выгрузками
# пересоздаются — идентификация по инструменту/ТФ/направлению/границам)
CASES = [
    {"export_zone_id": 551, "symbol": "SOLUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 95.26, "upper": 147.48,
     "claim": "январь–февраль 2026", "claim_ms_from": _ms("2026-01-01"),
     "claim_ms_to": _ms("2026-03-01"), "year_unspecified": False,
     "engine_display_until": "2026-08-17"},
    {"export_zone_id": 292, "symbol": "ETHUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 2111.89, "upper": 2879.22,
     "claim": "уже 2 февраля (год НЕ указан — гипотеза 2026)",
     "claim_ms_from": _ms("2026-02-02"), "claim_ms_to": _ms("2026-02-03"),
     "year_unspecified": True, "engine_display_until": "2026-08-17"},
    {"export_zone_id": 552, "symbol": "SOLUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 144.85, "upper": 159.99,
     "claim": "10 ноября 2025", "claim_ms_from": _ms("2025-11-10"),
     "claim_ms_to": _ms("2025-11-11"), "year_unspecified": False,
     "engine_display_until": "2026-10-05"},
    {"export_zone_id": 22, "symbol": "BTCUSDT", "timeframe": "W1",
     "direction": "bear", "lower": 112650.0, "upper": 124474.0,
     "claim": "29 сентября 2025", "claim_ms_from": _ms("2025-09-29"),
     "claim_ms_to": _ms("2025-09-30"), "year_unspecified": False,
     "engine_display_until": None},
    {"export_zone_id": 553, "symbol": "SOLUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 173.43, "upper": 218.0,
     "claim": "3 ноября 2025", "claim_ms_from": _ms("2025-11-03"),
     "claim_ms_to": _ms("2025-11-04"), "year_unspecified": False,
     "engine_display_until": None},
    {"export_zone_id": 23, "symbol": "BTCUSDT", "timeframe": "W1",
     "direction": "bull", "lower": 107255.0, "upper": 113667.28,
     "claim": "6 октября 2025", "claim_ms_from": _ms("2025-10-06"),
     "claim_ms_to": _ms("2025-10-07"), "year_unspecified": False,
     "engine_display_until": None},
]


def find_by_case(db: Database, case: dict):
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


def verify(db: Database, case: dict, out: list[str]) -> None:
    out.append(f"### экспорт №{case['export_zone_id']}: {case['symbol']} "
               f"{case['timeframe']} {case['direction']} "
               f"[{case['lower']}, {case['upper']}]")
    out.append(f"комментарий владельца: «{case['claim']}»"
               + (" [ГОД НЕ УТОЧНЁН — проверяется как гипотеза]"
                  if case["year_unspecified"] else ""))
    out.append(f"display_until движка в экспорте: {case['engine_display_until'] or '—'}")
    z = find_by_case(db, case)
    if z is None:
        out.append("зона не найдена в текущей БД — проверка по данным "
                   "экспорта невозможна (data_incomplete)\n")
        return
    out.append(f"соответствие в текущей БД: zone_id={z.id}, "
               f"confirmed_at={_ts(z.confirmed_at)}, "
               f"display_until={_ts(z.display_until)}, end_reason={z.end_reason}")
    candles = db.get_candles(z.instrument_id, z.timeframe)
    tf_ms = TIMEFRAME_MINUTES[z.timeframe] * 60_000
    scan_from = z.confirmed_at or base_end_ms(z)
    if scan_from is None or not candles:
        out.append("недостаточно данных для сканирования (data_incomplete)\n")
        return
    out.append(f"интервал данных: {_ts(candles[0].open_time)} … "
               f"{_ts(close_boundary_ms(candles[-1].open_time, z.timeframe))} "
               f"({len(candles)} свечей); сканирование от {_ts(scan_from)}")
    last_boundary = close_boundary_ms(candles[-1].open_time, z.timeframe)
    first = first_close_beyond(candles, z, scan_from, last_boundary)
    boundary_compared = z.lower if z.direction == Direction.BULL else z.upper
    if first is None:
        out.append("первой qualifying свечи НЕТ: строгого закрытия за границей "
                   f"{boundary_compared} в данных не найдено")
        if z.display_until is not None:
            out.append("РАСХОЖДЕНИЕ: в БД записано завершение — проверить "
                       "полноту истории свечей")
        out.append("вывод: актуальность по короткому хвосту не доказана и не "
                   "опровергнута (нужна полная история, ТЗ §6)\n")
        return
    fb = close_boundary_ms(first.open_time, z.timeframe)
    strict = (first.close < z.lower) if z.direction == Direction.BULL \
        else (first.close > z.upper)
    out.append(f"первая qualifying свеча: open_time={_ts(first.open_time)}, "
               f"close_time={_ts(first.close_time)}, граница закрытия={_ts(fb)}")
    out.append(f"  OHLC: O={first.open} H={first.high} L={first.low} C={first.close}")
    out.append(f"  сравниваемая граница: {boundary_compared}; строгое "
               f"условие ({'Close < L' if z.direction == Direction.BULL else 'Close > U'}): "
               f"{'ВЫПОЛНЕНО' if strict else 'НЕ выполнено'}")
    # недельная семантика: дата пользователя — open или close недели?
    hit_open = case["claim_ms_from"] == first.open_time
    hit_close = case["claim_ms_from"] == first.close_time - DAY + 1 or \
        case["claim_ms_from"] == fb - tf_ms
    in_claim_window = first.open_time <= case["claim_ms_from"] < fb
    out.append(f"  заявленная дата {_ts(case['claim_ms_from'])}: "
               f"{'open_time этой недели' if hit_open else ''}"
               f"{'внутри пробойной недели' if in_claim_window and not hit_open else ''}"
               f"{'' if (hit_open or in_claim_window) else 'не совпадает с пробойной свечой'}")
    if in_claim_window:
        out.append(f"  совпадение: сигнал о закрытии доступен на границе {_ts(fb)} — "
                   "дата пользователя НЕ является event_time (§12)")
    elif case["claim_ms_from"] < first.open_time:
        out.append("  расхождение: заявленная дата РАНЬШЕ первой qualifying "
                   "свечи в данных — проверить более ранние свечи полной "
                   "историей (верхняя граница не доказывает первый пробой)")
    else:
        out.append("  расхождение: заявленная дата ПОЗЖЕ первой qualifying "
                   "свечи — пробой по данным наступил раньше комментария")
    out.append("")


def main() -> None:
    load_detector_config()  # единые настройки правил
    diag_dir = ROOT / "data" / "diag"
    diag_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(datetime.datetime.now().timestamp())
    scratch = diag_dir / f"review_dates_scratch_{stamp}.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(LIVE) + suffix)
        if src.exists():
            shutil.copy(src, str(scratch) + suffix)
    db = Database(str(scratch))
    out: list[str] = [
        f"Проверка дат из комментариев (T23, ТЗ 06.10.2026 §12) — "
        f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}Z",
        f"БД: {LIVE} (scratch-копия)\n",
    ]
    try:
        for case in CASES:
            verify(db, case, out)
    finally:
        db.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(scratch) + suffix)
        if p.exists():
            p.unlink()
    text = "\n".join(out)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    path = diag_dir / f"review_dates_{stamp}.txt"
    path.write_text(text, encoding="utf-8")
    print(f"\nОтчёт сохранён: {path}")


if __name__ == "__main__":
    main()
