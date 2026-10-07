"""Read model окна «Альткоины» (ТЗ 07.10.2026 §16–§18).

Строит таблицу сетапов (§16: все колонки, ранжирование §17), детальную
карточку сетапа с графиком и панелью «Почему найдено», статус дневного
прогона (§18). Используется веб-API (app/web/alt_api.py); бот при
необходимости может вызывать те же функции без HTTP.

Принципы:
- только чтение БД; никаких пересчётов движка и выдуманных оценок
  (§17: вероятности и «качества 0–100» нет, CMC-rank — не оценка надёжности);
- ATH/минимум после ATH — агрегаты сохранённых alt_candle того же source_id,
  что использовал движок (история одного source_version, §4/§5 ТЗ);
  правило равных вершин — последняя свеча с ценой ATH (как в AthTracker);
- стадии ранжирования §17 — из сохранённого состояния (setup.state +
  flags_json), «новые входы» — события ENTRY_A/ENTRY_B, детектированные
  в последнем успешном дневном прогоне;
- завершённая история (terminal) не смешивается с активными — отдельный
  bucket "history".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import Database
from ..models import close_boundary_ms, now_ms
from ..models_alt import (
    ALT_RULE_VERSION,
    ALT_RULE_VERSION_V2,
    AltAsset,
    AltEpisodeState,
    AltFrozenRange,
    AltRangeCandidate,
    AltRangeEpisode,
    AltSetup,
    AltState,
    AltSweepEpisode,
)

DAY_MS = 86_400_000
MSK = ZoneInfo("Europe/Moscow")
ALT_TIMEFRAME = "D1"

TERMINAL_STATES = (
    AltState.CANCELLED.value,
    AltState.EXPIRED_NO_RETEST.value,
    AltState.TARGETS_COMPLETED.value,
)

STATE_RU = {
    AltState.DATA_PENDING.value: "Нет данных",
    AltState.SEARCHING.value: "Поиск",
    AltState.FORMING.value: "Формируется",
    AltState.MATURE.value: "Зрелый диапазон",
    AltState.ACTIVE_CONFIRMED.value: "Подтверждён",
    AltState.CANCELLED.value: "Отменён",
    AltState.EXPIRED_NO_RETEST.value: "Истёк: ретеста не было",
    AltState.TARGETS_COMPLETED.value: "Цели выполнены",
    AltState.REVIEW_REQUIRED.value: "Требует проверки",
}

CANCEL_MODE_RU = {
    "wick_on_closed_d1": "тень закрытой D1 (Low ≤ K)",
    "close_on_closed_d1": "закрытие D1 (Close ≤ K)",
}

# Подписи существующих типов событий. Новых торговых состояний здесь нет.
EVENT_RU = {
    "forming_started": "Начало формирования",
    "mature_frozen": "Диапазон зафиксирован",
    "manipulation_started": "Манипуляция началась",
    "manipulation_ended": "Манипуляция завершилась",
    "ssl_taken": "SSL снят",
    "bos_confirmed": "BOS",
    "sms_confirmed": "SMS",
    "breakout": "Выход из диапазона",
    "retest": "Ретест",
    "target_hit": "Цель достигнута",
    "cancelled": "Отмена",
    "expired_no_retest": "Срок ретеста истёк",
    "targets_completed": "Цели выполнены",
    "entry_a": "Вход A",
    "entry_b": "Вход B",
    "review_required": "Требует проверки",
    "data_stale": "Данные устарели",
}

# При равном времени события более позднее по смыслу сценария выигрывает.
_EVENT_RANK = {
    "cancelled": 100,
    "expired_no_retest": 100,
    "targets_completed": 100,
    "review_required": 90,
    "data_stale": 90,
    "entry_a": 80,
    "entry_b": 80,
    "retest": 70,
    "breakout": 60,
    "bos_confirmed": 40,
    "sms_confirmed": 40,
    "target_hit": 30,
    "ssl_taken": 20,
    "manipulation_ended": 15,
    "manipulation_started": 15,
    "mature_frozen": 10,
    "forming_started": 5,
}

# §12 ТЗ, дословно
K_NONPOSITIVE_TEXT = "По выбранной формуле ценовой уровень отмены неположительный"

# Стадии ранжирования §17
STAGE_NEW_ENTRY = 1        # новые возможности A/B текущего дневного run
STAGE_AWAITING_RETEST = 2  # подтверждённый выход, ждём ретест до deadline
STAGE_CONFIRMED = 3        # другие подтверждённые незавершённые
STAGE_MATURE = 4           # зрелые диапазоны
STAGE_FORMING = 5          # формирующиеся
STAGE_REVIEW = 6           # требуют проверки / нет данных

STAGE_RU = {
    STAGE_NEW_ENTRY: "Новый вход",
    STAGE_AWAITING_RETEST: "Ожидание ретеста",
    STAGE_CONFIRMED: "Подтверждён",
    STAGE_MATURE: "Зрелый",
    STAGE_FORMING: "Формируется",
    STAGE_REVIEW: "Требует проверки",
}

BUCKETS = (
    "eligible", "new_entries", "awaiting_retest", "mature", "forming",
    "review", "history", "all",
)

# Эпизоды диапазонов v2 (ТЗ 07.10.2026 R-05/R-07/R-08)
EPISODE_STATE_RU = {
    AltEpisodeState.FORMING.value: "Формируется",
    AltEpisodeState.MATURE.value: "Зрелая база",
    AltEpisodeState.ACTIVE.value: "Активная база",
    AltEpisodeState.ACCOMPANIMENT.value: "Сопровождение",
    AltEpisodeState.DECAYED.value: "Распад",
    AltEpisodeState.TERMINAL.value: "Завершён",
}

BASE_END_REASON_RU = {
    "breakout_confirmed": "Подтверждённый выход",
    "decay": "Распад базы",
    "none": "Без выхода",
}

SWEEP_STATE_RU = {
    "open": "Выход ниже продолжается",
    "return_pending": "Выход ниже, возврат не подтверждён",
    "returned": "Возврат подтверждён",
    "accepted_below": "Принятие ниже L",
}

# состояния эпизода, которые могут быть выбраны актуальными (R-08)
EPISODE_QUALIFIED_STATES = (
    AltEpisodeState.MATURE.value,
    AltEpisodeState.ACTIVE.value,
    AltEpisodeState.ACCOMPANIMENT.value,
)

# срок релевантности после подтверждённого выхода (R-08; §5 ТЗ — параметр,
# подлежит калибровке на данных; значение проектное, не торговое условие)
EPISODE_RELEVANCE_WINDOW_MS = 180 * DAY_MS

# потолок свечей в detail (предшествующее падение + диапазон + последние дни)
_DETAIL_CANDLE_LIMIT = 2000
_CANDLES_BEFORE_ATH_DAYS = 30


def _loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _is_terminal(setup: AltSetup) -> bool:
    return setup.terminated_ms is not None or setup.state in TERMINAL_STATES


def _last_event_brief(events: list[Any]) -> Optional[dict[str, Any]]:
    """Последнее событие строки списка: время, затем смысл, затем id."""
    if not events:
        return None
    event = max(
        events,
        key=lambda e: (
            e.event_time_ms or 0,
            _EVENT_RANK.get(e.event_type, 0),
            e.id or 0,
        ),
    )
    return {
        "id": event.id,
        "event_type": event.event_type,
        "event_time_ms": event.event_time_ms,
        "label_ru": EVENT_RU.get(event.event_type, event.event_type),
    }


def _structure_source_event_id(
    setup_id: int, kind: str, candle_open_time: int, anchors: Any
) -> Optional[str]:
    """Тот же ключ, которым движок пишет lifecycle-событие.

    SSL без formed_at не получает ключ: угадывать связь по цене нельзя.
    BOS_REV/SMS_REV в outbox не пишутся; ключ нужен только как идентификатор
    факта на графике.
    """
    inner: list[Any] = []
    if isinstance(anchors, dict) and isinstance(anchors.get("anchors"), list):
        inner = anchors["anchors"]
    if kind == "SSL":
        formed = None
        if inner and isinstance(inner[0], dict):
            formed = inner[0].get("formed_at")
        if formed is None:
            return None
        return f"ssl:{setup_id}:{formed}:{candle_open_time}"
    if kind in ("BOS", "SMS", "BOS_REV", "SMS_REV"):
        return f"{kind.lower()}:{setup_id}:{candle_open_time}"
    return None


def _candle_from_source_id(source_event_id: Optional[str]) -> Optional[int]:
    if not source_event_id or ":" not in source_event_id:
        return None
    tail = source_event_id.rsplit(":", 1)[-1]
    try:
        value = int(tail)
    except ValueError:
        return None
    # id сетапа тоже число, но не метка времени. Свеча D1 — миллисекунды.
    if value < 1_000_000_000_000:
        return None
    return value


def _confirmation_link(
    confirmation: Any, structure_events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Связь подтверждения со структурной строкой внутри одного сетапа.

    Совпадение: тип + точная свеча, уже зашитая в source_event_id движка.
    Округлённая цена не используется. Несколько совпадений — связь неизвестна.
    """
    if confirmation is None:
        return {"status": "absent", "structure_event_id": None}
    source_event_id = confirmation.source_event_id or ""
    if confirmation.event_type == "breakout":
        return {
            "status": "not_structure",
            "structure_event_id": None,
            "source_event_id": source_event_id,
        }
    kind = {"bos_confirmed": "BOS", "sms_confirmed": "SMS", "ssl_taken": "SSL"}.get(
        confirmation.event_type
    )
    if kind is None or not source_event_id:
        return {
            "status": "unknown",
            "structure_event_id": None,
            "source_event_id": source_event_id,
        }
    matches = [
        event for event in structure_events
        if event.get("kind") == kind and event.get("source_event_id") == source_event_id
    ]
    if len(matches) == 1:
        return {
            "status": "exact",
            "structure_event_id": matches[0]["id"],
            "source_event_id": source_event_id,
        }
    return {
        "status": "unknown",
        "structure_event_id": None,
        "source_event_id": source_event_id,
    }


