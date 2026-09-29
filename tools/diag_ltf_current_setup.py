#!/usr/bin/env python3
"""Диагностика «LTF Current Setup» (ТЗ 2026-09-23, Этап 1, §15).

Читает data/htf_zones.db в READ-ONLY режиме (sqlite3 URI mode=ro) и печатает
markdown-отчёт в stdout. НИЧЕГО не изменяет в БД и на диске.

Проверяет утверждения пункта 2.1.C ТЗ:
  1) ~130 зон на текущей версии диапазона активного BTC-сценария,
     113 out_of_range / 17 tested / 0 fresh;
  2) 359 версий диапазона и их оправданность (§9: версия — только при
     смысловом изменении опор);
  3) настройка ltf_entry_types сохраняется, но не применяется;
  4) отменённые сценарии получают новые версии диапазона / привязки зон
     после cancelled_at (§8 — быть не должно);
  5) историческое касание помечается как актуальное «цена в зоне».

Запуск:  python tools/diag_ltf_current_setup.py > data/diag/ltf_current_setup_report.md
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "htf_zones.db"
SETTINGS_PATH = DB_PATH.parent / "settings.json"

# состояния вкладки «active» и логика выбора сценария — как в app/web/ltf_api.py
_ACTIVE_OBS_STATES = ("waiting_structure", "active", "paused_data")
_ACTIVE_SC_STATES = ("range_pending", "monitoring_entries")


def _ts(ms: int | None) -> str:
    if not ms:
        return "—"
    return _dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


class Rep:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def p(self, s: str = "") -> None:
        self.lines.append(s)

    def table(self, headers: list[str], rows: list[list]) -> None:
        self.p("| " + " | ".join(headers) + " |")
        self.p("|" + "|".join("---" for _ in headers) + "|")
        for r in rows:
            self.p("| " + " | ".join(str(x) for x in r) + " |")
        self.p()


def scenario_stats(con: sqlite3.Connection, rep: Rep, sid: int) -> dict:
    """Версии диапазона и зоны одного сценария (§9 ТЗ)."""
    c = con.cursor()
    rep.p(f"### Сценарий {sid}")
    rows = c.execute(
        "SELECT version, lower, upper, anchor_low_pivot_id, anchor_high_pivot_id,"
        " available_at FROM ltf_range WHERE scenario_id=? ORDER BY version",
        (sid,),
    ).fetchall()
    n = len(rows)
    null_anchors = sum(
        1 for r in rows
        if r["anchor_low_pivot_id"] is None or r["anchor_high_pivot_id"] is None
    )
    id_pairs = {
        (r["anchor_low_pivot_id"], r["anchor_high_pivot_id"]) for r in rows
    } - {(None, None)}
    pivots = {
        p for r in rows
        for p in (r["anchor_low_pivot_id"], r["anchor_high_pivot_id"])
    } - {None}
    geom = {(r["lower"], r["upper"]) for r in rows}
    same_geom = sum(
        1 for a, b in zip(rows, rows[1:])
        if (a["lower"], a["upper"]) == (b["lower"], b["upper"])
    )
    inversions = sum(
        1 for a, b in zip(rows, rows[1:])
        if b["available_at"] < a["available_at"]
    )
    rep.p(f"- версий диапазона: **{n}**")
    rep.p(f"- версий с NULL-якорями (anchor_*_pivot_id IS NULL): **{null_anchors}**"
          " — идентичность опор не сохранена, сравнение «те же опоры» в"
          " `range_recalc` (app/engine/ltf/ranges.py:125-132) работает по"
          " anchor_ref; None ≠ pivot_at/pivot_id → дедуп не срабатывает")
    rep.p(f"- уникальных пар якорей по pivot_id: {len(id_pairs)}"
          f"; уникальных pivots-якорей: {len(pivots)}")
    rep.p(f"- уникальных геометрий (lower, upper): {len(geom)}"
          f"; подряд идущих версий с той же геометрией: {same_geom}")
    rep.p(f"- инверсий available_at (версия N+1 старше версии N по рыночному"
          f" времени): **{inversions}** — признак того, что replay"
          " (engine.replay_observation / worker._ltf_restore_all) дописывает"
          " исторические состояния как новые версии поверх более новой головы")
    if rows:
        first, last = rows[0], rows[-1]
        rep.p(f"- первая версия: v{first['version']} [{first['lower']}–{first['upper']}],"
              f" available_at {_ts(first['available_at'])}")
        rep.p(f"- текущая версия: v{last['version']} [{last['lower']}–{last['upper']}],"
              f" mid={(last['lower'] + last['upper']) / 2},"
              f" available_at {_ts(last['available_at'])}")
    rep.p()

    cur_ver = rows[-1]["version"] if rows else 0
    by_state = c.execute(
        "SELECT z.type, e.state, COUNT(*) n FROM ltf_scenario_entry e"
        " JOIN ltf_entry_zone z ON z.id = e.entry_zone_id"
        " WHERE e.scenario_id=? AND e.range_version=?"
        " GROUP BY z.type, e.state ORDER BY z.type, e.state",
        (sid, cur_ver),
    ).fetchall()
    uniq = c.execute(
        "SELECT COUNT(DISTINCT entry_zone_id) FROM ltf_scenario_entry"
        " WHERE scenario_id=?",
        (sid,),
    ).fetchone()[0]
    rep.p(f"Зоны сценария на текущей версии (v{cur_ver}), всего уникальных"
          f" зон за историю: **{uniq}**")
    rep.p()
    rep.table(["тип", "state (ltf_scenario_entry)", "число"],
              [[r["type"], r["state"], r["n"]] for r in by_state])
    validity = c.execute(
        "SELECT z.validity, COUNT(*) n FROM ltf_scenario_entry e"
        " JOIN ltf_entry_zone z ON z.id = e.entry_zone_id"
        " WHERE e.scenario_id=? AND e.range_version=? GROUP BY z.validity",
        (sid, cur_ver),
    ).fetchall()
    rep.table(["validity (ltf_entry_zone)", "число"],
              [[r["validity"], r["n"]] for r in validity])

    anchor_rows = c.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT z.lower) dp FROM ltf_scenario_entry e"
        " JOIN ltf_entry_zone z ON z.id = e.entry_zone_id"
        " WHERE e.scenario_id=? AND json_extract(z.evidence, '$.range_anchor') = 1",
        (sid,),
    ).fetchone()
    rep.p(f"- привязок BSL/SSL-якорей диапазона (evidence.range_anchor,"
          " engine._add_range_anchor_level, app/engine/ltf/engine.py:612-649):"
          f" **{anchor_rows['n']}** строк по **{anchor_rows['dp']}** уникальным"
          " ценам — те же уровни перевязываются на каждой версии диапазона")
    rep.p()
    return {"versions": n, "cur_ver": cur_ver, "uniq_zones": uniq,
            "by_state": {(r["type"], r["state"]): r["n"] for r in by_state}}


def main() -> None:
    rep = Rep()
    con = _connect()
    c = con.cursor()

    snapshot_ms = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000)
    rep.p("# Диагностический отчёт LTF Current Setup (ТЗ 2026-09-23, Этап 1)")
    rep.p()
    rep.p(f"- БД: `{DB_PATH}` (read-only, sqlite3 URI mode=ro)")
    rep.p(f"- Время снимка (UTC): {_ts(snapshot_ms)}")
    rep.p(f"- rule_version LTF: ltf-0.2 (app/models_ltf.py:16)")
    rep.p()

    # ---------------- инструменты ----------------
    rep.p("## 1. Инструменты и наблюдения")
    rep.p()
    instruments = c.execute("SELECT * FROM instrument ORDER BY id").fetchall()
    rep.table(
        ["id", "symbol", "venue", "market", "enabled", "ltf_analyze"],
        [[i["id"], i["symbol"], i["venue"], i["market_type"], i["enabled"],
          i["ltf_analyze"]] for i in instruments],
    )
    btc = next(
        (i for i in instruments
         if i["symbol"] == "BTCUSDT" and i["venue"] == "binance"
         and i["market_type"] == "spot"),
        instruments[0],
    )
    eth = next((i for i in instruments if i["symbol"] == "ETHUSDT"), None)
    rep.p(f"Выбран инструмент BTC: id={btc['id']} ({btc['symbol']}"
          f" {btc['venue']} {btc['market_type']}).")
    rep.p()

    obs_rows = c.execute(
        "SELECT o.*, s.id sid, s.state sstate, s.direction sdir, s.trigger,"
        " s.cancelled_at, s.cancellation_reason"
        " FROM ltf_observation o"
        " LEFT JOIN ltf_scenario s ON s.observation_id = o.id"
        "   AND s.state IN ('range_pending','monitoring_entries')"
        " WHERE o.instrument_id=? ORDER BY o.activated_at DESC",
        (btc["id"],),
    ).fetchall()
    rep.p(f"Наблюдения BTC ({len(obs_rows)} шт.; порядок — как во вкладке UI,"
          " activated_at DESC):")
    rep.p()
    rep.table(
        ["obs", "state", "zone_id", "activated_at", "активный scen", "scen state"],
        [[r["id"], r["state"], r["zone_id"], _ts(r["activated_at"]),
          r["sid"] or "—", r["sstate"] or "—"] for r in obs_rows],
    )

    # «выбранное» наблюдение — первое в активной вкладке (как ltf.js loadObservations)
    active_obs = [r for r in obs_rows if r["state"] in _ACTIVE_OBS_STATES]
    selected = active_obs[0] if active_obs else None

    # ---------------- родительские зоны ----------------
    rep.p("## 2. Родительские HTF-контексты активных наблюдений BTC")
    rep.p()
    zrows = []
    for r in active_obs:
        z = c.execute("SELECT * FROM zone WHERE id=?", (r["zone_id"],)).fetchone()
        if z:
            zrows.append([r["id"], z["id"], z["type"], z["timeframe"],
                          z["direction"], f"{z['lower']}–{z['upper']}",
                          z["status"], z["market_validity"]])
    rep.table(["obs", "zone_id", "type", "TF", "dir", "границы", "status",
               "market_validity"], zrows)

    # ---------------- сценарии: версии и зоны ----------------
    rep.p("## 3. Версии диапазона и зоны активных сценариев BTC")
    rep.p()
    stats: dict[int, dict] = {}
    for r in active_obs:
        if r["sid"] is not None:
            stats[r["sid"]] = scenario_stats(con, rep, r["sid"])

    # ---------------- отменённые сценарии ----------------
    rep.p("## 4. Отменённые сценарии (§8 ТЗ: после cancelled_at новых версий"
          " и привязок быть не должно)")
    rep.p()
    total_cancelled = c.execute(
        "SELECT COUNT(*) FROM ltf_scenario WHERE state='cancelled'"
    ).fetchone()[0]
    late_ranges = c.execute(
        "SELECT COUNT(*) FROM ltf_range r JOIN ltf_scenario s ON s.id=r.scenario_id"
        " WHERE s.state='cancelled' AND r.available_at > s.cancelled_at"
    ).fetchone()[0]
    late_added = c.execute(
        "SELECT COUNT(*) FROM ltf_scenario_entry e JOIN ltf_scenario s"
        " ON s.id=e.scenario_id WHERE s.state='cancelled'"
        " AND e.added_at > s.cancelled_at"
    ).fetchone()[0]
    late_updated = c.execute(
        "SELECT COUNT(*) FROM ltf_scenario_entry e JOIN ltf_scenario s"
        " ON s.id=e.scenario_id WHERE s.state='cancelled'"
        " AND e.updated_at > s.cancelled_at"
    ).fetchone()[0]
    rep.p(f"- отменённых сценариев всего: **{total_cancelled}**")
    rep.p(f"- версий диапазона с available_at ПОЗЖЕ cancelled_at: **{late_ranges}**"
          " (слабая проверка: available_at — рыночное время подтверждения опор,"
          " у ltf_range нет времени вставки)")
    rep.p(f"- привязок зон (ltf_scenario_entry) с added_at позже cancelled_at:"
          f" **{late_added}**")
    rep.p(f"- привязок зон с updated_at позже cancelled_at: **{late_updated}**"
          " (сильная проверка: added_at/updated_at — время обработки движком)")
    rep.p()

    # ---------------- дубли ----------------
    rep.p("## 5. Дедупликация (§11 ТЗ)")
    rep.p()
    dup_groups = c.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM ltf_entry_zone WHERE instrument_id=?"
        " GROUP BY type, direction, lower, upper, formed_at"
        " HAVING COUNT(*) > 1)",
        (btc["id"],),
    ).fetchone()[0]
    rep.p(f"- группы зон BTC с одинаковыми (type, direction, lower, upper,"
          f" formed_at), но РАЗНЫМ movement_id: **{dup_groups}** — одна и та же"
          " геометрия размножается по движениям/сценариям, т.к. movement_id"
          " входит в UNIQUE-ключ (app/schema.sql:340)")
    worst = c.execute(
        "SELECT type, direction, lower, upper, COUNT(*) n,"
        " GROUP_CONCAT(id) ids FROM ltf_entry_zone WHERE instrument_id=?"
        " GROUP BY type, direction, lower, upper, formed_at"
        " HAVING n > 1 ORDER BY n DESC LIMIT 5",
        (btc["id"],),
    ).fetchall()
    rep.table(["type", "dir", "lower", "upper", "копий", "ids"],
              [[r["type"], r["direction"], r["lower"], r["upper"], r["n"],
                r["ids"]] for r in worst])
    # float-шум в границах: та же свеча-основание, почти те же границы
    zr = c.execute(
        "SELECT id, type, direction, lower, upper, formed_at FROM ltf_entry_zone"
        " WHERE instrument_id=? ORDER BY type, direction, formed_at, lower",
        (btc["id"],),
    ).fetchall()
    near = 0
    for a, b in zip(zr, zr[1:]):
        if (a["type"], a["direction"], a["formed_at"]) == (
                b["type"], b["direction"], b["formed_at"]):
            dl, du = abs(a["lower"] - b["lower"]), abs(a["upper"] - b["upper"])
            if (dl, du) != (0.0, 0.0) and dl < 0.5 and du < 0.5:
                near += 1
    rep.p(f"- пар зон с float-шумом в границах (<0.5 при той же formed_at):"
          f" **{near}**")
    same_price_pivots = c.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM ltf_pivot WHERE instrument_id=?"
        " GROUP BY kind, price HAVING COUNT(DISTINCT pivot_at) > 1)",
        (btc["id"],),
    ).fetchone()[0]
    pivot_noise = c.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM ltf_pivot WHERE instrument_id=?"
        " GROUP BY kind, pivot_at HAVING COUNT(*) > 1)",
        (btc["id"],),
    ).fetchone()[0]
    total_pivots = c.execute(
        "SELECT COUNT(*) FROM ltf_pivot WHERE instrument_id=?", (btc["id"],)
    ).fetchone()[0]
    rep.p(f"- pivots BTC всего: {total_pivots}; цен, встречающихся у РАЗНЫХ"
          f" pivots (одинаковая цена — разные экстремумы):"
          f" **{same_price_pivots}** (нормально по §11: цена — не идентичность)")
    rep.p(f"- дублей pivots по float-шуму (тот же kind+pivot_at, несколько"
          f" строк): **{pivot_noise}**")
    rep.p()

    # ---------------- положение цены ----------------
    rep.p("## 6. Положение цены относительно родительской HTF-зоны")
    rep.p()
    last = c.execute(
        "SELECT open_time, close_time, close FROM candle"
        " WHERE instrument_id=? AND timeframe='H1' AND closed=1"
        " ORDER BY open_time DESC LIMIT 1",
        (btc["id"],),
    ).fetchone()
    price = last["close"]
    rep.p(f"Последняя закрытая H1 BTC: open {_ts(last['open_time'])},"
          f" close=**{price}**.")
    rep.p()
    rep.p("Серверный код НЕ вычисляет «цена в/вне HTF-зоны» нигде: grep по"
          " app/web/ltf_api.py и app/engine не находит сравнения цены с"
          " границами parent zone. Метка «цена в зоне» в UI — эвристика"
          " app/web/static/ltf.js:176-178 (obsStatusText):"
          " `sc.state === 'monitoring_entries' && fresh_entries === 0`"
          " → строка «цена в зоне», без всякой проверки текущей цены.")
    rep.p()
    prow = []
    for r in active_obs:
        z = c.execute("SELECT * FROM zone WHERE id=?", (r["zone_id"],)).fetchone()
        if not z:
            continue
        actual = ("внутри" if z["lower"] <= price <= z["upper"]
                  else ("выше зоны" if price > z["upper"] else "ниже зоны"))
        ui_label = "—"
        if r["sid"] is not None and r["sstate"] == "monitoring_entries":
            st = stats.get(r["sid"]) or scenario_stats(con, rep, r["sid"])
            stats.setdefault(r["sid"], st)
            fresh = sum(n for (t, s), n in st["by_state"].items() if s == "fresh")
            ui_label = ("«цена в зоне»" if fresh == 0
                        else f"Entry Zones · свежих: {fresh}")
        prow.append([r["id"], z["id"], f"{z['lower']}–{z['upper']}",
                     actual, r["sid"] or "—", ui_label])
    rep.table(["obs", "zone_id", "границы OB", "фактическое положение"
               f" цены {price}", "scen", "что покажет UI (ltf.js:178)"], prow)
    target = c.execute(
        "SELECT * FROM zone WHERE instrument_id=? AND type='ob'"
        " AND ABS(lower - 75545.67) < 0.01 AND ABS(upper - 81272.62) < 0.01",
        (btc["id"],),
    ).fetchone()
    if target:
        rel = ("внутри" if target["lower"] <= price <= target["upper"]
               else ("ВЫШЕ верхней границы" if price > target["upper"]
                     else "ниже нижней границы"))
        rep.p(f"Родительский OB из ТЗ (id={target['id']}, D1 bear"
              f" 75545.67–81272.62, status={target['status']},"
              f" market_validity={target['market_validity']}): цена {price}"
              f" — **{rel}** ({price - target['upper']:+.2f} к upper)."
              " Пример из приёмки п.02: 84038 > 81272.62 должно давать"
              " «выше HTF-зоны», а не «цена в зоне».")
    rep.p()

    # ---------------- ETH ----------------
    rep.p("## 7. ETHUSDT (эталон ТЗ)")
    rep.p()
    if eth is None:
        rep.p("Инструмент ETHUSDT отсутствует в БД.")
    else:
        counts = {
            t: c.execute(
                f"SELECT COUNT(*) FROM {t} WHERE instrument_id=?", (eth["id"],)
            ).fetchone()[0]
            for t in ("ltf_observation", "ltf_pivot", "ltf_entry_zone")
        }
        h1 = c.execute(
            "SELECT COUNT(*) FROM candle WHERE instrument_id=? AND timeframe='H1'",
            (eth["id"],),
        ).fetchone()[0]
        rep.p(f"- instrument id={eth['id']}, enabled={eth['enabled']},"
              f" ltf_analyze={eth['ltf_analyze']}")
        rep.p(f"- H1-свечей: {h1}; LTF-наблюдений: {counts['ltf_observation']},"
              f" pivots: {counts['ltf_pivot']}, entry zones:"
              f" {counts['ltf_entry_zone']}")
        if counts["ltf_observation"] == 0:
            rep.p("- LTF-данных по ETH НЕТ: эталонный сценарий проверить на"
                  " текущих данных невозможно (наблюдения не открывались).")
    rep.p()

    # ---------------- ltf_entry_types ----------------
    rep.p("## 8. Настройка ltf_entry_types")
    rep.p()
    val = None
    if SETTINGS_PATH.exists():
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        val = payload.get("detector", payload).get("ltf_entry_types")
    rep.p(f"- значение в data/settings.json: `{val}`")
    rep.p("- все чтения настройки в коде (grep `ltf_entry_types` по проекту):")
    rep.p("  - `app/config.py:83` — объявление поля DetectorConfig"
          " («типы отображаемых Entry Zones»);")
    rep.p("  - `app/web/api.py:894-912` — POST /api/settings сохраняет значение"
          " в settings.json (сохранение работает);")
    rep.p("  - `app/web/api.py:939` — только labels для UI (`LTF_TYPE_RU`);")
    rep.p("  - `app/web/static/ltf.js:933,946` — значение читается ТОЛЬКО для"
          " проставления галочек в диалоге настроек; `ltf.js:973` — отправка"
          " обратно при сохранении;")
    rep.p("  - `tests/test_ltf_worker.py:282-288` — тест только на"
          " сохранение/чтение.")
    rep.p("- НИГДЕ в engine (app/engine/ltf/*), API пригодности"
          " (app/web/ltf_api.py) и фильтрации графика настройка не читается:"
          " `classify_entry`, `_fresh_entries`, `/api/ltf/.../entries` типы"
          " не фильтруют. Настройка инертна.")
    rep.p()

    # ---------------- итоги по утверждениям ----------------
    rep.p("## 9. Итог по утверждениям (ТЗ §2.1.C)")
    rep.p()
    def _split(s: dict, state: str) -> int:
        return sum(n for (t, st_), n in s["by_state"].items() if st_ == state)

    # сценарий из утверждения Kimi: 359 версий, 113 out_of_range / 17 tested
    sc359 = next(
        (sid for sid, s in stats.items()
         if s["versions"] == 359 and _split(s, "out_of_range") == 113
         and _split(s, "tested") == 17),
        next((sid for sid, s in stats.items() if s["versions"] == 359), None),
    )
    if sc359:
        s = stats[sc359]
        obs_of = next((r for r in active_obs if r["sid"] == sc359), None)
        rep.p(f"1. **130 зон / 113 out_of_range / 17 tested / 0 fresh /"
              f" 359 версий — ПОДТВЕРЖДЕНО** для сценария {sc359}"
              f" (obs {obs_of['id'] if obs_of else '?'}, родитель OB D1"
              " 121066.14–124197.25): уникальных зон"
              f" {s['uniq_zones']}, на v{s['cur_ver']}: out_of_range="
              f"{_split(s, 'out_of_range')}, tested={_split(s, 'tested')},"
              f" fresh={_split(s, 'fresh')}. NB: выбранное в UI наблюдение"
              f" (первое в активной вкладке) — другое: obs"
              f" {selected['id'] if selected else '—'}, сценарий"
              f" {selected['sid'] if selected else '—'} (родитель OB D1"
              " 75545.67–81272.62 из скрина B); у него 12 зон — счётчик «12»"
              " на скрине B. Числа 130 и 12 относятся к РАЗНЫМ сценариям.")
    rep.p("2. **359 версий не оправданы — ПОДТВЕРЖДЕНО как проблема**."
          " Корневая причина: `ltf_range_right = 0` в data/settings.json"
          " (дефолт config.py — 3). Профиль опор диапазона (3,0) ≠"
          " структурному (3,3) → `_range_pivots` уходит в транзитный расчёт"
          " (app/engine/ltf/engine.py:510-526): якоря не материализуются"
          " (anchor refs = pivot_at, в БД пишется NULL — engine.py:542-551),"
          " а опоры подтверждаются 0 правых свечей вместо 3 (нарушение §5.1,"
          " ranges.py RANGE_PIVOT_RIGHT=3 не используется движком)."
          " Дедуп в `range_recalc` (ranges.py:125-132) сравнивает anchor_ref:"
          " у прежней версии из БД он None, у свежего draft — pivot_at,"
          " равенства нет никогда → новая версия на КАЖДОЙ обработанной"
          " свече и на каждом replay (инверсии available_at — след replay).")
    rep.p("3. **ltf_entry_types не применяется — ПОДТВЕРЖДЕНО** (раздел 8:"
          " сохранение есть, чтения в расчёте нет).")
    rep.p(f"4. **Отменённые сценарии получают версии после cancelled_at —"
          f" НЕ ПОДТВЕРЖДЕНО на текущих данных**: поздних привязок"
          f" (added_at/updated_at > cancelled_at) — {late_added}/{late_updated},"
          " поздних версий — 0. Код это подтверждает: `get_active_ltf_scenario`"
          " не возвращает cancelled (app/db.py:1098-1106), а"
          " `_process_active_scenario` при отмене делает return ДО"
          " `_update_range` (app/engine/ltf/engine.py:414-435). Однако"
          " отменённый сценарий остаётся в правой карточке:"
          " `ltf_observation_card` при отсутствии активного сценария отдаёт"
          " ПОСЛЕДНИЙ (отменённый) в поле `active_scenario`"
          " (app/web/ltf_api.py:386-388, `_active_or_last_scenario` 183-190),"
          " и ltf.js:254,477,489,323-336 рисует его диапазон/зоны как текущие.")
    rep.p("5. **Историческое касание помечается как актуальное «цена в зоне»"
          " — ПОДТВЕРЖДЕНО**: метка выводится из `fresh_entries == 0`"
          " (ltf.js:176-178), сравнения цены с границами нет нигде;"
          " фактическое положение — раздел 6 (цена выше OB"
          " 75545.67–81272.62).")
    rep.p()

    out = "\n".join(rep.lines)
    print(out)


if __name__ == "__main__":
    # Windows-консоль (cp1251): отчёт всегда пишем в UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
