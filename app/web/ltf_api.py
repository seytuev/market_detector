"""HTTP API окна LTF Confirmations (LTF-спека §3, §12, §13).

Маршруты: список наблюдений с фильтрами (§3.2), карточка (§3.3), слои
графика (§3.5), таблица Entry Zones с dist-формулами (§3.4), журнал,
ручное завершение сценария, разметка Entry Zones (ревью). Все за
owner-авторизацией; сериализация — to_dict() моделей плюс агрегация из
репозиториев Database.

ТЗ «LTF Current Setup» §14 — read model «текущая ситуация по активу»:
GET /api/ltf/instruments (одна строка на instrument_id, этап считает
сервер), GET /api/ltf/instruments/{id}/current (InstrumentCurrentView:
котировка, data_state, контексты с реальным is_price_inside_now,
не-отменённый current_scenario, диапазон, подходящие зоны, counts,
state_version), POST select-context (ручной выбор контекста, §7).
Старые маршруты наблюдений (история, журнал, разметка) сохранены.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from ..db import Database
from ..engine.ltf.context import context_complete, context_flags
from ..engine.ltf.eligibility import REASON_OK, REASON_OUTSIDE_PD, entry_reason
from ..engine.ltf.entries import H1_MS
from ..engine.ltf.pivots import PivotCandidate
from ..engine.ltf.ranges import provisional_range, zone_half
from ..models import now_ms
from ..models_ltf import (
    LTF_LEGACY_DECISIONS,
    LTF_REVIEW_DECISIONS,
    LtfEntryZone,
    LtfObservation,
    LtfReview,
    LtfReviewAssessment,
    LtfScenario,
    LtfScenarioEntry,
)

# вкладки списка наблюдений (§3.2)
_ACTIVE_STATES = {"waiting_structure", "active", "paused_data"}
_HISTORY_STATES = {"closed_by_parent", "closed_by_user", "closed_stale"}
_ENTRY_ORDER = {"FVG": 0, "OB": 1, "BSL": 2, "SSL": 3}

# этапы инструмента (ТЗ «LTF Current Setup» §4.2) — вычисляет сервер,
# фронт «текущий сценарий» самостоятельно не восстанавливает (§14)
STAGE_WAIT_HTF = "Ждём HTF-зону"
STAGE_WAIT_BOS = "Ждём BOS/SMS"
STAGE_WAIT_RANGE = "Ждём диапазон"
STAGE_RETRACEMENT = "Возврат в Premium/Discount"
STAGE_IN_ENTRY = "Цена в Entry Zone"
STAGE_NO_ZONES = "Нет подходящих зон"
STAGE_DATA_PENDING = "Недостаточно данных"


def _instrument_brief(db: Database, instrument_id: int) -> Optional[dict[str, Any]]:
    ins = db.get_instrument(instrument_id)
    if ins is None:
        return None
    return {
        "id": ins.id, "symbol": ins.symbol, "asset": ins.asset,
        "venue": ins.venue, "market_type": ins.market_type,
    }


def _zone_brief(db: Database, zone_id: int) -> Optional[dict[str, Any]]:
    z = db.get_zone(zone_id)
    if z is None:
        return None
    return {
        "id": z.id, "type": z.type.value, "timeframe": z.timeframe,
        "direction": z.direction.value, "lower": z.lower, "upper": z.upper,
        "status": z.status.value, "cycle_id": z.cycle_id,
        # §3.3/§4: основание и подтверждение зоны — разные моменты
        "formed_at": z.formed_at, "confirmed_at": z.confirmed_at,
    }


def _current_version(db: Database, scenario_id: int) -> int:
    rng = db.get_current_ltf_range(scenario_id)
    return rng.version if rng is not None else 0


class LtfSelectContextIn(BaseModel):
    """Ручной выбор HTF-контекста инструмента (§7)."""
    observation_id: int


class LtfReviewIn(BaseModel):
    """Решение ревью Entry Zone (аналог ReviewIn HTF, §15.3 HTF-спеки).

    Оценка только фиксируется: validity зоны, границы и привязки
    ltf_scenario_entry не меняются. fix_boundaries требует lower/upper —
    исправленные границы записываются в разметку, зона остаётся как была.
    """
    decision: str
    text: str = ""
    scenario_id: Optional[int] = None  # контекст: из какого сценария оцениваем
    reason_code: Optional[str] = None  # LTF-специфичный код причины
    lower: Optional[float] = None
    upper: Optional[float] = None
    evidence_source: str = "manual_ui"


def export_ltf_label(
    db: Database,
    zone: LtfEntryZone,
    decision: str,
    text: str,
    settings,
    *,
    assessment: LtfReviewAssessment,
    scenario_id: Optional[int] = None,
) -> str:
    """Пишет оценку Entry Zone в ltf_labels.jsonl рядом с БД (append-only).

    Отдельный от HTF-разметки (labels.jsonl) датасет: полный снимок зоны,
    контекст сценария/наблюдения/родительской HTF-зоны, все привязки
    ltf_scenario_entry этой зоны и OHLC исходных свечей. Версия 1.
    """
    ins = db.get_instrument(zone.instrument_id)
    entries = db.list_ltf_scenario_entries_by_zone(zone.id)

    # контекст сценария: явный scenario_id или последняя привязка зоны
    if scenario_id is None and entries:
        scenario_id = entries[-1].scenario_id
    scenario = db.get_ltf_scenario(scenario_id) if scenario_id else None
    observation = (
        db.get_ltf_observation(scenario.observation_id)
        if scenario is not None else None
    )

    # OHLC исходных свечей основания (fvg_candles / base_candles — open_time)
    source_ohlc = []
    source_ids = (
        zone.evidence.get("fvg_candles")
        or zone.evidence.get("base_candles")
        or []
    )
    if source_ids:
        by_open = {
            c.open_time: c
            for c in db.get_candles(zone.instrument_id, "H1")
        }
        for t in source_ids:
            c = by_open.get(t)
            if c:
                source_ohlc.append({
                    "open_time": t, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                })

    now = now_ms()
    record = {
        "purpose": "разметка LTF Entry Zones для ИИ-анализа (dev-режим)",
        "labels_version": 1,
        "exported_at": now,  # время снимка; не подменяет reviewed_at
        "decision": decision,
        "comment": text,
        "author": "owner",
        "instrument": _instrument_brief(db, ins.id) if ins else None,
        "entry_zone": zone.to_dict(),
        # контекст: сценарий оценки, наблюдение и родительская HTF-зона
        "scenario_id": scenario_id,
        "scenario": scenario.to_dict() if scenario is not None else None,
        "observation": observation.to_dict() if observation is not None else None,
        "parent_zone": (
            _zone_brief(db, observation.zone_id)
            if observation is not None else None
        ),
        "scenario_entries": [e.to_dict() for e in entries],
        "source_candles_ohlc": source_ohlc,
        # поля ревью
        "review_id": assessment.review_id,
        "reviewed_at": assessment.reviewed_at,
        "assessed_as_of": assessment.assessed_as_of,
        "review_decision": assessment.review_decision,
        "geometry_verdict": assessment.geometry_verdict,
        "lifecycle_verdict": assessment.lifecycle_verdict,
        "reason_code": assessment.reason_code,
        "requires_clarification": bool(assessment.requires_clarification),
        "corrected_lower": assessment.corrected_lower,
        "corrected_upper": assessment.corrected_upper,
    }
    path = Path(settings.db_path).parent / "ltf_labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


def _context_flags(db: Database, scenario_id: int) -> dict[str, Any]:
    """§18: агрегированный контекст сценария из событий context_update."""
    return context_flags(
        db.list_ltf_events(scenario_id=scenario_id, limit=1000)
    )


def _fresh_entries(
    db: Database, scenario_id: int
) -> list[tuple[LtfScenarioEntry, LtfEntryZone]]:
    """Подходящие зоны текущей версии диапазона (§3.2 счётчик).

    ТЗ «LTF Current Setup» §10/п.11: счётчик — только reason == "ok";
    outside_pd и отключённые типы в него не входят (строки до миграции
    без reason — fallback из state, eligibility.entry_reason).
    §18: при полном контексте (снятие SSL/BSL + тест 50% D1 FVG) в счётчик
    входят и контекстно допущенные FVG с reason == "outside_pd"."""
    ver = _current_version(db, scenario_id)
    rows = db.list_ltf_scenario_entries(scenario_id, state="fresh")
    if context_complete(_context_flags(db, scenario_id)):
        rows += db.list_ltf_scenario_entries(scenario_id, state="out_of_range")
    out = []
    for e in rows:
        if e.range_version != ver:
            continue
        if e.state == "fresh":
            if not e.eligible or entry_reason(e) != REASON_OK:
                continue
        elif entry_reason(e) != REASON_OUTSIDE_PD:
            continue
        zone = db.get_ltf_entry_zone(e.entry_zone_id)
        if zone is None:
            continue
        if e.state != "fresh" and zone.type != "FVG":
            continue  # §18: допуск вне Premium — только для FVG
        out.append((e, zone))
    return out


def _active_or_last_scenario(
    db: Database, observation_id: int
) -> Optional[LtfScenario]:
    sc = db.get_active_ltf_scenario(observation_id)
    if sc is not None:
        return sc
    scenarios = db.list_ltf_scenarios(observation_id=observation_id)
    return scenarios[-1] if scenarios else None


# --------------------------------------------------------------------- #
# Read model «LTF Current Setup» (ТЗ §6, §7, §14)
# --------------------------------------------------------------------- #

def _data_state(
    db: Database, settings, instrument_id: int,
    quote: Optional[tuple[float, int]], now: int,
) -> dict[str, Any]:
    """Состояние данных инструмента (§14): «данные поступают» и «последняя
    закрытая H1 обработана» — раздельно; устаревшая котировка не даёт
    заявлять положение цены актуальным (§6)."""
    last_h1 = db.last_candle(instrument_id, "H1")
    if last_h1 is None:
        return {"state": "data_pending", "reason": "no_h1_candles"}
    if quote is None:
        return {"state": "data_pending", "reason": "no_quote"}
    if now - quote[1] > 2 * settings.poll_seconds * 1000:
        return {"state": "stale", "reason": "quote_stale"}
    if now - last_h1.close_time > 2 * H1_MS:
        return {"state": "stale", "reason": "h1_stale"}
    return {"state": "ok", "reason": None}


def _select_context(
    db: Database, instrument_id: int, observations: list[LtfObservation],
    price: Optional[float] = None, fresh: bool = False,
) -> Optional[LtfObservation]:
    """Политика выбора контекста (§7, приоритет «цена внутри» согласован
    владельцем): ручной выбор (meta), если он ещё доступен → контекст, чья
    HTF-зона сейчас содержит цену (план «в реализации»: сначала с действующим
    сценарием, затем последний по активации) → контекст с последним
    действующим сценарием → последний валидный контакт. Правило навигации,
    не оценка торговой силы; ручной выбор, ушедший в историю, игнорируется
    (fallback-политика). При stale-котировке приоритет «цена внутри» не
    применяется: навигация по устаревшим данным недопустима."""
    active = [o for o in observations if o.state in _ACTIVE_STATES]
    if not active:
        return None
    raw = db.get_meta(f"ltf:selected_context:{instrument_id}")
    if raw:
        try:
            manual_id = int(raw)
        except ValueError:
            manual_id = None
        manual = next((o for o in active if o.id == manual_id), None)
        if manual is not None:
            return manual

    def with_scenario(candidates: list[LtfObservation]):
        return [
            (sc, o) for o in candidates
            if (sc := db.get_active_ltf_scenario(o.id)) is not None
        ]

    if price is not None and fresh:
        inside = [
            o for o in active
            if (z := db.get_zone(o.zone_id)) is not None
            and z.lower <= price <= z.upper
        ]
        if inside:
            sc_pairs = with_scenario(inside)
            if sc_pairs:
                return max(sc_pairs,
                           key=lambda so: (so[0].created_at, so[0].id))[1]
            return max(inside, key=lambda o: (o.activated_at, o.id))
    with_scenario_pairs = with_scenario(active)
    if with_scenario_pairs:
        return max(with_scenario_pairs,
                   key=lambda so: (so[0].created_at, so[0].id))[1]
    return max(active, key=lambda o: (o.activated_at, o.id))


def _context_view(
    db: Database, obs: LtfObservation,
    price: Optional[float], fresh: bool,
) -> dict[str, Any]:
    """HTFContext (§6): родительская зона с рыночным статусом.
    is_price_inside_now — реальное сравнение L <= price <= U и только при
    свежей котировке (иначе null + data_state stale на уровне ответа);
    last_touch_at — историческое касание, inside=true не удерживает."""
    zone = db.get_zone(obs.zone_id)
    inside: Optional[bool] = None
    position: Optional[str] = None
    last_touch_at: Optional[int] = None
    if zone is not None:
        touches = [v.entered_at for v in db.get_visits(zone.id, zone.cycle_id)]
        touches.append(obs.activated_at)
        last_touch_at = max(touches)
        if price is not None and fresh:
            inside = zone.lower <= price <= zone.upper
            position = (
                "inside" if inside
                else ("above" if price > zone.upper else "below")
            )
    return {
        "observation_id": obs.id,
        "direction": obs.direction.value,
        "state": obs.state,
        "activated_at": obs.activated_at,
        "parent_zone": _zone_brief(db, obs.zone_id),
        "parent_validity": zone.market_validity if zone is not None else None,
        "is_price_inside_now": inside,
        "price_position": position,
        "last_touch_at": last_touch_at,
    }


def _scenario_block(db: Database, sc: LtfScenario) -> dict[str, Any]:
    """Блок текущего (не отменённого) сценария: уровень слома и время
    закрытия — из структурного события-триггера; диапазон с опорами
    (pivot_at ≠ confirmed_at, §3.5)."""
    trigger_event = next(
        (e for e in db.list_ltf_structure_events(sc.id)
         if e.id == sc.trigger_event_id),
        None,
    )
    rng = db.get_current_ltf_range(sc.id)
    anchors = {"low": None, "high": None}
    if rng is not None:
        low_p = db.get_ltf_pivot(rng.anchor_low_pivot_id) \
            if rng.anchor_low_pivot_id else None
        high_p = db.get_ltf_pivot(rng.anchor_high_pivot_id) \
            if rng.anchor_high_pivot_id else None
        anchors = {
            "low": low_p.to_dict() if low_p else None,
            "high": high_p.to_dict() if high_p else None,
        }
    return {
        **sc.to_dict(),
        "break_level": trigger_event.break_level if trigger_event else None,
        "break_candle_open_time": (
            trigger_event.break_candle_open_time if trigger_event else None
        ),
        "range": rng.to_dict() if rng is not None else None,
        "anchors": anchors,
        "fresh_entries": len(_fresh_entries(db, sc.id)),
        # §18: контекст «снятие SSL/BSL + тест 50% D1 FVG» для карточки
        "context": {
            **_context_flags(db, sc.id),
            "complete": context_complete(_context_flags(db, sc.id)),
        },
    }


def _state_version(
    db: Database, obs: LtfObservation, sc: Optional[LtfScenario]
) -> int:
    """Единый маркер снимка (§14): график, правая карточка, счётчик и
    таблица относятся к одному scenario_id/state_version."""
    marks = [obs.updated_at, obs.activated_at]
    if sc is not None:
        marks += [sc.created_at, sc.updated_at, sc.cancelled_at or 0]
        rng = db.get_current_ltf_range(sc.id)
        if rng is not None:
            marks += [rng.available_at, rng.version]
        entries = db.list_ltf_scenario_entries(sc.id)
        marks += [e.updated_at for e in entries] or [0]
    return max(marks)


def _contexts_direction(observations: list[LtfObservation]) -> Optional[str]:
    """Направление по контекстам; при конфликтующих — «mixed»
    (§7: «Разные HTF-контексты», не усредняем)."""
    dirs = {o.direction.value for o in observations if o.state in _ACTIVE_STATES}
    if not dirs:
        return None
    return next(iter(dirs)) if len(dirs) == 1 else "mixed"


def _instrument_stage(
    db: Database,
    instrument_id: int,
    observations: list[LtfObservation],
    eligible_zones: list[dict[str, Any]],
    price: Optional[float],
    fresh: bool,
    ds: dict[str, Any],
) -> tuple[str, Optional[str]]:
    """Текущий этап инструмента (§4.2) — сервер вычисляет по согласованному
    состоянию; при нехватке данных — data_pending, а не «нет сетапа» (§14).
    Возвращает (stage, direction)."""
    if ds["state"] == "data_pending":
        return STAGE_DATA_PENDING, None
    selected = _select_context(db, instrument_id, observations)
    if selected is None:
        return STAGE_WAIT_HTF, None
    sc = db.get_active_ltf_scenario(selected.id)
    if sc is None:
        return STAGE_WAIT_BOS, _contexts_direction(observations)
    direction = sc.direction.value
    rng = db.get_current_ltf_range(sc.id)
    if rng is None:
        return STAGE_WAIT_RANGE, direction
    zones = [z for z in eligible_zones if z["scenario_id"] == sc.id]
    if not zones:
        return STAGE_NO_ZONES, direction
    if fresh and price is not None and any(
        z["lower"] <= price <= z["upper"] for z in zones
    ):
        return STAGE_IN_ENTRY, direction
    half = "Premium" if direction == "bear" else "Discount"
    return f"Возврат в {half}", direction


def _last_cancellation(
    db: Database, observation_id: int
) -> Optional[dict[str, Any]]:
    cancelled = [
        s for s in db.list_ltf_scenarios(observation_id=observation_id)
        if s.cancelled_at is not None
    ]
    if not cancelled:
        return None
    last = max(cancelled, key=lambda s: s.cancelled_at)
    return {
        "scenario_id": last.id,
        "reason": last.cancellation_reason,
        "cancelled_at": last.cancelled_at,
    }


def _pivot_brief(p) -> dict[str, Any]:
    return {
        "id": p.id, "price": p.price,
        "pivot_at": p.pivot_at, "confirmed_at": p.confirmed_at,
    }


def _provisional_range_block(
    db: Database, settings, instrument_id: int, sc: LtfScenario, now: int
) -> Optional[dict[str, Any]]:
    """§16.2 (предлагаемый режим, выключен по умолчанию): предварительный
    диапазон — временный конец текущего движения до подтверждения опоры
    тремя правыми свечами.

    Чистый read-only слой ответа: НЕ пишется в ltf_range, не входит в
    counts/eligible_entries, не участвует в eligibility, отмене сценария
    и уведомлениях. Подтверждённый расчёт (поле range) остаётся основным."""
    obs = db.get_ltf_observation(sc.observation_id)
    # горизонт — как у chart-эндпоинта: структура до касания HTF (§4)
    base = obs.activated_at if obs is not None else now
    since = base - settings.detector.ltf_history_days * 86_400_000
    pivots = [
        PivotCandidate(
            instrument_id=p.instrument_id, price=p.price, kind=p.kind,
            pivot_at=p.pivot_at, candle_open_time=p.candle_open_time,
            confirmed_at=p.confirmed_at or 0, left=p.left, right=p.right,
            state=p.state, pivot_id=p.id, role=p.role,
        )
        for p in db.list_ltf_pivots(instrument_id, since_ms=since)
    ]
    candles = db.get_candles(instrument_id, "H1", start_ms=since)
    draft = provisional_range(pivots, candles, sc.direction, now)
    if draft is None:
        return None
    return {
        "range_status": "provisional",  # §16.2: отдельный range_status
        # предлагаемый режим: владельцем отдельно не подтверждён (§16.2)
        "proposed_mode": True,
        "direction": draft.direction.value,
        "lower": draft.lower,
        "upper": draft.upper,
        "mid": draft.mid,
        **draft.evidence,
        "computed_at": now,
    }


def _level_broken(candles, level: float, since_ms: int, bear: bool) -> bool:
    """Уровень уже пробит закрытой H1 после подтверждения опорного pivot.

    Тот же критерий, что у detect_breaks (§6.1/§6.2): строгое закрытие за
    уровнем. Пробитый уровень «ожидаемым» не является — read-модель не должна
    показывать слом, который движок уже зафиксировал."""
    for c in candles:
        if c.open_time < since_ms:
            continue
        if bear and c.close < level:
            return True
        if not bear and c.close > level:
            return True
    return False


def _expected_levels(db: Database, obs: LtfObservation, settings) -> dict[str, Any]:
    """Ожидаемые уровни слома из подтверждённых pivots (§6.1–§6.4).

    bear: BOS — закрытие H1 строго ниже опорного HL, назначенного перед
    последним HH (как roles.py: откат перед HH получает роль HL); SMS —
    internal_low выше этого HL. Bull — зеркально (LL → опорный LH,
    internal_high ниже него). None, когда якорной структуры ещё нет или
    уровень уже пробит закрытой H1 (слом свершился — ожидания нет).
    """
    empty: dict[str, Any] = {"bos": None, "sms": None}
    since = obs.activated_at - settings.detector.ltf_history_days * 86_400_000
    pivots = [
        p for p in db.list_ltf_pivots(obs.instrument_id, since_ms=since)
        if p.state == "confirmed"
    ]
    bear = obs.direction.value == "bear"
    anchor_role = "HH" if bear else "LL"
    internal_role = "internal_low" if bear else "internal_high"
    ref_kind = "low" if bear else "high"
    anchor_idx = None
    for i, p in enumerate(pivots):
        if p.role == anchor_role:
            anchor_idx = i
    if anchor_idx is None:
        return empty
    ref = None
    for p in reversed(pivots[:anchor_idx]):
        if p.kind == ref_kind:
            ref = p
            break
    if ref is None:
        return empty
    candles = db.get_candles(obs.instrument_id, "H1", start_ms=ref.pivot_at)
    bos = None
    ref_since = ref.confirmed_at if ref.confirmed_at is not None else ref.pivot_at
    if not _level_broken(candles, ref.price, ref_since, bear):
        bos = {
            "direction": obs.direction.value,
            "level": ref.price,
            "ref_pivot": _pivot_brief(ref),
        }
    sms = None
    for p in pivots[anchor_idx + 1:]:
        if p.role != internal_role:
            continue
        p_since = p.confirmed_at if p.confirmed_at is not None else p.pivot_at
        if bear and p.price > ref.price:
            if not _level_broken(candles, p.price, p_since, bear):
                sms = {"level": p.price, "internal_pivot": _pivot_brief(p)}
        elif not bear and p.price < ref.price:
            if not _level_broken(candles, p.price, p_since, bear):
                sms = {"level": p.price, "internal_pivot": _pivot_brief(p)}
    return {"bos": bos, "sms": sms}


def _half_label(
    zone: LtfEntryZone, rng, direction: str
) -> str:
    """§3.4: в какой половине текущего диапазона зона: premium|discount|none."""
    return zone_half(zone.lower, zone.upper, zone.is_level, rng, direction)


def _dist(zone: LtfEntryZone, price: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """§3.4: dist_abs = max(L−P, 0, P−U) для диапазона, abs(P−K) для уровня."""
    if price is None or price <= 0:
        return None, None
    if zone.is_level:
        dist_abs = abs(price - zone.lower)
    else:
        dist_abs = max(zone.lower - price, 0.0, price - zone.upper)
    return dist_abs, 100 * dist_abs / price


def _entry_row(
    db: Database, entry: LtfScenarioEntry, zone: LtfEntryZone, sc: LtfScenario,
    price: Optional[float],
) -> dict[str, Any]:
    rng = db.get_current_ltf_range(sc.id)
    dist_abs, dist_pct = _dist(zone, price)
    liquidity_state = None
    if zone.type in ("BSL", "SSL"):
        # §3.4: «снятие ожидает закрытия H1» отличается от «подтверждено»
        tests = [t for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
                 if t.entry_zone_id == zone.id]
        if tests:
            liquidity_state = tests[-1].state
    # история ручных оценок зоны (разметка; на состояние зоны не влияет)
    assessments = db.get_ltf_assessments(zone.id)
    reason = entry_reason(entry)
    return {
        "entry_id": entry.id,
        "entry_zone_id": zone.id,
        "type": zone.type,
        "direction": zone.direction.value,
        "lower": zone.lower,
        "upper": zone.upper,
        "is_level": zone.is_level,
        "mid": zone.mid,
        "formed_at": zone.formed_at,
        "confirmed_at": zone.confirmed_at,
        "half": _half_label(zone, rng, sc.direction.value),
        "overlap": entry.overlap,
        "partial": entry.overlap == "partial",
        "eligible": entry.eligible,
        "state": entry.state,
        "reason": reason,
        # §18: контекстный допуск FVG вне Premium — отображается, не блокирует
        "outside_premium": (
            reason == REASON_OUTSIDE_PD
            and context_complete(_context_flags(db, sc.id))
        ),
        "max_test_depth": zone.max_test_depth,
        "first_test_at": zone.first_test_at,
        "liquidity_state": liquidity_state,
        "dist_abs": dist_abs,
        "dist_pct": dist_pct,
        "range_version": entry.range_version,
        "outdated": rng is not None and entry.range_version != rng.version,
        "source_candle_ids": (
            zone.evidence.get("fvg_candles")
            or zone.evidence.get("base_candles")
            or []
        ),
        "reviews": [r.to_dict() for r in db.get_ltf_reviews(zone.id)],
        "latest_assessment": (
            assessments[-1].to_dict() if assessments else None
        ),
    }


def register_ltf_routes(app, db: Database, settings, require_auth, ltf_engine=None) -> None:
    """Регистрирует маршруты окна LTF на существующем приложении."""
    from fastapi import Depends

    # ------------------------- наблюдения (§3.2) -------------------------

    @app.get("/api/ltf/observations", dependencies=[Depends(require_auth)])
    def ltf_observations(
        tab: str = "active",
        instrument_id: Optional[int] = None,
        htf_tf: Optional[str] = None,
        direction: Optional[str] = None,
        trigger: Optional[str] = None,
        entry_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        observations = db.list_ltf_observations(instrument_id=instrument_id)
        out = []
        for obs in observations:
            if tab == "active" and obs.state not in _ACTIVE_STATES:
                continue
            if tab == "history" and obs.state not in _HISTORY_STATES:
                continue
            if direction is not None and obs.direction.value != direction:
                continue
            zone = db.get_zone(obs.zone_id)
            if htf_tf is not None and (zone is None or zone.timeframe != htf_tf):
                continue
            sc = _active_or_last_scenario(db, obs.id)
            if trigger is not None and (sc is None or sc.trigger != trigger):
                continue
            fresh = _fresh_entries(db, sc.id) if sc is not None else []
            if entry_type is not None and not any(
                z.type == entry_type for _, z in fresh
            ):
                continue
            out.append({
                **obs.to_dict(),
                "instrument": _instrument_brief(db, obs.instrument_id),
                "parent_zone": _zone_brief(db, obs.zone_id),
                "scenario": sc.to_dict() if sc is not None else None,
                "fresh_entries": len(fresh),
            })
        out.sort(key=lambda o: o["activated_at"], reverse=True)
        return out

    # ------------------------- карточка (§3.3) -------------------------

    @app.get("/api/ltf/observations/{observation_id}",
             dependencies=[Depends(require_auth)])
    def ltf_observation_card(observation_id: int) -> dict[str, Any]:
        obs = db.get_ltf_observation(observation_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        scenarios = db.list_ltf_scenarios(observation_id=obs.id)
        # ТЗ «LTF Current Setup» §8/§4.4: active_scenario — только живой
        # сценарий; отменённый доступен в истории (scenarios/last_cancellation
        # и журнале), а в текущей карточке — ожидание нового сценария
        sc = db.get_active_ltf_scenario(obs.id)
        sc_block = _scenario_block(db, sc) if sc is not None else None
        return {
            "observation": obs.to_dict(),
            "instrument": _instrument_brief(db, obs.instrument_id),
            "parent_zone": _zone_brief(db, obs.zone_id),
            "active_scenario": sc_block,
            "awaiting_new_scenario": (
                sc is None and obs.state in _ACTIVE_STATES
            ),
            "last_cancellation": _last_cancellation(db, obs.id),
            "scenarios": [s.to_dict() for s in scenarios],
            "expected": _expected_levels(db, obs, settings),
            "state_version": _state_version(db, obs, sc),
        }

    # ------------------------- слои графика (§3.5) -------------------------

    @app.get("/api/ltf/observations/{observation_id}/chart",
             dependencies=[Depends(require_auth)])
    def ltf_observation_chart(observation_id: int) -> dict[str, Any]:
        obs = db.get_ltf_observation(observation_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        zone = db.get_zone(obs.zone_id)
        scenarios = db.list_ltf_scenarios(observation_id=obs.id)
        sc = db.get_active_ltf_scenario(obs.id) or (
            scenarios[-1] if scenarios else None
        )
        # окно наблюдения: структура до касания HTF — тот же horizon,
        # что при подгрузке истории (§4)
        since = obs.activated_at - settings.detector.ltf_history_days * 86_400_000
        pivots = [
            p.to_dict()
            for p in db.list_ltf_pivots(obs.instrument_id, since_ms=since)
        ]
        # структурные события — только активного (или последнего) сценария:
        # события отменённых сценариев на график не выводятся
        structure_events = (
            [e.to_dict() for e in db.list_ltf_structure_events(sc.id)]
            if sc is not None else []
        )
        structure_events.sort(key=lambda e: e["occurred_at"])
        ranges = []
        entries: list[dict[str, Any]] = []
        entries_excluded: list[dict[str, Any]] = []
        liquidity_tests: list[dict[str, Any]] = []
        if sc is not None:
            current = db.get_current_ltf_range(sc.id)
            ranges = [
                {**r.to_dict(),
                 "current": current is not None and r.id == current.id}
                for r in db.list_ltf_ranges(sc.id)
            ]
            ver = current.version if current is not None else None
            sc_entries = [
                e for e in db.list_ltf_scenario_entries(sc.id)
                if e.state != "invalid"
            ]
            if sc_entries:
                # нет строк текущей версии — показываем строки последней
                # версии, для которой они есть, а не пустой/старый набор
                if ver is None or not any(
                    e.range_version == ver for e in sc_entries
                ):
                    ver = max(e.range_version for e in sc_entries)
                sc_entries = [e for e in sc_entries if e.range_version == ver]
            allow_outside = context_complete(_context_flags(db, sc.id))
            for e in sc_entries:
                z = db.get_ltf_entry_zone(e.entry_zone_id)
                if z is None:
                    continue
                reason = entry_reason(e)
                admitted = reason == REASON_OK or (
                    allow_outside and reason == REASON_OUTSIDE_PD
                    and z.type == "FVG"
                )
                row = {
                    **z.to_dict(),
                    "entry_zone_id": z.id,
                    "entry_state": e.state,
                    "overlap": e.overlap,
                    "eligible": e.eligible,
                    "reason": reason,
                    "half": _half_label(z, current, sc.direction.value),
                    # §18: контекстный допуск FVG вне Premium
                    "outside_premium": admitted and reason == REASON_OUTSIDE_PD,
                }
                # ТЗ §10/§4.3: рабочий слой — только подходящие зоны
                # (§18: и контекстно допущенные FVG вне Premium); исключённые
                # доступны отдельной группой (слой «Исключённые зоны»
                # включается в UI отдельно)
                (entries if admitted else entries_excluded).append(row)
            liquidity_tests = [
                t.to_dict() for t in db.list_ltf_liquidity_tests(scenario_id=sc.id)
            ]
        return {
            "timeframe": "H1",
            "parent_zone": (
                {
                    **_zone_brief(db, zone.id),
                    "display_from": zone.display_from or zone.formed_at,
                    "display_until": zone.display_until,
                }
                if zone is not None else None
            ),
            "pivots": pivots,          # state=candidate|confirmed|ambiguous (§3.5)
            "structure_events": structure_events,
            "ranges": ranges,
            "entries": entries,        # подходящие, полные границы (§8.5/§10)
            "entries_excluded": entries_excluded,  # исключённые с reason (§10)
            "liquidity_tests": liquidity_tests,
            "expected": _expected_levels(db, obs, settings),
        }

    # ------------------- read model «LTF Current Setup» (§14) -------------------

    @app.get("/api/ltf/instruments", dependencies=[Depends(require_auth)])
    def ltf_instruments() -> list[dict[str, Any]]:
        """Левая панель «Активы» (§4.2): одна строка на instrument_id
        (symbol/venue/market), сколько бы Observation ни было у инструмента.
        Счётчики и последние события — агрегатными запросами, без N+1."""
        now = now_ms()
        by_instrument: dict[int, list[LtfObservation]] = {}
        for o in db.list_ltf_observations():
            by_instrument.setdefault(o.instrument_id, []).append(o)
        instruments = {i.id: i for i in db.get_instruments()}
        for ins in instruments.values():
            # «Анализировать» без наблюдений — тоже строка («Ждём HTF-зону»)
            if ins.enabled and ins.ltf_analyze:
                by_instrument.setdefault(ins.id, [])
        quotes = db.get_all_quotes()
        eligible_rows = db.list_ltf_eligible_zones()
        eligible_by_instrument: dict[int, list[dict[str, Any]]] = {}
        for row in eligible_rows:
            eligible_by_instrument.setdefault(
                row["instrument_id"], []
            ).append(row)
        last_event = db.get_ltf_last_event_at()
        out = []
        for iid, obs_list in by_instrument.items():
            ins = instruments.get(iid)
            if ins is None:
                continue
            quote = quotes.get(iid)
            ds = _data_state(db, settings, iid, quote, now)
            fresh = ds["state"] == "ok"
            eligible = eligible_by_instrument.get(iid, [])
            stage, direction = _instrument_stage(
                db, iid, obs_list, eligible,
                quote[0] if quote else None, fresh, ds,
            )
            selected = _select_context(db, iid, obs_list)
            htf_context = None
            if selected is not None:
                zone = db.get_zone(selected.zone_id)
                if zone is not None:
                    htf_context = {
                        "type": zone.type.value, "timeframe": zone.timeframe,
                    }
            out.append({
                "instrument": {
                    "id": ins.id, "symbol": ins.symbol, "asset": ins.asset,
                    "venue": ins.venue, "market_type": ins.market_type,
                },
                "stage": stage,
                "direction": direction,
                "htf_context": htf_context,
                "last_event_at": last_event.get(iid) or max(
                    (o.updated_at for o in obs_list), default=None
                ),
                "eligible_count": len(eligible),
                "contexts_count": sum(
                    1 for o in obs_list if o.state in _ACTIVE_STATES
                ),
                "data_state": ds,
            })
        out.sort(key=lambda r: (r["instrument"]["symbol"],
                                r["instrument"]["venue"],
                                r["instrument"]["market_type"]))
        return out

    @app.get("/api/ltf/instruments/{instrument_id}/current",
             dependencies=[Depends(require_auth)])
    def ltf_instrument_current(instrument_id: int) -> dict[str, Any]:
        """InstrumentCurrentView (§14): согласованный снимок «здесь и сейчас».
        current_scenario — только не отменённый; отменённый — в истории
        (journal/observations). После reconnect тот же снимок: отменённый
        сценарий текущим не удерживается."""
        ins = db.get_instrument(instrument_id)
        if ins is None:
            raise HTTPException(status_code=404, detail="Инструмент не найден")
        now = now_ms()
        quote = db.get_quote(instrument_id)
        price = quote[0] if quote else None
        ds = _data_state(db, settings, instrument_id, quote, now)
        fresh = ds["state"] == "ok"
        observations = db.list_ltf_observations(instrument_id=instrument_id)
        selected = _select_context(db, instrument_id, observations, price, fresh)
        contexts = [
            _context_view(db, o, price, fresh)
            for o in observations if o.state in _ACTIVE_STATES
        ]
        sc = (
            db.get_active_ltf_scenario(selected.id)
            if selected is not None else None
        )
        eligible_all = db.list_ltf_eligible_zones()
        stage, direction = _instrument_stage(
            db, instrument_id, observations,
            [z for z in eligible_all if z["instrument_id"] == instrument_id],
            price, fresh, ds,
        )
        sc_block: Optional[dict[str, Any]] = None
        rng_block: Optional[dict[str, Any]] = None
        eligible_rows: list[dict[str, Any]] = []
        counts = {"eligible": 0, "excluded": 0, "historical": 0}
        if sc is not None:
            sc_block = _scenario_block(db, sc)
            if sc_block["range"] is not None:
                rng_block = {**sc_block["range"],
                             "anchors": sc_block["anchors"]}
            # counts — по тем же строкам, что в ответе (п.19): как
            # /entries?view=eligible|excluded|history текущей версии
            current = db.get_current_ltf_range(sc.id)
            entries = [
                e for e in db.list_ltf_scenario_entries(sc.id)
                if e.state != "invalid"
            ]
            if current is not None:
                ver = current.version
            elif entries:
                ver = max(e.range_version for e in entries)
            else:
                ver = 0
            allow_outside = context_complete(_context_flags(db, sc.id))
            for e in entries:
                if e.range_version != ver:
                    counts["historical"] += 1
                    continue
                reason = entry_reason(e)
                z = None
                if reason == REASON_OK:
                    z = db.get_ltf_entry_zone(e.entry_zone_id)
                elif allow_outside and reason == REASON_OUTSIDE_PD:
                    # §18: контекстный допуск FVG вне Premium
                    z0 = db.get_ltf_entry_zone(e.entry_zone_id)
                    if z0 is not None and z0.type == "FVG":
                        z = z0
                if z is not None:
                    eligible_rows.append(
                        _entry_row(db, e, z, sc, price if fresh else None)
                    )
                elif reason != REASON_OK:
                    counts["excluded"] += 1
            counts["eligible"] = len(eligible_rows)
            eligible_rows.sort(key=lambda r: (
                _ENTRY_ORDER.get(r["type"], 9), r["confirmed_at"] or 0
            ))
        waiting: Optional[dict[str, Any]] = None
        if sc is None and selected is not None:
            waiting = {
                "status": "awaiting_new_scenario",
                # отменённый сценарий — только в истории (§8)
                "last_cancellation": _last_cancellation(db, selected.id),
            }
        last_closed = db.last_candle(instrument_id, "H1")
        last_processed = db.get_meta(f"ltf:h1:last_close:{instrument_id}")
        return {
            "instrument": _instrument_brief(db, instrument_id),
            "price": price,
            "quote_at": quote[1] if quote else None,
            "last_closed_h1": (
                last_closed.open_time if last_closed is not None else None
            ),
            "last_processed_h1": (
                int(last_processed) if last_processed else None
            ),
            "data_state": ds,
            "stage": stage,
            "direction": direction,
            "contexts": contexts,
            "selected_context_id": selected.id if selected else None,
            "current_scenario": sc_block,
            "scenario_waiting": waiting,
            "range": rng_block,
            # §16.2 (предлагаемый режим, выключен по умолчанию): отдельное
            # read-only поле; при выключенной настройке всегда None и
            # подтверждённых сигналов/версий/уведомлений не создаёт
            "provisional_range_enabled": (
                settings.detector.ltf_provisional_range_enabled
            ),
            "provisional_range": (
                _provisional_range_block(db, settings, instrument_id, sc, now)
                if (settings.detector.ltf_provisional_range_enabled
                    and sc is not None)
                else None
            ),
            "eligible_entries": eligible_rows,
            "counts": counts,
            "state_version": (
                _state_version(db, selected, sc)
                if selected is not None
                else max((o.updated_at for o in observations), default=0)
            ),
        }

    @app.post("/api/ltf/instruments/{instrument_id}/select-context",
              dependencies=[Depends(require_auth)])
    def ltf_select_context(
        instrument_id: int, body: LtfSelectContextIn
    ) -> dict[str, Any]:
        """Ручной выбор контекста (§7): сохраняется в meta и удерживается,
        пока контекст доступен; ушедший в историю — fallback-политика."""
        obs = db.get_ltf_observation(body.observation_id)
        if obs is None or obs.instrument_id != instrument_id:
            raise HTTPException(status_code=404, detail="Контекст не найден")
        if obs.state not in _ACTIVE_STATES:
            raise HTTPException(
                status_code=409,
                detail="Контекст в истории — ручной выбор недоступен",
            )
        db.set_meta(f"ltf:selected_context:{instrument_id}", str(obs.id))
        return {"selected_context_id": obs.id}

    # ------------------------- журнал наблюдения (§3.2) -------------------------

    @app.get("/api/ltf/observations/{observation_id}/journal",
             dependencies=[Depends(require_auth)])
    def ltf_observation_journal(observation_id: int) -> list[dict[str, Any]]:
        """Все события наблюдения, включая события отменённых сценариев
        (§6.5: отмена не стирает историю); нужен, когда активного
        сценария ещё/уже нет."""
        obs = db.get_ltf_observation(observation_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        events = db.list_ltf_events(observation_id=obs.id, limit=1000)
        return [
            e.to_dict()
            for e in sorted(events, key=lambda e: (e.occurred_at, e.id))
        ]

    # ------------------------- таблица Entry Zones (§3.4) -------------------------

    @app.get("/api/ltf/scenarios/{scenario_id}/entries",
             dependencies=[Depends(require_auth)])
    def ltf_scenario_entries(
        scenario_id: int, price: Optional[float] = None,
        include_all: bool = False, view: str = "eligible",
    ) -> list[dict[str, Any]]:
        """Таблица Entry Zones сценария.

        view (ТЗ «LTF Current Setup» §10/§12): eligible — только подходящие
        (reason == "ok", по умолчанию); excluded — исключённые с причиной
        (reason != "ok"); history — все строки всех версий (история причин).
        invalid-зоны скрыты во всех представлениях (рыночно неактуальны)."""
        if view not in ("eligible", "excluded", "history"):
            raise HTTPException(
                status_code=400,
                detail="view: eligible | excluded | history",
            )
        sc = db.get_ltf_scenario(scenario_id)
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        current = db.get_current_ltf_range(sc.id)
        entries = [
            e for e in db.list_ltf_scenario_entries(sc.id)
            if e.state != "invalid"
        ]
        if current is not None:
            ver = current.version
        elif entries:
            # диапазона ещё нет — последняя версия среди строк сценария,
            # иначе фильтр по несуществующей v0 скрывал бы всё
            ver = max(e.range_version for e in entries)
        else:
            ver = 0
        rows = []
        for e in entries:
            ok = entry_reason(e) == REASON_OK
            if view == "eligible" and not ok:
                continue
            if view == "excluded" and ok:
                continue
            if view != "history" and not include_all and e.range_version != ver:
                continue
            z = db.get_ltf_entry_zone(e.entry_zone_id)
            if z is not None:
                rows.append(_entry_row(db, e, z, sc, price))
        rows.sort(key=lambda r: (_ENTRY_ORDER.get(r["type"], 9),
                                 r["confirmed_at"] or 0))
        return rows

    # ------------------------- журнал (§3.2) -------------------------

    @app.get("/api/ltf/scenarios/{scenario_id}/journal",
             dependencies=[Depends(require_auth)])
    def ltf_scenario_journal(scenario_id: int) -> list[dict[str, Any]]:
        sc = db.get_ltf_scenario(scenario_id)
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        events = {
            e.id: e
            for e in db.list_ltf_events(scenario_id=sc.id, limit=1000)
        }
        for e in db.list_ltf_events(observation_id=sc.observation_id, limit=1000):
            events.setdefault(e.id, e)
        return [
            e.to_dict()
            for e in sorted(events.values(),
                            key=lambda e: (e.occurred_at, e.id))
        ]

    # ------------------------- ручное завершение (§12) -------------------------

    @app.post("/api/ltf/scenarios/{scenario_id}/close",
              dependencies=[Depends(require_auth)])
    def ltf_scenario_close(scenario_id: int) -> dict[str, Any]:
        sc = db.get_ltf_scenario(scenario_id)
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        if sc.state in ("cancelled", "closed"):
            raise HTTPException(status_code=409,
                                detail="Сценарий уже завершён")
        if ltf_engine is None:
            raise HTTPException(status_code=503, detail="LTF-модуль выключен")
        # родительская HTF-зона не трогается (§12) — внутри метода движка
        ltf_engine.close_scenario_manually(scenario_id)
        return db.get_ltf_scenario(scenario_id).to_dict()

    # ------------------------- разметка Entry Zones -------------------------

    @app.post("/api/ltf/entry-zones/{entry_zone_id}/review",
              dependencies=[Depends(require_auth)])
    def ltf_entry_zone_review(
        entry_zone_id: int, body: LtfReviewIn
    ) -> dict[str, Any]:
        """Оценка Entry Zone с раздельными вердиктами (аналог §15.3 HTF).

        Оценка ТОЛЬКО фиксируется: validity зоны, границы, first_test_at и
        привязки ltf_scenario_entry не меняются, движок не перезапускается.
        """
        zone = db.get_ltf_entry_zone(entry_zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Entry Zone не найдена")
        # обратная совместимость старых решений (как на HTF-ревью, R13)
        decision = LTF_LEGACY_DECISIONS.get(body.decision, body.decision)
        if decision not in LTF_REVIEW_DECISIONS:
            raise HTTPException(
                status_code=400,
                detail="decision: correct | now_irrelevant | fix_boundaries | "
                       "wrong_base | wrong_type | no_context | wrong "
                       "(legacy: confirmed | rejected | corrected)",
            )
        # wrong без кода причины — 'unknown', а не выдуманная причина
        reason_code = body.reason_code or (
            "unknown" if decision == "wrong" else decision
        )
        now = now_ms()
        # replay не нужен — состояние не меняем; фиксируем только опору
        # оценки: open_time последней закрытой H1-свечи инструмента
        last_h1 = db.last_candle(zone.instrument_id, "H1")
        assessed_as_of = last_h1.open_time if last_h1 is not None else now
        geometry_verdict = "unknown"
        requires_clarification = False
        corrected_lower: Optional[float] = None
        corrected_upper: Optional[float] = None

        if decision in ("correct", "now_irrelevant"):
            geometry_verdict = "valid"
        elif decision in ("wrong_base", "wrong_type", "wrong"):
            geometry_verdict = "invalid"
        elif decision == "fix_boundaries":
            if body.lower is None or body.upper is None:
                raise HTTPException(
                    status_code=422,
                    detail="fix_boundaries требует lower и upper",
                )
            geometry_verdict = "needs_correction"
            corrected_lower = float(body.lower)
            corrected_upper = float(body.upper)
        elif decision == "no_context":
            # нет контекста — невозможность оценки, а не ошибка геометрии
            requires_clarification = True

        # жизненный цикл зоны на LTF — первое касание (§9 LTF-спеки)
        lifecycle_verdict = "tested" if zone.first_test_at else None

        # решение сохраняем как нажато (история не теряется)
        review = LtfReview(
            id=None, entry_zone_id=entry_zone_id,
            scenario_id=body.scenario_id, decision=body.decision,
            text=body.text, created_at=now,
        )
        review.id = db.add_ltf_review(review)
        assessment = LtfReviewAssessment(
            id=None, entry_zone_id=entry_zone_id, review_id=review.id,
            review_decision=decision, geometry_verdict=geometry_verdict,
            lifecycle_verdict=lifecycle_verdict, reason_code=reason_code,
            evidence_source=body.evidence_source,
            assessed_as_of=assessed_as_of, reviewed_at=now,
            requires_clarification=requires_clarification,
            corrected_lower=corrected_lower, corrected_upper=corrected_upper,
        )
        assessment.id = db.add_ltf_assessment(assessment)

        # зону не трогаем; разметка — отдельным файлом ltf_labels.jsonl
        labels_file = export_ltf_label(
            db, zone, body.decision, body.text or "", settings,
            assessment=assessment, scenario_id=body.scenario_id,
        )
        return {
            "entry_zone": zone.to_dict(),
            "labels_file": labels_file,
            "review": review.to_dict(),
            "assessment": assessment.to_dict(),
            "reviews": [r.to_dict() for r in db.get_ltf_reviews(entry_zone_id)],
        }
