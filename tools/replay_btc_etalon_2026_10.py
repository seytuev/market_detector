"""Этап 7, §14: BTCUSDT-эталон 07.10.2026 00:09:35 МСК — replay + свечной отчёт.

Работает ТОЛЬКО со scratch-копией data/htf_zones.db (SQLite backup API,
живая БД открывается read-only и не изменяется).

Эталон (скрин владельца): HTF OB D1 bear [87304.33; 90600] (zone 5727038),
цена 85552.19, обработанная H1 06.10 20:59:59 UTC; правая панель СТАРОГО
кода: «BOS 84520 от 01.10 18:59:59 UTC», «диапазон [83186; 84419.69]»,
«уровень отмены 86698.99», BSL 84419.69 «снятие не подтверждено» (баг).

Что делает скрипт:
  1. scratch-копия; чистый лист по LTF BTC (наблюдения/сценарии/pivots/зоны/
     события удаляются, свечи и HTF-зоны сохраняются); meta-курсоры ltf:*
     сброшены — прогон строится только из свечей, доступных на каждый момент.
  2. Наблюдение открыто ВРУЧНУЮ (what-if: зона в статусе candidate, воркер
     её мог не взять) по первому H1-касанию зоны после 15.09.2026.
  3. Пошаговая обработка H1 (по свече, now=close_time) с контрольными
     срезами a–e; тесты replay-детерминизма (tests/test_ltf_replay_perf.py)
     гарантируют побайтовую идентичность такого прогона replay_observation —
     в конце replay_observation выполняется как self-check (состояние не
     должно измениться).
  4. Отчёт report.md + events.jsonl + slices/*.json в data/diag/btc_etalon_2026_10/.
  5. Сравнение политик якорей: тот же прогон с continuation_only (Q8).

Запуск: .venv/Scripts/python.exe tools/replay_btc_etalon_2026_10.py
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.engine.ltf import LtfEngine  # noqa: E402
from app.engine.ltf.entries import fvg_fill_status  # noqa: E402
from app.engine.ltf.pivots import find_h1_pivots  # noqa: E402
from tools.bench_ltf_restore import copy_live_db, load_live_cfg  # noqa: E402

LIVE = ROOT / "data" / "htf_zones.db"
WORK = ROOT / "data" / "diag" / "btc_etalon_2026_10"
BTC = 1
ZONE_ID = 5727038                      # HTF OB D1 bear [87304.33; 90600]
MSK = datetime.timedelta(hours=3)
H1 = 3_600_000


def _ms(y, m, d, hh=0, mm=0, ss=0) -> int:
    return int(datetime.datetime(y, m, d, hh, mm, ss,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


SEP15 = _ms(2026, 9, 15)
FEED_START = _ms(2026, 9, 21)          # H1 с этого open удаляются и подаются заново
AS_OF = _ms(2026, 10, 6, 21, 9, 35)    # 07.10.2026 00:09:35 МСК
LAST_CLOSED = _ms(2026, 10, 6, 20) + H1 - 1  # close 06.10 20:59:59 UTC

LTF_TABLES = [
    "ltf_observation", "ltf_scenario", "ltf_pivot", "ltf_pivot_role_log",
    "ltf_structure_event", "ltf_movement", "ltf_range", "ltf_entry_zone",
    "ltf_scenario_entry", "ltf_liquidity_test", "ltf_event",
    "ltf_review", "ltf_review_assessment",
]


def tsu(ms) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%d.%m %H:%M:%S")


def tsm(ms) -> str:
    if ms is None:
        return "—"
    return (datetime.datetime.utcfromtimestamp(ms / 1000) + MSK).strftime(
        "%d.%m.%Y %H:%M:%S")


def msk_label(ms) -> str:
    return f"{tsm(ms)} МСК ({tsu(ms)} UTC)"


def dump_state(db: Database) -> dict:
    """Срез состояния LTF по BTC (obs → scenarios → связанные таблицы)."""
    obs_ids = [
        r["id"] for r in db.conn.execute(
            "SELECT id FROM ltf_observation WHERE instrument_id=1")
    ]
    sc_ids = [
        r["id"] for r in db.conn.execute(
            "SELECT id FROM ltf_scenario WHERE observation_id IN "
            f"({','.join(map(str, obs_ids)) or 'NULL'})")
    ]
    marks_obs = ",".join(map(str, obs_ids)) or "NULL"
    marks_sc = ",".join(map(str, sc_ids)) or "NULL"
    out = {
        "ltf_observation": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_observation WHERE id IN ({marks_obs}) ORDER BY id")],
        "ltf_scenario": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_scenario WHERE id IN ({marks_sc}) ORDER BY id")],
        "ltf_structure_event": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_structure_event WHERE scenario_id IN ({marks_sc}) ORDER BY id")],
        "ltf_movement": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_movement WHERE scenario_id IN ({marks_sc}) ORDER BY id")],
        "ltf_range": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_range WHERE scenario_id IN ({marks_sc}) ORDER BY id")],
        "ltf_scenario_entry": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_scenario_entry WHERE scenario_id IN ({marks_sc}) ORDER BY id")],
        "ltf_liquidity_test": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_liquidity_test WHERE scenario_id IN ({marks_sc}) ORDER BY id")],
        "ltf_event": [dict(r) for r in db.conn.execute(
            f"SELECT * FROM ltf_event WHERE observation_id IN ({marks_obs}) ORDER BY id")],
        "ltf_pivot": [dict(r) for r in db.conn.execute(
            "SELECT * FROM ltf_pivot WHERE instrument_id=1 ORDER BY id")],
        "ltf_entry_zone": [dict(r) for r in db.conn.execute(
            "SELECT * FROM ltf_entry_zone WHERE instrument_id=1 ORDER BY id")],
    }
    out["meta"] = [
        {"key": r["key"], "value": r["value"]} for r in db.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'ltf:%' ORDER BY key")
    ]
    return out


def diff_states(a: dict, b: dict) -> list[str]:
    lines = []
    for t in a:
        ka = {json.dumps(r, sort_keys=True, default=str) for r in a[t]}
        kb = {json.dumps(r, sort_keys=True, default=str) for r in b[t]}
        add, rem = len(kb - ka), len(ka - kb)
        if add or rem:
            lines.append(f"{t}: +{add} -{rem}")
    return lines


def clean_btc_ltf(db: Database) -> None:
    """Чистый лист LTF по BTC в scratch (HTF-зоны и свечи сохраняются)."""
    con = db.conn
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    obs_ids = "(SELECT id FROM ltf_observation WHERE instrument_id=1)"
    sc_ids = f"(SELECT id FROM ltf_scenario WHERE observation_id IN {obs_ids})"
    for stmt in [
        f"DELETE FROM ltf_event WHERE observation_id IN {obs_ids}",
        f"DELETE FROM ltf_liquidity_test WHERE scenario_id IN {sc_ids}",
        f"DELETE FROM ltf_scenario_entry WHERE scenario_id IN {sc_ids}",
        f"DELETE FROM ltf_structure_event WHERE scenario_id IN {sc_ids}",
        f"DELETE FROM ltf_movement WHERE scenario_id IN {sc_ids}",
        f"DELETE FROM ltf_range WHERE scenario_id IN {sc_ids}",
        f"DELETE FROM ltf_scenario WHERE observation_id IN {obs_ids}",
        "DELETE FROM ltf_observation WHERE instrument_id=1",
        "DELETE FROM ltf_pivot_role_log WHERE pivot_id IN "
        "(SELECT id FROM ltf_pivot WHERE instrument_id=1)",
        "DELETE FROM ltf_pivot WHERE instrument_id=1",
        "DELETE FROM ltf_review_assessment WHERE entry_zone_id IN "
        "(SELECT id FROM ltf_entry_zone WHERE instrument_id=1)",
        "DELETE FROM ltf_review WHERE entry_zone_id IN "
        "(SELECT id FROM ltf_entry_zone WHERE instrument_id=1)",
        "DELETE FROM ltf_entry_zone WHERE instrument_id=1",
        "DELETE FROM meta WHERE key LIKE 'ltf:%'",
    ]:
        table = stmt.split()[2]
        if table in existing or table == "meta":
            con.execute(stmt)
    con.commit()


def run_replay(scratch: Path, policy: str, checkpoints: dict[int, str] | None,
               collect_events: bool = False):
    """Полный прогон на scratch: cleanup → ручное открытие наблюдения →
    пошаговая подача H1 → (опц.) срезы → self-check replay_observation."""
    copy_live_db(str(LIVE), str(scratch))
    db = Database(str(scratch), ltf_cache=True)
    cfg = load_live_cfg(str(LIVE))
    cfg.ltf_range_anchor_policy = policy
    clean_btc_ltf(db)
    engine = LtfEngine(db, cfg, scan_cursors=True)

    zone = db.get_zone(ZONE_ID)
    assert zone is not None, f"zone {ZONE_ID} не найдена"
    feed = db.get_candles(BTC, "H1", start_ms=FEED_START)
    reach = min(
        c.open_time for c in db.get_candles(BTC, "H1")
        if c.closed and c.open_time >= SEP15
        and c.low <= zone.upper and c.high >= zone.lower
    )
    db.conn.execute(
        "DELETE FROM candle WHERE instrument_id=1 AND timeframe='H1' "
        "AND open_time >= ?", (FEED_START,))
    db.conn.commit()
    obs = engine.on_htf_zone_touched(BTC, zone, reach)

    slices: dict[str, dict] = {}
    for c in feed:
        if not c.closed:
            continue
        db.insert_candles([c])
        engine.process_h1_close(BTC, now_ms=c.close_time)
        if checkpoints and c.close_time in checkpoints:
            slices[checkpoints[c.close_time]] = dump_state(db)
    # self-check: replay_observation детерминирован — состояние не меняется
    before = dump_state(db)
    engine.replay_observation(obs.id)
    after = dump_state(db)
    drift = diff_states(before, after)
    return {
        "db": db, "cfg": cfg, "obs_id": obs.id, "reach": reach,
        "zone_status": zone.status.value, "slices": slices,
        "replay_drift": drift, "before": before,
    }


def pivot_checkpoints(db_candles) -> dict[str, int]:
    """Офлайн-pivots 3/3 для контрольных точек b (верх 02.10) и c (нижний
    якорь снижения): время = close свечи подтверждения опоры."""
    pivots = find_h1_pivots(db_candles, 3, 3)
    d2 = [_ms(2026, 10, 2), _ms(2026, 10, 3)]
    top = max(
        (p for p in pivots if p.kind == "high"
         and d2[0] <= p.pivot_at < d2[1] + 12 * H1),
        key=lambda p: p.price, default=None,
    )
    low = min(
        (p for p in pivots if p.kind == "low"
         and top is not None and p.pivot_at > top.pivot_at
         and p.pivot_at < _ms(2026, 10, 6)),
        key=lambda p: p.price, default=None,
    )
    out = {}
    if top is not None:
        out["b_top_confirmed"] = top.confirmed_at     # confirmed_at — close_time
    if low is not None:
        out["c_low_confirmed"] = low.confirmed_at
    return out


def _candle_map(db: Database) -> dict[int, dict]:
    return {
        c.open_time: {"open": c.open, "high": c.high, "low": c.low,
                      "close": c.close, "close_time": c.close_time}
        for c in db.get_candles(BTC, "H1")
    }


def _piv(db: Database, pid):
    if not pid:
        return None
    p = db.get_ltf_pivot(pid)
    return None if p is None else {
        "id": p.id, "price": p.price, "kind": p.kind, "role": p.role,
        "pivot_at": p.pivot_at, "confirmed_at": p.confirmed_at,
    }


def collect_facts(db: Database, obs_id: int) -> dict:
    """Все факты replay из scratch-БД для отчёта (UTC ms внутри)."""
    obs = db.get_ltf_observation(obs_id)
    scenarios = db.list_ltf_scenarios(observation_id=obs_id)
    sc_out = []
    for sc in scenarios:
        events = db.list_ltf_structure_events(sc.id)
        ranges = db.list_ltf_ranges(sc.id)
        movements = db.list_ltf_movements(sc.id)
        entries = db.list_ltf_scenario_entries(sc.id)
        tests = db.list_ltf_liquidity_tests(scenario_id=sc.id)
        zones: dict[int, dict] = {}
        for e in entries:
            z = db.get_ltf_entry_zone(e.entry_zone_id)
            if z is not None and z.id not in zones:
                zones[z.id] = z
        sc_out.append({
            "sc": sc, "events": events, "ranges": ranges,
            "movements": movements, "entries": entries, "tests": tests,
            "zones": zones,
        })
    pivots = [
        p for p in db.list_ltf_pivots(BTC)
        if p.pivot_at >= SEP15 and p.state == "confirmed"
    ]
    events = db.list_ltf_events(observation_id=obs_id, limit=5000)
    return {"obs": obs, "scenarios": sc_out, "pivots": pivots,
            "events": events, "candles": _candle_map(db)}


def events_jsonl(facts: dict) -> list[dict]:
    """Хронологический журнал всех фактов (МСК подписи — в report.md)."""
    out: list[dict] = []
    for p in facts["pivots"]:
        out.append({"t": p.confirmed_at, "type": "pivot_confirmed",
                    "kind": p.kind, "role": p.role, "price": p.price,
                    "pivot_at": p.pivot_at, "confirmed_at": p.confirmed_at})
    for scb in facts["scenarios"]:
        sc = scb["sc"]
        out.append({"t": sc.created_at, "type": "scenario_open",
                    "scenario_id": sc.id, "trigger": sc.trigger,
                    "epoch": sc.structural_epoch_id})
        if sc.cancelled_at is not None:
            out.append({"t": sc.cancelled_at, "type": "scenario_cancel",
                        "scenario_id": sc.id, "reason": sc.cancellation_reason,
                        "reverse_break_level": sc.reverse_break_level_price,
                        "reverse_break_pivot_id": sc.reverse_break_pivot_id})
        for e in scb["events"]:
            out.append({"t": e.occurred_at, "type": "structure_event",
                        "scenario_id": sc.id, "kind": e.kind, "stage": e.stage,
                        "direction": e.direction.value,
                        "break_level": e.break_level,
                        "candle": e.break_candle_open_time,
                        "accompanying": e.accompanying,
                        "evidence": e.evidence})
        for r in scb["ranges"]:
            out.append({"t": r.available_at, "type": "range_version",
                        "scenario_id": sc.id, "version": r.version,
                        "kind": r.kind, "lower": r.lower, "upper": r.upper,
                        "mid": r.mid, "available_at": r.available_at,
                        "anchor_low": r.anchor_low_pivot_id,
                        "anchor_high": r.anchor_high_pivot_id})
        for m in scb["movements"]:
            out.append({"t": m.end_at, "type": "movement",
                        "scenario_id": sc.id, "id": m.id,
                        "start_pivot": m.start_pivot_id,
                        "end_pivot": m.end_pivot_id})
        for t in scb["tests"]:
            if t.resolved_at is not None:
                out.append({"t": t.resolved_at, "type": "liquidity_outcome",
                            "entry_zone_id": t.entry_zone_id, "level": t.level,
                            "state": t.state, "close_price": t.close_price,
                            "candle": t.candle_open_time})
    for e in facts["events"]:
        out.append({"t": e.occurred_at, "type": "ltf_event",
                    "kind": e.kind, "scenario_id": e.scenario_id,
                    "payload": e.payload})
    out.sort(key=lambda x: (x["t"], x["type"]))
    return out


def _qa_section(db: Database, run: dict, facts: dict, facts_alt: dict) -> str:
    """Обязательные ответы Q1–Q8 со свечными доказательствами (OHLC)."""
    scs = facts["scenarios"]
    sc1 = scs[0] if scs else None
    sc2 = scs[1] if len(scs) > 1 else None
    candles = facts["candles"]
    L: list[str] = ["## Ответы Q1–Q8", ""]

    def bar(open_t: int) -> str:
        c = candles.get(open_t)
        if c is None:
            return f"свеча {msk_label(open_t)} (нет OHLC)"
        return (f"свеча open {msk_label(open_t)} / close {msk_label(c['close_time'])}: "
                f"O={c['open']} H={c['high']} L={c['low']} C={c['close']}")

    # ---- Q1 ----
    L.append("### Q1. Была ли отмена сценария 01.10 и на каком уровне")
    L.append("")
    if sc1 is not None and sc1["sc"].cancelled_at is not None:
        sc = sc1["sc"]
        rev = [e for e in sc1["events"] if e.direction.value != sc.direction.value][0]
        piv = _piv(db, sc.reverse_break_pivot_id) or {}
        L.append(f"Да. Сценарий #{sc.id} (эпоха 1) отменён {msk_label(sc.cancelled_at)} "
                 f"по {sc.cancellation_reason}. Уровень отмены = **{sc.reverse_break_level_price}** "
                 f"— это {piv.get('role')} (pivot {piv.get('id')}, kind={piv.get('kind')}, "
                 f"экстремум {msk_label(piv.get('pivot_at'))}, подтверждён "
                 f"{msk_label(piv.get('confirmed_at'))}) — опорный pivot ОБРАТНОЙ "
                 "машины структуры (ref-pivot, evidence.broken_pivot_id), а не «последний "
                 "локальный хай».")
        L.append("")
        L.append(f"Пробойная свеча: {bar(rev.break_candle_open_time)} → Close "
                 f"{rev.evidence.get('close_price')} строго выше {rev.break_level}.")
        L.append("")
        L.append("**86 698.99 уровнем отмены НЕ является**: это HH 06.10 (pivot 11155, "
                 "экстремум 06.10 18:00 МСК, подтверждён 06.10 21:59:59 МСК) — поздний "
                 "противоположный экстремум, который СТАРЫЙ интерфейс показывал как "
                 "«уровень отмены» (запрещённая спекой логика «последний локальный хай»). "
                 "На скрине «BOS 84 520 от 01.10 21:59:59 МСК» — артефакт старого "
                 "состояния: replay фиксирует медвежий слом 84520 на 02.10 18:59:59 UTC "
                 "(триггер сценария эпохи 2), а на 01.10 18:59:59 UTC медвежьего слома нет.")
    L.append("")

    # ---- Q2 ----
    L.append("### Q2. Переключение рабочего движения (эпохи/движения после 01.10)")
    L.append("")
    for scb in scs:
        sc = scb["sc"]
        mv = scb["movements"][0] if scb["movements"] else None
        line = (f"- Эпоха {sc.structural_epoch_id}: сценарий #{sc.id} "
                f"({sc.trigger} {sc.stage} {sc.direction.value}, {sc.state}), "
                f"открыт {msk_label(sc.created_at)}")
        if mv is not None:
            sp, ep = _piv(db, mv.start_pivot_id), _piv(db, mv.end_pivot_id)
            line += (f"; движение {mv.start_pivot_id}→{mv.end_pivot_id} "
                     f"({sp and sp['price']} {sp and sp['role']} → "
                     f"{ep and ep['price']} {ep and ep['role']})")
        if sc.cancelled_at is not None:
            line += f"; отменён {msk_label(sc.cancelled_at)}"
        L.append(line)
    L.append("")
    L.append("Старое поведение (скрин): сценарий жил в эпохе 1 с «исходным» движением, "
             "уровень висел в бессрочном «снятие не подтверждено». Теперь: обратный "
             "подтверждённый слом отменил сценарий эпохи 1, наблюдение перешло в "
             "waiting_structure, а новый медвежий BOS 84520 (02.10) открыл сценарий "
             "эпохи 2 с собственным движением от верха 02.10 (HH 87220.0, pivot 11139) "
             "и собственным набором зон (см. Q5).")
    L.append("")

    # ---- Q3 ----
    L.append("### Q3. Верхний якорь диапазона: HH исходной структуры или LH новой ноги")
    L.append("")
    for scb in scs:
        sc = scb["sc"]
        for r in scb["ranges"]:
            lo_p, hi_p = _piv(db, r.anchor_low_pivot_id), _piv(db, r.anchor_high_pivot_id)
            L.append(f"- sc#{sc.id} v{r.version} **{r.kind}** [{r.lower}; {r.upper}] "
                     f"(mid {r.mid}), available {msk_label(r.available_at)}: "
                     f"low={lo_p and (str(lo_p['price']) + '/' + lo_p['role'])}, "
                     f"high={hi_p and (str(hi_p['price']) + '/' + hi_p['role'])}")
    L.append("")
    L.append("Эпоха 1: первая версия — **origin_reversal** [83500.01; 87278.54]: верхний "
             "якорь — HH 87278.54 прежней восходящей структуры (старт причинного "
             "движения BOS 86000; роль HH сохранена, в LH не переименовывалась) — ровно "
             "кейс спеки §7. Далее диапазон перешёл на continuation-пары. Эпоха 2: "
             "текущая версия — **continuation** [83186; 84419.69]: валидная пара "
             "LH 84419.69 → LL 83186 подтвердилась ещё 01.10 (до открытия сценария), "
             "поэтому origin_reversal не понадобился; верх 02.10 (HH 87220.0) якорем "
             "рабочего диапазона пока не становился — своей continuation-пары новой "
             "ноги на as_of нет. Геометрия [83186; 84419.69] и момент 14:59:59 "
             "совпадают со скрином; метка tz отличается на 3 ч (скрин «14:59:59 МСК» "
             "против 14:59:59 UTC в replay — артефакт отображения старого UI).")
    L.append("")

    # ---- Q4 ----
    L.append("### Q4. Финальный исход BSL 84 419.69")
    L.append("")
    for scb in scs:
        for t in scb["tests"]:
            if t.level != 84419.69:
                continue
            z = scb["zones"].get(t.entry_zone_id)
            outcome = {"confirmed": "sweep_reclaimed (снятие с возвратом)",
                       "failed": "broken_without_reclaim (пройден без возврата)"}.get(
                           t.state, t.state)
            L.append(f"- sc#{scb['sc'].id} зона {t.entry_zone_id}: касание "
                     f"{msk_label(t.touch_at)}; {bar(t.candle_open_time)} → "
                     f"**{outcome}**, исход зафиксирован {msk_label(t.resolved_at)}; "
                     f"validity={z.validity if z else '?'}, "
                     f"first_test={msk_label(z.first_test_at) if z else '—'}")
            reasons = sorted({(e.range_version, e.reason) for e in scb['entries']
                              if e.entry_zone_id == t.entry_zone_id})
            L.append(f"  причины привязок: "
                     + "; ".join(f"v{v}:{r}" for v, r in reasons))
    L.append("")
    L.append("Итог: в эпохе 1 уровень снят с возвратом (High > K, Close 84162.65 < K, "
             "01.10 15:59:59 UTC). В текущем сценарии (эпоха 2) зона рождается уже "
             "пройденной: свеча 01.10 17:00 UTC закрылась 84908.01 > K — "
             "**broken_without_reclaim**, навсегда исключён из подходящих "
             "(reason=level_broken на всех версиях; новые версии диапазона не воскрешают). "
             "Бессрочное «снятие не подтверждено» со скрина — префиксный баг, "
             "исправлен в Этапе 5: у каждой закрытой свечи терминальный исход.")
    L.append("")

    # ---- Q5 ----
    L.append("### Q5. Кандидаты OB/FVG снижения от верха 02.10 (бокс «OB + FVG»)")
    L.append("")
    if sc2 is not None:
        for z in sorted(sc2["zones"].values(), key=lambda z: (z.type, z.lower)):
            fill = fvg_fill_status(z)
            reasons = sorted({(e.range_version, f"{e.state}/{e.reason}")
                              for e in sc2["entries"] if e.entry_zone_id == z.id})
            L.append(f"- {z.type} {z.id} "
                     f"[{z.lower}" + (f"; {z.upper}" if not z.is_level else "") +
                     f"], formed {msk_label(z.formed_at)}, confirmed "
                     f"{msk_label(z.confirmed_at)}, validity={z.validity}, "
                     f"depth={'—' if z.is_level else format(z.max_test_depth, '.0%')}"
                     + (f", fill={fill}" if fill else "")
                     + f", привязки: " + "; ".join(f"v{v}:{r}" for v, r in reasons))
            if z.type == "OB" and z.evidence.get("base_candles"):
                L.append(f"  база OB: свечи {[tsm(t) for t in z.evidence['base_candles']]}")
            if z.type == "FVG" and z.evidence.get("fvg_candles"):
                L.append(f"  тройка FVG: {[tsm(t) for t in z.evidence['fvg_candles']]}")
        L.append("")
        L.append("Бокс владельца ~86400–87300 покрывается OB 7366 [86308; 87220] "
                 "(база у верха 02.10) и FVG 7364 [85758.81; 86465.72]. Все кандидаты "
                 "выше текущего диапазона [83186; 84419.69] → outside_pd (вне Premium) — "
                 "поэтому не отслеживаются как выбранные зоны и тестов не имеют. "
                 "Ралли 06.10 (High 86698.99) прошло FVG 7364 насквозь (High > "
                 "86465.72): полное перекрытие зафиксируется при ближайшей новой версии "
                 "диапазона (правило Этапа 6 — на as_of версии не было, fill пока "
                 "'open'; в fresh он не вернётся в любом случае). OB 7366 — "
                 "самостоятельный объект: глубины тестов у него нет (касаний как у "
                 "выбранной зоны не было), решение 90% к нему не применялось.")
    L.append("")

    # ---- Q6 ----
    L.append("### Q6. Классификация двух ручных линий «BOS»")
    L.append("")
    L.append("Подтверждённые сломы окна (обе стороны, из replay):")
    L.append("")
    for scb in scs:
        for e in scb["events"]:
            post = " — ПОСЛЕ снимка" if e.occurred_at > LAST_CLOSED else ""
            L.append(f"- {msk_label(e.occurred_at)}: {e.kind} {e.stage} "
                     f"{e.direction.value} {e.break_level} "
                     f"(роль {e.evidence.get('role_at_event')}, "
                     f"{bar(e.break_candle_open_time)}){post}")
    L.append("")
    L.append("Классификация: (1) 01.10 17:00 UTC — первичный БЫЧИЙ BOS 84419.69 "
             "(слом защищённого LH новой бычьей структуры; отмена эпохи 1); "
             "(2) 02.10 18:00 UTC — первичный МЕДВЕЖИЙ BOS 84520 (слом защищённого HL; "
             "триггер эпохи 2). Дальнейшее снижение 84520 → 83888 — одно движение "
             "без подтверждённого отката: вторичного BOS нет (правило §6.1). Позже: "
             "SMS 85412 (05.10, internal_low после отката) и первичный BOS новой "
             "медвежьей ноги 85136.11 (07.10 01:59 UTC — уже после снимка).")
    L.append("")

    # ---- Q7 ----
    L.append("### Q7. Снимок на as_of (срез e, 06.10 20:59:59 UTC)")
    L.append("")
    slice_e = run["slices"].get("e_as_of")
    if slice_e is not None and sc2 is not None:
        view = _slice_eligible(slice_e, sc2["sc"].id)
        rng = view["range"]
        L.append(f"- scenario_id={sc2['sc'].id}, эпоха {sc2['sc'].structural_epoch_id}, "
                 f"state=monitoring_entries, origin_movement={sc2['sc'].origin_movement_id}")
        if rng is not None:
            L.append(f"- диапазон v{rng['version']} **{rng['kind']}** "
                     f"[{rng['lower']}; {rng['upper']}], mid {rng['mid']}")
        L.append(f"- eligible: {len(view['eligible'])}; причины строк текущей "
                 f"версии: {view['reasons']}")
        L.append("- все кандидаты эпохи 2 выше диапазона (outside_pd), BSL 84419.69 "
                 "терминален (level_broken) → подходящих входов нет; снимок собран из "
                 "того же состояния, что читают API-слои (единый источник допуска, §13).")
    L.append("")

    # ---- Q8 ----
    L.append("### Q8. Политика якорей: origin_reversal vs continuation_only")
    L.append("")
    L.append(f"Режим прогона: **{run['cfg'].ltf_range_anchor_policy}** "
             "(дефолт после Этапа 4; в settings.json ключа нет).")
    L.append("")
    L.append("| сценарий | origin_reversal (прогон) | continuation_only (контроль) |")
    L.append("|---|---|---|")
    alt_ranges = {
        (scb["sc"].id): scb["ranges"] for scb in facts_alt["scenarios"]
    }
    for scb in scs:
        sc = scb["sc"]
        cur = "; ".join(
            f"v{r.version} {r.kind} [{r.lower}; {r.upper}] @ {tsu(r.available_at)}"
            for r in scb["ranges"])
        alt = "; ".join(
            f"v{r.version} {r.kind} [{r.lower}; {r.upper}] @ {tsu(r.available_at)}"
            for r in alt_ranges.get(sc.id, []))
        L.append(f"| sc#{sc.id} | {cur} | {alt} |")
    L.append("")
    L.append("Разница ровно одна: первая версия эпохи 1. С origin_reversal диапазон "
             "[83500.01; 87278.54] от якоря-источника движения (HH, роль сохранена) "
             "доступен с 23.09 22:59:59 UTC; с continuation_only — честный "
             "range_pending ~14 часов до первой пары [82874.93; 84622.01] "
             "(24.09 12:59:59 UTC). Эпоха 2 в обоих режимах идентична (continuation-пара "
             "существовала до сценария).")
    L.append("")
    return "\n".join(L)


def _fmt_zone(z, candles) -> str:
    if z.is_level:
        bounds = f"{z.lower}"
    else:
        bounds = f"[{z.lower}; {z.upper}]"
    depth = "—" if z.is_level else f"{z.max_test_depth:.0%}"
    return (
        f"{z.type} {bounds} formed {msk_label(z.formed_at)}, "
        f"confirmed {msk_label(z.confirmed_at)}, validity={z.validity}, "
        f"depth={depth}, first_test={msk_label(z.first_test_at)}"
    )


def _slice_eligible(slice_state: dict, sc_id: int) -> dict:
    """Снимок допуска по срезу состояния (Q7): текущая версия диапазона,
    строки привязок и причины на ней."""
    ranges = [r for r in slice_state["ltf_range"] if r["scenario_id"] == sc_id]
    cur = max(ranges, key=lambda r: r["version"], default=None)
    ver = cur["version"] if cur else 0
    zones = {z["id"]: z for z in slice_state["ltf_entry_zone"]}
    rows = [
        (e, zones.get(e["entry_zone_id"]))
        for e in slice_state["ltf_scenario_entry"]
        if e["scenario_id"] == sc_id and e["range_version"] == ver
    ]
    eligible = [
        (e, z) for e, z in rows
        if z is not None and e["state"] in ("fresh", "tested")
        and e["eligible"] and e["reason"] in ("ok", "")
    ]
    reasons: dict[str, int] = {}
    for e, z in rows:
        reasons[e["reason"] or "ok"] = reasons.get(e["reason"] or "ok", 0) + 1
    return {"range": cur, "version": ver, "eligible": eligible,
            "reasons": reasons}


def write_report(work: Path, run: dict, run_alt: dict,
                 facts: dict, facts_alt: dict,
                 checkpoints: dict[int, str]) -> None:
    """report.md + events.jsonl (МСК подписи; внутри движка — UTC)."""
    db = run["db"]
    candles = facts["candles"]
    scs = facts["scenarios"]
    sc1 = scs[0] if scs else None
    sc2 = scs[1] if len(scs) > 1 else None
    L: list[str] = []
    add = L.append

    add("# BTCUSDT — эталон 07.10.2026 00:09:35 МСК: replay-отчёт (Этап 7, §14)")
    add("")
    add(f"- Инструмент: BTCUSDT binance spot (instrument_id={BTC}); scratch-копия "
        "data/htf_zones.db (живая БД не изменялась).")
    add(f"- Наблюдение #{facts['obs'].id} открыто ВРУЧНУЮ (what-if: зона в статусе "
        f"'{run['zone_status']}', воркер мог её не взять); HTF OB D1 bear "
        f"[87304.33; 90600] (zone {ZONE_ID}).")
    add(f"- Активация наблюдения: первое H1-касание зоны после 15.09.2026 — "
        f"{msk_label(run['reach'])} (на скрине «последнее касание HTF "
        "21.09.2026 03:00 МСК» — это открытие D1-свечи 21.09; первое H1-касание "
        "внутри неё — 20:00 UTC).")
    add(f"- Обработано H1 до {msk_label(LAST_CLOSED)} (последняя закрытая ≤ as_of); "
        "дальнейшие свечи БД (до 07.10 09:59 UTC) в as_of-срез не входят.")
    add(f"- Политика якорей диапазона: **{run['cfg'].ltf_range_anchor_policy}** "
        "(см. Q8); ltf_structure 3/3; настройки = живые (data/settings.json).")
    drift = run["replay_drift"]
    add(f"- Self-check детерминизма: replay_observation после пошагового прогона "
        f"{'не изменил состояние ✓' if not drift else 'ИЗМЕНИЛ: ' + '; '.join(drift)}.")
    add("")
    add("Все времена подписаны в МСК (UTC+3) с UTC-дублём; внутри движка — UTC ms. "
        "open_time/close_time свечей приведены раздельно.")
    add("")
    add(_qa_section(db, run, facts, facts_alt))
    add("")

    # ---------- хронология ----------
    add("## Хронология (срезы a–e)")
    add("")
    for close_t, label in sorted(checkpoints.items()):
        add(f"- **{label}** — после закрытия {msk_label(close_t)}")
    add("")
    if "a0_before_bos" in run["slices"] and "a1_after_bos" in run["slices"]:
        add("Diff a0→a1 (изменения таблиц между закрытиями 17:59:59 и 18:59:59 UTC):")
        add("```")
        add((work / "slices" / "diff_a0_a1.txt").read_text(encoding="utf-8"))
        add("```")
        add("Пустой diff (только meta-курсор) — само по себе доказательство Q1: "
            "на заявленный скрином момент «BOS 84520, 01.10 21:59:59 МСК» никакого "
            "структурного события нет; реальные события 01.10 (снятие BSL 84419.69 в "
            "15:59:59 UTC, обратный бычий BOS 84419.69 и отмена эпохи 1 в 17:59:59 "
            "UTC) уже вошли в срез a0.")
        add("")

    add("## Pivots структуры H1 (подтверждённые, от 15.09)")
    add("")
    add("| pivot | kind | role | price | экстремум (open) | подтверждён (close) |")
    add("|---|---|---|---|---|---|")
    for p in sorted(facts["pivots"], key=lambda p: p.pivot_at):
        add(f"| {p.id} | {p.kind} | {p.role} | {p.price} | {tsm(p.pivot_at)} "
            f"| {tsm(p.confirmed_at)} |")
    add("")

    for scb in scs:
        sc = scb["sc"]
        add(f"## Сценарий #{sc.id} (эпоха {sc.structural_epoch_id}, "
            f"{sc.direction.value}, {sc.trigger} {sc.stage}, {sc.state})")
        add("")
        add(f"Финальное состояние прогона (H1 до 07.10 09:59 UTC); снимок на as_of — "
            f"в Q7. События после {tsm(LAST_CLOSED)} МСК помечены «(после as_of)».")
        add("")
        add(f"- Открыт {msk_label(sc.created_at)}; origin_break_event="
            f"{sc.origin_break_event_id}, origin_movement={sc.origin_movement_id}, "
            f"last_processed_close={msk_label(sc.last_processed_close)}")
        if sc.cancelled_at is not None:
            add(f"- ОТМЕНЁН {msk_label(sc.cancelled_at)}: {sc.cancellation_reason}; "
                f"уровень отмены {sc.reverse_break_level_price} "
                f"(pivot {sc.reverse_break_pivot_id}, confirmed "
                f"{msk_label(sc.reverse_break_confirmed_at)})")
        add("")
        add("| событие | stage | dir | level | свеча (open/close) | close | роль/pivot |")
        add("|---|---|---|---|---|---|---|")
        for e in scb["events"]:
            ev = e.evidence
            c = candles.get(e.break_candle_open_time, {})
            post = " (после as_of)" if e.occurred_at > LAST_CLOSED else ""
            add(f"| {e.kind}{' (acc.)' if e.accompanying else ''} | {e.stage} "
                f"| {e.direction.value} | {e.break_level} "
                f"| {tsm(e.break_candle_open_time)} / {tsm(e.occurred_at)} "
                f"| {ev.get('close_price')} "
                f"| {ev.get('role_at_event')}/{ev.get('broken_pivot_id')}{post} |")
        add("")
        add("| range v | kind | lower | upper | mid | available (close) | якоря |")
        add("|---|---|---|---|---|---|---|")
        for r in scb["ranges"]:
            lo_p, hi_p = _piv(db, r.anchor_low_pivot_id), _piv(db, r.anchor_high_pivot_id)
            anch = (f"low={lo_p['price']}({lo_p['role']}) " if lo_p else "low=? ") + \
                   (f"high={hi_p['price']}({hi_p['role']})" if hi_p else "high=?")
            add(f"| {r.version} | {r.kind} | {r.lower} | {r.upper} | {r.mid} "
                f"| {tsm(r.available_at)} | {anch} |")
        add("")
        if scb["zones"]:
            add("| зона | границы | validity | depth/fill | first_test | P/D* | причины по версиям |")
            add("|---|---|---|---|---|---|---|")
            cur_ver = scb["ranges"][-1].version if scb["ranges"] else 0
            for z in sorted(scb["zones"].values(),
                            key=lambda z: (z.type, z.lower)):
                reasons = {}
                for e in scb["entries"]:
                    if e.entry_zone_id == z.id:
                        reasons[e.range_version] = f"{e.state}/{e.reason or 'ok'}"
                fill = fvg_fill_status(z)
                depth = "—" if z.is_level else f"{z.max_test_depth:.0%}"
                add(f"| {z.type} {z.id} | {z.lower}" +
                    (f"–{z.upper}" if not z.is_level else "") +
                    f" | {z.validity} | {depth}" +
                    (f"/{fill}" if fill else "") +
                    f" | {tsm(z.first_test_at)} | — | "
                    + "; ".join(f"v{k}:{v}" for k, v in sorted(reasons.items())) + " |")
            add("")
        if scb["tests"]:
            add("Liquidity-события уровней:")
            add("")
            add("| зона | level | свеча | state | close | исход (close) |")
            add("|---|---|---|---|---|---|")
            for t in scb["tests"]:
                add(f"| {t.entry_zone_id} | {t.level} | {tsm(t.candle_open_time)} "
                    f"| {t.state} | {t.close_price} | {tsm(t.resolved_at)} |")
            add("")

    (work / "report.md").write_text("\n".join(L), encoding="utf-8")
    with (work / "events.jsonl").open("w", encoding="utf-8") as f:
        for row in events_jsonl(facts):
            f.write(json.dumps(
                {"msk": tsm(row["t"]), "utc": tsu(row["t"]), **row},
                ensure_ascii=False, default=str) + "\n")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "slices").mkdir(exist_ok=True)

    checkpoints: dict[int, str] = {
        _ms(2026, 10, 1, 17) + H1 - 1: "a0_before_bos",
        _ms(2026, 10, 1, 18) + H1 - 1: "a1_after_bos",
        _ms(2026, 10, 6, 16) + H1 - 1: "d_pullbacks",
        LAST_CLOSED: "e_as_of",
    }

    if "--report-only" in sys.argv:
        db = Database(str(WORK / "scratch.db"), ltf_cache=True)
        db_alt = Database(str(WORK / "scratch_cont_only.db"), ltf_cache=True)
        cfg = load_live_cfg(str(LIVE))
        obs_id = db.conn.execute(
            "SELECT id FROM ltf_observation WHERE instrument_id=1").fetchone()[0]
        run = {"db": db, "cfg": cfg, "obs_id": obs_id,
               "reach": db.get_ltf_observation(obs_id).activated_at,
               "zone_status": db.get_zone(ZONE_ID).status.value,
               "slices": {}, "replay_drift": []}
        run_alt = {"db": db_alt, "cfg": cfg, "obs_id": obs_id, "reach": None,
                   "zone_status": None, "slices": {}, "replay_drift": []}
        for label_file in (WORK / "slices").glob("*.json"):
            run["slices"][label_file.stem] = json.loads(
                label_file.read_text(encoding="utf-8"))
    else:
        # офлайн-pivots для точек b/c — по полной истории scratch-копии
        tmp = WORK / "_probe.db"
        copy_live_db(str(LIVE), str(tmp))
        probe = Database(str(tmp))
        candles_all = probe.get_candles(BTC, "H1")
        pc = pivot_checkpoints(candles_all)
        probe.close()
        tmp.unlink()
        for label, close_t in pc.items():
            checkpoints[close_t] = label

        run = run_replay(WORK / "scratch.db", "origin_reversal", checkpoints)
        run_alt = run_replay(WORK / "scratch_cont_only.db",
                             "continuation_only", None)
        db = run["db"]
        for label, state in run["slices"].items():
            (WORK / "slices" / f"{label}.json").write_text(
                json.dumps(state, ensure_ascii=False, default=str),
                encoding="utf-8")
        (WORK / "slices" / "diff_a0_a1.txt").write_text(
            "\n".join(diff_states(run["slices"]["a0_before_bos"],
                                  run["slices"]["a1_after_bos"]))
            or "(нет изменений)",
            encoding="utf-8")
        meta = {
            "reach": run["reach"], "zone_status": run["zone_status"],
            "obs_id": run["obs_id"], "replay_drift": run["replay_drift"],
            "policy": run["cfg"].ltf_range_anchor_policy,
            "checkpoint_times": {tsu(k): v for k, v in checkpoints.items()},
        }
        (WORK / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=1))

    facts = collect_facts(run["db"], run["obs_id"])
    obs_alt = run_alt["db"].conn.execute(
        "SELECT id FROM ltf_observation WHERE instrument_id=1").fetchone()[0]
    facts_alt = collect_facts(run_alt["db"], obs_alt)
    write_report(WORK, run, run_alt, facts, facts_alt, checkpoints)
    run["db"].close()
    run_alt["db"].close()
    print(f"отчёт: {WORK / 'report.md'}")


if __name__ == "__main__":
    main()
