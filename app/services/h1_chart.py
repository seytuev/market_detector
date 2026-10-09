"""Снимок слоёв H1: точки, BOS/SMS и зоны инструмента.

Лимит 20 — только подписи основных точек. Расчёт структуры, опоры событий
и зоны им не ограничены. BOS/SMS берутся из той же машины пробоев
(``scan_instrument_structure``): смена роли без закрытия строго за уровнем
событие не создаёт. Снимок ничего не пишет в БД и не рассылает сигналы.
Визуальный лимит не является версией торговой стратегии.
"""
from __future__ import annotations

from typing import Any, Optional

from ..db import Database
from ..engine.ltf.breaks import (
    StructureEventDraft,
    expected_structure_conditions,
    scan_instrument_structure,
)
from ..engine.ltf.entries import (
    build_movement,
    detect_entry_zones,
    fvg_fill_status,
)
from ..engine.ltf.pivots import PivotCandidate
from ..engine.ltf.ranges import current_range
from ..models import Direction, now_ms
from ..models_ltf import RULE_VERSION_LTF, LtfEntryZone, LtfPivot

STRUCTURAL_ROLES = frozenset({"HH", "HL", "LH", "LL"})
DIAGNOSTIC_ROLES = frozenset({"internal_high", "internal_low", "none"})
MARKER_LIMIT = 20
HISTORY_PAD_MS = 48 * 3_600_000
DISPLAY_WINDOW_MS = 7 * 86_400_000
DISPLAY_WINDOW_LABEL = (
    "Окно показа 7 суток после снятия. Это не продление уровня."
)
H1_MS = 3_600_000

LABEL_BEAR_WAIT = "Слом вниз подтверждён; ждём нисходящую последовательность опор"
LABEL_BEAR_PAIR = "Нисходящая структура подтверждена: LH → LL"
LABEL_BULL_WAIT = "Слом вверх подтверждён; ждём восходящую последовательность опор"
LABEL_BULL_PAIR = "Восходящая структура подтверждена: HL → HH"
LABEL_AMBIGUOUS = "Направление структуры не подтверждено"
ADMISSION_UNRATED = "Пригодность для сценария не оценивалась"

POINT_MODES = frozenset({"recent", "history", "hidden"})


def role_at(pivot: LtfPivot, log: list[dict[str, Any]], as_of: int) -> str:
    """Роль, известная на as_of. Без журнала — текущая, если она уже назначена."""
    if not log:
        if pivot.role_assigned_at and pivot.role_assigned_at > as_of:
            return "none"
        return pivot.role or "none"
    known: Optional[str] = None
    for row in log:
        if row["changed_at"] > as_of:
            if known is None:
                return row["old_role"] or "none"
            break
        known = row["new_role"]
    if known is None:
        return pivot.role or "none"
    return known


def select_pivot_markers(
    rows: list[dict[str, Any]],
    *,
    as_of: int,
    mode: str = "recent",
    window_from: Optional[int] = None,
    window_to: Optional[int] = None,
    include_diagnostic: bool = False,
    limit: int = MARKER_LIMIT,
    page: int = 0,
) -> dict[str, Any]:
    """Последние ``limit`` основных точек либо история видимого окна.

    ``mode=recent`` не смотрит на окно: это последние точки на as_of.
    Диагностические роли в лимит 20 не входят. Superseded и дубли одной
    опоры текущей версии уже должны быть сняты вызывающим; здесь повторно
    отбрасываются superseded и дубль (pivot_at, kind, price).
    """
    if mode not in POINT_MODES:
        mode = "recent"
    deduped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if row.get("superseded_by"):
            continue
        if row.get("pivot_at") is None or row["pivot_at"] > as_of:
            continue
        confirmed_at = row.get("confirmed_at")
        state = row.get("state") or "candidate"
        if state == "confirmed" and (confirmed_at is None or confirmed_at > as_of):
            continue
        if state not in ("confirmed", "ambiguous", "candidate"):
            continue
        key = (row["pivot_at"], row.get("kind"), row.get("price"))
        rank = (row.get("calc_version_id") or 0, row.get("id") or 0)
        prev = deduped.get(key)
        if prev is None or rank >= prev[0]:
            deduped[key] = (rank, row)
    ordered = sorted(
        (item[1] for item in deduped.values()),
        key=lambda r: (r["pivot_at"], r.get("id") or 0),
    )
    structural = [
        r for r in ordered
        if r.get("state") == "confirmed" and r.get("role") in STRUCTURAL_ROLES
    ]
    diagnostic = [
        r for r in ordered
        if r.get("state") == "ambiguous"
        or r.get("role") in DIAGNOSTIC_ROLES
        or (
            r.get("state") == "confirmed"
            and r.get("role") not in STRUCTURAL_ROLES
        )
    ]
    total_structural = len(structural)
    if mode == "hidden":
        chosen: list[dict[str, Any]] = []
        has_more = False
        page_index = 0
    elif mode == "history":
        start = (window_from - HISTORY_PAD_MS) if window_from is not None else None
        end = (window_to + HISTORY_PAD_MS) if window_to is not None else None
        if start is not None:
            structural = [r for r in structural if r["pivot_at"] >= start]
            diagnostic = [r for r in diagnostic if r["pivot_at"] >= start]
        if end is not None:
            structural = [r for r in structural if r["pivot_at"] <= end]
            diagnostic = [r for r in diagnostic if r["pivot_at"] <= end]
        page_index = max(0, page)
        begin = page_index * limit
        chosen = structural[begin:begin + limit]
        has_more = begin + limit < len(structural)
    else:
        chosen = structural[-limit:]
        has_more = False
        page_index = 0
    if include_diagnostic and mode != "hidden":
        seen = {r.get("id") for r in chosen}
        for row in diagnostic:
            if row.get("id") not in seen:
                chosen.append(row)
        chosen.sort(key=lambda r: (r["pivot_at"], r.get("id") or 0))
    return {
        "points": chosen,
        "selection": {
            "mode": mode,
            "limit": limit,
            "total_structural": total_structural,
            "shown": len([r for r in chosen if r.get("role") in STRUCTURAL_ROLES]),
            "order": "pivot_at,id",
            "as_of": as_of,
            "window_from": window_from,
            "window_to": window_to,
            "page": page_index,
            "has_more": has_more,
            "diagnostic": include_diagnostic,
        },
    }


