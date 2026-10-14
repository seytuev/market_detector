"""Эпизод HTF-контекста, нога разворота и одна карточка рыночного слома.

Эпизод хранит факт взаимодействия отдельно от lifecycle зоны: отработанный
FVG и снятый уровень контекст не стирают. PD считается от исходного LL до
максимума, который уже виден на закрытых H1. Иллюстрация 80 400 / 83 600
константой не является. Пересечение BSL само по себе long не подтверждает.
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from ..engine.ltf.breaks import detect_breaks
from ..engine.ltf.eligibility import evaluate_entry
from ..engine.ltf.ranges import RangeDraft
from ..models import Direction, Zone, ZoneType
from .htf_parent import expected_direction_for_source

RULE_VERSION = "htf-context-1"
HOUR_MS = 3_600_000
CYCLE_BASE = 10_000_000
INTERACTIONS = (
    "touch", "partial_fill", "full_fill",
    "level_cross", "sweep_reclaim", "break_acceptance",
)
TERMINAL_SOURCE = ("breaker_broken", "prb_broken", "jumped_through")


def wait_ms(cfg, timeframe: Optional[str]) -> int:
    """Срок ожидания. 0 в настройке ТФ означает общий продуктовый default."""
    hours = int(cfg.htf_context_wait_hours)
    if timeframe == "W1" and int(getattr(cfg, "htf_context_wait_hours_w1", 0) or 0) > 0:
        hours = int(cfg.htf_context_wait_hours_w1)
    elif timeframe == "D1" and int(getattr(cfg, "htf_context_wait_hours_d1", 0) or 0) > 0:
        hours = int(cfg.htf_context_wait_hours_d1)
    return hours * HOUR_MS


def tick_size(instrument) -> float:
    precision = int(getattr(instrument, "precision", 8) or 0)
    return float(Decimal(1).scaleb(-precision))


def normalize_price(price: float, tick: float) -> float:
    if not tick:
        return float(price)
    quantum = Decimal(str(tick))
    value = (Decimal(str(price)) / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(value * quantum)


def candidate_direction(zone: Zone) -> str:
    """MANUAL без заданного направления остаётся нейтральным до BOS H1;
    остальные источники — по правилу ожидания §3 (BSL→bear, SSL→bull)."""
    evidence = zone.evidence or {}
    if zone.source == "manual" and evidence.get("direction_unset"):
        return "neutral"
    if zone.type == ZoneType.MANUAL and evidence.get("direction_unset"):
        return "neutral"
    return expected_direction_for_source(zone)


def classify_interaction(zone: Zone, hint: Optional[str] = None) -> str:
    """Не называть пересечение sweep, пока возврат не доказан."""
    if hint in INTERACTIONS:
        return hint
    evidence = zone.evidence or {}
    marked = evidence.get("interaction")
    if marked in INTERACTIONS:
        return marked
    if zone.type in (ZoneType.BSL, ZoneType.SSL):
        if evidence.get("reclaimed") or evidence.get("sweep_reclaim"):
            return "sweep_reclaim"
        return "level_cross"
    if zone.type == ZoneType.FVG:
        depth = float(getattr(zone, "max_test_depth", 0) or 0)
        if depth >= 1:
            return "full_fill"
        if depth > 0 or getattr(zone, "has_tests", False):
            return "partial_fill"
    return "touch"


def transition_key(instrument, break_open: int, direction: str) -> str:
    return "|".join([
        instrument.venue, instrument.market_type, str(instrument.id),
        "H1", str(int(break_open)), direction,
    ])


def break_key(instrument, break_open: int, direction: str, anchor: str) -> str:
    return transition_key(instrument, break_open, direction) + "|" + anchor


def notification_key(recipient: str, channel: str, transition: str, kind: str) -> str:
    return "|".join([str(recipient), channel, transition, kind])


def anchor_key(pivot_ref: Any, role: Any) -> str:
    """Происхождение опоры, без scenario_id и без округлённой цены."""
    return f"pivot:{pivot_ref}:{role or 'none'}"


def entry_segment(
    lower: float, upper: float, leg_low: float, eq: float, *, tick: float = 0,
) -> Optional[tuple[float, float]]:
    """Участок покупки внутри discount. EQ входит. Выше EQ — не участок."""
    lo = normalize_price(max(lower, leg_low), tick)
    hi = normalize_price(min(upper, eq), tick)
    if lo > hi:
        return None
    return lo, hi


def discount_relation(
    lower: float, upper: float, leg_low: float, eq: float, *, tick: float = 0,
) -> str:
    segment = entry_segment(lower, upper, leg_low, eq, tick=tick)
    if segment is None:
        return "none"
    seg_lo, seg_hi = segment
    zone_lo = normalize_price(lower, tick)
    zone_hi = normalize_price(upper, tick)
    if seg_lo <= zone_lo and zone_hi <= seg_hi:
        return "full"
    return "partial"


def observed_extreme(
    candles, start_open: int, as_of_close: int, *, side: str,
) -> Optional[tuple[float, int]]:
    """Экстремум только закрытых свечей, уже доступных на as_of."""
    best: Optional[tuple[float, int]] = None
    for candle in candles:
        if not candle.closed or candle.open_time < start_open:
            continue
        if candle.close_time > as_of_close:
            continue
        price = candle.high if side == "high" else candle.low
        if best is None:
            best = (price, candle.open_time)
            continue
        if side == "high" and price >= best[0]:
            best = (price, candle.open_time)
        elif side == "low" and price <= best[0]:
            best = (price, candle.open_time)
    return best


def original_extreme(pivots, direction: str, break_open: int, as_of: int):
    """Исходный LL восходящей ноги или HH нисходящей. Не последний HL/LH."""
    kind = "low" if direction == "bull" else "high"
    pool = [
        p for p in pivots
        if p.kind == kind and p.state == "confirmed"
        and p.pivot_at <= break_open and p.confirmed_at is not None
        and p.confirmed_at <= as_of
    ]
    if not pool:
        return None
    if direction == "bull":
        return min(pool, key=lambda p: (p.price, p.pivot_at))
    return max(pool, key=lambda p: (p.price, -p.pivot_at))


def explain_missing_zone(candles, direction: Direction, hint_low: float, hint_high: float) -> str:
    """Красная разметка без OHLC-паттерна зоной не становится."""
    from ..engine.fvg import scan_fvgs
    found = [
        f for f in scan_fvgs(candles, "H1")
        if f.direction == direction and not (f.upper < hint_low or f.lower > hint_high)
    ]
    if found:
        return "детектор нашёл FVG в этом диапазоне по OHLC"
    return (
        "детектор не нашёл OB/FVG по OHLC: между свечами нет разрыва "
        f"в диапазоне {hint_low}–{hint_high}. Линия на рисунке зону не создаёт."
    )


def _row(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _loads(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _commit(db) -> None:
    db._commit()
    db._bump_ltf_cache()


def load_episode(db, episode_id: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        "SELECT * FROM htf_context_episode WHERE id=?", (episode_id,)
    ).fetchone()
    return _row(row) if row else None


def open_episode(db, instrument_id: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND state='awaiting_h1'
           ORDER BY id DESC LIMIT 1""",
        (instrument_id,),
    ).fetchone()
    return _row(row) if row else None