# ---------------------------------------------------------------------------
# Агрегаты свечей источника (ATH / минимум после ATH / последний Close, §5)
# ---------------------------------------------------------------------------


def _candle_stats(db: Database, source_id: int) -> Optional[dict[str, Any]]:
    """ATH по теням всей сохранённой истории, минимум после него и
    последняя закрытая D1 — из alt_candle одного source_id.

    Правило равных вершин как у движка (§5): начало ATH-эпизода — ПОСЛЕДНЯЯ
    свеча с ценой ATH; минимум — первая свеча с минимальным Low после неё.
    """
    row = db.conn.execute(
        "SELECT COUNT(*) AS n, MAX(high) AS ath FROM alt_candle WHERE source_id=?",
        (source_id,),
    ).fetchone()
    if row is None or not row["n"]:
        return None
    ath = row["ath"]
    ath_ot = db.conn.execute(
        "SELECT MAX(open_time) AS ot FROM alt_candle WHERE source_id=? AND high=?",
        (source_id, ath),
    ).fetchone()["ot"]
    pmin_row = db.conn.execute(
        "SELECT MIN(low) AS pmin FROM alt_candle WHERE source_id=? AND open_time>?",
        (source_id, ath_ot),
    ).fetchone()
    p_min = pmin_row["pmin"]
    p_min_ot = None
    if p_min is not None:
        p_min_ot = db.conn.execute(
            "SELECT MIN(open_time) AS ot FROM alt_candle "
            "WHERE source_id=? AND open_time>? AND low=?",
            (source_id, ath_ot, p_min),
        ).fetchone()["ot"]
    last = db.conn.execute(
        "SELECT open_time, close FROM alt_candle WHERE source_id=? "
        "ORDER BY open_time DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    drawdown = (1 - p_min / ath) if (p_min is not None and ath > 0) else None
    return {
        "candles_total": row["n"],
        "ath_price": ath,
        "ath_open_time": ath_ot,
        "p_min": p_min,
        "p_min_open_time": p_min_ot,
        "drawdown": drawdown,
        "last_open_time": last["open_time"],
        "last_close": last["close"],
    }


# ---------------------------------------------------------------------------
# Строка таблицы
# ---------------------------------------------------------------------------


def _source_brief(source: Any) -> Optional[dict[str, Any]]:
    if source is None:
        return None
    return {
        "id": source.id,
        "venue": source.venue,
        "symbol": source.symbol,
        "quote": source.quote,
        "history_scope": source.history_scope,
        "source_version": source.source_version,
        "earliest_available_ms": source.earliest_available_ms,
        "last_closed_ms": source.last_closed_ms,
    }


def _width_pcts(lower: float, upper: float) -> tuple[Optional[float], Optional[float]]:
    """§16: ширина вверх (U/L−1)×100% и снижение (1−L/U)×100% — обе,
    правильные знаки (U=2L → +100% и −50%)."""
    up = (upper / lower - 1) * 100 if lower > 0 else None
    down = (1 - lower / upper) * 100 if upper > 0 else None
    return up, down


def _distance_to_entry(
    stage: int,
    geometry: Optional[dict[str, float]],
    last_close: Optional[float],
    structure_level: Optional[float],
) -> Optional[float]:
    """§17: близость ко входу в %.

    Ретест: 0 при P∈[M,U], иначе min(|P−M|,|P−U|)/P×100% (P — последний
    закрытый Close, P>0). Ожидание BOS: |K_structure−P|/P×100% при известной
    активной опоре. Неизвестное — None (сортируется ПОСЛЕ известных, null≠0).
    """
    if last_close is None or last_close <= 0:
        return None
    p = last_close
    if stage in (STAGE_NEW_ENTRY, STAGE_AWAITING_RETEST) and geometry:
        m, u = geometry["mid"], geometry["upper"]
        if m <= p <= u:
            return 0.0
        return min(abs(p - m), abs(p - u)) / p * 100
    if structure_level is not None:
        return abs(structure_level - p) / p * 100
    return None


def _targets_block(setup: AltSetup, flags: dict[str, Any]) -> list[dict[str, Any]]:
    hit = set(flags.get("targets_hit") or [])
    out = []
    for t in _loads(setup.targets_json, []):
        out.append({
            "tp": t.get("tp"),
            "price": t.get("price"),
            "hit": t.get("tp") in hit,
            "passed_at_confirmation": bool(t.get("passed_at_confirmation")),
        })
    return out


def _cancel_block(setup: AltSetup) -> dict[str, Any]:
    k = setup.cancel_price
    reachable = bool(setup.cancel_reachable) and k is not None and k > 0
    return {
        "price": k,
        "mode": setup.cancel_mode,
        "mode_ru": CANCEL_MODE_RU.get(setup.cancel_mode, setup.cancel_mode),
        # §12: способ подтверждения K — проектный выбор v1, не утверждён
        "mode_note": "проектная настройка v1 (не согласована владельцем)",
        "reachable": reachable,
        "nonpositive_text": K_NONPOSITIVE_TEXT if not reachable else None,
    }


def _geometry_of(
    frozen: Optional[AltFrozenRange], candidate: Optional[AltRangeCandidate]
) -> Optional[dict[str, float]]:
    src = frozen if frozen is not None else candidate
    if src is None:
        return None
    return {
        "lower": src.lower, "upper": src.upper,
        "width": src.width, "mid": src.mid,
    }


def _setup_row(
    db: Database,
    asset: AltAsset,
    source: Any,
    stats: Optional[dict[str, Any]],
    setup: AltSetup,
    new_entry_setup_ids: set[int],
    run_reasons: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Строка таблицы §16 для сохранённого сетапа (активного или terminal)."""
    flags = _loads(setup.flags_json, {})
    frozen = db.get_alt_frozen_range(setup.range_id)
    candidate = (
        db.get_alt_range_candidate(frozen.range_id) if frozen is not None else None
    )
    geometry = _geometry_of(frozen, candidate)
    last_close = stats["last_close"] if stats else None

    if _is_terminal(setup):
        stage = None  # история — вне активного ранжирования
    elif setup.id in new_entry_setup_ids:
        stage = STAGE_NEW_ENTRY
    elif setup.state == AltState.REVIEW_REQUIRED.value:
        stage = STAGE_REVIEW
    elif flags.get("breakout_confirmed") and not flags.get("retest_received"):
        stage = STAGE_AWAITING_RETEST
    elif setup.state == AltState.ACTIVE_CONFIRMED.value:
        stage = STAGE_CONFIRMED
    elif setup.state == AltState.MATURE.value:
        stage = STAGE_MATURE
    else:
        stage = STAGE_MATURE

    # §17 «ожидание BOS»: активная структурная опора — уровень последнего
    # сохранённого структурного события (проектное приближение: трекер
    # «защищённого LH» живёт в памяти replay и не персистится)
    structure_level: Optional[float] = None
    if stage in (STAGE_CONFIRMED, STAGE_MATURE):
        events = db.list_alt_structure_events(setup.id)
        if events:
            structure_level = events[-1].level_price
    distance = _distance_to_entry(
        stage or STAGE_MATURE, geometry, last_close, structure_level
    )

    entries = db.list_alt_entry_opportunities(setup.id)
    entry_kinds = sorted({e.kind for e in entries})
    last_event = _last_event_brief(db.list_alt_events(setup.id))
    start_ot = (
        frozen.start_anchor_open_time if frozen is not None
        else (candidate.start_anchor_open_time if candidate else None)
    )
    age_days = None
    if start_ot is not None and stats:
        age_days = int(
            (close_boundary_ms(stats["last_open_time"], ALT_TIMEFRAME) - start_ot)
            // DAY_MS
        )
    consolidation_days = None
    if frozen is not None and start_ot is not None:
        # длительность самой консолидации — до фиксации зрелости (§8:
        # не каждая свеча после пробоя — новый день аккумуляции)
        consolidation_days = int((frozen.mature_at_ms - start_ot) // DAY_MS)
    elif candidate is not None:
        consolidation_days = candidate.n_days

    width_up = width_down = None
    if geometry:
        width_up, width_down = _width_pcts(geometry["lower"], geometry["upper"])

    reason = run_reasons.get(asset.id)
    row = {
        "kind": "setup",
        "setup_id": setup.id,
        "candidate_id": frozen.range_id if frozen else None,
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        },
        "source": _source_brief(source),
        "state": setup.state,
        "state_ru": STATE_RU.get(setup.state, setup.state),
        "stage": stage,
        "stage_ru": STAGE_RU.get(stage) if stage else "История",
        "terminal": _is_terminal(setup),
        "universe_eligible": bool(setup.universe_eligible),
        "ath_price": stats["ath_price"] if stats else None,
        "ath_open_time": stats["ath_open_time"] if stats else None,
        "p_min": stats["p_min"] if stats else None,
        "p_min_open_time": stats["p_min_open_time"] if stats else None,
        "drawdown_pct": (
            stats["drawdown"] * 100 if stats and stats["drawdown"] is not None
            else None
        ),
        "last_close": last_close,
        "last_close_open_time": stats["last_open_time"] if stats else None,
        **({"range": geometry} if geometry else {"range": None}),
        "width_up_pct": width_up,
        "width_down_pct": width_down,
        "age_days": age_days,
        "consolidation_days": consolidation_days,
        "flags": {
            "structure_event": bool(flags.get("structure_event")),
            "ssl_event": bool(flags.get("ssl_event")),
            "manipulation_active": bool(flags.get("manipulation_active")),
            "breakout_confirmed": bool(flags.get("breakout_confirmed")),
            "retest_received": bool(flags.get("retest_received")),
            "entry_a_confirmed": bool(flags.get("entry_a_confirmed")),
            "upper_excursion": bool(flags.get("upper_excursion")),
            "lower_excursion": bool(flags.get("lower_excursion")),
            "targets_hit": sorted(flags.get("targets_hit") or []),
        },
        "entries": [
            {"kind": e.kind, "event_time_ms": e.event_time_ms,
             "price": e.price, "zone": _loads(e.zone_json, None)}
            for e in entries
        ],
        "entry_kinds": entry_kinds,
        "targets": _targets_block(setup, flags),
        "cancel": _cancel_block(setup),
        "breakout_close": setup.breakout_close,
        "breakout_closed_at": setup.breakout_closed_at,
        "retest_deadline_ms": setup.retest_deadline_ms,
        "distance_pct": distance,
        "terminated_ms": setup.terminated_ms,
        "updated_ms": setup.updated_ms,
        "last_event": last_event,
        "run_status": reason.get("status") if reason else None,
        "reason": reason.get("reason") if reason else None,
    }
    return row


def _candidate_row(
    db: Database,
    asset: AltAsset,
    source: Any,
    stats: Optional[dict[str, Any]],
    candidate: AltRangeCandidate,
    run_reasons: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Строка «формирующийся диапазон» — сетапа ещё нет (§6: forming виден
    в стандартном списке со 51 дня, freeze ещё не случился)."""
    geometry = _geometry_of(None, candidate)
    last_close = stats["last_close"] if stats else None
    width_up = width_down = None
    if geometry:
        width_up, width_down = _width_pcts(geometry["lower"], geometry["upper"])
    distance = _distance_to_entry(STAGE_FORMING, geometry, last_close, None)
    age_days = None
    if stats:
        age_days = int(
            (close_boundary_ms(stats["last_open_time"], ALT_TIMEFRAME)
             - candidate.start_anchor_open_time) // DAY_MS
        )
    reason = run_reasons.get(asset.id)
    state = candidate.state
    return {
        "kind": "candidate",
        "setup_id": None,
        "candidate_id": candidate.id,
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        },
        "source": _source_brief(source),
        "state": state,
        "state_ru": STATE_RU.get(state, state),
        "stage": STAGE_FORMING if state == AltState.FORMING.value else STAGE_REVIEW,
        "stage_ru": (
            STAGE_RU[STAGE_FORMING] if state == AltState.FORMING.value
            else STAGE_RU[STAGE_REVIEW]
        ),
        "terminal": False,
        "universe_eligible": True,
        "ath_price": stats["ath_price"] if stats else None,
        "ath_open_time": stats["ath_open_time"] if stats else None,
        "p_min": stats["p_min"] if stats else None,
        "p_min_open_time": stats["p_min_open_time"] if stats else None,
        "drawdown_pct": (
            stats["drawdown"] * 100 if stats and stats["drawdown"] is not None
            else None
        ),
        "last_close": last_close,
        "last_close_open_time": stats["last_open_time"] if stats else None,
        "range": geometry,
        "width_up_pct": width_up,
        "width_down_pct": width_down,
        "age_days": age_days,
        "consolidation_days": candidate.n_days,
        "flags": {},
        "entries": [],
        "entry_kinds": [],
        "targets": [],
        "cancel": None,
        "breakout_close": None,
        "breakout_closed_at": None,
        "retest_deadline_ms": None,
        "distance_pct": distance,
        "terminated_ms": None,
        "updated_ms": candidate.updated_ms,
        "last_event": None,
        "run_status": reason.get("status") if reason else None,
        "reason": reason.get("reason") if reason else None,
    }


def _nodata_row(
    asset: AltAsset,
    source: Any,
    stats: Optional[dict[str, Any]],
    reason: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Строка bucket «нет данных» (§17 п.6: review/no-data — отдельная
    выборка): ошибка источника или пропуск в последнем прогоне — это НЕ
    «сетапов нет» (§18)."""
    return {
        "kind": "nodata",
        "setup_id": None,
        "candidate_id": None,
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        },
        "source": _source_brief(source),
        "state": AltState.DATA_PENDING.value,
        "state_ru": STATE_RU[AltState.DATA_PENDING.value],
        "stage": STAGE_REVIEW,
        "stage_ru": STAGE_RU[STAGE_REVIEW],
        "terminal": False,
        "universe_eligible": None,
        "ath_price": stats["ath_price"] if stats else None,
        "ath_open_time": stats["ath_open_time"] if stats else None,
        "p_min": stats["p_min"] if stats else None,
        "p_min_open_time": stats["p_min_open_time"] if stats else None,
        "drawdown_pct": (
            stats["drawdown"] * 100 if stats and stats["drawdown"] is not None
            else None
        ),
        "last_close": stats["last_close"] if stats else None,
        "last_close_open_time": stats["last_open_time"] if stats else None,
        "range": None,
        "width_up_pct": None,
        "width_down_pct": None,
        "age_days": None,
        "consolidation_days": None,
        "flags": {},
        "entries": [],
        "entry_kinds": [],
        "targets": [],
        "cancel": None,
        "breakout_close": None,
        "breakout_closed_at": None,
        "retest_deadline_ms": None,
        "distance_pct": None,
        "terminated_ms": None,
        "updated_ms": asset.updated_ms,
        "last_event": None,
        "run_status": reason.get("status") if reason else None,
        "reason": (
            (reason or {}).get("reason")
            or ("история неполная (partial)" if source and source.history_scope == "partial"
                else ("нет свечей D1" if source else "источник не выбран"))
        ),
    }


# ---------------------------------------------------------------------------
# Таблица (§16 + ранжирование §17)
# ---------------------------------------------------------------------------


def _rank_key(row: dict[str, Any]) -> tuple:
    """§17: внутри стадии — расстояние до входа ASC (null ПОСЛЕ известных,
    null ≠ 0), длительность консолидации DESC, историческое падение DESC,
    CMC-rank ASC, asset_id ASC."""
    dist = row["distance_pct"]
    return (
        row["stage"] or 99,
        0 if dist is not None else 1,
        dist if dist is not None else 0.0,
        -(row["consolidation_days"] or 0),
        -(row["drawdown_pct"] or 0.0),
        row["asset"]["cmc_rank"] or 10**9,
        row["asset"]["id"],
    )


def _run_reasons(last_run: Any) -> dict[int, dict[str, Any]]:
    """per_asset причины последнего прогона (skip/error) — колонка «причины»."""
    out: dict[int, dict[str, Any]] = {}
    if last_run is None:
        return out
    for entry in _loads(last_run.summary_json, {}).get("per_asset", []):
        if entry.get("status") != "processed":
            out[entry["asset_id"]] = {
                "status": entry.get("status"),
                "reason": entry.get("reason"),
            }
    return out


def _new_entry_setup_ids(db: Database, last_run: Any) -> set[int]:
    """Сетапы с новыми входами A/B, детектированными в последнем успешном
    дневном прогоне (§17 п.1). События пишутся движком с detected_at_ms
    прогона; run_id у alt_event не заполняется — окно по started/finished."""
    if last_run is None:
        return set()
    start = last_run.started_ms
    finish = last_run.finished_ms or now_ms()
    rows = db.conn.execute(
        "SELECT DISTINCT setup_id FROM alt_event "
        "WHERE event_type IN ('entry_a','entry_b') AND detected_at_ms BETWEEN ? AND ?",
        (start, finish),
    ).fetchall()
    return {r["setup_id"] for r in rows}


def alt_setups_table(
    db: Database,
    settings: Settings,
    bucket: str = "eligible",
    venue: Optional[str] = None,
    rank_min: Optional[int] = None,
    rank_max: Optional[int] = None,
    age_min: Optional[int] = None,
    age_max: Optional[int] = None,
    dd_min: Optional[float] = None,
    dd_max: Optional[float] = None,
    structure: Optional[str] = None,
) -> dict[str, Any]:
    """Таблица §16 с ранжированием §17.

    bucket: eligible (активные, по умолчанию) | new_entries | awaiting_retest
    | mature | forming | review | history (terminal) | all. Вторичные
    фильтры — venue, диапазоны ранга/возраста/падения, наличие структурных
    событий (structure=bos_sms,manipulation,breakout — любое из).
    """
    last_run = db.get_latest_alt_run(statuses=("ok", "no_universe"))
    reasons = _run_reasons(last_run)
    new_entries = _new_entry_setup_ids(db, last_run)
    structure_wanted = {
        p.strip() for p in (structure or "").split(",") if p.strip()
    }

    active_rows: list[dict[str, Any]] = []
    history_rows: list[dict[str, Any]] = []
    stats_cache: dict[int, Optional[dict[str, Any]]] = {}
    # Биржи — из источников, а не из уже отфильтрованных строк.
    venues: set[str] = set()

    for asset in db.list_alt_assets():
        source = db.get_alt_instrument_source(asset.id)
        if source is not None and source.venue:
            venues.add(source.venue)
        stats: Optional[dict[str, Any]] = None
        if source is not None:
            if source.id not in stats_cache:
                stats_cache[source.id] = _candle_stats(db, source.id)
            stats = stats_cache[source.id]

        setups = db.list_alt_setups(asset.id)
        active = [s for s in setups if not _is_terminal(s)]
        terminal = [s for s in setups if _is_terminal(s)]
        for s in terminal:
            history_rows.append(
                _setup_row(db, asset, source, stats, s, set(), reasons)
            )
        if active:
            # один живой сетап на актив (новые диапазоны — после завершения)
            row = _setup_row(
                db, asset, source, stats, active[-1], new_entries, reasons
            )
            active_rows.append(row)
            continue
        if terminal:
            continue  # §17: история не смешивается с активной
        # Сетапа нет: forming-кандидат (со 51 дня, §6) — в стандартный список;
        # SEARCHING/range_pending — внутренний поиск, не выводим
        candidates = db.list_alt_range_candidates(asset.id)
        forming = [
            c for c in candidates if c.state == AltState.FORMING.value
        ]
        if forming:
            active_rows.append(
                _candidate_row(db, asset, source, stats, forming[-1], reasons)
            )
            continue
        if any(c.state == AltState.REVIEW_REQUIRED.value for c in candidates):
            active_rows.append(_candidate_row(
                db, asset, source, stats,
                [c for c in candidates
                 if c.state == AltState.REVIEW_REQUIRED.value][-1],
                reasons,
            ))
            continue
        # «нет данных»: ошибка/пропуск в последнем прогоне, нет источника
        # или свечей, partial-история — отдельная выборка (§18)
        reason = reasons.get(asset.id)
        has_problem = (
            reason is not None
            or source is None
            or stats is None
            or source.history_scope == "partial"
        )
        if has_problem:
            active_rows.append(_nodata_row(asset, source, stats, reason))

    # --- bucket ---
    def bucket_of(row: dict[str, Any]) -> str:
        if row["terminal"]:
            return "history"
        stage = row["stage"]
        return {
            STAGE_NEW_ENTRY: "new_entries",
            STAGE_AWAITING_RETEST: "awaiting_retest",
            STAGE_CONFIRMED: "confirmed",  # своя стадия §17, отдельного фильтра нет
            STAGE_MATURE: "mature",
            STAGE_FORMING: "forming",
            STAGE_REVIEW: "review",
        }.get(stage, "review")

    counts: dict[str, int] = {b: 0 for b in BUCKETS if b != "all"}
    all_rows = active_rows + history_rows
    for row in all_rows:
        counts[bucket_of(row)] = counts.get(bucket_of(row), 0) + 1
    counts["eligible"] = sum(
        1 for r in active_rows if r["stage"] != STAGE_REVIEW
    )

    if bucket == "eligible":
        rows = [r for r in active_rows if r["stage"] != STAGE_REVIEW]
    elif bucket == "history":
        rows = list(history_rows)
    elif bucket == "all":
        rows = list(all_rows)
    elif bucket in ("new_entries", "awaiting_retest", "mature", "forming",
                    "review"):
        rows = [r for r in active_rows if bucket_of(r) == bucket]
    else:
        rows = [r for r in active_rows if r["stage"] != STAGE_REVIEW]

    # --- вторичные фильтры (§16) ---
    def visible(row: dict[str, Any]) -> bool:
        src = row["source"] or {}
        if venue and src.get("venue") != venue:
            return False
        rank = row["asset"]["cmc_rank"] or 0
        if rank_min is not None and rank < rank_min:
            return False
        if rank_max is not None and rank > rank_max:
            return False
        age = row["age_days"]
        if age_min is not None and (age is None or age < age_min):
            return False
        if age_max is not None and (age is None or age > age_max):
            return False
        dd = row["drawdown_pct"]
        if dd_min is not None and (dd is None or dd < dd_min):
            return False
        if dd_max is not None and (dd is None or dd > dd_max):
            return False
        if structure_wanted:
            flags = row["flags"] or {}
            has = {
                "bos_sms": flags.get("structure_event"),
                "manipulation": flags.get("manipulation_active"),
                "breakout": flags.get("breakout_confirmed"),
            }
            if not any(has.get(w) for w in structure_wanted):
                return False
        return True

    rows = [r for r in rows if visible(r)]
    # §17: активные ранжируются; история — по завершению (свежие первыми)
    if bucket == "history":
        rows.sort(key=lambda r: (r["terminated_ms"] or 0, r["asset"]["id"]),
                  reverse=True)
    else:
        rows.sort(key=_rank_key)

    return {
        "as_of_ms": last_run.as_of_ms if last_run else None,
        "run_id": last_run.id if last_run else None,
        "bucket": bucket,
        "buckets": counts,
        "venues": sorted(venues),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Детальная карточка (график + «Почему найдено», §16)
# ---------------------------------------------------------------------------


def _anchor(open_time: int, pivot_right: int) -> dict[str, Any]:
    """Якорь-pivot: цена/время формирования и когда стал доступен
    (закрытие третьей правой D1, §5/§10 — правило 3+3)."""
    return {
        "open_time": open_time,
        "available_at_ms": close_boundary_ms(
            open_time + pivot_right * DAY_MS, ALT_TIMEFRAME
        ),
    }


def _detail_candles(
    db: Database, source_id: int, ath_open_time: Optional[int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Серия D1 для графика и границы загруженного фрагмента.

    Фрагмент начинается у контекста ATH и обрезается потолком. Это не вся
    рыночная история пары: признак усечения и текст отдаются клиенту явно.
    """
    start = 0
    if ath_open_time:
        start = max(0, ath_open_time - _CANDLES_BEFORE_ATH_DAYS * DAY_MS)
    candles = db.get_alt_candles(source_id, start_ms=start or None)
    truncated = len(candles) > _DETAIL_CANDLE_LIMIT
    if truncated:
        candles = candles[-_DETAIL_CANDLE_LIMIT:]
    loaded = [
        {"open_time": c.open_time, "open": c.open, "high": c.high,
         "low": c.low, "close": c.close, "volume": c.volume}
        for c in candles
    ]
    windowed = bool(start)
    if truncated:
        note = (
            "Загружен хвост истории источника: более ранние свечи в этот "
            "фрагмент не вошли. Это не вся рыночная история пары."
        )
    elif windowed:
        note = (
            "На графике фрагмент от контекста ATH, а не вся рыночная история пары."
        )
    else:
        note = (
            "Показаны все сохранённые свечи этого источника. "
            "Это не утверждение, что биржа отдала историю с листинга."
        )
    bounds = {
        "loaded_from_ms": loaded[0]["open_time"] if loaded else None,
        "loaded_to_ms": loaded[-1]["open_time"] if loaded else None,
        "count": len(loaded),
        "truncated": truncated,
        "windowed": windowed,
        "limit": _DETAIL_CANDLE_LIMIT,
        "note": note,
    }
    return loaded, bounds


def _classifier_block(candidate: Optional[AltRangeCandidate], settings: Settings) -> Optional[dict[str, Any]]:
    if candidate is None:
        return None
    metrics = _loads(candidate.metrics_json, {}).get("classifier")
    if metrics is None:
        return None
    cfg = settings.alt_config
    return {
        **metrics,
        "thresholds": {
            "slope_max": cfg.classifier_slope_max,
            "center_shift_max": cfg.classifier_center_shift_max,
            "block_days": cfg.classifier_block_days,
        },
        "thresholds_note": "проектные пороги классификатора, не торговые условия (§7.4)",
    }


def alt_setup_detail(db: Database, setup_id: int, settings: Settings) -> Optional[dict[str, Any]]:
    """Всё для большого D1-графика и панели «Почему найдено» (§16):
    опоры с временем доступности, frozen range, классификатор с порогами,
    версии расширений, структурные события, эпизоды манипуляции, входы,
    цели, K, отмена/истечение, свечи, источник, as_of, версии."""
    setup = db.get_alt_setup(setup_id)
    if setup is None:
        return None
    asset = db.get_alt_asset(setup.asset_id)
    source = db.get_alt_instrument_source(setup.asset_id)
    frozen = db.get_alt_frozen_range(setup.range_id)
    candidate = (
        db.get_alt_range_candidate(frozen.range_id) if frozen is not None else None
    )
    flags = _loads(setup.flags_json, {})
    cfg = settings.alt_config

    stats = _candle_stats(db, source.id) if source is not None else None
    metrics = _loads(candidate.metrics_json, {}) if candidate is not None else {}

    events = db.list_alt_events(setup.id)
    confirmation_event = (
        db.get_alt_event(setup.confirmation_event_id)
        if setup.confirmation_event_id else None
    )
    entries = db.list_alt_entry_opportunities(setup.id)
    last_run = db.get_latest_alt_run(statuses=("ok", "no_universe"))

    anchors = None
    if frozen is not None:
        anchors = {
            "start": _anchor(frozen.start_anchor_open_time, cfg.pivot_right),
            "rebound": _anchor(frozen.rebound_anchor_open_time, cfg.pivot_right),
            "alternative_anchor_open_times":
                metrics.get("alternative_anchor_open_times") or [],
        }

    if source is not None:
        candles, candle_history = _detail_candles(
            db, source.id, stats["ath_open_time"] if stats else None
        )
    else:
        candles, candle_history = [], {
            "loaded_from_ms": None, "loaded_to_ms": None, "count": 0,
            "truncated": False, "windowed": False, "limit": _DETAIL_CANDLE_LIMIT,
            "note": "Источник не выбран — свечей нет.",
        }

    structure_events = []
    for event in db.list_alt_structure_events(setup.id):
        anchors_obj = _loads(event.anchors_json, {})
        structure_events.append({
            "id": event.id,
            "kind": event.kind,
            "level_price": event.level_price,
            "close_price": event.close_price,
            "candle_open_time": event.candle_open_time,
            "anchors": anchors_obj,
            "historical": (
                bool(anchors_obj.get("historical"))
                if isinstance(anchors_obj, dict) else False
            ),
            "source_event_id": _structure_source_event_id(
                setup.id, event.kind, event.candle_open_time, anchors_obj
            ),
        })
    link = _confirmation_link(confirmation_event, structure_events)
    confirm_candle = None
    if link.get("status") == "exact":
        matched = next(
            event for event in structure_events
            if event["id"] == link["structure_event_id"]
        )
        confirm_candle = matched["candle_open_time"]
    elif confirmation_event is not None:
        confirm_candle = _candle_from_source_id(confirmation_event.source_event_id)

    target_snapshot = flags.get("target_snapshot")
    return {
        "kind": "setup",
        "setup_id": setup.id,
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        } if asset else None,
        "source": _source_brief(source),
        "state": setup.state,
        "state_ru": STATE_RU.get(setup.state, setup.state),
        "terminal": _is_terminal(setup),
        "terminated_ms": setup.terminated_ms,
        "universe_eligible": bool(setup.universe_eligible),
        "flags": flags,
        "frozen_range": (
            {
                "id": frozen.id, "range_id": frozen.range_id,
                "lower": frozen.lower, "upper": frozen.upper,
                "width": frozen.width, "mid": frozen.mid,
                "start_anchor_open_time": frozen.start_anchor_open_time,
                "rebound_anchor_open_time": frozen.rebound_anchor_open_time,
                "included_candles": frozen.included_candles,
                "mature_at_ms": frozen.mature_at_ms,
                "classifier_version": frozen.classifier_version,
                "range_version": frozen.range_version,
            } if frozen is not None else None
        ),
        "range_versions": metrics.get("versions") or [],
        "anchors": anchors,
        "classifier": _classifier_block(candidate, settings),
        "structure_events": structure_events,
        "manipulation_episodes": [
            {"id": m.id,
             "started_candle_open_time": m.started_candle_open_time,
             "ended_candle_open_time": m.ended_candle_open_time,
             "min_price": m.min_price, "days_below": m.days_below}
            for m in db.list_alt_manipulation_episodes(setup.id)
        ],
        "entries": [
            {"id": e.id, "kind": e.kind, "event_time_ms": e.event_time_ms,
             "price": e.price, "zone": _loads(e.zone_json, None),
             "bases": _loads(e.bases_json, [])}
            for e in entries
        ],
        "targets": _targets_block(setup, flags),
        "target_snapshot": target_snapshot,
        "cancel": _cancel_block(setup),
        "breakout": (
            {"close": setup.breakout_close, "closed_at": setup.breakout_closed_at,
             "retest_deadline_ms": setup.retest_deadline_ms}
            if setup.breakout_closed_at is not None else None
        ),
        "confirmation": (
            {
                "event_id": confirmation_event.id,
                "event_type": confirmation_event.event_type,
                "event_time_ms": confirmation_event.event_time_ms,
                "source_event_id": confirmation_event.source_event_id,
                "candle_open_time": confirm_candle,
                "payload": _loads(confirmation_event.payload_json, {}),
                "structure_link": link,
            }
            if confirmation_event is not None else None
        ),
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "event_time_ms": e.event_time_ms,
                "detected_at_ms": e.detected_at_ms,
                "source_event_id": e.source_event_id,
                "label_ru": EVENT_RU.get(e.event_type, e.event_type),
                "payload": _loads(e.payload_json, {}),
            }
            for e in events
        ],
        "ath": stats,
        "candles": candles,
        "candle_history": candle_history,
        "as_of_ms": last_run.as_of_ms if last_run else None,
        "versions": {
            "rule": ALT_RULE_VERSION,
            "rules_v2": ALT_RULE_VERSION_V2,
            "classifier": (
                frozen.classifier_version if frozen is not None
                else cfg.classifier_version
            ),
            "source": source.source_version if source is not None else None,
            "range": frozen.range_version if frozen is not None else None,
        },
        "formulas": {
            "cancel": "K = 2L − U (нижняя граница минус высота исходного диапазона)",
            "targets": "TP_n = U + n×W, n = 1..4",
            "width_up": "(U/L − 1) × 100%",
            "width_down": "(1 − L/U) × 100%",
            "retest_zone": "[M, U], M = (L + U)/2",
        },
    }