def annotate_epochs(events: list[StructureEventDraft]) -> None:
    """Эпоха структурной машины: новый primary BOS другого направления
    открывает эпоху. Повторный primary той же стороны — продолжение и
    время первого слома не двигает."""
    epoch = 0
    direction: Optional[Direction] = None
    ordered = sorted(
        events,
        key=lambda e: (
            e.occurred_at,
            0 if e.kind == "BOS" and e.stage == "primary" else 1,
            e.level_key,
        ),
    )
    for ev in ordered:
        continuation = False
        before = direction
        if ev.kind == "BOS" and ev.stage == "primary":
            if direction is not None and ev.direction == direction:
                continuation = True
            else:
                epoch += 1
                direction = ev.direction
        after = direction
        ev.evidence = {
            **ev.evidence,
            "epoch": epoch,
            "continuation": continuation,
            "structure_before": before.value if before is not None else None,
            "structure_after": after.value if after is not None else None,
        }


def structure_transition(
    events: list[StructureEventDraft],
    pivots: list[PivotCandidate],
    as_of: int,
) -> dict[str, Any]:
    """Стадия смены структуры по эпохе машины и связанной паре ``current_range``.

    «Пара подтверждена» — последняя связанная LH→LL или HL→HH, чьё
    подтверждение позже причинного слома. Две любые последние подписи
    пару не создают. Время слома остаётся временем его свечи.
    """
    primaries = [
        e for e in events
        if e.kind == "BOS" and e.stage == "primary" and e.occurred_at <= as_of
    ]
    primaries.sort(key=lambda e: (e.occurred_at, e.level_key))
    if not primaries:
        return _transition_body("ambiguous", LABEL_AMBIGUOUS, None, None, None, False)
    direction: Optional[Direction] = None
    causal: Optional[StructureEventDraft] = None
    for ev in primaries:
        if direction is None or ev.direction != direction:
            direction = ev.direction
            causal = ev
    assert causal is not None and direction is not None
    pair = _pair_after(pivots, direction, causal.occurred_at, as_of)
    if direction == Direction.BEAR:
        stage = "bear_pair" if pair else "bear_waiting"
        label = LABEL_BEAR_PAIR if pair else LABEL_BEAR_WAIT
    else:
        stage = "bull_pair" if pair else "bull_waiting"
        label = LABEL_BULL_PAIR if pair else LABEL_BULL_WAIT
    return _transition_body(stage, label, direction, causal, pair, False)


def _transition_body(
    stage: str,
    label: str,
    direction: Optional[Direction],
    causal: Optional[StructureEventDraft],
    pair: Optional[dict[str, Any]],
    continuation: bool,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "label": label,
        "direction": direction.value if direction is not None else None,
        "causal_event_key": causal.level_key if causal is not None else None,
        "causal_event_id": _event_id(causal) if causal is not None else None,
        "break_at": causal.occurred_at if causal is not None else None,
        "break_candle_open_time": (
            causal.break_candle_open_time if causal is not None else None
        ),
        "pair": pair,
        "continuation": continuation,
    }


def _pair_after(
    pivots: list[PivotCandidate],
    direction: Direction,
    break_at: int,
    as_of: int,
) -> Optional[dict[str, Any]]:
    rng = current_range(pivots, direction, as_of)
    if rng is None or rng.available_at <= break_at:
        return None
    kind = "LH_LL" if direction == Direction.BEAR else "HL_HH"
    return {
        "low_pivot_id": rng.anchor_low_ref,
        "high_pivot_id": rng.anchor_high_ref,
        "confirmed_at": rng.available_at,
        "kind": kind,
        "lower": rng.lower,
        "upper": rng.upper,
    }


def _event_id(ev: StructureEventDraft) -> str:
    return f"h1:{ev.level_key}:{ev.stage}:{ev.occurred_at}"