def episode_sources(db, episode_id: int, as_of: Optional[int] = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM htf_context_source WHERE episode_id=?"
    args: list[Any] = [episode_id]
    if as_of is not None:
        q += " AND interaction_at<=?"
        args.append(as_of)
    q += " ORDER BY interaction_at, id"
    return [_row(r) for r in db.conn.execute(q, args).fetchall()]


def leg_at(db, episode_id: int, as_of: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        """SELECT * FROM htf_context_leg
           WHERE episode_id=? AND as_of<=?
           ORDER BY version DESC LIMIT 1""",
        (episode_id, as_of),
    ).fetchone()
    return _row(row) if row else None


def latest_leg(db, episode_id: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        """SELECT * FROM htf_context_leg
           WHERE episode_id=? ORDER BY version DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return _row(row) if row else None


# --------------------------------------------------------------------- #
# Машина состояний контекста (правила v1 §4)
#
# Эпизод — один контекст (направление + группа источников). Встречный
# кандидат B — новый эпизод со ссылкой predecessor_episode_id на A;
# возврат оформляется двумя раздельными фактами (проход B своим ТФ и
# обратный BOS/SMS H1), в любом порядке их доступности.
# --------------------------------------------------------------------- #

# состояния, в которых эпизод является действующим контекстом машины
_MACHINE_V2 = (
    "context_active", "opposing_confirmed",
    "opposing_source_breached_waiting_return", "context_resumed",
)


def _episode_base_direction(db, row: dict[str, Any]) -> Optional[str]:
    """Направление контекста эпизода: агрегат, а при конфликте (neutral) —
    направление первого источника (основание эпизода)."""
    direction = row.get("candidate_direction")
    if direction in ("bull", "bear"):
        return direction
    for source in episode_sources(db, int(row["id"])):
        marked = _loads(source.get("evidence")).get("direction")
        if marked in ("bull", "bear"):
            return marked
    return None


def current_context(db, instrument_id: int) -> Optional[dict[str, Any]]:
    """Действующий контекст машины §4.

    Пока кандидат B ждёт (opposing_pending, он же open_episode) — действует
    A. При context_resumed цепочка разрешается к предшественнику (A
    восстановлен без нового касания, правило 6)."""
    row = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND state_v2 IN ('context_active',
               'opposing_confirmed','opposing_source_breached_waiting_return',
               'context_resumed')
           ORDER BY id DESC LIMIT 1""",
        (instrument_id,),
    ).fetchone()
    if row is None:
        return None
    row = _row(row)
    seen = set()
    while (
        row is not None and row["state_v2"] == "context_resumed"
        and row["predecessor_episode_id"] is not None
        and int(row["id"]) not in seen
    ):
        seen.add(int(row["id"]))
        row = load_episode(db, int(row["predecessor_episode_id"]))
    return row


def causally_linked(source: dict[str, Any], event) -> bool:
    """§3: контакт → движение → (H1-зона движения) → BOS/SMS — одна
    причинная цепочка. Геометрическое нахождение слома внутри HTF-зоны не
    требуется (R32); часового фильтра нет (R35); один контакт служит
    основанием нескольких следующих BOS/SMS того же движения. Контакт и
    слом на одной H1 допустимы (R34)."""
    if event is None or not source.get("active", 1):
        return False
    return int(event.occurred_at) >= int(source["interaction_at"])


def opposing_contact_before(
    db, instrument_id: int, direction: Direction, at: int
) -> bool:
    """§6.1: есть ли встречный HTF-источник с доказанным контактом до
    момента at (основание квалифицированного обратного слома). Направление
    ожидания источника противоположно направлению сценария."""
    opposing = "bull" if direction == Direction.BEAR else "bear"
    rows = db.conn.execute(
        """SELECT s.interaction_at, s.active, s.evidence
           FROM htf_context_source s
           JOIN htf_context_episode e ON e.id = s.episode_id
           WHERE e.instrument_id=?""",
        (instrument_id,),
    ).fetchall()
    for row in rows:
        if not row["active"]:
            continue
        if _loads(row["evidence"]).get("direction") != opposing:
            continue
        if int(row["interaction_at"]) <= int(at):
            return True
    return False


def _aggregate_direction(sources: list[dict[str, Any]]) -> str:
    """Детерминированный агрегат направления активных источников (F12).

    Порядок вставки не влияет: источники сортируются по
    (interaction_at, zone_id, id). Нейтральные источники направления не
    задают; "neutral" — только когда направленных источников нет или их
    направления реально конфликтуют (F11), а не от перестановки вставки."""
    active = sorted(
        (s for s in sources if s.get("active", 1)),
        key=lambda s: (
            int(s["interaction_at"]),
            int(s.get("zone_id") or 0),
            int(s["id"]),
        ),
    )
    concrete = {
        d for d in (_loads(s.get("evidence")).get("direction") for s in active)
        if d and d != "neutral"
    }
    if len(concrete) == 1:
        return next(iter(concrete))
    return "neutral"


def _dir_ru(direction: Optional[str]) -> str:
    return {"bull": "вверх", "bear": "вниз"}.get(direction or "", "—")


def emit_context_transition(
    db, cfg, instrument_id: int, *, fact: str, text: str, occurred_at: int,
    dedupe_key: str, zone: Optional[Zone] = None,
    observation_id: Optional[int] = None,
    scenario_id: Optional[int] = None,
    detected_at: Optional[int] = None,
    processing_mode: Optional[str] = None,
    detection_lag_ms: Optional[int] = None,
) -> Optional[Any]:
    """§9: смысловое сообщение о переходе машины контекста.

    LtfEvent kind="context_transition" (доставка — группа bos_sms). Дедуп по
    ключу — повторная обработка не создаёт второго сообщения; лаг по общему
    правилу: catchup/replay — delayed (не доставляется), живой переход — live.
    detected_at — время обработки вызывающего (в replay детерминировано
    временем свечи; None — wall clock). processing_mode — явный режим
    вызывающего (replay/catchup пути движка; None — вычисляется по лагу).
    None, если событие уже было или нет наблюдения-носителя."""
    from ..models import now_ms
    from ..models_ltf import LtfEvent

    if observation_id is None and zone is not None:
        obs = db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
        observation_id = obs.id if obs is not None else None
    if observation_id is None:
        return None
    from ..engine.ltf.engine import _processing_mode
    now = int(detected_at) if detected_at is not None else now_ms()
    lag = (
        int(detection_lag_ms) if detection_lag_ms is not None
        else max(0, now - int(occurred_at))
    )
    mode = (
        processing_mode if processing_mode is not None
        else _processing_mode(lag, cfg.ltf_live_grace_seconds * 1000)
    )
    ev, created = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=observation_id, scenario_id=scenario_id,
        kind="context_transition", payload={"fact": fact, "text": text},
        occurred_at=int(occurred_at), detected_at=now, dedupe_key=dedupe_key,
        delayed=mode != "live", processing_mode=mode, detection_lag_ms=lag,
    ))
    return ev if created else None


def _first_source_zone(db, episode) -> Optional[Zone]:
    for source in episode_sources(db, int(episode["id"])):
        if source.get("zone_id"):
            zone = db.get_zone(int(source["zone_id"]))
            if zone is not None:
                return zone
    return None