def alt_candidate_detail(db: Database, candidate_id: int, settings: Settings) -> Optional[dict[str, Any]]:
    """Деталь формирующегося диапазона (сетапа ещё нет): опоры, живые
    границы, классификатор, свечи. Без целей/K — они появятся после freeze."""
    candidate = db.get_alt_range_candidate(candidate_id)
    if candidate is None:
        return None
    asset = db.get_alt_asset(candidate.asset_id)
    source = db.get_alt_instrument_source(candidate.asset_id)
    cfg = settings.alt_config
    metrics = _loads(candidate.metrics_json, {})
    stats = _candle_stats(db, source.id) if source is not None else None
    if source is not None:
        candles, candle_history = _detail_candles(
            db, source.id, stats["ath_open_time"] if stats else None
        )
    else:
        candles, candle_history = [], {
            "loaded_from_ms": None, "loaded_to_ms": None, "count": 0,
            "truncated": False, "windowed": False, "limit": _DETAIL_CANDLE_LIMIT,
            "note": "Источник не выбран — свечей нет.",
        }
    last_run = db.get_latest_alt_run(statuses=("ok", "no_universe"))
    return {
        "kind": "candidate",
        "candidate_id": candidate.id,
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        } if asset else None,
        "source": _source_brief(source),
        "state": candidate.state,
        "state_ru": STATE_RU.get(candidate.state, candidate.state),
        "terminal": False,
        "range": _geometry_of(None, candidate),
        "n_days": candidate.n_days,
        "anchors": {
            "start": _anchor(candidate.start_anchor_open_time, cfg.pivot_right),
            "rebound": _anchor(candidate.rebound_anchor_open_time, cfg.pivot_right),
            "alternative_anchor_open_times":
                metrics.get("alternative_anchor_open_times") or [],
        },
        "range_versions": metrics.get("versions") or [],
        "classifier": _classifier_block(candidate, settings),
        "structure_events": [],
        "manipulation_episodes": [],
        "entries": [],
        "targets": [],
        "cancel": None,
        "events": [],
        "confirmation": None,
        "ath": stats,
        "candles": candles,
        "candle_history": candle_history,
        "as_of_ms": last_run.as_of_ms if last_run else None,
        "versions": {
            "rule": ALT_RULE_VERSION,
            "rules_v2": ALT_RULE_VERSION_V2,
            "classifier": cfg.classifier_version,
            "source": source.source_version if source is not None else None,
            "range": candidate.version,
        },
        "formulas": {
            "cancel": "K = 2L − U (после freeze диапазона)",
            "targets": "TP_n = U + n×W, n = 1..4 (после подтверждения)",
            "width_up": "(U/L − 1) × 100%",
            "width_down": "(1 − L/U) × 100%",
        },
    }


