"""Read model «LTF Current Setup» (ТЗ §6, §7, §14) — сервисный слой.

Снимок «текущая ситуация по активам» без FastAPI-зависимостей: тела
эндпоинтов GET /api/ltf/instruments и GET /api/ltf/instruments/{id}/current
(см. app/web/ltf_api.py) вынесены сюда, чтобы read model мог вызывать и
Telegram-бот напрямую, без HTTP.
"""
from __future__ import annotations

from typing import Any, Optional

from ..db import Database
from ..engine.ltf.context import context_complete, context_flags
from ..engine.ltf.eligibility import (
    ADMISSION_CONTEXT,
    admitted_scenario_entries,
    evaluate_final,
)
from ..engine.ltf.entries import fvg_fill_status
from ..engine.ltf.breaks import expected_reverse_condition
from ..engine.ltf.pivots import PivotCandidate
from ..engine.ltf.ranges import provisional_range, zone_half
from ..models import TIMEFRAME_MINUTES, EventKind, ZoneStatus, ZoneType, now_ms
from .htf_parent import (
    PARENT_QUERY_STATUSES,
    PARENT_TIMEFRAMES,
    eligible_htf_parent,
    parent_decision,
    policy_types,
)
from ..models_ltf import (
    LtfEntryZone,
    LtfObservation,
    LtfScenario,
    LtfScenarioEntry,
)
from .quality import data_quality, quote_stale_limit_ms

# вкладка «active» списка наблюдений (§3.2)
_ACTIVE_STATES = {"waiting_structure", "active", "paused_data"}
_ENTRY_ORDER = {"FVG": 0, "OB": 1, "BSL": 2, "SSL": 3}

# этапы инструмента (ТЗ «LTF Current Setup» §4.2) — вычисляет сервер,
# фронт «текущий сценарий» самостоятельно не восстанавливает (§14)
STAGE_WAIT_HTF = "Ждём HTF-зону"
STAGE_H1_CALC = "HTF-зона достигнута, рассчитываем H1"
STAGE_IN_HTF = "Цена в HTF-зоне"
STAGE_WAIT_BOS = "Ждём BOS/SMS"
STAGE_WAIT_RANGE = "Ждём диапазон"
STAGE_RETRACEMENT = "Ожидаем возврат в Premium/Discount"
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


def _context_flags(db: Database, scenario_id: int) -> dict[str, Any]:
    """§18: агрегированный контекст сценария из событий context_update."""
    return context_flags(
        db.list_ltf_events(scenario_id=scenario_id, limit=1000)
    )


def _fresh_entries(
    db: Database, scenario_id: int
) -> list[tuple[LtfScenarioEntry, LtfEntryZone]]:
    """Подходящие зоны текущей версии диапазона (§3.2 счётчик).

    Отбор делегирован admitted_scenario_entries — единому источнику правила
    допуска (L01): reason == "ok" по обычному правилу; при полном контексте
    §18 — и контекстно допущенные FVG с reason == "outside_pd"."""
    return [
        (e, zone)
        for e, zone, _fe in admitted_scenario_entries(db, scenario_id)
    ]


# --------------------------------------------------------------------- #
# Read model «LTF Current Setup» (ТЗ §6, §7, §14)
# --------------------------------------------------------------------- #

def _data_state(
    db: Database, settings, instrument_id: int,
    quote: Optional[tuple[float, int]], now: int,
) -> dict[str, Any]:
    """Состояние данных инструмента (§14, D02): каналы раздельно — котировка
    (возраст/порог), последняя закрытая H1, флаг источника от воркера
    (meta stale:), восстановление (meta replaying:), курсор обработки и
    непрерывность истории. «ok» — только при подтверждённой свежести всех
    каналов; устаревшая котировка не даёт заявлять положение цены
    актуальным (§6). Пороги — настройкой stale_* (0/авто для котировки:
    2 интервала опроса).

    F03: тонкая обёртка над единой quality.data_quality — то же решение
    используют service_status и гейт доставки LTF; параметр quote
    сохранён для совместимости вызовов (котировку читает сама оценка)."""
    return data_quality(db, settings, instrument_id, now)


# Основания выбора контекста (L05): стабильные коды для снимка /current.
# Автовыбор — навигационная политика («что показать»), не доказательство
# силы сценария; в UI — нейтральная подпись «Показан контекст: …».
BASIS_MANUAL = "manual"                # «Выбран вручную»
BASIS_PRICE_INSIDE = "price_inside"    # «Цена внутри зоны»
BASIS_LAST_SCENARIO = "last_scenario"  # «Последний действующий сценарий»
BASIS_LAST_CONTACT = "last_contact"    # «Последний контакт с зоной»

# L06: группы приоритета внимания списка активов — по убыванию. Показывают
# необходимость внимания, НЕ вероятность успеха/оценку прибыльности.
ATTENTION_ORDER = [
    "review",         # «Требует проверки» — есть зоны-кандидаты (status candidate)
    "price_in_zone",  # «Цена в зоне» — свежая котировка внутри HTF-зоны
    "eligible",       # «Есть подходящие зоны»
    "awaiting",       # «Ожидается структура» — наблюдение без сценария/диапазона
    "data_problem",   # «Проблема данных» — data_state != ok
    "none",
]
ATTENTION_REASON_RU = {
    "review": "Требует проверки",
    "price_in_zone": "Цена в зоне",
    "eligible": "Есть подходящие зоны",
    "awaiting": "Ожидается структура",
    "data_problem": "Проблема данных",
    "none": "—",
}