def record_interaction(
    db, cfg, instrument_id: int, zone: Zone, occurred_at: int,
    interaction: Optional[str] = None, evidence: Optional[dict] = None,
) -> Optional[dict[str, Any]]:
    """Один эпизод инструмента впитывает новое взаимодействие до истечения.

    Повтор того же события срок не двигает. Невалидный OB выпадает из
    источников и не стирает остальные. Закрытый вручную эпизод не воскресает.

    Машина §4: контакт противоположного направления при действующем
    контексте A создаёт встречного кандидата B (predecessor = A, A не
    стирается); контакт направления A при живом кандидате B пишется в
    эпизод A, а не в B.
    """
    if zone.market_validity == "invalid":
        drop_source(db, instrument_id, zone.id, occurred_at, "market_invalid")
        return open_episode(db, instrument_id)
    kind = classify_interaction(zone, interaction)
    # R30: ручной источник, созданный ПОЗЖЕ заявленного контакта, не
    # причиняет задним числом то, что было до его появления. Эффективное
    # время контакта — не раньше времени доступности источника; заявленное
    # сохраняется в evidence источника
    known_at = int(getattr(zone, "created_at", 0) or 0) if zone.source == "manual" else 0
    effective_at = max(int(occurred_at), known_at)
    direction = candidate_direction(zone)
    current = open_episode(db, instrument_id)
    ctx = current_context(db, instrument_id)
    if (
        current is not None and ctx is not None
        and int(current["id"]) != int(ctx["id"]) and direction != "neutral"
    ):
        open_dir = _episode_base_direction(db, current)
        ctx_dir = _episode_base_direction(db, ctx)
        if (
            open_dir not in (None, "neutral") and direction != open_dir
            and ctx_dir not in (None, "neutral") and direction == ctx_dir
        ):
            current = ctx
    if (
        current is not None and current["state"] == "awaiting_h1"
        and int(current["expires_at"]) < int(effective_at)
    ):
        db.conn.execute(
            """UPDATE htf_context_episode
               SET state='expired', state_v2='expired',
                   closed_at=?, close_reason='wait_expired'
               WHERE id=? AND state='awaiting_h1'""",
            (effective_at, current["id"]),
        )
        _commit(db)
        current = None
    predecessor_id = None
    if current is None:
        # новый эпизод. §4 правило 1: при действующем контексте A
        # противоположного направления — это встречный кандидат B со
        # ссылкой на предшественника; контакт A не удаляет
        if ctx is not None and direction != "neutral":
            ctx_dir = _episode_base_direction(db, ctx)
            if ctx_dir not in (None, "neutral") and direction != ctx_dir:
                predecessor_id = int(ctx["id"])
        expires = int(effective_at) + wait_ms(cfg, zone.timeframe)
        basis = f"{zone.type.value}:{zone.timeframe}:{direction}"
        cur = db.conn.execute(
            """INSERT INTO htf_context_episode
               (instrument_id, started_at, last_distinct_interaction_at,
                candidate_direction, basis, state, expires_at, rule_version,
                evidence, state_v2, predecessor_episode_id)
               VALUES (?,?,?,?,?,'awaiting_h1',?,?,?,
                       'awaiting_initial_confirmation',?)""",
            (instrument_id, effective_at, effective_at, direction, basis,
             expires, RULE_VERSION, json.dumps(evidence or {}, ensure_ascii=False),
             predecessor_id),
        )
        _commit(db)
        current = load_episode(db, int(cur.lastrowid))
    assert current is not None
    payload = dict(evidence or {})
    if effective_at != int(occurred_at):
        payload["declared_interaction_at"] = int(occurred_at)
    payload["zone_type"] = zone.type.value
    payload["timeframe"] = zone.timeframe
    payload["source"] = zone.source
    payload["direction"] = direction
    cur = db.conn.execute(
        """INSERT OR IGNORE INTO htf_context_source
           (episode_id, zone_id, zone_version, interaction, interaction_at, evidence)
           VALUES (?,?,?,?,?,?)""",
        (current["id"], zone.id, int((zone.evidence or {}).get("boundary_version", 1) or 1),
         kind, effective_at, json.dumps(payload, ensure_ascii=False)),
    )
    inserted = cur.rowcount == 1
    if inserted:
        # F12: агрегат пересчитывается из множества активных источников —
        # итог не зависит от порядка прихода. F7: last/expiry монотонны —
        # запоздалый старый контакт не откатывает окно ожидания назад
        merged = _aggregate_direction(episode_sources(db, int(current["id"])))
        expires = int(effective_at) + wait_ms(cfg, zone.timeframe)
        db.conn.execute(
            """UPDATE htf_context_episode
               SET last_distinct_interaction_at=MAX(last_distinct_interaction_at, ?),
                   expires_at=MAX(expires_at, ?), candidate_direction=?
               WHERE id=?""",
            (effective_at, expires, merged, current["id"]),
        )
        if predecessor_id is not None:
            # §4 правило 1: кандидат B ожидает свою реакцию/структуру;
            # источник B сохранён на строке кандидата
            db.set_episode_state_v2(
                int(current["id"]), "opposing_pending",
                predecessor_episode_id=predecessor_id,
                opposing_source_id=int(cur.lastrowid),
            )
            # §9: сообщение о переходе в opposing_pending
            pred = load_episode(db, predecessor_id)
            text = (
                f"Основной контекст {_dir_ru(_episode_base_direction(db, pred))}. "
                f"Достигнут встречный HTF {_dir_ru(direction)}; ожидаем BOS/SMS H1"
            )
            emit_context_transition(
                db, cfg, instrument_id, fact="opposing_pending", text=text,
                occurred_at=effective_at,
                dedupe_key=f"ctxtr:{int(current['id'])}:opposing_pending",
                zone=zone,
            )
    _commit(db)
    return load_episode(db, int(current["id"]))


def drop_source(db, instrument_id: int, zone_id: int, now: int, reason: str) -> None:
    """Инвалидация одного OB снимает только его. Пустой эпизод закрывается."""
    episodes = db.conn.execute(
        """SELECT id FROM htf_context_episode
           WHERE instrument_id=? AND state='awaiting_h1'""",
        (instrument_id,),
    ).fetchall()
    for episode in episodes:
        db.conn.execute(
            "UPDATE htf_context_source SET active=0 WHERE episode_id=? AND zone_id=?",
            (episode["id"], zone_id),
        )
        left = db.conn.execute(
            "SELECT COUNT(*) AS n FROM htf_context_source WHERE episode_id=? AND active=1",
            (episode["id"],),
        ).fetchone()["n"]
        if int(left) == 0:
            db.conn.execute(
                """UPDATE htf_context_episode
                   SET state='invalidated', state_v2='invalidated',
                       invalidated_at=?, invalid_reason=?
                   WHERE id=?""",
                (now, reason, episode["id"]),
            )
        else:
            # F11: агрегат пересчитывается из оставшихся активных источников —
            # удалённый источник не продолжает поддерживать своё направление
            merged = _aggregate_direction(episode_sources(db, episode["id"]))
            db.conn.execute(
                "UPDATE htf_context_episode SET candidate_direction=? WHERE id=?",
                (merged, episode["id"]),
            )
    _commit(db)


def close_episode(db, episode_id: int, now: int, reason: str = "closed_by_user") -> None:
    db.conn.execute(
        """UPDATE htf_context_episode
           SET state='closed_by_user', state_v2='closed_by_user',
               closed_at=?, close_reason=?
           WHERE id=? AND state IN ('awaiting_h1','confirmed')""",
        (now, reason, episode_id),
    )
    _commit(db)


def close_episodes_for_scenario(db, scenario_id: int, now: int) -> None:
    rows = db.conn.execute(
        """SELECT id FROM htf_context_episode
           WHERE confirmed_scenario_id=? AND state='confirmed'""",
        (scenario_id,),
    ).fetchall()
    for row in rows:
        close_episode(db, int(row["id"]), now, "closed_by_user")


def note_source_outcome(db, zone: Zone, now: int, cfg=None) -> None:
    """Снятие и отработка источник оставляют. Пробой родителя — нет.

    Правила v1 §4: проход ВСТРЕЧНОГО источника B своим ТФ — факт (а)
    возврата A; он записывается в эпизод B машины, а не удаляет источник.
    """
    reason = zone.end_reason or ""
    passed = (
        zone.market_validity == "invalid"
        or reason.startswith(TERMINAL_SOURCE)
        or (zone.evidence or {}).get("pass_confirmed_at") is not None
    )
    if not passed:
        return
    # §7: подтверждённый проход собственного ТФ завершает текущую активацию
    # зоны (переходы контекста — §4; квалифицированная отмена — отдельно)
    for act in db.list_zone_activations(zone.id, zone.cycle_id):
        if act["ended_at"] is None:
            db.update_zone_activation(
                int(act["id"]), ended_at=now, end_reason="source_passed",
            )
    if not note_opposing_source_passed(db, zone, now, cfg=cfg):
        drop_source(db, zone.instrument_id, zone.id, now, reason or "market_invalid")