# ---------------------------------------------------------------------------
# Статус прогона (§18)
# ---------------------------------------------------------------------------


def _next_run_msk(cfg: Any, now: Optional[datetime] = None) -> int:
    """Следующий штатный запуск (ms, слот job_hour:job_minute МСК)."""
    now_msk = (now or datetime.now(MSK)).astimezone(MSK)
    slot = now_msk.replace(
        hour=cfg.job_hour_msk, minute=cfg.job_minute_msk,
        second=0, microsecond=0,
    )
    if slot <= now_msk:
        slot += timedelta(days=1)
    return int(slot.timestamp() * 1000)


def _run_brief(run: Any) -> Optional[dict[str, Any]]:
    if run is None:
        return None
    summary = _loads(run.summary_json, {})
    per_asset = summary.get("per_asset") or []
    skipped = [
        {"asset_id": e.get("asset_id"), "symbol": e.get("symbol"),
         "reason": e.get("reason")}
        for e in per_asset if e.get("status") != "processed"
    ]
    return {
        "id": run.id,
        "status": run.status,
        "started_ms": run.started_ms,
        "finished_ms": run.finished_ms,
        "as_of_ms": run.as_of_ms,
        "processed": run.processed,
        "errors": run.errors,
        "trigger": summary.get("trigger"),
        "universe_stale": bool(summary.get("universe_stale")),
        "universe_error": summary.get("universe_error"),
        "skipped": skipped,
        "error": summary.get("error"),
    }


