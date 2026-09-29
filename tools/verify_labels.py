"""Проверка новых правил на размеченных примерах labels.jsonl.

Чистый replay BTCUSDT из свечей боевой БД (копия без зон/событий) новым
движком; сверка ключевых кейсов разметки по ГЕОМЕТРИИ (id в новой БД другие).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DetectorConfig
from app.db import Database
from app.engine import replay_from_db
from app.models import ZoneStatus, ZoneType

SRC = Path("data/htf_zones.db")
TMP = Path("data/verify_labels.db")


def main() -> None:
    shutil.copy(SRC, TMP)
    # чистая БД: свечи и инструменты остаются, разметка/зоны/события сбрасываются
    conn = sqlite3.connect(TMP)
    for t in ("zone", "zone_relation", "event", "visit", "alert_state", "delivery"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM meta WHERE key LIKE 'replay_done:%'")
    conn.commit()
    conn.close()

    db = Database(str(TMP))
    cfg = DetectorConfig()
    stats = replay_from_db(db, cfg, 1, timeframes={"D1", "W1"})  # BTC, кейсы разметки — D1
    print("replay:", stats)

    zones = db.get_zones(1)

    def find(ztype, lo, hi, tol=1.0):
        return [z for z in zones if z.type == ztype
                and abs(z.lower - lo) <= tol and abs(z.upper - hi) <= tol]

    # --- кейс 164398 (R12): медвежий OB D1 72512.49–74590.77 ---
    for z in find(ZoneType.OB, 72512.49, 74590.77):
        rel = db.get_relation(z.id)
        fvg = db.get_zone(rel.confirming_fvg_id) if rel and rel.confirming_fvg_id else None
        ok = fvg is not None and fvg.confirmed_at == z.confirmed_at
        rng = z.evidence.get("confirming_fvg_range")
        match = rng == [fvg.lower, fvg.upper] if (fvg and rng) else None
        print(f"[164398] OB {z.lower}–{z.upper} status={z.status.value} "
              f"consistent={ok} range_match={match} fvg={fvg.id if fvg else None} "
              f"converted_pending={z.evidence.get('breakout_close_at')}")

    # --- кейс 164407 (§9.3): бычий OB — якорь 57800.19 ---
    for z in zones:
        if z.type == ZoneType.OB and z.direction.value == "bull" and z.timeframe == "D1":
            if abs(z.lower - 57800.19) < 1.0 or abs(z.lower - 58201.0) < 1.0:
                anchor = z.evidence.get("boundary_anchor")
                print(f"[164407] OB {z.lower}–{z.upper} status={z.status.value} anchor={anchor}")

    # --- кейс 164404: медвежий OB 65354–67292.15 — завершение и display_until ---
    for z in find(ZoneType.OB, 65354.0, 67292.15):
        print(f"[164404] OB status={z.status.value} display_until={z.display_until} "
              f"end={z.end_reason} forbidden={z.breaker_forbidden}")

    # --- H1: тронутые зоны должны быть в архиве (§9.8) ---
    h1 = [z for z in zones if z.timeframe == "H1"]
    h1_touched = [z for z in h1 if db.get_events(z.id)]
    bad = [z for z in h1_touched if z.status in (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED, ZoneStatus.CANDIDATE)]
    print(f"[H1] всего={len(h1)} с_событиями={len(h1_touched)} активные_тронутые={len(bad)}")

    # --- Breakers: созданы ли, с новым FVG ---
    brs = [z for z in zones if z.type == ZoneType.BREAKER]
    for b in brs:
        print(f"[BRK] {b.direction.value} {b.lower}–{b.upper} confirmed={b.confirmed_at} "
              f"fvg={b.evidence.get('breakout_fvg_range')}")
    if not brs:
        print("[BRK] ни одного Breaker на BTC за 180 дней по новому правилу")

    # сводка статусов
    from collections import Counter
    print("статусы:", dict(Counter(z.status.value for z in zones)))
    print("типы/ТФ:", dict(Counter(f"{z.type.value}/{z.timeframe}" for z in zones)))
    db.close()


if __name__ == "__main__":
    main()