def _save_episode_evidence(db, episode_id: int, evidence: dict) -> None:
    db.conn.execute(
        "UPDATE htf_context_episode SET evidence=? WHERE id=?",
        (json.dumps(evidence, ensure_ascii=False), episode_id),
    )
    _commit(db)


def _maybe_resume(db, episode_id: int, cfg=None, detected_at: Optional[int] = None) -> None:
    """Переходы возврата A↔B по известным фактам (§4 правила 4–9).

    Оба факта известны (в любом порядке) → context_resumed, время возврата —
    доступность последнего (R07/R08). Только проход B → ожидание обратного
    слома. Цепочка длиннее A→B (A→B→C) → awaiting_clarity: произвольный
    старый контекст автоматически не извлекается (R37)."""
    row = load_episode(db, episode_id)
    if row is None:
        return
    pass_at = row["htf_pass_confirmed_at"]
    if pass_at is None:
        return  # известен лишь обратный слом — ждём закрытия своего ТФ за B
    pred = (
        load_episode(db, int(row["predecessor_episode_id"]))
        if row["predecessor_episode_id"] is not None else None
    )
    if pred is not None and pred["predecessor_episode_id"] is not None:
        db.set_episode_state_v2(int(row["id"]), "awaiting_clarity")
        return
    evidence = _loads(row["evidence"])
    rev = evidence.get("reverse_break")
    zone = _first_source_zone(db, row)
    if rev is None:
        db.set_episode_state_v2(
            int(row["id"]), "opposing_source_breached_waiting_return",
        )
        if cfg is not None:
            # §9: проход встречного HTF подтверждён, ждём обратный слом
            tf = zone.timeframe if zone is not None else "D1/W1"
            text = (
                f"Встречный HTF пройден закрытием {tf}; для возврата "
                f"{_dir_ru(_episode_base_direction(db, pred))}-сценария "
                f"ожидается обратный BOS/SMS H1"
            )
            emit_context_transition(
                db, cfg, row["instrument_id"],
                fact="opposing_source_breached_waiting_return", text=text,
                occurred_at=int(pass_at),
                dedupe_key=f"ctxtr:{int(row['id'])}:breached_waiting",
                zone=zone, detected_at=detected_at,
            )
        return
    resume_at = max(int(pass_at), int(rev["available_at"]))
    evidence["resumed_at"] = resume_at
    db.set_episode_state_v2(int(row["id"]), "context_resumed")
    _save_episode_evidence(db, int(row["id"]), evidence)
    if pred is not None:
        # A восстановлен без нового касания его источника (правило 6)
        db.set_episode_state_v2(int(pred["id"]), "context_resumed")
        if cfg is not None:
            # §9: предыдущий контекст восстановлен двумя фактами
            text = (
                f"Предыдущий {_dir_ru(_episode_base_direction(db, pred))}-контекст "
                f"восстановлен: проход встречного HTF и обратный BOS/SMS H1 "
                f"подтверждены"
            )
            emit_context_transition(
                db, cfg, row["instrument_id"], fact="context_resumed",
                text=text, occurred_at=resume_at,
                dedupe_key=f"ctxtr:{int(row['id'])}:context_resumed",
                zone=zone, detected_at=detected_at,
            )


