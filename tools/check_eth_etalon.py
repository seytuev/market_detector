"""Этап 6, приёмка п.23: ETHUSDT-эталон владельца на КОПИИ БД.

Эталон (скрин владельца, ETHUSDT Binance H1, 2026-09-23):
  HTF FVG D1 ~2714-2755, медвежий BOS H1 ~2730, диапазон движения ~2793->2636,
  цена ~2668, OB/FVG для возврата в Premium.

Что делает скрипт (только копии в data/diag/stage6/, живая БД read-only):
  A. Живая БД (ro): есть ли FVG D1 эталона в таблицах детектора и его статус.
  B. Чистый replay HTF (текущий движок) на scratch-копии: какие зоны/статусы
     производит ТЕКУЩИЙ код из тех же D1/W1-свечей (без ручной подгонки).
  C. Дозагрузка недостающих H1 (после 2026-09-16) из публичного API Binance
     в scratch-копию (живая не трогается).
  D. LTF-движок на scratch: открытие наблюдения по эталонному FVG
     (htf_context_types=FVG), replay + обработка H1; фиксация BOS/SMS,
     опор диапазона и подходящих зон. Режим what-if: открытие выполняется
     вручную в обход фильтра статуса воркера — помечено в выводе.

Запуск: .venv/Scripts/python.exe tools/check_eth_etalon.py
"""
from __future__ import annotations

import asyncio
import datetime
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.binance import BinanceSpotAdapter  # noqa: E402
from app.config import load_detector_config, load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.engine.ltf import LtfEngine  # noqa: E402
from app.engine.scanner import Scanner  # noqa: E402
from app.models import TIMEFRAME_MINUTES  # noqa: E402

LIVE = ROOT / "data" / "htf_zones.db"
WORK = ROOT / "data" / "diag" / "stage6"
ETH = 2


