"""Эталон PUMP (ТЗ «Альткоины D1» §19): replay PUMPUSDT spot Bybit D1.

Скачивает ВСЮ доступную историю D1 спотовой пары PUMPUSDT с Bybit
(BybitSpotAdapter, публичный V5, без ключа; пагинация klines от 0 —
внутри адаптера), прогоняет её через AltEngine с AltConfig по умолчанию
на изолированной БД (временный файл; production data/*.db НЕ трогается)
и печатает структурированный replay-отчёт (RU):

источник и полнота истории → ATH (равные вершины) → минимум после ATH →
drawdown → опоры (время формирования vs доступности, §5/T07) → freeze
(когда распознан mature, L/U/W/M, метрики классификатора vs пороги,
classifier_version) → выносы/манипуляция → BOS/SMS → breakout → ретесты →
входы A/B → TP1..TP4 (какие достигнуты) → K и режим отмены, достижимость →
текущее состояние сетапа → хронология событий.

Негативные контроли (§7.4/T10): направленные серии через ПОЛНЫЙ путь
движка — две синтетические (падение/рост с осцилляцией, мотив векторов
§7.4) и реальные направленные альты с Bybit (--neg-symbols); классификатор
обязан их отклонить (без freeze).

CLI:
  --db PATH          путь scratch-БД (по умолчанию временный файл)
  --out PATH         куда записать отчёт (по умолчанию — только stdout)
  --json             дополнительно JSON (рядом с --out суффикс .json,
                     без --out — в stdout после отчёта)
  --neg-symbols CSV  реальные негативные контроли Bybit spot
                     (по умолчанию HYPEUSDT,LINKUSDT,AVAXUSDT — явно направленные
                     серии; пусто — пропустить)

Запуск: .venv/Scripts/python.exe tools/replay_alt_pump.py \
            --out data/diag/alt_pump_replay.txt --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.base import AdapterError  # noqa: E402
from app.adapters.bybit import BybitSpotAdapter  # noqa: E402
from app.alt.engine import AltEngine, classify_sideways  # noqa: E402
from app.config import AltConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import close_boundary_ms, now_ms  # noqa: E402
from app.models_alt import (  # noqa: E402
    AltAsset,
    AltCandle,
    AltInstrumentSource,
)

DAY_MS = 86_400_000
MSK = datetime.timedelta(hours=3)
SYMBOL = "PUMPUSDT"
VENUE = "bybit"
# Replay-идентификатор актива (не из CMC — модуль вселенной здесь не
# задействован; движку нужен лишь уникальный ключ alt_asset)
REPLAY_CMC_ID = 900001


def tsu(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=datetime.timezone.utc
    ).strftime("%d.%m.%Y %H:%M UTC")


def tsm(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    return (
        datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
        + MSK
    ).strftime("%d.%m.%Y %H:%M МСК")


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{p:.8g}"


# ---------------------------------------------------------------------------
# Загрузка истории
# ---------------------------------------------------------------------------


async def fetch_d1_history(adapter: BybitSpotAdapter, symbol: str) -> list[Any]:
    """Вся доступная история закрытых D1 (пагинация — внутри адаптера,
    include_forming=False: формирующаяся D1 не участвует, §4)."""
    return await adapter.klines(symbol, "D1", 0, now_ms(),
                                include_forming=False)


def detect_gaps(open_times: Sequence[int]) -> list[dict[str, int]]:
    """Пропуски суток между соседними свечами (фиксируются, не заполняются)."""
    gaps: list[dict[str, int]] = []
    for a, b in zip(open_times, open_times[1:]):
        missing = int((b - a) // DAY_MS - 1)
        if missing > 0:
            gaps.append({"from_open_time": a, "to_open_time": b,
                         "missing_days": missing})
    return gaps


# ---------------------------------------------------------------------------
# Replay через движок на изолированной БД
# ---------------------------------------------------------------------------


def replay_series(
    db: Database,
    cfg: AltConfig,
    candles: Sequence[Any],
    *,
    cmc_id: int,
    symbol: str,
    venue: str = VENUE,
    scope: str = "full",
    detected_at_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Актив+источник+свечи в scratch-БД, полный replay AltEngine."""
    asset = db.upsert_alt_asset(
        AltAsset(id=None, cmc_id=cmc_id, symbol=symbol, name=symbol)
    )
    assert asset.id is not None
    src = db.upsert_alt_instrument_source(AltInstrumentSource(
        id=None, asset_id=asset.id, venue=venue, symbol=symbol,
        earliest_available_ms=(candles[0].open_time if candles else 0),
        last_closed_ms=(candles[-1].open_time if candles else 0),
        history_scope=scope,
    ))
    assert src.id is not None
    db.insert_alt_candles([
        AltCandle(
            source_id=src.id, open_time=c.open_time, open=c.open,
            high=c.high, low=c.low, close=c.close,
            volume=float(getattr(c, "volume", 0.0) or 0.0),
        )
        for c in candles
    ])
    stored = db.get_alt_candles(src.id)
    engine = AltEngine(db, cfg)
    summary = engine.process_asset_history(
        asset.id, src.id, stored, detected_at_ms
    )
    summary["_asset_id"] = asset.id
    summary["_source_id"] = src.id
    return summary