def note_opposing_source_passed(db, zone: Zone, pass_at: int, cfg=None) -> bool:
    """Проход встречного источника B собственным ТФ — факт (а) возврата (§4).

    True — зона является источником действующего встречного контекста B и
    факт записан в его эпизод (обычный drop_source не требуется). Время —
    рыночное время закрытия своего ТФ (первая фиксация сохраняется)."""
    rows = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND state_v2 IN
               ('opposing_confirmed','opposing_source_breached_waiting_return')
           ORDER BY id""",
        (zone.instrument_id,),
    ).fetchall()
    consumed = False
    for row in rows:
        row = _row(row)
        direction = _episode_base_direction(db, row)
        own = [
            s for s in episode_sources(db, int(row["id"]))
            if s.get("zone_id") == zone.id
            and _loads(s.get("evidence")).get("direction") == direction
        ]
        if not own:
            continue
        consumed = True
        if row["htf_pass_confirmed_at"] is None:
            db.set_episode_state_v2(
                int(row["id"]), row["state_v2"], htf_pass_confirmed_at=int(pass_at),
            )
            _maybe_resume(db, int(row["id"]), cfg=cfg)
    return consumed


def _note_reverse_break_for_opposing(
    db, instrument_id: int, candle, avail, candles, now: int, cfg=None,
) -> None:
    """Факт (б) возврата: обратный BOS/SMS H1 в направлении A, пока действует
    встречный контекст B. Первый обратный слом сохраняется (правило 8/R36:
    ранее возникшего достаточно, новый BOS не обязателен); факты (а) и (б)
    могут прийти в любом порядке (правило 5)."""
    ctx = current_context(db, instrument_id)
    if ctx is None or ctx["state_v2"] not in (
        "opposing_confirmed", "opposing_source_breached_waiting_return"
    ):
        return
    if ctx["predecessor_episode_id"] is None:
        return
    pred = load_episode(db, int(ctx["predecessor_episode_id"]))
    if pred is None:
        return
    pred_dir = _episode_base_direction(db, pred)
    if pred_dir not in ("bull", "bear"):
        return
    evidence = _loads(ctx["evidence"])
    if evidence.get("reverse_break"):
        return
    event = _primary_break(
        avail, candles, Direction(pred_dir), candle, now,
        since_ms=int(ctx["started_at"]),
    )
    if event is None:
        return
    chain_sources = (
        episode_sources(db, int(ctx["id"]), candle.close_time)
        + episode_sources(db, int(pred["id"]), candle.close_time)
    )
    if not any(causally_linked(s, event) for s in chain_sources):
        return
    evidence["reverse_break"] = {
        "occurred_at": event.occurred_at,
        "available_at": int(candle.close_time),
        "break_candle_open_time": event.break_candle_open_time,
        "kind": event.kind,
        "direction": pred_dir,
    }
    _save_episode_evidence(db, int(ctx["id"]), evidence)
    _maybe_resume(db, int(ctx["id"]), cfg=cfg, detected_at=int(candle.close_time))


# --------------------------------------------------------------------- #
# Время жизни контекста (правила v1 §8): 60 дней от опоры ноги
# --------------------------------------------------------------------- #

def horizon_ms(cfg) -> int:
    return int(cfg.htf_context_horizon_days) * 86_400_000


def _leg_anchor_at(db, episode_id: int, policy: str) -> Optional[int]:
    """Время глобальной опоры ноги эпизода (§8).

    leg_extreme (provisional default, §12.1) — свеча последнего обновлённого
    экстремума ноги (high_candle_open последней версии ноги); leg_start —
    начало ноги (исходная опора = protected_candle_open). Поиск опор не
    ограничен последними 60 днями — происхождение ноги не теряется."""
    if policy == "leg_start":
        ep = load_episode(db, episode_id)
        if ep is not None and ep.get("protected_candle_open") is not None:
            return int(ep["protected_candle_open"])
        leg0 = latest_leg(db, episode_id)
        return int(leg0["as_of"]) if leg0 is not None else None
    leg = latest_leg(db, episode_id)
    if leg is not None and leg.get("high_candle_open") is not None:
        return int(leg["high_candle_open"])
    return None


def _apply_leg_anchor(db, cfg, episode_id: int, available_at: int) -> None:
    """Обновить якорь ноги и контрольную дату контекста (§8).

    Якорь пересчитывается при новой версии ноги (новый экстремум); версии
    ноги в htf_context_leg историю хранят сами. Контрольная дата = якорь +
    горизонт — только для подтверждённых состояний; неподтверждённое
    ожидание живёт на техническом 168ч сроке (R24)."""
    ep = load_episode(db, episode_id)
    if ep is None or (ep.get("state_v2") or "") not in _MACHINE_V2:
        return
    anchor = _leg_anchor_at(
        db, episode_id, getattr(cfg, "htf_context_leg_anchor", "leg_extreme"),
    )
    if anchor is None:
        return
    if ep.get("leg_anchor_at") is not None and int(ep["leg_anchor_at"]) == anchor:
        return
    db.conn.execute(
        """UPDATE htf_context_episode
           SET leg_anchor_at=?, leg_anchor_available_at=?, expires_at=?
           WHERE id=?""",
        (anchor, int(available_at), anchor + horizon_ms(cfg), episode_id),
    )
    _commit(db)


def _h1_coverage_gap(db, instrument_id: int, start_open: int, end_close: int) -> bool:
    """Пробел в закрытых H1 на окне проверки: отсутствие обнаруженного слома
    при пробеле не равно доказанному отсутствию (§8)."""
    candles = db.get_candles(instrument_id, "H1", start_ms=start_open)
    if not candles:
        return True
    expected = int(start_open)
    for c in candles:
        if c.open_time > end_close:
            break
        if c.open_time != expected:
            return True
        expected = c.open_time + HOUR_MS
    return expected + HOUR_MS - 1 <= int(end_close)


def _extend_horizon(db, cfg, episode_id: int, expires_at: int) -> None:
    db.conn.execute(
        """UPDATE htf_context_episode
           SET horizon_extended=1, expires_at=?
           WHERE id=?""",
        (int(expires_at) + horizon_ms(cfg), episode_id),
    )
    _commit(db)


def _check_horizon(db, cfg, instrument_id: int, candle) -> None:
    """§8: контрольная дата горизонта контекста.

    Дата истекла, а квалифицированного обратного слома нет — контекст НЕ
    закрывается: продление (horizon_extended=1) и сдвиг контрольной даты
    (R39). Неполные данные для проверки слома — сохранение контекста с
    пометкой качества (data_quality_note). Закрытие — только переходами
    §4/§5, календарная дата сама по себе не отменяет (R24)."""
    rows = db.conn.execute(
        """SELECT id FROM htf_context_episode
           WHERE instrument_id=? AND state_v2 IN
             ('context_active','opposing_confirmed',
              'opposing_source_breached_waiting_return')
           ORDER BY id""",
        (instrument_id,),
    ).fetchall()
    for r in rows:
        ep = load_episode(db, int(r["id"]))
        if ep is None or int(ep["expires_at"]) >= int(candle.close_time):
            continue
        if ep["leg_anchor_at"] is None:
            # эпизоды, подтверждённые до появления полей: якорь лениво
            _apply_leg_anchor(db, cfg, ep["id"], candle.close_time)
            ep = load_episode(db, int(r["id"]))
            if ep is None or ep["leg_anchor_at"] is None:
                continue
            if int(ep["expires_at"]) >= int(candle.close_time):
                continue
        evidence = _loads(ep["evidence"])
        if evidence.get("reverse_break"):
            continue  # переход возврата уже в работе (§4)
        if _h1_coverage_gap(
            db, instrument_id, int(ep["leg_anchor_at"]), int(candle.close_time)
        ):
            if not ep.get("data_quality_note"):
                db.conn.execute(
                    "UPDATE htf_context_episode SET data_quality_note=? WHERE id=?",
                    ("горизонт истёк; полнота данных для проверки обратного "
                     "слома не подтверждена — контекст сохранён", ep["id"]),
                )
                _commit(db)
        _extend_horizon(db, cfg, int(ep["id"]), int(ep["expires_at"]))


def context_state_v2(db, instrument_id: int) -> Optional[dict[str, Any]]:
    """Проекция машины §4 для UI/Telegram (шаблоны — отдельный этап).

    Основной контекст, встречный кандидат, какое условие возврата ещё
    ожидается (waiting_for: htf_pass_close | reverse_h1_break), сохранённый
    предшественник."""
    row = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND state IN ('awaiting_h1','confirmed')
           ORDER BY id DESC LIMIT 1""",
        (instrument_id,),
    ).fetchone()
    if row is None:
        return None
    latest = _row(row)
    v2 = latest["state_v2"] or (
        "context_active" if latest["state"] == "confirmed"
        else "awaiting_initial_confirmation"
    )
    current = current_context(db, instrument_id)
    out: dict[str, Any] = {
        "state_v2": v2,
        "episode_id": int(latest["id"]),
        "main": None, "opposing": None,
        "waiting_for": None, "predecessor": None,
        # §8: продление горизонта и якорь ноги — для отображения вместо
        # «истечения» (expires_at — контрольная дата, не срок контекста)
        "horizon_extended": bool(latest.get("horizon_extended")),
        "leg_anchor_at": latest.get("leg_anchor_at"),
        "data_quality_note": latest.get("data_quality_note"),
    }
    if current is not None:
        out["main"] = {
            "episode_id": int(current["id"]),
            "direction": _episode_base_direction(db, current),
        }
    if v2 in (
        "opposing_pending", "opposing_confirmed",
        "opposing_source_breached_waiting_return", "awaiting_clarity",
    ):
        source_zone = next(
            (s.get("zone_id") for s in episode_sources(db, int(latest["id"]))),
            None,
        )
        out["opposing"] = {
            "episode_id": int(latest["id"]),
            "direction": _episode_base_direction(db, latest),
            "source_zone_id": source_zone,
        }
        if latest["predecessor_episode_id"] is not None:
            pred = load_episode(db, int(latest["predecessor_episode_id"]))
            if pred is not None:
                out["predecessor"] = {
                    "episode_id": int(pred["id"]),
                    "direction": _episode_base_direction(db, pred),
                }
        evidence = _loads(latest["evidence"])
        if latest["htf_pass_confirmed_at"] is not None and not evidence.get("reverse_break"):
            out["waiting_for"] = "reverse_h1_break"
        elif evidence.get("reverse_break") and latest["htf_pass_confirmed_at"] is None:
            out["waiting_for"] = "htf_pass_close"
        out["htf_pass_confirmed_at"] = latest["htf_pass_confirmed_at"]
        out["reverse_break"] = evidence.get("reverse_break")
    return out