def _to_candidate(pivot: LtfPivot, role: str) -> PivotCandidate:
    return PivotCandidate(
        instrument_id=pivot.instrument_id,
        price=pivot.price,
        kind=pivot.kind,
        pivot_at=pivot.pivot_at,
        candle_open_time=pivot.candle_open_time,
        confirmed_at=pivot.confirmed_at or 0,
        left=pivot.left,
        right=pivot.right,
        state=pivot.state,
        pivot_id=pivot.id,
        role=role,
    )


def _pivot_row(pivot: LtfPivot, role: str) -> dict[str, Any]:
    row = pivot.to_dict()
    row["role"] = role
    return row


def _level_pivot_id(ev: StructureEventDraft) -> Optional[int]:
    raw = ev.evidence.get("broken_pivot_id")
    if raw is None and ev.ref_pivot_ids:
        raw = ev.ref_pivot_ids[-1]
    return int(raw) if raw is not None else None


def _event_dict(
    ev: StructureEventDraft,
    pivots_by_id: dict[int, PivotCandidate],
    scenario_ids: list[int],
) -> dict[str, Any]:
    level_id = _level_pivot_id(ev)
    level = pivots_by_id.get(level_id) if level_id is not None else None
    return {
        "id": _event_id(ev),
        "kind": ev.kind,
        "stage": ev.stage,
        "direction": ev.direction.value,
        "break_level": ev.break_level,
        "level_pivot_id": level.pivot_id if level is not None else level_id,
        "level_pivot_at": level.pivot_at if level is not None else None,
        "break_candle_open_time": ev.break_candle_open_time,
        "confirmed_at": ev.occurred_at,
        "occurred_at": ev.occurred_at,
        "detected_at": ev.detected_at,
        "epoch": ev.evidence.get("epoch"),
        "continuation": bool(ev.evidence.get("continuation")),
        "accompanying": ev.accompanying,
        "level_key": ev.level_key,
        "origin_known": level is not None,
        "scenario_ids": scenario_ids,
        "structure_before": ev.evidence.get("structure_before"),
        "structure_after": ev.evidence.get("structure_after"),
        "ref_pivot_ids": list(ev.ref_pivot_ids),
    }


def _scenario_index(db: Database, instrument_id: int) -> dict[tuple, list[int]]:
    """Связь старых событий сценария с фактом машины. Цена сама по себе
    ключом не является: совпадают level_key, kind, stage и occurred_at."""
    index: dict[tuple, list[int]] = {}
    for level_key, kind, stage, occurred_at, scenario_id in db.list_ltf_structure_links(
        instrument_id
    ):
        key = (level_key, kind, stage, occurred_at)
        bucket = index.setdefault(key, [])
        if scenario_id not in bucket:
            bucket.append(scenario_id)
    return index


# Живой снимок идей. Исторический as_of сюда не кладётся: прошлый момент
# должен остаться активным, даже если сейчас идея уже закрыта.
_LIVE_PROJECTION: dict[tuple, tuple] = {}


def _project_for_chart(db, instrument_id, cfg, moment, zones, candles, *, live: bool):
    from .htf_ideas import project_ideas
    from ..engine.ltf.relevance import VERSION
    last = db.last_candle(instrument_id, "H1")
    cacheable = live and last is not None and moment >= last.close_time
    seq = db.get_state_seq() if cacheable else None
    key = (id(db), instrument_id)
    if cacheable:
        hit = _LIVE_PROJECTION.get(key)
        if hit is not None and hit[0] == seq and hit[1] == VERSION:
            return hit[2]
    result = project_ideas(
        db, instrument_id, cfg, moment, zones=zones, candles=candles,
    )
    if cacheable:
        _LIVE_PROJECTION[key] = (seq, VERSION, result)
    return result


def _geometry_key(type_: str, direction: str, formed_at: int, lower: float, upper: float):
    return (type_, direction, formed_at, round(lower, 8), round(upper, 8))


def _clip_tests(zone: LtfEntryZone, as_of: int) -> tuple[Optional[int], float, Optional[float]]:
    """На историческом as_of будущий тест ещё не случился."""
    if zone.first_test_at is not None and zone.first_test_at <= as_of:
        return zone.first_test_at, zone.max_test_depth, zone.test_extreme
    return None, 0.0, None


def _sparse(values: list[float], prefer_high: bool) -> list[list[float]]:
    """Разреженная таблица минимума или максимума. Запрос отрезка — O(1)."""
    n = len(values)
    if n == 0:
        return []
    table = [values]
    span = 1
    while span * 2 <= n:
        prev = table[-1]
        row = [0.0] * (n - span * 2 + 1)
        for i in range(len(row)):
            left = prev[i]
            right = prev[i + span]
            if prefer_high:
                row[i] = left if left >= right else right
            else:
                row[i] = left if left <= right else right
        table.append(row)
        span *= 2
    return table


def _range_pick(table: list[list[float]], left: int, right: int, prefer_high: bool) -> float:
    length = right - left + 1
    k = length.bit_length() - 1
    span = 1 << k
    a = table[k][left]
    b = table[k][right - span + 1]
    if prefer_high:
        return a if a >= b else b
    return a if a <= b else b