def alt_run_status(db: Database, cfg: Any) -> dict[str, Any]:
    """§18: последний успешный run, следующий запуск (МСК), обработанные/
    ошибочные пары, as_of, running-флаг, свежесть вселенной.
    Сбой источника ≠ «сетапов нет» — universe_stale показывается явно."""
    last_ok = db.get_latest_alt_run(statuses=("ok", "no_universe"))
    last_any = db.get_latest_alt_run()
    running = db.get_running_alt_run()
    snap = db.get_latest_alt_universe_snapshot()
    return {
        "running": running is not None,
        "running_since_ms": running.started_ms if running else None,
        "last_run": _run_brief(last_ok),
        "last_attempt": (
            _run_brief(last_any)
            if last_any is not None and (last_ok is None or last_any.id != last_ok.id)
            else None
        ),
        "next_run_ms": _next_run_msk(cfg),
        "job_time_msk": f"{cfg.job_hour_msk:02d}:{cfg.job_minute_msk:02d}",
        "job_enabled": bool(cfg.job_enabled),
        "universe": (
            {
                "snapshot_id": int(snap["id"]),
                "taken_ms": int(snap["taken_ms"]),
                "stale": bool(snap["stale"]),
                "source": snap["source"],
            } if snap is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# Эпизоды диапазонов v2 (ТЗ 07.10.2026 R-05/R-07/R-08/R-09)
# ---------------------------------------------------------------------------


def _episode_relevance(
    episode: AltRangeEpisode,
    last_close: Optional[float],
    as_of_ms: Optional[int],
    relevance_window_ms: int,
) -> Optional[str]:
    """Связь квалифицированного эпизода с текущей ценой (R-08):
    цена внутри базы либо недавний подтверждённый выход с ещё действующим
    сопровождением. None — эпизод не релевантен."""
    if episode.state not in EPISODE_QUALIFIED_STATES:
        return None
    if last_close is not None and episode.lower <= last_close <= episode.upper:
        return "inside_base"
    if (
        episode.base_end_reason == "breakout_confirmed"
        and episode.base_end_confirmed_at_ms is not None
        and as_of_ms is not None
        and 0 <= as_of_ms - episode.base_end_confirmed_at_ms <= relevance_window_ms
        and (
            episode.accompaniment_end_open_time is None
            or episode.accompaniment_end_open_time >= as_of_ms
        )
    ):
        return "recent_breakout_accompaniment"
    return None


def _episode_selection_key(episode: AltRangeEpisode) -> tuple[int, int, int]:
    """Свежесть эпизода и устойчивый порядок равных (R-08): свежесть базы,
    затем время подтверждения, затем ID. Ширина в ключ не входит — узость
    сама по себе не преимущество."""
    return (*_episode_parity_key(episode), episode.id or 0)


def _episode_parity_key(episode: AltRangeEpisode) -> tuple[int, int]:
    """Содержательный паритет кандидатов (без ID): совпадение свежести и
    времени подтверждения означает сопоставимых конкурентов (R-08)."""
    confirmation = episode.base_end_confirmed_at_ms or episode.detected_at_ms or 0
    return (episode.base_start_open_time, confirmation)


def select_current_episode(
    episodes: list[AltRangeEpisode],
    last_close: Optional[float],
    as_of_ms: Optional[int],
    relevance_window_ms: int = EPISODE_RELEVANCE_WINDOW_MS,
) -> tuple[Optional[AltRangeEpisode], str, list[AltRangeEpisode]]:
    """Выбор актуального диапазона (R-08). Чистая функция read model.

    Возвращает (episode, reason, alternatives):
    - episode=None с явной причиной, если актуального диапазона нет
      ("no_episodes" | "no_qualified_episode" | "no_relevant_episode");
    - сопоставимые противоречивые кандидаты (паритет свежести и времени
      подтверждения) → (None, "ambiguous", кандидаты) — статус
      неоднозначности, альтернативы отсортированы устойчиво (по ID);
    - иначе лучший эпизод, причина ("inside_base" |
      "recent_breakout_accompaniment") и остальные релевантные альтернативы.
    """
    if not episodes:
        return None, "no_episodes", []
    relevant: list[tuple[AltRangeEpisode, str]] = []
    for ep in episodes:
        rel = _episode_relevance(ep, last_close, as_of_ms, relevance_window_ms)
        if rel is not None:
            relevant.append((ep, rel))
    if not relevant:
        reason = (
            "no_qualified_episode"
            if not any(ep.state in EPISODE_QUALIFIED_STATES for ep in episodes)
            else "no_relevant_episode"
        )
        return None, reason, []
    # цена внутри базы — более сильная связь, чем сопровождение после выхода
    best_category = min(rel for _, rel in relevant)  # inside_base < recent_...
    category = sorted(
        (ep for ep, rel in relevant if rel == best_category),
        key=_episode_selection_key,
        reverse=True,
    )
    if len(category) > 1 and (
        _episode_parity_key(category[0]) == _episode_parity_key(category[1])
    ):
        return None, "ambiguous", category
    return category[0], best_category, category[1:]


def _sweep_episode_to_dict(sweep: AltSweepEpisode) -> dict[str, Any]:
    return {
        "id": sweep.id,
        "episode_id": sweep.episode_id,
        "state": sweep.state,
        "state_ru": SWEEP_STATE_RU.get(sweep.state, sweep.state),
        "start_open_time": sweep.start_open_time,
        "min_price": sweep.min_price,
        "min_open_time": sweep.min_open_time,
        "end_open_time": sweep.end_open_time,
        "return_confirmed": bool(sweep.return_confirmed),
        "return_confirmed_at_ms": sweep.return_confirmed_at_ms,
        "created_ms": sweep.created_ms,
        "updated_ms": sweep.updated_ms,
    }


def _range_episode_to_dict(
    episode: AltRangeEpisode, sweeps: list[AltSweepEpisode]
) -> dict[str, Any]:
    return {
        "id": episode.id,
        "asset_id": episode.asset_id,
        "source_id": episode.source_id,
        "origin_key": episode.origin_key,
        "rules_version": episode.rules_version,
        "state": episode.state,
        "state_ru": EPISODE_STATE_RU.get(episode.state, episode.state),
        "anchor_start_open_time": episode.anchor_start_open_time,
        "base_start_open_time": episode.base_start_open_time,
        "base_end_open_time": episode.base_end_open_time,
        "base_end_reason": episode.base_end_reason,
        "base_end_reason_ru": (
            BASE_END_REASON_RU.get(episode.base_end_reason)
            if episode.base_end_reason is not None else None
        ),
        "base_end_confirmed_at_ms": episode.base_end_confirmed_at_ms,
        "accompaniment_end_open_time": episode.accompaniment_end_open_time,
        "lower": episode.lower,
        "upper": episode.upper,
        "mid": episode.mid,
        "width": episode.width,
        "wick_low": episode.wick_low,
        "wick_high": episode.wick_high,
        "quality": _loads(episode.quality_json, {}),
        "selection_rank_reason": episode.selection_rank_reason,
        "detected_at_ms": episode.detected_at_ms,
        "created_ms": episode.created_ms,
        "updated_ms": episode.updated_ms,
        "sweeps": [_sweep_episode_to_dict(s) for s in sweeps],
    }


def alt_asset_ranges_history(db: Database, asset_id: int) -> Optional[dict[str, Any]]:
    """«История диапазонов» актива (R-08): все эпизоды v2 от старых к новым
    (пусто, пока движок v2 не включён) плюс v1-блок того же актива с явной
    версией правил — исторические и текущие, v1 и v2 различимы (§8.12 ТЗ).
    Только чтение; v1-данные не изменяются."""
    asset = db.get_alt_asset(asset_id)
    if asset is None:
        return None
    episodes = db.list_alt_range_episodes(asset_id)
    episode_blocks = [
        _range_episode_to_dict(ep, db.list_alt_sweep_episodes(ep.id))
        for ep in episodes
    ]

    source = db.get_alt_instrument_source(asset_id)
    last_close: Optional[float] = None
    as_of_ms: Optional[int] = None
    if source is not None and source.last_closed_ms:
        tail = db.get_alt_candles(
            source.id, start_ms=source.last_closed_ms, end_ms=source.last_closed_ms
        )
        if tail:
            last_close = tail[-1].close
            as_of_ms = tail[-1].open_time
    selected, reason, alternatives = select_current_episode(
        episodes, last_close, as_of_ms
    )
    current = {
        "episode_id": selected.id if selected is not None else None,
        "reason": reason,
        "alternatives": [ep.id for ep in alternatives],
    }

    v1_ranges = []
    for cand in db.list_alt_range_candidates(asset_id):
        frozen = db.get_alt_frozen_range_by_range(cand.id)
        v1_ranges.append({
            "rules_version": ALT_RULE_VERSION,
            "candidate_id": cand.id,
            "origin_key": cand.origin_key,
            "state": cand.state,
            "state_ru": STATE_RU.get(cand.state, cand.state),
            "lower": cand.lower,
            "upper": cand.upper,
            "mid": cand.mid,
            "width": cand.width,
            "start_anchor_open_time": cand.start_anchor_open_time,
            "rebound_anchor_open_time": cand.rebound_anchor_open_time,
            "n_days": cand.n_days,
            "version": cand.version,
            "frozen": (
                {
                    "id": frozen.id,
                    "mature_at_ms": frozen.mature_at_ms,
                    "included_candles": frozen.included_candles,
                    "classifier_version": frozen.classifier_version,
                    "range_version": frozen.range_version,
                } if frozen is not None else None
            ),
        })
    return {
        "asset": {
            "id": asset.id, "cmc_id": asset.cmc_id, "symbol": asset.symbol,
            "name": asset.name, "cmc_rank": asset.cmc_rank,
        },
        "source": _source_brief(source),
        "rules_versions": {"v1": ALT_RULE_VERSION, "v2": ALT_RULE_VERSION_V2},
        "current": current,
        "episodes_v2": episode_blocks,
        "v1": {
            "rules_version": ALT_RULE_VERSION,
            "note": "Данные движка v1 (кандидаты и frozen ranges); не редактируются расчётом v2 (R-09).",
            "ranges": v1_ranges,
        },
    }