def remember_break(
    db, instrument, event, anchor: str, created_at: int, payload: dict,
) -> None:
    direction = event.direction.value if hasattr(event.direction, "value") else str(event.direction)
    key = transition_key(instrument, event.break_candle_open_time, direction)
    full = break_key(instrument, event.break_candle_open_time, direction, anchor)
    db.conn.execute(
        """INSERT OR IGNORE INTO market_transition
           (instrument_id, market_break_key, market_transition_key, direction,
            break_candle_open_time, anchor_key, payload, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (instrument.id, full, key, direction, event.break_candle_open_time,
         anchor, json.dumps(payload, ensure_ascii=False), created_at),
    )
    _commit(db)


def _primary_break(avail, candles, direction: Direction, candle, now: int, since_ms: int):
    """Причинное структурное событие свечи: BOS и SMS равноправны (§6.1),
    тип сохраняется (SMS не переименовывается в BOS). На одной свече оба —
    детерминированный выбор по доступности уровня: раньше подтверждённый
    pivot, затем level_key; тип в выборе не участвует."""
    scan = detect_breaks(
        avail, candles, direction, now, since_ms=since_ms, stop_on_cancellation=False,
    )
    hits = [
        e for e in scan.events
        if e.kind in ("BOS", "SMS") and e.stage == "primary"
        and e.occurred_at == candle.close_time
    ]
    if not hits:
        return None
    hits.sort(key=lambda e: (
        e.evidence.get("pivot_confirmed_at") or 0, e.level_key, e.kind,
    ))
    return hits[0]


def _order_unknown(sources: list[dict], candle) -> bool:
    for source in sources:
        if not source.get("active", 1):
            continue
        moment = int(source["interaction_at"])
        if candle.open_time <= moment <= candle.close_time:
            evidence = _loads(source.get("evidence"))
            if not evidence.get("order_proven"):
                return True
    return False


def plan_context_bar(db, cfg, instrument_id: int, candle, avail, candles, now: int) -> Optional[dict]:
    """Решение по бару до журнала отмен: ключ перехода и можно ли открыть long/short."""
    # §4: сначала факты машины возврата — обратный BOS/SMS в направлении A
    # фиксируется независимо от наличия ожидающего эпизода; §8: контрольная
    # дата горизонта (продление вместо календарного закрытия)
    _note_reverse_break_for_opposing(db, instrument_id, candle, avail, candles, now, cfg)
    _check_horizon(db, cfg, instrument_id, candle)
    current = open_episode(db, instrument_id)
    if current is None:
        return None
    if int(current["expires_at"]) < int(candle.close_time):
        db.conn.execute(
            """UPDATE htf_context_episode
               SET state='expired', state_v2='expired',
                   closed_at=?, close_reason='wait_expired'
               WHERE id=? AND state='awaiting_h1'""",
            (candle.close_time, current["id"]),
        )
        _commit(db)
        return {
            "episode_id": current["id"],
            "confirm": False,
            "wait_reason": "окно ожидания HTF-контекста истекло; нужен новый самостоятельный контакт",
            "transition_key": None,
            "direction": None,
        }
    sources = episode_sources(db, int(current["id"]), candle.close_time)
    since = int(current["started_at"])
    # Касание внутри свечи BOS не должно вырезать сам слом из скана:
    # порядок неизвестен, но факт закрытия виден.
    if any(
        s.get("active", 1) and candle.open_time <= int(s["interaction_at"]) <= candle.close_time
        for s in sources
    ):
        since = min(since, int(candle.open_time))
    bull = _primary_break(avail, candles, Direction.BULL, candle, now, since)
    bear = _primary_break(avail, candles, Direction.BEAR, candle, now, since)
    if bull is not None and bear is not None:
        return {
            "episode_id": current["id"],
            "confirm": False,
            "wait_reason": "на одном закрытии H1 есть BOS в обе стороны; направление не выбирается",
            "transition_key": None,
            "direction": None,
            "event": None,
        }
    event = bull or bear
    if event is None:
        return None
    direction = event.direction.value
    instrument = db.get_instrument(instrument_id)
    key = transition_key(instrument, event.break_candle_open_time, direction)
    if _order_unknown(sources, candle):
        db.conn.execute(
            "UPDATE htf_context_episode SET intrabar_order_unknown=1 WHERE id=?",
            (current["id"],),
        )
        _commit(db)
        return {
            "episode_id": current["id"],
            "confirm": False,
            "wait_reason": "порядок HTF-касания и BOS внутри одной H1 не доказан",
            "transition_key": key,
            "direction": direction,
            "event": event,
            "break_open": event.break_candle_open_time,
        }
    confirm = True
    wait = None
    # Правила v1 §3 (R09): пересечение уровня (level_cross) — полноценный
    # контакт для ожидания; направление задаёт источник (BSL→bear, SSL→bull).
    # §6.1 (R11): SMS подтверждает наравне с BOS — тип сохраняется в событии,
    # отдельного ожидания BOS не требуется
    if confirm and not context_supports_direction(db, current, sources, direction):
        confirm = False
        wait = (
            "сохранённый контекст не задаёт это направление; "
            "BOS сам по себе новое направление не открывает"
        )
    if confirm and original_extreme(
        avail, direction, event.break_candle_open_time, candle.close_time,
    ) is None:
        confirm = False
        wait = "нет исходной опоры причинной ноги"
    return {
        "episode_id": current["id"],
        "confirm": confirm,
        "wait_reason": wait,
        "transition_key": key,
        "direction": direction,
        "event": event,
        "break_open": event.break_candle_open_time,
    }


def _high_confirmed(pivots, extreme: float, start_open: int, as_of: int, tick: float) -> bool:
    for pivot in pivots:
        if pivot.kind != "high" or pivot.state != "confirmed":
            continue
        if pivot.confirmed_at is None or pivot.confirmed_at > as_of:
            continue
        if pivot.pivot_at < start_open:
            continue
        if abs(normalize_price(pivot.price, tick) - normalize_price(extreme, tick)) <= tick:
            return True
    return False


def save_leg(
    db, episode_id: int, low: float, high: float, eq: float, status: str,
    as_of: int, high_open: Optional[int], frozen: bool, tick: float,
) -> dict[str, Any]:
    """Новый наблюдаемый high — новая версия. Замороженная нога не растёт."""
    low = normalize_price(low, tick)
    high = normalize_price(high, tick)
    eq = normalize_price((low + high) / 2, tick)
    current = latest_leg(db, episode_id)
    if current is not None and int(current["frozen"]):
        return current
    if current is not None and current["status"] == status and current["low"] == low and current["high"] == high:
        return current
    version = 1 if current is None else int(current["version"]) + 1
    db.conn.execute(
        """INSERT INTO htf_context_leg
           (episode_id, version, low, high, eq, status, as_of, high_candle_open, frozen)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (episode_id, version, low, high, eq, status, as_of, high_open, int(frozen)),
    )
    _commit(db)
    return latest_leg(db, episode_id)


def _range_draft(direction: Direction, leg: dict) -> RangeDraft:
    return RangeDraft(
        direction=direction,
        lower=leg["low"],
        upper=leg["high"],
        mid=leg["eq"],
        anchor_low_ref=None,
        anchor_high_ref=None,
        available_at=int(leg["as_of"]),
        kind="reversal_leg",
        evidence={"range_status": leg["status"]},
    )


def pd_status_of(leg: Optional[dict]) -> Optional[str]:
    if leg is None:
        return None
    return "provisional" if leg["status"] == "provisional" else None


def context_supports_direction(db, episode, sources, direction: str) -> bool:
    """Long открывается, если источник нейтральный или уже этого направления.

    Только медвежий родитель продолжения не порождает второй сценарий на
    обратном BOS: отмена short остаётся, автоматический long — нет.
    """
    if episode.get("candidate_direction") in ("neutral", direction):
        return True
    for source in sources:
        if not source.get("active", 1):
            continue
        marked = _loads(source.get("evidence")).get("direction")
        if marked in ("neutral", direction):
            return True
        if source.get("zone_id"):
            zone = db.get_zone(int(source["zone_id"]))
            if zone is not None and candidate_direction(zone) in ("neutral", direction):
                return True
    return False