def _first_index(
    table: list[list[float]],
    start: int,
    n: int,
    prefer_high: bool,
    level: float,
    at_least: bool,
) -> Optional[int]:
    """Первый индекс >= start, где значение строго за level.

    at_least=True ищет максимум >= level или минимум <= level.
    Иначе — максимум > level или минимум < level.
    """
    if start >= n:
        return None
    whole = _range_pick(table, start, n - 1, prefer_high)
    if prefer_high:
        hit = whole >= level if at_least else whole > level
    else:
        hit = whole <= level if at_least else whole < level
    if not hit:
        return None
    lo, hi = start, n
    while lo < hi:
        mid = (lo + hi) // 2
        cur = _range_pick(table, start, mid, prefer_high)
        if prefer_high:
            ok = cur >= level if at_least else cur > level
        else:
            ok = cur <= level if at_least else cur < level
        if ok:
            hi = mid
        else:
            lo = mid + 1
    return lo


class _CandleIndex:
    """Закрытые свечи до as_of. Снятие уровня и заполнение FVG ищутся
    по всей жизни зоны, а не только по окну расчёта структуры."""

    def __init__(self, candles, as_of: int):
        rows = [c for c in candles if c.closed and c.close_time <= as_of]
        self.open_time = [c.open_time for c in rows]
        self.close_time = [c.close_time for c in rows]
        self.high = [c.high for c in rows]
        self.low = [c.low for c in rows]
        close = [c.close for c in rows]
        self._max_close = _sparse(close, True)
        self._min_close = _sparse(close, False)
        self._max_high = _sparse(self.high, True)
        self._min_low = _sparse(self.low, False)

    def start(self, after_ms: int) -> int:
        lo, hi = 0, len(self.open_time)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.open_time[mid] <= after_ms:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _close_time(self, index: Optional[int]) -> Optional[int]:
        if index is None:
            return None
        return self.close_time[index]


def _lifecycle_index(db: Database, instrument_id: int, candles, stored, moment: int, load_from: int):
    start = load_from
    if stored:
        earliest = min(zone.formed_at for zone in stored)
        if earliest < start:
            start = earliest
    series = candles
    if start < load_from:
        series = db.get_candles(
            instrument_id, "H1", start_ms=start, end_ms=moment, closed_only=False,
        )
    return _CandleIndex(series, moment)


def _fvg_filled_at(zone: LtfEntryZone, index: _CandleIndex) -> Optional[int]:
    if zone.type != "FVG":
        return None
    after = zone.confirmed_at or zone.formed_at
    i = index.start(after)
    n = len(index.open_time)
    # Полное заполнение — первая свеча, которая касается дальней границы.
    # Свеча целиком по ту сторону зоны касанием не считается: то же правило,
    # что у прежнего прохода по свечам.
    if zone.direction == Direction.BULL:
        far = zone.lower
        while i < n:
            touched = _first_index(index._min_low, i, n, False, far, True)
            if touched is None:
                return None
            if index.high[touched] >= far:
                return index.close_time[touched]
            nxt = _first_index(index._max_high, touched + 1, n, True, far, True)
            if nxt is None:
                return None
            i = nxt
        return None
    far = zone.upper
    while i < n:
        touched = _first_index(index._max_high, i, n, True, far, True)
        if touched is None:
            return None
        if index.low[touched] <= far:
            return index.close_time[touched]
        nxt = _first_index(index._min_low, touched + 1, n, False, far, True)
        if nxt is None:
            return None
        i = nxt
    return None


def _level_facts(kind: str, level: float, index: _CandleIndex, after_ms: int):
    start = index.start(after_ms)
    n = len(index.open_time)
    if kind == "BSL":
        cross_i = _first_index(index._max_close, start, n, True, level, False)
        reclaim_i = (
            _first_index(index._min_close, cross_i + 1, n, False, level, False)
            if cross_i is not None else None
        )
    else:
        cross_i = _first_index(index._min_close, start, n, False, level, False)
        reclaim_i = (
            _first_index(index._max_close, cross_i + 1, n, True, level, False)
            if cross_i is not None else None
        )
    crossed = index._close_time(cross_i)
    reclaimed = index._close_time(reclaim_i)
    if reclaimed is not None:
        state = "reclaimed"
    elif crossed is not None:
        state = "crossed"
    else:
        state = "active"
    return state, crossed, reclaimed