def _select_context_with_basis(
    db: Database, instrument_id: int, observations: list[LtfObservation],
    price: Optional[float] = None, fresh: bool = False,
    policy=None, as_of: Optional[int] = None,
) -> tuple[Optional[LtfObservation], Optional[str]]:
    """Политика выбора контекста (§7, приоритет «цена внутри» согласован
    владельцем): ручной выбор (meta), если он ещё доступен → контекст, чья
    HTF-зона сейчас содержит цену (план «в реализации»: сначала с действующим
    сценарием, затем последний по активации) → контекст с последним
    действующим сценарием → последний валидный контакт. Правило навигации,
    не оценка торговой силы; ручной выбор, ушедший в историю, игнорируется
    (fallback-политика). При stale-котировке приоритет «цена внутри» не
    применяется: навигация по устаревшим данным недопустима.

    Возвращает (контекст, основание) — код BASIS_* или None, если активных
    контекстов нет."""
    active = [o for o in observations if o.state in _ACTIVE_STATES]
    if policy is not None:
        # F32: наблюдение с родителем, который больше не актуален,
        # текущим контекстом не выбирается. История строки сохраняется.
        active = [
            o for o in active
            if eligible_htf_parent(db.get_zone(o.zone_id), policy, as_of=as_of)
        ]
    if not active:
        return None, None
    raw = db.get_meta(f"ltf:selected_context:{instrument_id}")
    if raw:
        try:
            manual_id = int(raw)
        except ValueError:
            manual_id = None
        manual = next((o for o in active if o.id == manual_id), None)
        if manual is not None:
            return manual, BASIS_MANUAL

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
                return (max(sc_pairs,
                            key=lambda so: (so[0].created_at, so[0].id))[1],
                        BASIS_PRICE_INSIDE)
            return (max(inside, key=lambda o: (o.activated_at, o.id)),
                    BASIS_PRICE_INSIDE)
    with_scenario_pairs = with_scenario(active)
    if with_scenario_pairs:
        return (max(with_scenario_pairs,
                    key=lambda so: (so[0].created_at, so[0].id))[1],
                BASIS_LAST_SCENARIO)
    return (max(active, key=lambda o: (o.activated_at, o.id)),
            BASIS_LAST_CONTACT)