def ts(ms):
    if ms is None:
        return None
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def fresh_copy(dst: Path) -> None:
    for p in [dst, Path(str(dst) + "-wal"), Path(str(dst) + "-shm")]:
        if p.exists():
            p.unlink()
    src = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    tgt = sqlite3.connect(str(dst))
    src.backup(tgt)
    tgt.close()
    src.close()


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    out: dict = {}

    # ---------- A. живая БД read-only ----------
    ro = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    rows = ro.execute(
        """select id, type, timeframe, direction, lower, upper, formed_at, confirmed_at,
                  status, end_reason, market_validity, rule_version, created_at
           from zone where instrument_id=? and type='fvg' and timeframe in ('D1','W1')
           and lower between 2600 and 2900 order by lower""", (ETH,)).fetchall()
    out["A_live_fvg_near_etalon"] = [
        dict(zip(["id", "type", "tf", "dir", "lower", "upper", "formed", "confirmed",
                  "status", "end_reason", "mkt", "rules", "created"], r)) for r in rows
    ]
    for r in out["A_live_fvg_near_etalon"]:
        for k in ("formed", "confirmed", "created"):
            r[k] = ts(r[k]) if r[k] else r[k]
    ev = ro.execute(
        "select kind, occurred_at, detected_at, price, depth from event where zone_id=5727134"
    ).fetchall()
    out["A_etalon_fvg_events"] = [
        {"kind": k, "occurred": ts(o), "detected": ts(d), "price": p, "depth": dep}
        for k, o, d, p, dep in ev
    ]
    ro.close()

    # ---------- B. чистый HTF replay текущим движком ----------
    scratch = WORK / "eth_replay.db"
    fresh_copy(scratch)
    shutil.copy2(ROOT / "data" / "settings.json", WORK / "settings.json")
    db = Database(str(scratch))
    db.conn.execute("PRAGMA foreign_keys=OFF")  # scratch-копия: чистый лист по ETH
    cfg = load_detector_config()
    # чистый лист по ETH: удаляем производные детектора/LTF только instrument 2
    for table, col in [
        ("event", "zone_id"), ("visit", "zone_id"),
        ("zone_relation", "zone_id"),
    ]:
        db.conn.execute(
            f"DELETE FROM {table} WHERE {col} IN (SELECT id FROM zone WHERE instrument_id=?)",
            (ETH,))
    db.conn.execute("DELETE FROM inner_level WHERE instrument_id=?", (ETH,))
    db.conn.execute(
        "DELETE FROM zone_relation WHERE predecessor_ob_id IN "
        "(SELECT id FROM zone WHERE instrument_id=?)", (ETH,))
    db.conn.execute("DELETE FROM zone WHERE instrument_id=?", (ETH,))
    # LTF-данных по ETH нет (диагностика этапа 1: 0 наблюдений/pivots/зон) —
    # очистка LTF-таблиц не требуется; проверим это явно
    ltf_counts = {
        "observations": db.conn.execute(
            "SELECT COUNT(*) FROM ltf_observation WHERE instrument_id=?", (ETH,)).fetchone()[0],
        "pivots": db.conn.execute(
            "SELECT COUNT(*) FROM ltf_pivot WHERE instrument_id=?", (ETH,)).fetchone()[0],
        "entry_zones": db.conn.execute(
            "SELECT COUNT(*) FROM ltf_entry_zone WHERE instrument_id=?", (ETH,)).fetchone()[0],
    }
    assert not any(ltf_counts.values()), f"LTF-данные ETH не пусты: {ltf_counts}"
    db.conn.commit()

    scanner = Scanner(db, cfg)
    scanner.replay_instrument(ETH, timeframes={"D1", "W1"})
    zones = db.get_zones(ETH)
    etalon = [z for z in zones if z.type.value == "fvg" and z.timeframe == "D1"
              and abs(z.lower - 2714.64) < 1.0 and abs(z.upper - 2754.53) < 1.0]
    out["B_replay_etalon_fvg"] = [
        {"id": z.id, "lower": z.lower, "upper": z.upper, "status": z.status.value,
         "end_reason": z.end_reason, "formed": ts(z.formed_at),
         "confirmed": ts(z.confirmed_at), "mkt": z.market_validity} for z in etalon
    ]
    out["B_replay_counts"] = {}
    for z in zones:
        key = f"{z.type.value}/{z.timeframe}/{z.status.value}"
        out["B_replay_counts"][key] = out["B_replay_counts"].get(key, 0) + 1
    if etalon:
        zid = etalon[0].id
        out["B_etalon_events"] = [
            {"kind": e.kind.value, "occurred": ts(e.occurred_at), "price": e.price,
             "depth": e.depth}
            for e in db.get_events(zid)
        ]

    # ---------- C. дозагрузка H1 после 2026-09-16 ----------
    settings = load_settings()
    adapter = BinanceSpotAdapter(settings.binance_base_url)
    last = db.last_candle(ETH, "H1")
    out["C_h1_before"] = {"count": len(db.get_candles(ETH, "H1")),
                          "last": ts(last.open_time) if last else None}
    start = last.open_time + TIMEFRAME_MINUTES["H1"] * 60_000

    async def _fetch():
        import time
        return await adapter.klines("ETHUSDT", "H1", start, int(time.time() * 1000))

    candles = asyncio.run(_fetch())
    for c in candles:
        c.instrument_id = ETH
    db.insert_candles(candles)
    closed_new = [c for c in candles if c.closed]
    out["C_h1_fetched"] = len(candles)
    out["C_h1_closed_new"] = len(closed_new)
    out["C_h1_after"] = {"count": len(db.get_candles(ETH, "H1")),
                         "last": ts(db.last_candle(ETH, "H1").open_time)}

    # ---------- D. LTF-движок на scratch ----------
    cfg.htf_context_types = "FVG,OB"
    engine = LtfEngine(db, cfg)
    out["D_observations"] = []
    if etalon:
        z = etalon[0]
        zone_row = db.get_zone(z.id)
        # два варианта «первого входа»:
        # 1) семантика worker._first_htf_reach_ms — от display_from, включая
        #    D1-свечу формирования (уходит в дату формирования зоны);
        # 2) первый H1-вход после confirmed_at (как было бы в живом потоке:
        #    наблюдение открывается по факту касания валидной зоны).
        reach_worker = None
        start_from = zone_row.display_from or zone_row.formed_at or 0
        for c in list(db.get_candles(ETH, "D1")) + list(db.get_candles(ETH, "H1")):
            if not c.closed or c.open_time < start_from:
                continue
            if c.low <= zone_row.upper and c.high >= zone_row.lower:
                reach_worker = c.open_time if reach_worker is None else min(reach_worker, c.open_time)
        reach = None
        for c in db.get_candles(ETH, "H1"):
            if not c.closed or c.open_time < (zone_row.confirmed_at or 0):
                continue
            if c.low <= zone_row.upper and c.high >= zone_row.lower:
                reach = c.open_time
                break
        out["D_first_reach_worker_semantics"] = ts(reach_worker)
        out["D_first_reach"] = ts(reach)
        out["D_zone_status_at_open"] = zone_row.status.value
        out["D_zone_end_reason"] = zone_row.end_reason
        # WHAT-IF: воркер открыл бы наблюдение только для статусов active/weakened
        # (worker.LTF_PARENT_VALID_STATUSES); здесь наблюдение открыто вручную,
        # чтобы проверить механику BOS/диапазона/зон на эталонном эпизоде.
        obs = engine.on_htf_zone_touched(ETH, zone_row, reach)
        engine.replay_observation(obs.id)
        engine.process_h1_close(ETH)
        obs = db.get_ltf_observation(obs.id)
        scen = db.get_active_ltf_scenario(obs.id)
        scens = db.list_ltf_scenarios(obs.id)
        obs_info = {
            "obs_id": obs.id, "state": obs.state, "zone_id": obs.zone_id,
            "what_if_manual_open": True,
            "scenarios": [
                {"id": s.id, "state": s.state, "trigger": s.trigger, "stage": s.stage,
                 "direction": s.direction.value, "created": ts(s.created_at),
                 "cancelled": ts(s.cancelled_at), "reason": s.cancellation_reason}
                for s in scens
            ],
        }
        if scen is not None:
            rng = db.get_current_ltf_range(scen.id)
            events = db.list_ltf_structure_events(scen.id)
            obs_info["active_scenario"] = scen.id
            obs_info["structure_events"] = [
                {"kind": e.kind, "stage": e.stage, "direction": e.direction.value,
                 "break_level": e.break_level,
                 "break_candle": ts(e.break_candle_open_time),
                 "occurred": ts(e.occurred_at)}
                for e in events
            ]
            if rng is not None:
                anchor = {}
                for side, pid in (("low", rng.anchor_low_pivot_id),
                                  ("high", rng.anchor_high_pivot_id)):
                    if pid:
                        r = db.conn.execute(
                            "SELECT price, pivot_at, kind FROM ltf_pivot WHERE id=?",
                            (pid,)).fetchone()
                        if r:
                            anchor[side] = {"pivot_id": pid, "price": r[0],
                                            "pivot_at": ts(r[1]), "kind": r[2]}
                obs_info["range"] = {
                    "lower": rng.lower, "upper": rng.upper, "mid": rng.mid,
                    "version": rng.version, "available_at": ts(rng.available_at),
                    "anchors": anchor,
                }
                obs_info["range_versions"] = len(db.list_ltf_ranges(scen.id))
            eligible = [e for e in db.list_ltf_eligible_zones()
                        if e["scenario_id"] == scen.id]
            entries = []
            for e in eligible:
                zr = db.conn.execute(
                    "SELECT type, direction, lower, upper, validity, max_test_depth "
                    "FROM ltf_entry_zone WHERE id=?", (e["entry_zone_id"],)).fetchone()
                if zr:
                    entries.append({"zone_id": e["entry_zone_id"], "type": zr[0],
                                    "dir": zr[1], "lower": zr[2], "upper": zr[3],
                                    "validity": zr[4], "max_test_depth": zr[5]})
            obs_info["eligible_entries"] = entries
            obs_info["eligible_count"] = len(entries)
            reason_rows = db.conn.execute(
                "SELECT reason, COUNT(*) FROM ltf_scenario_entry WHERE scenario_id=? "
                "GROUP BY reason", (scen.id,)).fetchall()
            obs_info["entry_reasons"] = {r[0]: r[1] for r in reason_rows}
        out["D_observations"].append(obs_info)

    report = WORK / "eth_etalon_report.json"
    report.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    db.close()


if __name__ == "__main__":
    main()