def _zone_payload(
    *,
    zone_id: Any,
    type_: str,
    direction: Direction,
    lower: float,
    upper: float,
    formed_at: int,
    confirmed_at: Optional[int],
    first_test_at: Optional[int],
    max_test_depth: float,
    test_extreme: Optional[float],
    validity: str,
    provenance: str,
    source: str,
    rule_version: str,
    index: _CandleIndex,
    as_of: int,
    candidate: bool,
) -> dict[str, Any]:
    ghost = LtfEntryZone(
        id=None, instrument_id=0, type=type_, direction=direction,
        lower=lower, upper=upper, formed_at=formed_at, confirmed_at=confirmed_at,
        first_test_at=first_test_at, validity=validity,
        max_test_depth=max_test_depth, test_extreme=test_extreme,
        source=source, rule_version=rule_version,
    )
    fill = fvg_fill_status(ghost) if type_ == "FVG" and not candidate else None
    filled_at = _fvg_filled_at(ghost, index) if type_ == "FVG" and not candidate else None
    if filled_at is None and fill == "filled":
        filled_at = first_test_at
    level_state = crossed = reclaimed = None
    if type_ in ("BSL", "SSL") and not candidate:
        level_state, crossed, reclaimed = _level_facts(
            type_, lower, index, confirmed_at or formed_at,
        )
    if candidate or confirmed_at is None:
        lifecycle = "candidate"
        display_until = None
    elif filled_at is not None:
        lifecycle = "ended"
        display_until = filled_at
    elif level_state in ("crossed", "reclaimed"):
        lifecycle = "ended"
        display_until = crossed
    else:
        lifecycle = "active"
        display_until = None
    recently = (
        type_ in ("BSL", "SSL")
        and crossed is not None
        and 0 <= as_of - crossed <= DISPLAY_WINDOW_MS
    )
    return {
        "id": zone_id,
        "type": type_,
        "timeframe": "H1",
        "direction": direction.value,
        "lower": lower,
        "upper": upper,
        "is_level": type_ in ("BSL", "SSL") or lower == upper,
        "formed_at": formed_at,
        "confirmed_at": None if candidate else confirmed_at,
        "display_from": formed_at,
        "display_until": display_until,
        "lifecycle": lifecycle,
        "fill": fill,
        "level_state": level_state,
        "crossed_at": crossed,
        "reclaimed_at": reclaimed,
        "display_window_ms": DISPLAY_WINDOW_MS if recently else None,
        "display_window_label": DISPLAY_WINDOW_LABEL if recently else None,
        "first_test_at": first_test_at,
        "max_test_depth": max_test_depth,
        "validity": validity,
        "provenance": provenance,
        "source": source,
        "rule_version": rule_version,
        "candidate": candidate or confirmed_at is None,
        "recently_taken": recently,
    }


def _stored_zone_visible(zone: LtfEntryZone, as_of: int) -> bool:
    if zone.formed_at > as_of:
        return False
    if zone.confirmed_at is not None and zone.confirmed_at > as_of:
        return False
    return True