def _select_context(
    db: Database, instrument_id: int, observations: list[LtfObservation],
    price: Optional[float] = None, fresh: bool = False,
    policy=None, as_of: Optional[int] = None,
) -> Optional[LtfObservation]:
    """Выбор контекста без основания (совместимость); логика и docstring
    политики — в _select_context_with_basis."""
    return _select_context_with_basis(
        db, instrument_id, observations, price, fresh, policy, as_of
    )[0]


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
        # §5/§12: уровень отмены с происхождением (для живого сценария — None)
        "reverse_break": (
            {
                "price": sc.reverse_break_level_price,
                "pivot_id": sc.reverse_break_pivot_id,
                "confirmed_at": sc.reverse_break_confirmed_at,
            }
            if sc.reverse_break_level_price is not None else None
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
    """Единый маркер снимка (§14, D01): монотонный счётчик версии состояния
    state_seq — график, правая карточка, счётчик и таблица относятся к
    одному scenario_id/state_version; WS сообщает ту же версию, и по её
    возрастанию клиент перечитывает снимок после reconnect."""
    return db.get_state_seq()


def _contexts_direction(observations: list[LtfObservation]) -> Optional[str]:
    """Направление по контекстам; при конфликтующих — «mixed»
    (§7: «Разные HTF-контексты», не усредняем)."""
    dirs = {o.direction.value for o in observations if o.state in _ACTIVE_STATES}
    if not dirs:
        return None
    return next(iter(dirs)) if len(dirs) == 1 else "mixed"


def _scenario_counts(
    db: Database, sc: LtfScenario
) -> tuple[list[tuple[LtfScenarioEntry, LtfEntryZone]], dict[str, int]]:
    """Допуск привязок сценария на текущей версии диапазона — единый цикл
    (evaluate_final) для counts/eligible_entries снимка /current и для
    eligible_count списка активов (§13: один снимок для всего интерфейса).
    Возвращает (допущенные пары (entry, zone), counts как в /entries:
    eligible/excluded по текущей версии, historical — строки прошлых
    версий)."""
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
    tests = db.list_ltf_liquidity_tests(scenario_id=sc.id)
    eligible: list[tuple[LtfScenarioEntry, LtfEntryZone]] = []
    counts = {"eligible": 0, "excluded": 0, "historical": 0}
    for e in entries:
        if e.range_version != ver:
            counts["historical"] += 1
            continue
        z = db.get_ltf_entry_zone(e.entry_zone_id)
        if z is None:
            continue
        fe = evaluate_final(
            e, z, allow_outside=allow_outside, liquidity_tests=tests,
        )
        if fe.eligible_now:
            eligible.append((e, z))
        else:
            counts["excluded"] += 1
    counts["eligible"] = len(eligible)
    return eligible, counts


def _instrument_stage(
    db: Database,
    observations: list[LtfObservation],
    selected: Optional[LtfObservation],
    sc: Optional[LtfScenario],
    zones: list[LtfEntryZone],
    price: Optional[float],
    fresh: bool,
    ds: dict[str, Any],
) -> tuple[str, Optional[str]]:
    """Текущий этап инструмента (§4.2) — из того же результата допуска
    (zones — зоны, прошедшие evaluate_final), что counts/eligible_entries;
    при нехватке данных — data_pending, а не «нет сетапа» (§14).
    Возвращает (stage, direction)."""
    if ds["state"] == "data_pending":
        return STAGE_DATA_PENDING, None
    if selected is None:
        return STAGE_WAIT_HTF, None
    if sc is None:
        return STAGE_WAIT_BOS, _contexts_direction(observations)
    direction = sc.direction.value
    rng = db.get_current_ltf_range(sc.id)
    if rng is None:
        return STAGE_WAIT_RANGE, direction
    if not zones:
        return STAGE_NO_ZONES, direction
    if fresh and price is not None and any(
        z.lower <= price <= z.upper for z in zones
    ):
        return STAGE_IN_ENTRY, direction
    half = "Premium" if direction == "bear" else "Discount"
    # §11.3 ТЗ 07.10.2026: цена вне подходящей половины — ОЖИДАНИЕ возврата,
    # а не свершившийся возврат
    return f"Ожидаем возврат в {half}", direction


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
        # §5/§12: «почему отменён» — уровень обратного слома и его pivot
        "reverse_break_level_price": last.reverse_break_level_price,
        "reverse_break_pivot_id": last.reverse_break_pivot_id,
        "reverse_break_confirmed_at": last.reverse_break_confirmed_at,
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
    fe = evaluate_final(
        entry, zone,
        allow_outside=context_complete(_context_flags(db, sc.id)),
        liquidity_tests=db.list_ltf_liquidity_tests(scenario_id=sc.id),
    )
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
        # L01: итоговое решение — единая функция evaluate_final
        "eligible_now": fe.eligible_now,
        "reason": fe.primary_reason,
        "blocking_reasons": list(fe.blocking_reasons),
        "admission_basis": fe.admission_basis,
        # §18: контекстный допуск FVG вне Premium — отображается, не блокирует
        "outside_premium": fe.admission_basis == ADMISSION_CONTEXT,
        # §9 (Этап 5): точечный уровень (W=0) глубины не имеет — null,
        # UI показывает «—» вместо «0%»
        "max_test_depth": None if zone.is_level else zone.max_test_depth,
        # §10 (Этап 6): перекрытие FVG (open | partially_filled | filled)
        "fill_status": fvg_fill_status(zone),
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


# --------------------------------------------------------------------- #
# Публичный интерфейс read model
# --------------------------------------------------------------------- #

def _fmt_px(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _parent_zones(db: Database, instrument_id: int):
    return db.get_zones(
        instrument_id=instrument_id,
        statuses=list(PARENT_QUERY_STATUSES),
        timeframes=set(PARENT_TIMEFRAMES),
        types=[ZoneType.OB, ZoneType.FVG],
    )


def _zone_params(zone) -> dict[str, Any]:
    return {
        "zone_id": zone.id,
        "type": zone.type.value,
        "timeframe": zone.timeframe,
        "direction": zone.direction.value,
        "lower": zone.lower,
        "upper": zone.upper,
    }


def _type_name(zone) -> str:
    """Имя типа в тексте для человека: OB/FVG, не значение enum."""
    raw = zone.type.value if hasattr(zone.type, "value") else str(zone.type)
    return str(raw).upper()


def _disabled_type_message(zone, allowed) -> str:
    names = ", ".join(sorted(allowed)) or "—"
    return (
        f"Цена в {_type_name(zone)} {zone.timeframe} "
        f"{_fmt_px(zone.lower)}–{_fmt_px(zone.upper)}. "
        f"Анализ {_type_name(zone)} отключён; разрешены контексты {names}"
    )


def _cursors(db: Database, instrument_id: int) -> dict[str, Any]:
    def _i(key: str) -> Optional[int]:
        raw = db.get_meta(key)
        return int(raw) if raw else None
    return {
        "structure_last_processed_h1": _i(f"ltf:h1:last_close:{instrument_id}"),
        "scenario_last_processed_h1": _i(
            f"ltf:h1:scenario_last_close:{instrument_id}"
        ),
        "cursor": "split",
    }


def _review_state(count: int) -> dict[str, Any]:
    return {
        "needed": count > 0,
        "count": count,
        "label": "Нужна проверка" if count else None,
        "scope": "instrument",
    }


def _liquidity_facts(db: Database, instrument_id: int, limit: int = 8):
    """Снятия SSL/BSL — отдельные рыночные факты, не торговый сценарий."""
    facts = []
    for e in db.list_events_for_instrument(instrument_id, limit=300):
        if e.kind != EventKind.LEVEL_TAKEN:
            continue
        z = db.get_zone(e.zone_id)
        facts.append({
            "event_id": e.id,
            "kind": "level_taken",
            "instrument_id": instrument_id,
            "zone_id": e.zone_id,
            "type": z.type.value if z is not None else None,
            "timeframe": z.timeframe if z is not None else None,
            "level": e.price,
            "occurred_at": e.occurred_at,
            "source": "htf_scanner",
            "confirmation": "crossed",
            "creates_scenario": False,
        })
        if len(facts) >= limit:
            break
    return facts


def _context_wait(
    db: Database, settings, ins, observations, selected, price, ds, now: int,
) -> Optional[dict[str, Any]]:
    """Код причины отсутствия контекста (F22).

    Порядок: источник → анализ выключен → выбранный контекст (тогда None:
    structure_pending не подменяет рыночную стадию, data_state.reason
    остаётся processing_lag) → структура не посчитана → прежний родитель
    больше не актуален → цена в зоне выключенного типа → родитель есть,
    наблюдения ещё нет → ждём касания → подтверждённых родителей нет.
    Достигнутая выключенная зона при уже выбранном контексте — поле
    reached_disabled, не этот код."""
    cfg = settings.detector
    if ds.get("reason") in ("source_stale", "no_h1_candles"):
        return {
            "code": "source_unavailable",
            "message": "Источник данных недоступен или свечи H1 не поступили.",
            "params": {"data_reason": ds.get("reason")},
        }
    if not cfg.ltf_enabled or not ins.ltf_analyze:
        return {
            "code": "analysis_disabled",
            "message": "Анализ выключен для инструмента или глобально.",
            "params": {
                "ltf_enabled": bool(cfg.ltf_enabled),
                "ltf_analyze": bool(ins.ltf_analyze),
            },
        }
    if selected is not None:
        return None
    structure = _cursors(db, ins.id)["structure_last_processed_h1"]
    last_h1 = db.last_candle(ins.id, "H1")
    if last_h1 is not None and (structure is None or structure < last_h1.close_time):
        return {
            "code": "structure_pending",
            "message": "Свечи поступают; структура H1 ещё не рассчитана.",
            "params": {
                "last_closed_h1": last_h1.close_time,
                "structure_last_processed_h1": structure,
            },
        }
    stale_parents = [
        o for o in observations
        if o.state in _ACTIVE_STATES
        and not eligible_htf_parent(db.get_zone(o.zone_id), cfg, as_of=now)
    ]
    zones = _parent_zones(db, ins.id)
    allowed = policy_types(cfg)
    disabled_hit = None
    pending = None
    awaiting = None
    for zone in zones:
        decision = parent_decision(zone, cfg, as_of=now)
        inside = price is not None and zone.lower <= price <= zone.upper
        if decision.reason == "type_disabled" and inside and disabled_hit is None:
            disabled_hit = zone
        elif decision.eligible and inside and pending is None:
            pending = zone
        elif decision.eligible and awaiting is None:
            awaiting = zone
    if disabled_hit is not None and pending is None:
        params = _zone_params(disabled_hit)
        params["allowed"] = sorted(allowed)
        return {
            "code": "context_type_disabled",
            "message": _disabled_type_message(disabled_hit, allowed),
            "params": params,
        }
    if pending is not None:
        params = _zone_params(pending)
        return {
            "code": "observation_pending",
            "message": STAGE_H1_CALC,
            "params": params,
            "direction": pending.direction.value,
        }
    if stale_parents and awaiting is None and pending is None:
        return {
            "code": "parent_no_longer_relevant",
            "message": "Предыдущий контекст ушёл в историю.",
            "params": {"observation_ids": [o.id for o in stale_parents]},
        }
    if awaiting is not None and not ins.ltf_analyze:
        params = _zone_params(awaiting)
        return {
            "code": "awaiting_contact",
            "message": (
                f"Есть допустимая зона {_type_name(awaiting)} "
                f"{awaiting.timeframe} {_fmt_px(awaiting.lower)}–"
                f"{_fmt_px(awaiting.upper)}; ждём касания."
            ),
            "params": params,
        }
    if not any(parent_decision(z, cfg, as_of=now).eligible for z in zones):
        if disabled_hit is not None:
            params = _zone_params(disabled_hit)
            params["allowed"] = sorted(allowed)
            return {
                "code": "context_type_disabled",
                "message": _disabled_type_message(disabled_hit, allowed),
                "params": params,
            }
        return {
            "code": "no_confirmed_parent",
            "message": "Нет подтверждённых родителей разрешённых типов.",
            "params": {"allowed": sorted(allowed)},
        }
    if awaiting is not None:
        params = _zone_params(awaiting)
        return {
            "code": "awaiting_contact",
            "message": (
                f"Есть допустимая зона {_type_name(awaiting)} "
                f"{awaiting.timeframe} {_fmt_px(awaiting.lower)}–"
                f"{_fmt_px(awaiting.upper)}; ждём касания."
            ),
            "params": params,
        }
    return {
        "code": "no_confirmed_parent",
        "message": "Нет подтверждённых родителей разрешённых типов.",
        "params": {"allowed": sorted(allowed)},
    }


def _disabled_reached(db, settings, instrument_id, price, now: int):
    if price is None:
        return []
    cfg = settings.detector
    rows = []
    for zone in _parent_zones(db, instrument_id):
        if not (zone.lower <= price <= zone.upper):
            continue
        if parent_decision(zone, cfg, as_of=now).reason != "type_disabled":
            continue
        rows.append({
            **_zone_params(zone),
            "message": _disabled_type_message(zone, policy_types(cfg)),
        })
    return rows


def _cancel_condition(db, settings, obs, sc) -> dict[str, Any]:
    empty = {
        "status": "undefined", "source": "reverse_machine",
        "kind": None, "level": None, "side": None,
        "pivot_id": None, "confirmed_at": None,
    }
    if sc is None or obs is None:
        return empty
    if sc.reverse_break_level_price is not None:
        side = "above" if sc.direction.value == "bear" else "below"
        return {
            "status": "occurred",
            "source": "reverse_machine",
            "kind": sc.cancellation_reason,
            "level": sc.reverse_break_level_price,
            "side": side,
            "pivot_id": sc.reverse_break_pivot_id,
            "confirmed_at": sc.reverse_break_confirmed_at,
        }
    since = obs.activated_at - settings.detector.ltf_history_days * 86_400_000
    pivots = [
        PivotCandidate(
            instrument_id=p.instrument_id, price=p.price, kind=p.kind,
            pivot_at=p.pivot_at, candle_open_time=p.candle_open_time,
            confirmed_at=p.confirmed_at or 0, left=p.left, right=p.right,
            state=p.state, pivot_id=p.id, role=p.role,
        )
        for p in db.list_ltf_pivots(obs.instrument_id, since_ms=since)
    ]
    candles = db.get_candles(obs.instrument_id, "H1", start_ms=since)
    start = sc.created_at
    return expected_reverse_condition(
        pivots, candles, sc.direction, now_ms(),
        since_ms=since, cancel_not_before_ms=start,
    )


def _engine_state(db: Database, settings, ins) -> dict[str, Any]:
    """Состояние расчёта по инструменту: флаги, курсоры, разрешённые типы.
    Секретов и токенов здесь нет."""
    cfg = settings.detector
    return {
        "ltf_enabled": bool(cfg.ltf_enabled),
        "ltf_analyze": bool(ins.ltf_analyze),
        "replaying": db.get_meta(f"replaying:{ins.id}") == "1",
        "allowed_types": sorted(policy_types(cfg)),
        **_cursors(db, ins.id),
    }


def _finish_stage(
    stage: str, direction: Optional[str], selected, sc, zone,
    price: Optional[float], fresh: bool, wait: Optional[dict[str, Any]],
) -> tuple[str, Optional[str], str]:
    """stage остаётся прежней строкой для бота и существующих проверок.

    Исключение — наблюдения ещё нет, а цена уже в допустимом родителе:
    stage становится «HTF-зона достигнута, рассчитываем H1».
    waiting_structure сохраняет «Ждём BOS/SMS»; расчётная фраза уходит
    в market_stage. Цена внутри выбранного родителя со сценарием
    получает префикс «Цена в HTF-зоне»."""
    if selected is None and wait and wait.get("code") == "observation_pending":
        stage = STAGE_H1_CALC
        direction = wait.get("direction") or direction
    if selected is None:
        if wait and wait.get("code") == "observation_pending":
            market = STAGE_H1_CALC
        elif wait and wait.get("message"):
            market = wait["message"]
        else:
            market = stage
    elif sc is None and selected.state == "waiting_structure":
        market = STAGE_H1_CALC
    elif (
        fresh and price is not None and zone is not None
        and zone.lower <= price <= zone.upper
    ):
        market = f"{STAGE_IN_HTF}. {stage}"
    else:
        market = stage
    return stage, direction, market


def _direction_conflict(observations: list[LtfObservation]) -> Optional[dict[str, Any]]:
    if _contexts_direction(observations) != "mixed":
        return None
    return {
        "note": (
            "Активные контексты смотрят в разные стороны; "
            "направления не объединяются."
        ),
        "directions": sorted({
            o.direction.value for o in observations if o.state in _ACTIVE_STATES
        }),
    }


def _attention_group(
    db: Database, instrument_id: int,
    observations: list[LtfObservation], eligible_count: int,
    stage: str, ds: dict[str, Any],
    price: Optional[float], fresh: bool,
    has_candidates: bool,
    policy=None, as_of: Optional[int] = None,
) -> str:
    """L06: первая применимая группа из ATTENTION_ORDER (по убыванию
    приоритета). «Цена в зоне» — только по свежей котировке: при stale
    защита _select_context_with_basis приоритет «цена внутри» не применяет,
    и группа не присваивается."""
    if has_candidates:
        return "review"
    if fresh and price is not None:
        selected = _select_context(
            db, instrument_id, observations, price, fresh, policy, as_of,
        )
        if selected is not None:
            zone = db.get_zone(selected.zone_id)
            if zone is not None and zone.lower <= price <= zone.upper:
                return "price_in_zone"
    if eligible_count > 0:
        return "eligible"
    if stage in (STAGE_WAIT_BOS, STAGE_WAIT_RANGE):
        return "awaiting"
    if ds["state"] != "ok":
        return "data_problem"
    return "none"


def instruments_overview(db: Database, settings) -> list[dict[str, Any]]:
    """Левая панель «Активы» (§4.2): одна строка на instrument_id
    (symbol/venue/market), сколько бы Observation ни было у инструмента.
    eligible_count и stage — тем же циклом допуска (evaluate_final), что
    снимок /current выбранного контекста (§13: один снимок); последние
    события и счётчики кандидатов — агрегатными запросами."""
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
    last_event = db.get_ltf_last_event_at()
    # L06: зоны-кандидаты на проверку — одним агрегатным запросом (без N+1)
    candidate_counts = db.count_candidate_zones()
    out = []
    for iid, obs_list in by_instrument.items():
        ins = instruments.get(iid)
        if ins is None:
            continue
        quote = quotes.get(iid)
        price = quote[0] if quote else None
        ds = _data_state(db, settings, iid, quote, now)
        fresh = ds["state"] == "ok"
        policy = settings.detector
        selected = _select_context(
            db, iid, obs_list, price, fresh, policy, now,
        )
        sc = (
            db.get_active_ltf_scenario(selected.id)
            if selected is not None else None
        )
        eligible: list[tuple[LtfScenarioEntry, LtfEntryZone]] = []
        if sc is not None:
            eligible, _counts = _scenario_counts(db, sc)
        stage, direction = _instrument_stage(
            db, obs_list, selected, sc,
            [z for _, z in eligible], price, fresh, ds,
        )
        zone = db.get_zone(selected.zone_id) if selected is not None else None
        wait = _context_wait(
            db, settings, ins, obs_list, selected, price, ds, now,
        )
        stage, direction, market_stage = _finish_stage(
            stage, direction, selected, sc, zone, price, fresh, wait,
        )
        htf_context = None
        if zone is not None:
            htf_context = {
                "type": zone.type.value, "timeframe": zone.timeframe,
            }
        review_count = candidate_counts.get(iid, 0)
        attention = _attention_group(
            db, iid, obs_list, len(eligible), stage, ds,
            price, fresh,
            review_count > 0,
            policy, now,
        )
        out.append({
            "instrument": {
                "id": ins.id, "symbol": ins.symbol, "asset": ins.asset,
                "venue": ins.venue, "market_type": ins.market_type,
            },
            "stage": stage,
            "market_stage": market_stage,
            "direction": direction,
            "direction_conflict": _direction_conflict(obs_list),
            "htf_context": htf_context,
            "wait": wait,
            "review_state": _review_state(review_count),
            "engine_state": _engine_state(db, settings, ins),
            "reached_disabled": _disabled_reached(
                db, settings, iid, price, now,
            ),
            "last_event_at": last_event.get(iid) or max(
                (o.updated_at for o in obs_list), default=None
            ),
            "eligible_count": len(eligible),
            "contexts_count": sum(
                1 for o in obs_list if o.state in _ACTIVE_STATES
            ),
            "data_state": ds,
            # L06: приоритет внимания — код группы (ATTENTION_ORDER) и
            # краткая причина; НЕ оценка прибыльности сетапа
            "attention": attention,
            "attention_reason": ATTENTION_REASON_RU[attention],
        })
    out.sort(key=lambda r: (r["instrument"]["symbol"],
                            r["instrument"]["venue"],
                            r["instrument"]["market_type"]))
    return out


def instrument_current(
    db: Database, settings, instrument_id: int
) -> Optional[dict[str, Any]]:
    """InstrumentCurrentView (§14): согласованный снимок «здесь и сейчас».
    current_scenario — только не отменённый; отменённый — в истории
    (journal/observations). После reconnect тот же снимок: отменённый
    сценарий текущим не удерживается.

    None, если инструмент не найден (HTTP-слой отвечает 404)."""
    ins = db.get_instrument(instrument_id)
    if ins is None:
        return None
    now = now_ms()
    quote = db.get_quote(instrument_id)
    price = quote[0] if quote else None
    ds = _data_state(db, settings, instrument_id, quote, now)
    fresh = ds["state"] == "ok"
    observations = db.list_ltf_observations(instrument_id=instrument_id)
    policy = settings.detector
    selected, basis = _select_context_with_basis(
        db, instrument_id, observations, price, fresh, policy, now,
    )
    contexts = [
        _context_view(db, o, price, fresh)
        for o in observations
        if o.state in _ACTIVE_STATES
        and eligible_htf_parent(db.get_zone(o.zone_id), policy, as_of=now)
    ]
    sc = (
        db.get_active_ltf_scenario(selected.id)
        if selected is not None else None
    )
    sc_block: Optional[dict[str, Any]] = None
    rng_block: Optional[dict[str, Any]] = None
    eligible_rows: list[dict[str, Any]] = []
    eligible_zones: list[LtfEntryZone] = []
    counts = {"eligible": 0, "excluded": 0, "historical": 0}
    if sc is not None:
        sc_block = _scenario_block(db, sc)
        if sc_block["range"] is not None:
            rng_block = {**sc_block["range"],
                         "anchors": sc_block["anchors"]}
        # counts — по тем же строкам, что в ответе (п.19): как
        # /entries?view=eligible|excluded|history текущей версии; тот же
        # цикл допуска использует и список активов (§13)
        eligible, counts = _scenario_counts(db, sc)
        eligible_zones = [z for _, z in eligible]
        eligible_rows = [
            _entry_row(db, e, z, sc, price if fresh else None)
            for e, z in eligible
        ]
        eligible_rows.sort(key=lambda r: (
            _ENTRY_ORDER.get(r["type"], 9), r["confirmed_at"] or 0
        ))
    # этап — из того же результата допуска, что counts/eligible_entries
    # (§13: карточка, счётчик и таблица не расходятся внутри снимка)
    stage, direction = _instrument_stage(
        db, observations, selected, sc, eligible_zones, price, fresh, ds,
    )
    parent_zone = db.get_zone(selected.zone_id) if selected is not None else None
    wait = _context_wait(
        db, settings, ins, observations, selected, price, ds, now,
    )
    stage, direction, market_stage = _finish_stage(
        stage, direction, selected, sc, parent_zone, price, fresh, wait,
    )
    waiting: Optional[dict[str, Any]] = None
    if sc is None and selected is not None:
        waiting = {
            "status": "awaiting_new_scenario",
            # отменённый сценарий — только в истории (§8)
            "last_cancellation": _last_cancellation(db, selected.id),
        }
    last_closed = db.last_candle(instrument_id, "H1")
    last_processed = db.get_meta(f"ltf:h1:last_close:{instrument_id}")
    movements = db.list_ltf_movements(sc.id) if sc is not None else []
    return {
        "instrument": _instrument_brief(db, instrument_id),
        # §13: идентичность снимка — все панели интерфейса читают один
        # instrument/context/scenario/range/state_version
        "instrument_id": instrument_id,
        "context_id": selected.id if selected else None,
        "scenario_id": sc.id if sc is not None else None,
        "movement_id": movements[-1].id if movements else None,
        "structural_epoch_id": sc.structural_epoch_id if sc is not None else None,
        "range_version": rng_block["version"] if rng_block else None,
        "as_of": now,
        "price": price,
        "quote_at": quote[1] if quote else None,
        # F03: last_closed_h1 — close_time последней закрытой H1, в тех же
        # единицах, что курсор движка last_processed_h1 (раньше отдавался
        # open_time — сравнение «свеча обработана» было смещено на час)
        "last_closed_h1": (
            last_closed.close_time if last_closed is not None else None
        ),
        "last_processed_h1": (
            int(last_processed) if last_processed else None
        ),
        "data_state": ds,
        "stage": stage,
        "market_stage": market_stage,
        "direction": direction,
        "direction_conflict": _direction_conflict(observations),
        "wait": wait,
        "review_state": _review_state(
            db.count_candidate_zones().get(instrument_id, 0)
        ),
        "engine_state": _engine_state(db, settings, ins),
        "liquidity_facts": _liquidity_facts(db, instrument_id),
        "reached_disabled": _disabled_reached(
            db, settings, instrument_id, price, now,
        ),
        "cancel_condition": _cancel_condition(db, settings, selected, sc),
        "contexts": contexts,
        "selected_context_id": selected.id if selected else None,
        # L05: основание выбора (код BASIS_*) — навигационная политика,
        # не оценка силы сценария; None, когда активных контекстов нет
        "selected_context_basis": basis,
        # L05: среди активных контекстов есть противоположные направления
        # (direction тогда уже "mixed" — семантика сохранена)
        "contexts_conflict": _contexts_direction(observations) == "mixed",
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
        "state_version": _state_version(db, selected, sc),
    }


# --------------------------------------------------------------------- #
# Слои графика наблюдения (§3.5) — эндпоинт /chart делегирует сюда
# --------------------------------------------------------------------- #

def _pivot_brief(p) -> dict[str, Any]:
    return {
        "id": p.id, "price": p.price,
        "pivot_at": p.pivot_at, "confirmed_at": p.confirmed_at,
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


def observation_chart_layers(
    db: Database, settings, observation_id: int
) -> Optional[dict[str, Any]]:
    """Слои графика наблюдения (§3.5): свечной horizon от касания HTF,
    pivots, структурные события активного (или последнего) сценария,
    диапазоны, подходящие/исключённые entry-зоны, тесты ликвидности,
    ожидаемые уровни. None — наблюдение не найдено (HTTP-слой отвечает 404).
    Используется и веб-эндпоинтом, и рендером графика для Telegram-бота."""
    obs = db.get_ltf_observation(observation_id)
    if obs is None:
        return None
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
        tests = db.list_ltf_liquidity_tests(scenario_id=sc.id)
        for e in sc_entries:
            z = db.get_ltf_entry_zone(e.entry_zone_id)
            if z is None:
                continue
            fe = evaluate_final(
                e, z, allow_outside=allow_outside, liquidity_tests=tests,
            )
            row = {
                **z.to_dict(),
                "entry_zone_id": z.id,
                "entry_state": e.state,
                "overlap": e.overlap,
                "eligible": e.eligible,
                "eligible_now": fe.eligible_now,
                "reason": fe.primary_reason,
                "blocking_reasons": list(fe.blocking_reasons),
                "admission_basis": fe.admission_basis,
                "half": _half_label(z, current, sc.direction.value),
                # §18: контекстный допуск FVG вне Premium
                "outside_premium": (
                    fe.admission_basis == ADMISSION_CONTEXT
                ),
            }
            # ТЗ §10/§4.3: рабочий слой — только подходящие зоны
            # (§18: и контекстно допущенные FVG вне Premium); исключённые
            # доступны отдельной группой (слой «Исключённые зоны»
            # включается в UI отдельно)
            (entries if fe.eligible_now else entries_excluded).append(row)
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


def instrument_structure(
    db: Database, settings, instrument_id: int,
    context_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Общая структура H1 инструмента (F12–F14).

    Слои сценария и Entry Zones появляются только у явно переданного
    допустимого контекста. context_id=None — честные пустые ranges/entries:
    диапазон и зоны входа не выдумываются. Формирующаяся свеча отдельно
    от закрытых и pivot не подтверждает."""
    ins = db.get_instrument(instrument_id)
    if ins is None:
        return None
    now = now_ms()
    since = now - settings.detector.ltf_history_days * 86_400_000
    raw = db.get_candles(
        instrument_id, "H1", start_ms=since, closed_only=False,
    )
    forming = None
    for candle in reversed(raw):
        if candle.closed:
            continue
        forming = {
            "time": candle.open_time // 1000,
            "open": candle.open, "high": candle.high,
            "low": candle.low, "close": candle.close,
            "open_time": candle.open_time, "close_time": candle.close_time,
            "closed": False,
        }
        break
    pivots = [
        p.to_dict()
        for p in db.list_ltf_pivots(instrument_id, since_ms=since)
    ]
    ranges: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    entries_excluded: list[dict[str, Any]] = []
    structure_events: list[dict[str, Any]] = []
    expected: dict[str, Any] = {"bos": None, "sms": None}
    used_context = None
    if context_id is not None:
        obs = db.get_ltf_observation(context_id)
        parent = db.get_zone(obs.zone_id) if obs is not None else None
        if (
            obs is not None
            and obs.instrument_id == instrument_id
            and eligible_htf_parent(parent, settings.detector, as_of=now)
        ):
            layers = observation_chart_layers(db, settings, obs.id)
            if layers is not None:
                used_context = obs.id
                ranges = layers["ranges"]
                entries = layers["entries"]
                entries_excluded = layers["entries_excluded"]
                structure_events = layers["structure_events"]
                expected = layers.get("expected") or expected
    return {
        "timeframe": "H1",
        "instrument_id": instrument_id,
        "context_id": used_context,
        "as_of": now,
        "window_from": since,
        "forming": forming,
        "pivots": pivots,
        "ranges": ranges,
        "entries": entries,
        "entries_excluded": entries_excluded,
        "structure_events": structure_events,
        "expected": expected,
        "processing": _cursors(db, instrument_id),
        "state_version": db.get_state_seq(),
    }


# --------------------------------------------------------------------- #
# Состояние сервиса (ТЗ бота п.11, /status)
# --------------------------------------------------------------------- #

def service_status(db: Database, settings) -> dict[str, Any]:
    """Состояние сервиса: котировки (свежесть как в _data_state), обработка
    H1 (close_time последней закрытой vs курсор ltf:h1:last_close — канал
    processing единой оценки качества, F03), свежесть HTF-свечей D1/W1
    (логика /api/health), очередь доставки, пропуски данных по активам.
    Цель — отличить «сетапа нет» от «данные не обработаны»."""
    now = now_ms()
    instruments = {i.id: i for i in db.get_instruments(enabled_only=True)}

    quotes = db.get_all_quotes()
    horizon = quote_stale_limit_ms(settings)
    stale_symbols: list[str] = []
    last_quote_at: Optional[int] = None
    for iid, ins in instruments.items():
        q = quotes.get(iid)
        if q is None:
            stale_symbols.append(ins.symbol)
            continue
        last_quote_at = max(last_quote_at or 0, q[1])
        if now - q[1] > horizon:
            stale_symbols.append(ins.symbol)
    quotes_ok = bool(instruments) and not stale_symbols

    # F03: отставание обработки — канал processing единой оценки качества:
    # курсор ltf:h1:last_close и последняя закрытая H1 сравниваются в одних
    # единицах (close_time); раньше курсор (close_time) мерился с open_time
    last_closed: Optional[int] = None
    last_processed: Optional[int] = None
    h1_lagging: list[str] = []
    for iid, ins in instruments.items():
        ch = data_quality(db, settings, iid, now)["channels"]
        if ch["h1"]["last_at"] is None:
            continue
        last_closed = max(last_closed or 0, ch["h1"]["last_at"])
        processed = ch["processing"]["last_at"]
        if processed is not None:
            last_processed = max(last_processed or 0, processed)
        if ch["processing"]["status"] == "lagging":
            h1_lagging.append(ins.symbol)

    # HTF-свечи — как /api/health: просрочено, если последняя закрытая свеча
    # старше двух периодов её ТФ (только включённые ТФ поиска)
    scan_tfs = {
        t.strip() for t in settings.detector.scan_timeframes.split(",")
        if t.strip() in TIMEFRAME_MINUTES
    } or set(TIMEFRAME_MINUTES)
    htf_stale: list[str] = []
    for iid, ins in instruments.items():
        for tf, minutes in TIMEFRAME_MINUTES.items():
            if tf not in scan_tfs or tf == "H1":
                continue
            c = db.last_candle(iid, tf)
            if c is None:
                continue
            if now - c.close_time > 2 * minutes * 60_000:
                htf_stale.append(f"{ins.symbol} {tf}")

    delivery_pending = (
        len(db.pending_deliveries()) + len(db.pending_ltf_events())
    )

    gaps = [
        {
            "symbol": row["instrument"]["symbol"],
            "state": row["data_state"]["state"],
            "reason": row["data_state"]["reason"],
        }
        for row in instruments_overview(db, settings)
        if row["data_state"]["state"] != "ok"
    ]
    return {
        "time": now,
        "quotes": {
            "ok": quotes_ok,
            "last_quote_at": last_quote_at,
            "stale": stale_symbols,
        },
        "h1": {
            "last_closed": last_closed,
            "last_processed": last_processed,
            "lagging": h1_lagging,
        },
        "htf_stale": htf_stale,
        "delivery_pending": delivery_pending,
        "data_gaps": gaps,
    }