def apply_context_bar(
    engine, instrument_id: int, candle, avail, candles, now: int,
    processing_mode: str, detection_lag_ms: int, result, plan: Optional[dict],
) -> None:
    """После отмен этого закрытия: подтвердить противоположную идею, если план готов."""
    if not plan or not plan.get("confirm") or plan.get("event") is None:
        return
    db = engine.db
    episode = load_episode(db, int(plan["episode_id"]))
    if episode is None or episode["state"] != "awaiting_h1":
        return
    direction = Direction(plan["direction"])
    # Слом этой свечи уже открыл сценарий, даже если тот позже отменён:
    # replay не создаёт второй. Живое продолжение того же направления,
    # открытое более ранним SMS, тоже не плодит «разворот» той же стороны.
    # Эпизод остаётся для противоположного BOS.
    source_zone_ids = {
        int(s["zone_id"]) for s in episode_sources(db, int(episode["id"]))
        if s.get("zone_id")
    }
    same_candle = False
    live_same = False
    for obs in db.list_ltf_observations(instrument_id=instrument_id):
        for scenario in db.list_ltf_scenarios(observation_id=obs.id):
            if scenario.direction != direction:
                continue
            if scenario.state not in ("cancelled", "closed"):
                live_same = True
            if scenario.trigger_event_id is None:
                continue
            trigger = next(
                (e for e in db.list_ltf_structure_events(scenario.id)
                 if e.id == scenario.trigger_event_id),
                None,
            )
            if trigger is None or trigger.occurred_at != candle.close_time:
                continue
            same_candle = True
            # Причинное подтверждение контекста (§4): сценарий этого слома
            # от наблюдения встречного эпизода ИЛИ от наблюдения источника
            # этого эпизода (обычный путь: контакт → BOS от той же зоны, R01)
            if (obs.evidence or {}).get("reversal_episode_id") == int(episode["id"]) or (
                obs.zone_id in source_zone_ids
            ):
                _confirm_episode(
                    db, episode, scenario.id, now, avail, candles, plan["event"], direction,
                    cfg=engine.cfg, processing_mode=processing_mode,
                    detection_lag_ms=detection_lag_ms,
                )
                _apply_leg_anchor(
                    db, engine.cfg, int(episode["id"]), candle.close_time,
                )
    if same_candle or live_same:
        return
    event = plan["event"]
    extreme = original_extreme(avail, direction.value, event.break_candle_open_time, candle.close_time)
    if extreme is None:
        db.conn.execute(
            "UPDATE htf_context_episode SET evidence=? WHERE id=?",
            (json.dumps({"wait": "нет исходной опоры LL/HH"}, ensure_ascii=False), episode["id"]),
        )
        _commit(db)
        plan["confirm"] = False
        plan["wait_reason"] = "нет исходной опоры причинной ноги"
        return
    side = "high" if direction == Direction.BULL else "low"
    seen = observed_extreme(candles, extreme.pivot_at, candle.close_time, side=side)
    if seen is None:
        return
    instrument = db.get_instrument(instrument_id)
    tick = tick_size(instrument)
    frozen = False
    if direction == Direction.BULL:
        frozen = _high_confirmed(avail, seen[0], extreme.pivot_at, candle.close_time, tick)
    status = "confirmed" if frozen else "provisional"
    if direction == Direction.BULL:
        low, high = extreme.price, seen[0]
    else:
        low, high = seen[0], extreme.price
    leg = save_leg(
        db, int(episode["id"]), low, high, (low + high) / 2, status,
        candle.close_time, seen[1], frozen, tick,
    )
    protected_kind = "ll" if direction == Direction.BULL else "hh"
    db.conn.execute(
        """UPDATE htf_context_episode
           SET protected_price=?, protected_candle_open=?, protected_pivot_ref=?,
               protected_kind=?
           WHERE id=?""",
        (normalize_price(extreme.price, tick), extreme.pivot_at,
         extreme.pivot_id if extreme.pivot_id is not None else extreme.pivot_at,
         protected_kind, episode["id"]),
    )
    _commit(db)
    source = episode_sources(db, int(episode["id"]))
    zone_id = next((int(s["zone_id"]) for s in source if s.get("zone_id")), None)
    if zone_id is None:
        return
    from ..models_ltf import LtfObservation
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone_id,
        zone_version=1, cycle_id=CYCLE_BASE + int(episode["id"]),
        direction=direction, state="waiting_structure",
        activated_at=int(episode["started_at"]),
        created_at=now, updated_at=now,
        evidence={
            "reversal_episode_id": int(episode["id"]),
            "protected_price": normalize_price(extreme.price, tick),
            "protected_candle_open": extreme.pivot_at,
            "protected_pivot_ref": extreme.pivot_id if extreme.pivot_id is not None else extreme.pivot_at,
            "protected_kind": protected_kind,
            "pd_status": leg["status"] if leg else status,
        },
    ))
    before = set(result.scenarios_created)
    engine._maybe_open_scenario(
        obs, avail, candles, candle, now, processing_mode, detection_lag_ms, result,
    )
    opened = [sid for sid in result.scenarios_created if sid not in before]
    if not opened:
        return
    _confirm_episode(db, episode, opened[-1], now, avail, candles, event, direction,
                     cfg=engine.cfg, processing_mode=processing_mode,
                     detection_lag_ms=detection_lag_ms)
    _apply_leg_anchor(db, engine.cfg, int(episode["id"]), candle.close_time)


def _confirm_episode(db, episode, scenario_id: int, now: int, avail, candles, event, direction: Direction, cfg=None, processing_mode: Optional[str] = None, detection_lag_ms: int = 0) -> None:
    db.conn.execute(
        """UPDATE htf_context_episode
           SET state='confirmed', confirmed_scenario_id=?, confirmed_at=?
           WHERE id=? AND state='awaiting_h1'""",
        (scenario_id, now, episode["id"]),
    )
    _commit(db)
    # §4: подтверждённый встречный сценарий получает приоритет, предшественник
    # сохраняется; первый контекст — обычный context_active
    if episode.get("predecessor_episode_id") is not None:
        db.set_episode_state_v2(int(episode["id"]), "opposing_confirmed")
        if cfg is not None:
    # §9: сообщение о подтверждении встречного сценария
            pred = load_episode(db, int(episode["predecessor_episode_id"]))
            adj = "bullish" if direction == Direction.BULL else "bearish"
            pred_adj = "bullish" if _episode_base_direction(db, pred) == "bull" else "bearish"
            text = (
                f"Встречный {adj}-сценарий подтверждён от HTF. "
                f"Предыдущий {pred_adj}-контекст сохранён"
            )
            emit_context_transition(
                db, cfg, episode["instrument_id"], fact="opposing_confirmed",
                text=text, occurred_at=now,
                dedupe_key=f"ctxtr:{int(episode['id'])}:opposing_confirmed",
                zone=_first_source_zone(db, episode),
                scenario_id=scenario_id, detected_at=now,
                processing_mode=processing_mode,
                detection_lag_ms=detection_lag_ms,
            )
    else:
        db.set_episode_state_v2(int(episode["id"]), "context_active")


def refresh_reversal_leg(db, cfg, obs, avail, candles, candle) -> Optional[dict]:
    episode_id = (obs.evidence or {}).get("reversal_episode_id")
    if not episode_id:
        return None
    episode = load_episode(db, int(episode_id))
    if episode is None or episode["protected_price"] is None:
        return None
    start = int(episode["protected_candle_open"] or 0)
    direction = obs.direction.value if hasattr(obs.direction, "value") else str(obs.direction)
    side = "high" if direction == "bull" else "low"
    seen = observed_extreme(candles, start, candle.close_time, side=side)
    if seen is None:
        return latest_leg(db, int(episode_id))
    instrument = db.get_instrument(obs.instrument_id)
    tick = tick_size(instrument)
    frozen = bool(latest_leg(db, int(episode_id)) and latest_leg(db, int(episode_id))["frozen"])
    if direction == "bull" and not frozen:
        frozen = _high_confirmed(avail, seen[0], start, candle.close_time, tick)
    if direction == "bull":
        low, high = float(episode["protected_price"]), seen[0]
    else:
        low, high = seen[0], float(episode["protected_price"])
    leg = save_leg(
        db, int(episode_id), low, high, (low + high) / 2,
        "confirmed" if frozen else "provisional",
        candle.close_time, seen[1], frozen, tick,
    )
    # §8: новый экстремум ноги обновляет якорь и контрольную дату (без
    # переписывания истории — версии ноги хранятся)
    _apply_leg_anchor(db, cfg, int(episode_id), candle.close_time)
    return leg


def reversal_range_for(db, obs, fallback, scenario_direction: Direction):
    """Диапазон допуска разворота. Continuation-диапазон не подменяется в БД."""
    episode_id = (obs.evidence or {}).get("reversal_episode_id") if obs is not None else None
    if not episode_id:
        return fallback, None
    leg = latest_leg(db, int(episode_id))
    if leg is None:
        return fallback, None
    return _range_draft(scenario_direction, leg), pd_status_of(leg)