# ---------------------------------------------------------------------------
# Синтетические направленные серии (негативный контроль через полный путь)
# ---------------------------------------------------------------------------

T0 = (1_700_000_000_000 // DAY_MS) * DAY_MS  # выровненное начало суток UTC


def trend_series(drift: float, days: int = 150, amp: float = 0.07,
                 period: int = 15) -> list[AltCandle]:
    """ATH=10 (день 4) → падение до 1.9 (−81%, день 5) → НАПРАВЛЕННЫЙ участок
    `days` дней: close = 2.0 + drift·t + amp·sin(2πt/period) (High=Low=Close,
    мотив векторов §7.4 — осцилляция даёт pivots 3+3, дрейф — направление).

    drift < 0 — продолжающееся падение; drift > 0 — V-образный рост.
    """
    candles: list[AltCandle] = []

    def ac(i: int, o: float, h: float, l: float, c: float) -> None:
        candles.append(AltCandle(source_id=1, open_time=T0 + i * DAY_MS,
                                 open=o, high=h, low=l, close=c))

    for i, p in enumerate([8.0, 9.0, 9.5, 9.8, 10.0]):
        ac(i, p, p, p, p)
    ac(5, 9.9, 9.9, 1.9, 2.5)  # −81% от ATH — факт глубокого падения
    for t in range(days):
        price = 2.0 + drift * t + amp * math.sin(2 * math.pi * t / period)
        assert price > 0
        ac(6 + t, price, price, price, price)
    return candles


def run_synthetic_negative(name: str, drift: float, cfg: AltConfig
                           ) -> dict[str, Any]:
    """Синтетический направленный кейс через полный путь движка (in-memory)."""
    db = Database(":memory:")
    try:
        candles = trend_series(drift)
        summary = replay_series(db, cfg, candles, cmc_id=900100,
                                symbol=name)
        return {
            "kind": "synthetic", "name": name,
            "candles": len(candles),
            "state": summary.get("state"),
            "frozen_range_id": summary.get("frozen_range_id"),
            "setup_state": summary.get("setup_state"),
            "classifier": summary.get("classifier"),
            "n_days": summary.get("n_days"),
            "data_errors": summary.get("data_errors"),
        }
    finally:
        db.close()


async def run_real_negative(adapter: BybitSpotAdapter, symbol: str,
                            cfg: AltConfig) -> dict[str, Any]:
    """Реальный направленный альт D1: полный путь движка + прямой расчёт
    классификатора на последних 120 закрытых D1 (W = max High − min Low
    окна — как в синтетических векторах §7.4)."""
    candles = await fetch_d1_history(adapter, symbol)
    db = Database(":memory:")
    try:
        summary = replay_series(db, cfg, candles, cmc_id=900200,
                                symbol=symbol)
    finally:
        db.close()
    direct: Optional[dict[str, Any]] = None
    if len(candles) >= 120:
        win = candles[-120:]
        closes = [c.close for c in win]
        width = max(c.high for c in win) - min(c.low for c in win)
        res = classify_sideways(closes, width, cfg, candles=win)
        direct = {
            "window_last_days": len(win),
            "from_open_time": win[0].open_time,
            "to_open_time": win[-1].open_time,
            "ready": res.ready, "sideways": res.sideways,
            "slope_normalized": res.slope_normalized,
            "center_shift": res.center_shift,
            "slope_sign": res.slope_sign,
            "failed_conditions": list(res.failed_conditions),
        }
    return {
        "kind": "real", "name": symbol, "venue": VENUE,
        "candles": len(candles),
        "first_open_time": candles[0].open_time if candles else None,
        "last_open_time": candles[-1].open_time if candles else None,
        "state": summary.get("state"),
        "frozen_range_id": summary.get("frozen_range_id"),
        "setup_state": summary.get("setup_state"),
        "classifier": summary.get("classifier"),
        "direct_last120": direct,
    }


# ---------------------------------------------------------------------------
# Сбор фактов отчёта из scratch-БД
# ---------------------------------------------------------------------------


def collect_setup_facts(db: Database, summary: dict[str, Any],
                        cfg: AltConfig,
                        candles: Sequence[Any]) -> dict[str, Any]:
    """Все сущности сетапа для отчёта (frozen/события/структура/эпизоды/
    входы/цели) + пересчёт метрик классификатора НА МОМЕНТ freeze
    (summary хранит последний результат — на конец replay)."""
    facts: dict[str, Any] = {
        "frozen": None, "setup": None, "events": [], "structure": [],
        "episodes": [], "entries": [], "candidate": None,
        "classifier_at_freeze": None, "anchor_availability": {},
    }
    setup_id = summary.get("setup_id")
    frozen_id = summary.get("frozen_range_id")
    if frozen_id is None:
        return facts
    frozen = db.get_alt_frozen_range(frozen_id)
    facts["frozen"] = frozen
    cand = db.get_alt_range_candidate(frozen.range_id)
    facts["candidate"] = cand
    if setup_id is None:
        return facts
    setup = db.get_alt_setup(setup_id)
    facts["setup"] = setup
    facts["events"] = db.list_alt_events(setup.id)
    facts["structure"] = db.list_alt_structure_events(setup.id)
    facts["episodes"] = db.list_alt_manipulation_episodes(setup.id)
    facts["entries"] = db.list_alt_entry_opportunities(setup.id)

    ots = [c.open_time for c in candles]
    index = {ot: i for i, ot in enumerate(ots)}
    # Доступность опор: pivot подтверждён после закрытия pivot_right свечей
    for label, formed_at in (
        ("start", frozen.start_anchor_open_time),
        ("rebound", frozen.rebound_anchor_open_time),
    ):
        i = index.get(formed_at)
        avail_at = None
        if i is not None and i + cfg.pivot_right < len(candles):
            avail_at = close_boundary_ms(
                candles[i + cfg.pivot_right].open_time, "D1"
            )
        facts["anchor_availability"][label] = {
            "formed_at": formed_at, "available_at": avail_at,
        }
    # Классификатор на момент freeze: участок start..свеча freeze
    freeze_open = frozen.mature_at_ms - DAY_MS
    i0 = index.get(frozen.start_anchor_open_time)
    i1 = index.get(freeze_open)
    if i0 is not None and i1 is not None and i1 >= i0:
        seg = list(candles[i0:i1 + 1])
        res = classify_sideways(
            [c.close for c in seg], frozen.width, cfg, candles=seg
        )
        facts["classifier_at_freeze"] = {
            "ready": res.ready, "sideways": res.sideways,
            "slope_b": res.slope_b, "slope_sign": res.slope_sign,
            "slope_normalized": res.slope_normalized,
            "center_shift": res.center_shift,
            "failed_conditions": list(res.failed_conditions),
            "efficiency": res.efficiency, "n_blocks": res.n_blocks,
            "classifier_version": res.classifier_version,
        }
    return facts


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------


def _clf_lines(clf: Optional[dict[str, Any]], cfg: AltConfig,
               indent: str = "  ") -> list[str]:
    if not clf:
        return [f"{indent}классификатор не вызывался (нет готового участка)"]
    lines = [
        f"{indent}slope_normalized = {fmt_price(clf.get('slope_normalized'))} "
        f"(порог {cfg.classifier_slope_max}) — "
        f"{'OK' if (clf.get('slope_normalized') or 9e9) <= cfg.classifier_slope_max else 'НАРУШЕН'}",
        f"{indent}center_shift = {fmt_price(clf.get('center_shift'))} "
        f"(порог {cfg.classifier_center_shift_max}) — "
        f"{'OK' if (clf.get('center_shift') or 9e9) <= cfg.classifier_center_shift_max else 'НАРУШЕН'}",
        f"{indent}знак наклона b: {clf.get('slope_sign')}, "
        f"efficiency = {fmt_price(clf.get('efficiency'))}, "
        f"вердикт: {'БОКОВИК' if clf.get('sideways') else 'НАПРАВЛЕННЫЙ (отклонён)'}"
        + (f", нарушено: {', '.join(clf.get('failed_conditions') or [])}"
           if clf.get('failed_conditions') else ""),
        f"{indent}classifier_version = {clf.get('classifier_version')}",
    ]
    return lines


def render_report(data: dict[str, Any], cfg: AltConfig) -> str:
    L: list[str] = []
    add = L.append
    s = data["summary"]
    ath = s.get("ath") or {}
    facts = data["facts"]
    frozen = facts["frozen"]
    setup = facts["setup"]

    add("=" * 72)
    add(f"ЭТАЛОН PUMP — replay {SYMBOL} · {VENUE} spot · D1 (ТЗ §19)")
    add("=" * 72)
    add(f"Прогон: {tsm(data['run_at_ms'])}; движок AltEngine, AltConfig по "
        f"умолчанию; scratch-БД: {data['db_path']} (production data/*.db не "
        "использовалась). Replay детерминирован: detected_at = граница "
        "закрытия последней свечи.")
    add("")

    add("1. ИСТОЧНИК И ПОЛНОТА ИСТОРИИ")
    add(f"  Источник: Bybit V5 GET /v5/market/kline, category=spot, "
        f"interval=D, symbol={SYMBOL}; пагинация от start=0 до пустого "
        "ответа; незакрытая (формирующаяся) D1 исключена.")
    add(f"  Первая закрытая D1 (earliest_available): {tsu(data['first_open'])}")
    add(f"  Последняя закрытая D1 (last_closed):     {tsu(data['last_open'])}")
    add(f"  Свечей получено: {s.get('candles_total')}, валидных: "
        f"{s.get('candles_valid')}; ошибок данных: "
        f"{len(s.get('data_errors') or [])}"
        + (f" {s['data_errors'][:5]}" if s.get("data_errors") else ""))
    add(f"  history_scope = {s.get('history_scope')} (вся доступная у "
        "площадки история подтверждена полной пагинацией)")
    gaps = data["gaps"]
    if gaps:
        add(f"  Пропуски суток (gaps, НЕ заполняются): {len(gaps)}")
        for g in gaps[:10]:
            add(f"    {tsu(g['from_open_time'])} → {tsu(g['to_open_time'])}: "
                f"{g['missing_days']} дн.")
    else:
        add("  Пропуски суток (gaps): нет — история непрерывна.")
    add("")

    add("2. ATH (по теням всей доступной истории, §5)")
    add(f"  ATH = {fmt_price(ath.get('ath_price'))} на свече "
        f"{tsu(ath.get('ath_open_time'))}")
    eq = ath.get("equal_top_open_times") or []
    if len(eq) > 1:
        add(f"  Равные вершины (та же цена): "
            + ", ".join(tsu(t) for t in eq)
            + " — начало эпизода: ПОСЛЕДНЯЯ свеча с этой ценой "
              "(проектное правило)")
    else:
        add("  Равных вершин нет.")
    add(f"  Эпизодов ATH всего: {ath.get('episodes_total')}")
    add("")

    add("3. МИНИМУМ ПОСЛЕ ATH И ПРОСАДКА")
    add(f"  P_min = {fmt_price(ath.get('p_min'))} на свече "
        f"{tsu(ath.get('p_min_open_time'))}")
    dd = ath.get("drawdown")
    add(f"  Drawdown = 1 − P_min/ATH = {dd * 100:.2f}%"
        if dd is not None else "  Drawdown: нет свечей после ATH")
    add(f"  Порог > {cfg.drawdown_threshold:.0%}: "
        + (f"ДОСТИГНУТ на свече {tsu(ath.get('deep_drop_open_time'))} "
           "(строго: ровно 80% не подошло бы, T05)"
           if ath.get("deep_drop_achieved") else "не достигнут"))
    add("")

    add("4. ОПОРЫ ДИАПАЗОНА (§5: формирование vs доступность, T07)")
    if frozen is not None:
        av = facts["anchor_availability"]
        st, rb = av.get("start", {}), av.get("rebound", {})
        add(f"  Стартовый якорь (pivot low 3+3): свеча "
            f"{tsu(st.get('formed_at'))}; доступен алгоритму после закрытия "
            f"3 правых — {tsu(st.get('available_at'))}")
        add(f"  Якорь отскока (первый pivot high после старта): свеча "
            f"{tsu(rb.get('formed_at'))}; доступен — "
            f"{tsu(rb.get('available_at'))}")
        add("  Начало отрисовки диапазона ≠ доступность сигнала тогда: "
            "обе опоры видны только после своих правых свечей.")
        if s.get("review_required"):
            add(f"  REVIEW_REQUIRED: альтернативные равные опоры "
                f"{[tsu(t) for t in (s.get('alternative_anchors') or [])]}")
    else:
        add("  Диапазон не заморожен — опор зрелого сетапа нет. "
            f"Состояние: {s.get('state')}")
    add("")

    add("5. FREEZE — ЗРЕЛЫЙ ДИАПАЗОН (§6–§8)")
    if frozen is not None:
        add(f"  Распознан mature: {tsu(frozen.mature_at_ms)} "
            f"(N_days = {facts['candidate'].n_days if facts['candidate'] else '—'}, "
            f"включено свечей: {frozen.included_candles}, "
            f"range_version = {frozen.range_version})")
        add(f"  L = {fmt_price(frozen.lower)}, U = {fmt_price(frozen.upper)}, "
            f"W = {fmt_price(frozen.width)}, M = {fmt_price(frozen.mid)}")
        if frozen.lower > 0 and frozen.upper > 0:
            add(f"  Ширина вверх (U/L−1) = {(frozen.upper / frozen.lower - 1) * 100:.2f}%; "
                f"снижение от U к L (1−L/U) = {(1 - frozen.lower / frozen.upper) * 100:.2f}%")
        add("  Классификатор НА МОМЕНТ freeze (пересчёт участка "
            "start..свеча freeze):")
        for line in _clf_lines(facts["classifier_at_freeze"], cfg):
            add(line)
        clf_last = s.get("classifier")
        if clf_last:
            add("  Классификатор на конец replay (тот же участок до "
                "последней D1, до freeze расширение разрешено):")
            for line in _clf_lines(clf_last, cfg):
                add(line)
    else:
        add("  Freeze не состоялся.")
        for line in _clf_lines(s.get("classifier"), cfg):
            add(line)
    add("")

    add("6. ВЫНОСЫ / МАНИПУЛЯЦИЯ (§9: Low < L после freeze, границы не "
        "расширяются)")
    if facts["episodes"]:
        for ep in facts["episodes"]:
            end = (f"завершён возвратом Close>L на {tsu(ep.ended_candle_open_time)}"
                   if ep.ended_candle_open_time else "АКТИВЕН на конец replay")
            add(f"  Эпизод #{ep.id}: начало {tsu(ep.started_candle_open_time)}, "
                f"min = {fmt_price(ep.min_price)}, дней ниже L = "
                f"{ep.days_below}, {end}")
    else:
        add("  Эпизодов манипуляции не зафиксировано.")
    add("")

    add("7. СТРУКТУРА D1 (§10: BOS/SMS/SSL по подтверждённым pivots 3+3)")
    if facts["structure"]:
        for e in facts["structure"]:
            hist = ""
            try:
                hist = " [historical — до maturity, не вход]" if json.loads(
                    e.anchors_json).get("historical") else ""
            except (ValueError, AttributeError):
                pass
            add(f"  {e.kind} уровень {fmt_price(e.level_price)}, закрытие "
                f"{fmt_price(e.close_price)} на {tsu(e.candle_open_time)}{hist}")
    else:
        add("  Структурных событий нет.")
    add("")

    add("8. BREAKOUT И РЕТЕСТ (§11)")
    if setup is not None and setup.breakout_closed_at is not None:
        add(f"  Breakout: Close = {fmt_price(setup.breakout_close)} > U, "
            f"закрытие {tsu(setup.breakout_closed_at)}")
        add(f"  Дедлайн ретеста (+{cfg.retest_window_days} дн, включителен): "
            f"{tsu(setup.retest_deadline_ms)}")
        retests = [e for e in facts["events"] if e.event_type == "retest"]
        if retests:
            for e in retests:
                p = json.loads(e.payload_json)
                add(f"  Ретест [M,U] на {tsu(e.event_time_ms)}"
                    + (" (журнал — повторное касание)" if p.get("journal") else "")
                    + (f", глубина ниже M: {fmt_price(p.get('depth_below_mid'))}"
                       if p.get("depth_below_mid") else ""))
        else:
            add("  Касаний области ретеста [M,U] не было.")
    else:
        add("  Подтверждённого breakout (Close > U) не было.")
    add("")

    add("9. ВХОДЫ A/B (§10/§11: один первый A и один первый B на сетап)")
    if facts["entries"]:
        for o in facts["entries"]:
            if o.kind == "A":
                add(f"  Вход A: цена {fmt_price(o.price)} на "
                    f"{tsu(o.event_time_ms)}, основания {o.bases_json}")
            else:
                add(f"  Вход B: зона {o.zone_json} на {tsu(o.event_time_ms)}")
    else:
        add("  Возможностей входа не выдано.")
    add("")

    add("10. ЦЕЛИ TP1–TP4 (§13: TP_n = U + n·W от снимка подтверждения)")
    if setup is not None and setup.targets_json and setup.targets_json != "[]":
        targets = json.loads(setup.targets_json)
        flags = json.loads(setup.flags_json or "{}")
        hit = set(flags.get("targets_hit") or [])
        hit_events = [e for e in facts["events"]
                      if e.event_type == "target_hit"]
        hit_dates: dict[int, list[str]] = {}
        for e in hit_events:
            for n in json.loads(e.payload_json).get("levels", []):
                hit_dates.setdefault(n, []).append(tsu(e.event_time_ms))
        for t in targets:
            n = t["tp"]
            mark = "не достигнута"
            if t.get("passed_at_confirmation"):
                mark = "пройдена к моменту подтверждения (не будущий потенциал)"
            elif n in hit:
                mark = "ДОСТИГНУТА " + ", ".join(hit_dates.get(n, []))
            add(f"  TP{n} = {fmt_price(t['price'])} — {mark}")
    else:
        add("  Подтверждения сетапа не было — целей нет.")
    add("")

    add("11. ОТМЕНА K (§12)")
    if setup is not None:
        add(f"  K = 2L − U = {fmt_price(setup.cancel_price)}; режим "
            f"подтверждения: {setup.cancel_mode} (проектный выбор v1, не "
            "утверждённая калибровка)")
        if setup.cancel_reachable:
            add("  Достижимость K: да (положительная цена).")
        else:
            add("  Достижимость K: НЕТ — по выбранной формуле уровень отмены "
                "неположительный; не обрезается и не заменяется (T25).")
        if setup.state == "cancelled":
            add(f"  Сетап ОТМЕНЁН: {tsu(setup.terminated_ms)}")
    else:
        add("  Сетапа нет — K не рассчитывался.")
    add("")

    add("12. ТЕКУЩЕЕ СОСТОЯНИЕ SETUP")
    add(f"  Состояние актива: {s.get('state')}; "
        f"setup_state: {s.get('setup_state') or '—'}")
    if setup is not None:
        add(f"  setup_id = {setup.id}, флаги: {setup.flags_json}")
        if setup.terminated_ms is not None:
            add(f"  Терминальное завершение: {tsu(setup.terminated_ms)}")
    add("")

    add("13. НЕГАТИВНЫЕ КОНТРОЛИ (§7.4/T10: направленные серии обязаны быть "
        "отклонены)")
    for neg in data["negatives"]:
        if "error" in neg:
            add(f"  {neg['name']}: ОШИБКА — {neg['error']}")
            continue
        verdict = ("ОТКЛОНЁН (freeze не состоялся)"
                   if neg.get("frozen_range_id") is None
                   else "ВНИМАНИЕ: freeze состоялся!")
        add(f"  [{neg['kind']}] {neg['name']}: состояние {neg.get('state')}, "
            f"{verdict}")
        if neg.get("classifier"):
            add("    классификатор движка (конец replay, участок от первой "
                "опоры после падения):")
            for line in _clf_lines(neg["classifier"], cfg, indent="      "):
                add(line)
        d = neg.get("direct_last120")
        if d:
            add(f"    прямой расчёт на последних {d['window_last_days']} D1 "
                f"({tsu(d['from_open_time'])} → {tsu(d['to_open_time'])}):")
            for line in _clf_lines(d, cfg, indent="      "):
                add(line)
    add("")

    add("14. ХРОНОЛОГИЯ СОБЫТИЙ (outbox, event_time — рыночное время)")
    if facts["events"]:
        for e in sorted(facts["events"], key=lambda x: x.event_time_ms):
            add(f"  {tsu(e.event_time_ms)}  {e.event_type:22s} "
                f"{e.payload_json}")
    else:
        add("  Событий нет.")
    add("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def amain(args: argparse.Namespace) -> int:
    cfg = AltConfig()
    db_path = args.db or str(
        Path(tempfile.gettempdir()) / "alt_pump_replay_scratch.db"
    )
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-shm", "-wal"):
        p = Path(db_path + suffix)
        if p.exists() and not args.db:
            p.unlink()  # чистый scratch при дефолтном пути

    adapter = BybitSpotAdapter()
    negatives: list[dict[str, Any]] = []
    try:
        print(f"Загрузка истории {SYMBOL} ({VENUE} spot D1)...",
              file=sys.stderr)
        try:
            candles = await fetch_d1_history(adapter, SYMBOL)
        except AdapterError as exc:
            print(f"ОШИБКА СЕТИ/ИСТОЧНИКА: {exc}", file=sys.stderr)
            print("Replay PUMP невозможен без данных Bybit spot PUMPUSDT "
                  "(§19: замена фьючерсом/другой биржей недопустима).",
                  file=sys.stderr)
            return 2
        if not candles:
            print("ОШИБКА: Bybit вернул пустую историю по "
                  f"{SYMBOL}.", file=sys.stderr)
            return 2

        db = Database(db_path)
        try:
            summary = replay_series(db, cfg, candles,
                                    cmc_id=REPLAY_CMC_ID, symbol=SYMBOL)
            facts = collect_setup_facts(db, summary, cfg, candles)
        finally:
            db.close()

        # Негативные контроли: синтетика (всегда) + реальные (если заданы)
        negatives.append(run_synthetic_negative(
            "SYNTH-DOWN (2.0 − t/150 + 0.07·sin, 150 дней)", -1 / 150, cfg))
        negatives.append(run_synthetic_negative(
            "SYNTH-UP (2.0 + t/150 + 0.07·sin, 150 дней)", 1 / 150, cfg))
        for sym in [x.strip() for x in args.neg_symbols.split(",")
                    if x.strip()]:
            try:
                print(f"Негативный контроль: {sym}...", file=sys.stderr)
                negatives.append(await run_real_negative(adapter, sym, cfg))
            except AdapterError as exc:
                negatives.append({"kind": "real", "name": sym,
                                  "error": str(exc)})
    finally:
        await adapter.aclose()

    data = {
        "run_at_ms": now_ms(),
        "db_path": db_path,
        "symbol": SYMBOL, "venue": VENUE,
        "first_open": candles[0].open_time,
        "last_open": candles[-1].open_time,
        "gaps": detect_gaps([c.open_time for c in candles]),
        "summary": summary,
        "facts": facts,
        "negatives": negatives,
    }
    report = render_report(data, cfg)
    print(report)

    json_data = {
        "run_at_ms": data["run_at_ms"], "symbol": SYMBOL, "venue": VENUE,
        "first_open_time": data["first_open"],
        "last_open_time": data["last_open"], "gaps": data["gaps"],
        "summary": summary,
        "frozen_range": (
            None if facts["frozen"] is None else {
                k: v for k, v in vars(facts["frozen"]).items()
            }),
        "classifier_at_freeze": facts["classifier_at_freeze"],
        "anchor_availability": facts["anchor_availability"],
        "setup": (
            None if facts["setup"] is None else {
                k: v for k, v in vars(facts["setup"]).items()
            }),
        "events": [
            {"type": e.event_type, "event_time_ms": e.event_time_ms,
             "payload": json.loads(e.payload_json)}
            for e in facts["events"]
        ],
        "negatives": negatives,
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"Отчёт записан: {out}", file=sys.stderr)
        if args.json:
            jpath = out.with_suffix(".json")
            jpath.write_text(
                json.dumps(json_data, ensure_ascii=False, indent=1,
                           default=str),
                encoding="utf-8")
            print(f"JSON записан: {jpath}", file=sys.stderr)
    elif args.json:
        print(json.dumps(json_data, ensure_ascii=False, indent=1,
                         default=str))
    return 0


def main() -> None:
    # Консоль Windows (cp1251) не кодирует часть RU-типографики (−, ≠, ·)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="путь scratch-БД (по умолчанию временный файл)")
    ap.add_argument("--out", default=None,
                    help="куда записать отчёт (по умолчанию — только stdout)")
    ap.add_argument("--json", action="store_true",
                    help="дополнительно выдать JSON отчёта")
    ap.add_argument("--neg-symbols", default="HYPEUSDT,LINKUSDT,AVAXUSDT",
                    help="CSV реальных направленных альтов Bybit spot для "
                         "негативного контроля (пусто — только синтетика)")
    raise SystemExit(asyncio.run(amain(ap.parse_args())))


if __name__ == "__main__":
    main()