def _scan_zones(pivots, candles, events, cfg, as_of: int, index: _CandleIndex) -> list[dict[str, Any]]:
    lookback = (cfg.uncalibrated_consolidation_max_candles + 5) * H1_MS
    found: list[dict[str, Any]] = []
    seen = set()
    for ev in events:
        if ev.kind != "BOS" or ev.occurred_at > as_of:
            continue
        movement = build_movement(0, pivots, candles, ev, ev.direction, lookback)
        if movement is None:
            continue
        detection = detect_entry_zones(candles, movement, pivots, ev.direction, cfg)
        for draft in detection.zones:
            if draft.formed_at > as_of:
                continue
            if draft.confirmed_at and draft.confirmed_at > as_of:
                continue
            key = _geometry_key(
                draft.type, draft.direction.value, draft.formed_at,
                draft.lower, draft.upper,
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(_zone_payload(
                zone_id=f"h1:{draft.type}:{draft.direction.value}:{draft.formed_at}:{draft.lower}:{draft.upper}",
                type_=draft.type, direction=draft.direction,
                lower=draft.lower, upper=draft.upper,
                formed_at=draft.formed_at, confirmed_at=draft.confirmed_at,
                first_test_at=draft.first_test_at,
                max_test_depth=draft.max_test_depth,
                test_extreme=draft.test_extreme,
                validity=draft.validity, provenance="ltf_h1",
                source="h1_scan", rule_version=RULE_VERSION_LTF,
                index=index, as_of=as_of, candidate=False,
            ))
    return found


def _chart_zone(zone: dict[str, Any], historical: bool) -> bool:
    if zone.get("candidate") or zone.get("lifecycle") == "candidate":
        return True
    if zone.get("lifecycle") == "ended":
        return historical
    return True


def _default_shown(zone: dict[str, Any], as_of: int) -> bool:
    if zone.get("candidate") or zone.get("lifecycle") == "candidate":
        return False
    if zone.get("lifecycle") == "ended":
        return False
    return True


def _intersects(zone: dict[str, Any], start: int, end: int) -> bool:
    left = zone.get("display_from") or zone.get("formed_at") or 0
    right = zone.get("display_until") if zone.get("display_until") is not None else end
    return left <= end and right >= start


def _layer_counts(items: list[dict[str, Any]], shown: list[dict[str, Any]], outside: int):
    shown_ids = {id(item) for item in shown}
    return {
        "total": len(items),
        "shown": len(shown),
        "filtered": len([item for item in items if id(item) not in shown_ids]),
        "outside_view": outside,
    }


def assemble_h1_layers(
    db: Database,
    settings,
    instrument_id: int,
    *,
    as_of: Optional[int] = None,
    window_from: Optional[int] = None,
    window_to: Optional[int] = None,
    points: str = "recent",
    diagnostic: bool = False,
    page: int = 0,
    limit: int = 500,
    context_id: Optional[int] = None,
    zone_history: bool = False,
    setup_event: Optional[bool] = None,
) -> dict[str, Any]:
    """Аддитивные группы снимка. Повторный вызов строки не вставляет."""
    moment = as_of if as_of is not None else now_ms()
    cfg = settings.detector
    horizon = moment - cfg.ltf_history_days * 86_400_000
    load_from = horizon
    if window_from is not None:
        load_from = min(load_from, window_from - HISTORY_PAD_MS)
    raw_pivots = db.list_ltf_pivots(instrument_id, since_ms=load_from)
    # Текущая роль уже верна, если её назначили не позже as_of.
    # Журнал нужен только опорам, которые пересмотрели позже этой точки.
    need_log = [
        p.id for p in raw_pivots
        if p.id is not None and (not p.role_assigned_at or p.role_assigned_at > moment)
    ]
    logs = db.list_ltf_pivot_role_logs(instrument_id, need_log)
    candles = db.get_candles(
        instrument_id, "H1", start_ms=load_from, end_ms=moment, closed_only=False,
    )
    cursors_raw = db.get_meta(f"ltf:h1:last_close:{instrument_id}")
    calc_done = cursors_raw is not None or bool(raw_pivots)
    sample_from = window_from if window_from is not None else horizon
    sample_to = window_to if window_to is not None else moment

    role_by_id: dict[int, str] = {}
    rows = []
    candidates: list[PivotCandidate] = []
    for pivot in raw_pivots:
        if pivot.pivot_at > moment:
            continue
        role = role_at(pivot, logs.get(pivot.id, []), moment)
        role_by_id[pivot.id] = role
        if pivot.state == "confirmed" and pivot.confirmed_at and pivot.confirmed_at <= moment:
            candidates.append(_to_candidate(pivot, role))
        rows.append(_pivot_row(pivot, role))

    marker_pack = select_pivot_markers(
        rows, as_of=moment, mode=points, window_from=window_from,
        window_to=window_to, include_diagnostic=diagnostic,
        limit=MARKER_LIMIT if points != "history" else max(1, limit),
        page=page,
    )
    events = scan_instrument_structure(candidates, candles, moment, since_ms=0)
    annotate_epochs(events)
    by_id = {p.pivot_id: p for p in candidates if p.pivot_id is not None}
    links = _scenario_index(db, instrument_id)
    event_rows = []
    for ev in events:
        if ev.occurred_at > moment:
            continue
        ids = links.get((ev.level_key, ev.kind, ev.stage, ev.occurred_at), [])
        event_rows.append(_event_dict(ev, by_id, ids))
    visible_events = [
        ev for ev in event_rows
        if _event_intersects(ev, sample_from, sample_to)
    ]
    transition = structure_transition(events, candidates, moment)
    expected = _expected(candidates, candles, transition, moment)

    stored = [
        z for z in db.list_ltf_entry_zones(instrument_id=instrument_id)
        if _stored_zone_visible(z, moment)
    ]
    life_from = load_from
    if stored:
        earliest = min(
            z.formed_at if z.confirmed_at is None else min(z.formed_at, z.confirmed_at)
            for z in stored
        )
        life_from = min(life_from, earliest)
    lifecycle_candles = db.get_candles(
        instrument_id, "H1", start_ms=life_from, end_ms=moment,
    )
    ideas, idea_links, lifecycle_facts = _project_for_chart(
        db, instrument_id, cfg, moment, stored, lifecycle_candles,
        live=as_of is None,
    )
    index = _lifecycle_index(db, instrument_id, candles, stored, moment, load_from)
    zones: list[dict[str, Any]] = []
    seen = set()
    for zone in stored:
        zone, fact = lifecycle_facts[zone.id]
        first, depth, extreme = _clip_tests(zone, moment)
        key = _geometry_key(zone.type, zone.direction.value, zone.formed_at, zone.lower, zone.upper)
        seen.add(key)
        zones.append(_zone_payload(
            zone_id=zone.id, type_=zone.type, direction=zone.direction,
            lower=zone.lower, upper=zone.upper, formed_at=zone.formed_at,
            confirmed_at=zone.confirmed_at, first_test_at=first,
            max_test_depth=depth, test_extreme=extreme, validity=zone.validity,
            provenance="ltf_entry", source=zone.source,
            rule_version=zone.rule_version, index=index, as_of=moment,
            candidate=zone.confirmed_at is None,
        ))
        zones[-1].update(relevance=fact, idea_links=idea_links.get(zone.id, []))
        if fact["reason"] not in ("ok", "data_gap", "unconfirmed"):
            zones[-1].update(lifecycle="ended", display_until=fact["excluded_at"], recently_taken=False)
    if candles:
        for extra in _scan_zones(candidates, candles, events, cfg, moment, index):
            key = _geometry_key(
                extra["type"], extra["direction"], extra["formed_at"],
                extra["lower"], extra["upper"],
            )
            if key in seen:
                continue
            seen.add(key)
            from ..engine.ltf.relevance import zone_at
            ghost = LtfEntryZone(
                id=None, instrument_id=instrument_id, type=extra["type"],
                direction=Direction(extra["direction"]), lower=extra["lower"], upper=extra["upper"],
                formed_at=extra["formed_at"], confirmed_at=extra["confirmed_at"],
            )
            rebuilt, fact = zone_at(ghost, lifecycle_candles, moment, cfg)
            extra.update(relevance=fact, idea_links=[], max_test_depth=rebuilt.max_test_depth,
                         first_test_at=rebuilt.first_test_at, validity=rebuilt.validity)
            if fact["reason"] not in ("ok", "data_gap", "unconfirmed"):
                extra.update(lifecycle="ended", display_until=fact["excluded_at"], recently_taken=False)
            zones.append(extra)
    zones.sort(key=lambda z: (z["formed_at"], str(z["id"])))

    admission = _admission(db, instrument_id, context_id, zones, stored)
    for row in admission:
        fact = lifecycle_facts.get(row["zone_id"])
        if fact and not fact[1]["relevant"] and row["eligibility"] != "not_evaluated":
            row.update(eligibility="excluded", reason=fact[1]["reason"])
    # По умолчанию на график не попадает вся история завершённых зон.
    # Завершённые зоны и снятая ликвидность доступны только в истории.
    # «Исторические зоны» возвращает завершённые объекты выбранного интервала.
    chart_zones = [
        z for z in zones
        if _chart_zone(z, zone_history)
    ]
    chart_ids = {z["id"] for z in chart_zones}
    admission = [row for row in admission if row["zone_id"] in chart_ids]
    zones = chart_zones
    in_sample = [z for z in zones if _intersects(z, sample_from, sample_to)]
    sample_ids = {id(z) for z in in_sample}
    outside = [z for z in zones if id(z) not in sample_ids]
    shown_zones = [z for z in in_sample if _default_shown(z, moment)]
    zone_status = _zone_status(
        calc_done, candles, in_sample, shown_zones, len(outside),
        sample_from, sample_to,
    )
    anchor_ids = set()
    for ev in visible_events:
        if ev.get("level_pivot_id") is not None:
            anchor_ids.add(ev["level_pivot_id"])
        for ref in ev.get("ref_pivot_ids") or []:
            anchor_ids.add(ref)
    if transition.get("pair"):
        anchor_ids.add(transition["pair"].get("low_pivot_id"))
        anchor_ids.add(transition["pair"].get("high_pivot_id"))
    anchor_ids.discard(None)
    anchors = [
        _pivot_row(p, role_by_id.get(p.id, p.role))
        for p in raw_pivots if p.id in anchor_ids
    ]
    state_seq = db.get_state_seq()
    structural_rows = [
        r for r in rows
        if r.get("state") == "confirmed" and r.get("role") in STRUCTURAL_ROLES
    ]
    structural_in_sample = [
        r for r in structural_rows
        if sample_from <= r["pivot_at"] <= sample_to
    ]
    shown_structural_ids = {
        p.get("id") for p in marker_pack["points"]
        if p.get("role") in STRUCTURAL_ROLES
    }
    shown_in_sample = [
        r for r in structural_in_sample if r.get("id") in shown_structural_ids
    ]
    scenario_open = _scenario_is_open(db, instrument_id, context_id)
    return {
        "pivot_markers": marker_pack["points"],
        "pivot_selection": marker_pack["selection"],
        "anchor_refs": anchors,
        "structural_events": visible_events,
        "structure_transition": transition,
        "expected_structure": expected,
        "detected_zones": zones,
        "scenario_admission": admission,
        "htf_ideas": ideas,
        "roles_as_of": role_by_id,
        "layer_status": {
            "pivots": {
                "state": "calculated" if calc_done else "no_data",
                "reason": None if calc_done else "расчёт структуры H1 не завершён",
                "total": len(structural_in_sample),
                "shown": len(shown_in_sample),
                "filtered": max(0, len(structural_in_sample) - len(shown_in_sample)),
                "outside_view": max(0, len(structural_rows) - len(structural_in_sample)),
                "sample_from": sample_from,
                "sample_to": sample_to,
            },
            "events": {
                "state": "calculated" if calc_done else "no_data",
                "reason": None if calc_done else "расчёт структуры H1 не завершён",
                "total": len(visible_events),
                "shown": len(visible_events),
                "filtered": 0,
                "outside_view": max(0, len(event_rows) - len(visible_events)),
                "sample_from": sample_from,
                "sample_to": sample_to,
            },
            "zones": zone_status,
        },
        "snapshot": {
            "instrument_id": instrument_id,
            "timeframe": "H1",
            "source": "h1_chart",
            "rule_version": RULE_VERSION_LTF,
            "data_version": state_seq,
            "snapshot_id": f"{instrument_id}:{moment}:{state_seq}:{points}",
            "state_version": state_seq,
            "as_of": moment,
            "scenario_open": scenario_open,
            "calc": {
                "structure_last_processed_h1": int(cursors_raw) if cursors_raw else None,
            },
        },
        "reversal": _reversal_view(db, instrument_id, moment, cfg),
        "setup": _local_setup(
            db, cfg, instrument_id, moment, candles, candidates,
            as_of is not None if setup_event is None else setup_event,
        ),
    }


def _local_setup(db, cfg, instrument_id: int, moment: int, candles, pivots, event_frame: bool):
    """Общий снимок ноги. event_frame — явный прошлый кадр, не заполненный сервером now."""
    from .h1_setup import project_setup
    return project_setup(
        db, cfg, instrument_id, moment,
        mode="event" if event_frame else "current",
        candles=candles, pivots=pivots,
    )


def _reversal_view(db, instrument_id: int, as_of: int, cfg):
    """Та же проекция, что у карточки и бота. Нет эпизода — ключ пустой."""
    from .htf_context import reversal_projection
    return reversal_projection(db, instrument_id, as_of, cfg)


def _scenario_is_open(db: Database, instrument_id: int, context_id: Optional[int]) -> bool:
    if context_id is None:
        return False
    obs = db.get_ltf_observation(context_id)
    if obs is None or obs.instrument_id != instrument_id:
        return False
    return db.get_active_ltf_scenario(obs.id) is not None


def _event_intersects(ev: dict[str, Any], start: int, end: int) -> bool:
    left = ev.get("level_pivot_at") or ev.get("break_candle_open_time") or ev["occurred_at"]
    right = ev.get("break_candle_open_time") or ev["occurred_at"]
    return left <= end and right >= start


def _expected(pivots, candles, transition, as_of: int) -> dict[str, Any]:
    direction_name = transition.get("direction")
    if direction_name not in ("bear", "bull"):
        return {"bos": None, "sms": None}
    direction = Direction(direction_name)
    cond = expected_structure_conditions(pivots, candles, direction, as_of)
    return {
        "bos": _expected_slot(cond.get("bos"), "bos", direction_name),
        "sms": _expected_slot(cond.get("sms"), "sms", direction_name),
    }


def _expected_slot(cond: Optional[dict[str, Any]], kind: str, direction: str):
    if not cond:
        return None
    status = cond.get("status")
    level = cond.get("level")
    if status in ("occurred", "superseded", "unavailable") or level is None:
        return None
    pivot = cond.get("pivot")
    slot = {
        "kind": kind.upper(),
        "direction": direction,
        "level": level,
        "status": status,
        "label": f"Ожидаемый {kind.upper()}",
        "confirmed": False,
    }
    if kind == "bos":
        slot["ref_pivot"] = pivot
    else:
        slot["internal_pivot"] = pivot
        slot["prerequisite"] = cond.get("missing")
    return slot


def _admission(db, instrument_id, context_id, zones, stored_zones) -> list[dict[str, Any]]:
    by_geom = {
        _geometry_key(z.type, z.direction.value, z.formed_at, z.lower, z.upper): z
        for z in stored_zones
    }
    scenario = None
    if context_id is not None:
        obs = db.get_ltf_observation(context_id)
        if obs is not None and obs.instrument_id == instrument_id:
            scenario = db.get_active_ltf_scenario(obs.id) or (
                db.list_ltf_scenarios(observation_id=obs.id)[-1]
                if db.list_ltf_scenarios(observation_id=obs.id) else None
            )
    entries = []
    version = None
    if scenario is not None:
        current = db.get_current_ltf_range(scenario.id)
        version = current.version if current is not None else None
        entries = [
            e for e in db.list_ltf_scenario_entries(scenario.id)
            if e.state != "invalid" and (version is None or e.range_version == version)
        ]
    by_zone = {e.entry_zone_id: e for e in entries}
    out = []
    for zone in zones:
        stored = by_geom.get(_geometry_key(
            zone["type"], zone["direction"], zone["formed_at"], zone["lower"], zone["upper"],
        ))
        entry = by_zone.get(stored.id) if stored is not None and scenario is not None else None
        if scenario is None or entry is None:
            eligibility = "not_evaluated"
            reason = ADMISSION_UNRATED
            scenario_id = scenario.id if scenario is not None else None
            range_version = version
        elif entry.reason == "eligible_provisional":
            eligibility = "eligible_provisional"
            reason = entry.reason
            scenario_id = scenario.id
            range_version = entry.range_version
        elif entry.eligible and entry.reason in ("", "ok"):
            eligibility = "eligible"
            reason = entry.reason or "ok"
            scenario_id = scenario.id
            range_version = entry.range_version
        else:
            eligibility = "excluded"
            reason = entry.reason or entry.state or "excluded"
            scenario_id = scenario.id
            range_version = entry.range_version
        out.append({
            "zone_id": zone["id"],
            "scenario_id": scenario_id,
            "eligibility": eligibility,
            "reason": reason,
            "range_version": range_version,
        })
    return out


def _zone_status(calc_done, candles, in_sample, shown, outside, sample_from, sample_to):
    if not candles and not calc_done:
        state, reason = "no_data", "нет свечей H1"
    elif not calc_done:
        state, reason = "no_data", "расчёт структуры H1 не завершён"
    else:
        state, reason = "calculated", None
    counts = _layer_counts(in_sample, shown, outside)
    return {
        "state": state,
        "reason": reason,
        **counts,
        "sample_from": sample_from,
        "sample_to": sample_to,
    }