def reversal_projection(db, instrument_id: int, as_of: int, cfg=None) -> Optional[dict[str, Any]]:
    """Одна проекция зон, PD и причин на отсечку. Будущий high не входит."""
    row = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND started_at<=?
           ORDER BY id DESC LIMIT 1""",
        (instrument_id, as_of),
    ).fetchone()
    if row is None:
        return None
    episode = _row(row)
    if episode["state"] == "confirmed" and episode["confirmed_at"] and int(episode["confirmed_at"]) > as_of:
        episode["state"] = "awaiting_h1"
        episode["confirmed_scenario_id"] = None
    leg = leg_at(db, int(episode["id"]), as_of)
    instrument = db.get_instrument(instrument_id)
    tick = tick_size(instrument) if instrument is not None else 0
    sources = []
    for source in episode_sources(db, int(episode["id"]), as_of):
        evidence = _loads(source.get("evidence"))
        zone = db.get_zone(source["zone_id"]) if source.get("zone_id") else None
        sources.append({
            "zone_id": source.get("zone_id"),
            "interaction": source["interaction"],
            "interaction_at": source["interaction_at"],
            "active": bool(source.get("active", 1)),
            "type": evidence.get("zone_type") or (zone.type.value if zone else None),
            "timeframe": evidence.get("timeframe") or (zone.timeframe if zone else None),
            "source": evidence.get("source") or (zone.source if zone else None),
        })
    pd = None
    if leg is not None:
        pd = {
            "L": leg["low"], "H": leg["high"], "EQ": leg["eq"],
            "status": leg["status"], "version": leg["version"],
            "as_of": leg["as_of"],
            "label": "подтверждена" if leg["status"] == "confirmed" else "предварительная",
        }
    zones = []
    scenario_id = episode.get("confirmed_scenario_id")
    if scenario_id and episode["state"] == "confirmed" and leg is not None:
        scenario = db.get_ltf_scenario(int(scenario_id))
        if scenario is not None:
            draft = _range_draft(scenario.direction, leg)
            status = pd_status_of(leg)
            for entry in db.list_ltf_scenario_entries(scenario.id):
                zone = db.get_ltf_entry_zone(entry.entry_zone_id)
                if zone is None or zone.formed_at > as_of:
                    continue
                if zone.confirmed_at and zone.confirmed_at > as_of:
                    continue
                decision = evaluate_entry(
                    zone, scenario.direction, cfg or _cfg_of(db), draft,
                    movements=db.list_ltf_movements(scenario.id),
                    liquidity_tests=[
                        t for t in db.list_ltf_liquidity_tests(scenario_id=scenario.id)
                        if t.touch_at <= as_of
                    ],
                    pd_status=status,
                )
                segment = None
                if decision.reason in ("ok", "eligible_provisional"):
                    if scenario.direction == Direction.BULL:
                        segment = entry_segment(
                            zone.lower, zone.upper, leg["low"], leg["eq"], tick=tick,
                        )
                    else:
                        segment = _premium_segment(
                            zone.lower, zone.upper, leg["eq"], leg["high"], tick,
                        )
                allowed = decision.reason in ("ok", "eligible_provisional")
                zones.append({
                    "id": zone.id,
                    "type": zone.type,
                    "direction": zone.direction.value,
                    "lower": zone.lower,
                    "upper": zone.upper,
                    "formed_at": zone.formed_at,
                    "reason": decision.reason,
                    "overlap": decision.overlap,
                    "segment": list(segment) if segment else None,
                    "allowed": allowed,
                })
    protected = None
    if episode.get("protected_price") is not None:
        protected = {
            "price": episode["protected_price"],
            "kind": episode["protected_kind"],
            "candle_open": episode["protected_candle_open"],
            "pivot_ref": episode["protected_pivot_ref"],
            "rule": (
                "закрытие H1 ниже исходного LL отменяет long; фитиль только записывается"
                if episode.get("protected_kind") == "ll"
                else "закрытие H1 выше исходного HH отменяет short; фитиль только записывается"
            ),
        }
    return {
        "episode_id": episode["id"],
        "state": episode["state"],
        "candidate_direction": episode["candidate_direction"],
        "intrabar_order_unknown": bool(episode.get("intrabar_order_unknown")),
        "sources": sources,
        "pd": pd,
        "protected": protected,
        "zones": zones,
        "as_of": as_of,
        "rule_version": episode["rule_version"],
    }


def _premium_segment(lower, upper, eq, high, tick):
    lo = normalize_price(max(lower, eq), tick)
    hi = normalize_price(min(upper, high), tick)
    if lo > hi:
        return None
    return lo, hi


def _cfg_of(db):
    from ..config import DetectorConfig
    return DetectorConfig()


def transition_card_text(events, ctx, projection: Optional[dict]) -> str:
    """Одна карточка перехода: отмены, long если есть, источники, PD, зоны."""
    symbol = ctx.instrument.symbol if ctx and ctx.instrument else "Инструмент"
    cancels = [e for e in events if e.kind == "cancellation"]
    confirmed = [
        e for e in events
        if e.kind in ("bos", "sms") and (e.payload or {}).get("reversal_confirmed")
    ]
    wait = next(
        ((e.payload or {}).get("long_wait_reason") for e in events
         if (e.payload or {}).get("long_wait_reason")),
        None,
    )
    direction = None
    for e in events:
        direction = (e.payload or {}).get("break_direction") or direction
    up = direction == "bull"
    kind = confirmed[0].kind.upper() if confirmed else "BOS"
    if confirmed and cancels:
        head = (
            f"{kind} H1 вверх подтверждён. Медвежий сценарий отменён; бычий сценарий подтверждён."
            if up else
            f"{kind} H1 вниз подтверждён. Бычий сценарий отменён; медвежий сценарий подтверждён."
        )
    elif confirmed:
        head = f"{kind} H1 вверх подтверждён. Бычий сценарий подтверждён." if up else (
            f"{kind} H1 вниз подтверждён. Медвежий сценарий подтверждён."
        )
    elif cancels:
        head = "Сценарий отменён одним закрытием H1."
        if len(cancels) > 1:
            head = f"Отменено сценариев: {len(cancels)}. Это один рыночный слом."
    else:
        head = "Переход H1."
    lines = [f"{symbol} · H1", head]
    if wait and not confirmed:
        lines.append(f"Long не подтверждён: {wait}")
    if projection:
        bits = []
        for source in projection.get("sources") or []:
            label = f"{source.get('type') or 'зона'} {source.get('timeframe') or ''}".strip()
            bits.append(f"{label} · {source.get('interaction')}")
        if bits:
            lines.append("Контекст: " + "; ".join(bits))
        pd = projection.get("pd")
        if pd:
            lines.append(
                f"PD: LL {pd['L']} → наблюдаемый high {pd['H']}; 50% {pd['EQ']}. "
                f"Верхняя опора {pd['label']}."
            )
        allowed = [z for z in projection.get("zones") or [] if z.get("allowed")]
        if allowed:
            rendered = ", ".join(
                f"{z['type']} {z['lower']}–{z['upper']}"
                + (" · предварительный PD" if z["reason"] == "eligible_provisional" else "")
                for z in allowed
            )
            lines.append("Ожидаем откат к зонам H1 в discount: " + rendered)
        elif projection.get("state") == "confirmed":
            rejected = [z for z in projection.get("zones") or [] if not z.get("allowed")]
            if rejected:
                lines.append(
                    "Готового входа нет: " + "; ".join(
                        f"{z['type']} {z['lower']}–{z['upper']} · {z['reason']}" for z in rejected
                    )
                )
            else:
                lines.append("Подходящих зон в discount пока нет. Подтверждение сценария не означает готовность входа.")
        protected = projection.get("protected")
        if protected and confirmed:
            lines.append(f"Отмена: {protected['price']}. {protected['rule']}")
    return "\n".join(lines)


def source_summary(db, episode_id: int) -> list[dict[str, Any]]:
    return episode_sources(db, episode_id)
