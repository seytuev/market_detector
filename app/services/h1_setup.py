"""Локальный PD текущего движения H1.

Один инструмент и масштаб H1 имеют одну текущую ногу на заданный as_of.
Нога начинается в причинной опоре импульса, который дал BOS, и сразу
получает диапазон до уже наблюдаемого экстремума. Подтверждение pivot
меняет только статус. HTF-родители эту ногу не размножают.

Расчёт не пишет ltf_event и не отправляет уведомления. Старые версии
диапазона сценария остаются журналом.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..engine.ltf.breaks import StructureEventDraft, _SideScan
from ..engine.ltf.eligibility import evaluate_entry
from ..engine.ltf.entries import MovementDraft, detect_entry_zones
from ..engine.ltf.pivots import PivotCandidate
from ..engine.ltf.ranges import RangeDraft
from ..models import Candle, Direction
from ..models_ltf import LtfEntryZone

H1_MS = 3_600_000
FRAME_PAD_BARS = 6


def pd_bounds(direction: str, origin: float, endpoint: float):
    """Long: L = origin, H = endpoint. Short — зеркально. H <= L → None."""
    if direction == Direction.BULL.value or direction == Direction.BULL:
        low, high = origin, endpoint
    else:
        low, high = endpoint, origin
    if high <= low:
        return None
    return low, high, (low + high) / 2.0


def _new_state() -> dict[str, Any]:
    return {"current": None, "epoch": 0, "quality": "ok"}


def _origin_from_pivot(pivot: PivotCandidate) -> dict[str, Any]:
    return {
        "key": f"pivot:{pivot.kind}:{pivot.pivot_at}",
        "price": float(pivot.price),
        "at": int(pivot.pivot_at),
        "known_at": int(pivot.confirmed_at),
        "provenance": "confirmed_pivot",
        "role": pivot.role,
    }


def causal_origin(direction, event: StructureEventDraft, pivots, candles):
    """Структурный экстремум, из которого вышел импульс этого BOS.

    Подтверждённый pivot того же вида, уже известный к BOS. Если его ещё
    нет — наблюдаемый экстремум свечи внутри свинга от предыдущей
    противоположной опоры. Будущий confirmed pivot не подставляется.
    """
    bull = _is_bull(direction)
    kind = "low" if bull else "high"
    bos_at = int(event.occurred_at)
    break_open = int(event.break_candle_open_time)
    confirmed = [
        p for p in pivots
        if p.state == "confirmed" and p.kind == kind
        and int(p.confirmed_at) <= bos_at and int(p.pivot_at) <= break_open
    ]
    if confirmed:
        return _origin_from_pivot(max(confirmed, key=lambda p: (p.pivot_at, p.confirmed_at)))
    opposite = "high" if bull else "low"
    bounds = [
        p for p in pivots
        if p.state == "confirmed" and p.kind == opposite
        and int(p.confirmed_at) <= bos_at and int(p.pivot_at) < break_open
    ]
    left = max((int(p.pivot_at) for p in bounds), default=None)
    pool = [
        c for c in candles
        if c.closed and int(c.close_time) <= bos_at and int(c.open_time) <= break_open
        and (left is None or int(c.open_time) > left)
    ]
    if not pool:
        return None
    if bull:
        bar = min(pool, key=lambda c: (c.low, -c.open_time))
        price = float(bar.low)
    else:
        bar = max(pool, key=lambda c: (c.high, c.open_time))
        price = float(bar.high)
    return {
        "key": f"candidate:{bar.open_time}",
        "price": price,
        "at": int(bar.open_time),
        "known_at": int(bar.close_time),
        "provenance": "structural_candidate",
        "role": None,
    }


def correction_pivot(direction, previous_bos_at: int, event: StructureEventDraft, pivots):
    """Подтверждённая коррекция между прежним BOS и новым сломом.

    Для продолжения вверх это low (HL), для вниз — high (LH). Без такой
    опоры новый слом остаётся фактом той же ноги.
    """
    kind = "low" if _is_bull(direction) else "high"
    found = [
        p for p in pivots
        if p.state == "confirmed" and p.kind == kind
        and int(p.confirmed_at) <= int(event.occurred_at)
        and int(previous_bos_at) < int(p.pivot_at) < int(event.break_candle_open_time)
    ]
    if not found:
        return None
    return max(found, key=lambda p: p.pivot_at)


def _is_bull(direction) -> bool:
    value = direction.value if isinstance(direction, Direction) else direction
    return value == Direction.BULL.value


def _dir_value(direction) -> str:
    return direction.value if isinstance(direction, Direction) else str(direction)


def _blank_leg(direction: str, event: StructureEventDraft, epoch: int, as_of: int) -> dict[str, Any]:
    evidence = event.evidence or {}
    broken = evidence.get("broken_pivot_id")
    role = evidence.get("role_at_event")
    return {
        "id": None,
        "direction": direction,
        "origin": None,
        "origin_anchor_key": None,
        "origin_price": None,
        "origin_at": None,
        "origin_known_at": None,
        "trigger_bos_key": f"{event.level_key}@{int(event.occurred_at)}",
        "bos_at": int(event.occurred_at),
        "broken_anchor_key": (
            f"pivot:{broken}:{role or 'none'}" if broken is not None else event.level_key
        ),
        "state": "developing",
        "endpoint_price": None,
        "endpoint_at": None,
        "endpoint_status": "unresolved",
        "as_of": as_of,
        "revision": 0,
        "revisions": [],
        "facts": [{"key": event.level_key, "at": int(event.occurred_at)}],
        "reason": None,
        "range_status": "range_pending",
        "lower": None,
        "upper": None,
        "eq": None,
        "superseded_at": None,
        "epoch": epoch,
        "data_quality": "ok",
        "new_revision": None,
        "break_open": int(event.break_candle_open_time),
    }


def _same_revision(a: dict, b: dict) -> bool:
    keys = ("lower", "upper", "eq", "endpoint_price", "endpoint_at", "range_status", "reason")
    return all(a.get(k) == b.get(k) for k in keys)


def _push_revision(leg: dict, as_of: int) -> None:
    rev = {
        "lower": leg["lower"],
        "upper": leg["upper"],
        "eq": leg["eq"],
        "endpoint_price": leg["endpoint_price"],
        "endpoint_at": leg["endpoint_at"],
        "range_status": leg["range_status"],
        "as_of": int(as_of),
        "reason": leg["reason"],
    }
    last = leg["revisions"][-1] if leg["revisions"] else None
    if last is not None and _same_revision(last, rev):
        return
    if last is not None and int(last["as_of"]) == int(as_of):
        rev["revision"] = last["revision"]
        leg["revisions"][-1] = rev
        leg["revision"] = last["revision"]
        leg["new_revision"] = "replace"
        return
    number = 1 if last is None else int(last["revision"]) + 1
    rev["revision"] = number
    leg["revisions"].append(rev)
    leg["revision"] = number
    leg["new_revision"] = "insert"


def _apply_bounds(leg: dict) -> None:
    if leg["origin_price"] is None or leg["endpoint_price"] is None:
        leg["lower"] = leg["upper"] = leg["eq"] = None
        leg["range_status"] = "range_pending"
        leg["reason"] = leg["reason"] or "range_pending"
        leg["state"] = "developing"
        return
    bounds = pd_bounds(leg["direction"], leg["origin_price"], leg["endpoint_price"])
    if bounds is None:
        leg["lower"] = leg["upper"] = leg["eq"] = None
        leg["range_status"] = "range_pending"
        leg["reason"] = "range_pending"
        leg["state"] = "developing"
        return
    leg["lower"], leg["upper"], leg["eq"] = bounds
    confirmed = leg["endpoint_status"] == "confirmed"
    leg["range_status"] = "confirmed" if confirmed else "provisional"
    leg["state"] = "pivot_confirmed" if confirmed else "developing"
    if leg["reason"] in ("range_pending", "origin_unresolved"):
        leg["reason"] = None


def _endpoint_status(direction: str, endpoint_at, endpoint_price, pivots) -> str:
    if endpoint_at is None or endpoint_price is None:
        return "unresolved"
    kind = "high" if direction == Direction.BULL.value else "low"
    for pivot in pivots:
        if (
            pivot.state == "confirmed"
            and pivot.kind == kind
            and int(pivot.pivot_at) == int(endpoint_at)
            and float(pivot.price) == float(endpoint_price)
        ):
            return "confirmed"
    return "provisional"


def _seed_endpoint(leg: dict, candles, pivots, as_of: int) -> None:
    if leg["origin_at"] is None:
        _push_revision(leg, as_of)
        return
    pool = [
        c for c in candles
        if c.closed and int(c.open_time) >= int(leg["origin_at"]) and int(c.close_time) <= int(as_of)
    ]
    if not pool:
        leg["reason"] = leg["reason"] or "range_pending"
        leg["range_status"] = "range_pending"
        _push_revision(leg, as_of)
        return
    if leg["direction"] == Direction.BULL.value:
        bar = max(pool, key=lambda c: (c.high, c.open_time))
        leg["endpoint_price"] = float(bar.high)
    else:
        bar = min(pool, key=lambda c: (c.low, -c.open_time))
        leg["endpoint_price"] = float(bar.low)
    leg["endpoint_at"] = int(bar.open_time)
    leg["endpoint_status"] = _endpoint_status(
        leg["direction"], leg["endpoint_at"], leg["endpoint_price"], pivots,
    )
    _apply_bounds(leg)
    _push_revision(leg, as_of)


def _extend_endpoint(leg: dict, candle: Candle, pivots) -> None:
    if leg["origin_at"] is None or int(candle.open_time) < int(leg["origin_at"]):
        leg["as_of"] = int(candle.close_time)
        return
    price = float(candle.high if leg["direction"] == Direction.BULL.value else candle.low)
    moved = leg["endpoint_price"] is None or price > float(leg["endpoint_price"])
    if leg["direction"] != Direction.BULL.value:
        moved = leg["endpoint_price"] is None or price < float(leg["endpoint_price"])
    if moved:
        leg["endpoint_price"] = price
        leg["endpoint_at"] = int(candle.open_time)
    status = _endpoint_status(leg["direction"], leg["endpoint_at"], leg["endpoint_price"], pivots)
    status_changed = status != leg["endpoint_status"]
    leg["endpoint_status"] = status
    if moved or status_changed:
        _apply_bounds(leg)
        _push_revision(leg, candle.close_time)
    leg["as_of"] = int(candle.close_time)


def _open_leg(state, event, pivots, candles, as_of: int, origin: Optional[dict]) -> dict:
    state["epoch"] = int(state["epoch"]) + 1
    leg = _blank_leg(_dir_value(event.direction), event, state["epoch"], as_of)
    leg["data_quality"] = state.get("quality") or "ok"
    if origin is None:
        leg["reason"] = "origin_unresolved"
        leg["range_status"] = "range_pending"
        leg["endpoint_status"] = "unresolved"
        _push_revision(leg, as_of)
    else:
        leg["origin"] = origin
        leg["origin_anchor_key"] = origin["key"]
        leg["origin_price"] = origin["price"]
        leg["origin_at"] = origin["at"]
        leg["origin_known_at"] = origin["known_at"]
        _seed_endpoint(leg, candles, pivots, as_of)
    state["current"] = leg
    return leg


def apply_candle(state: dict, candle: Candle, events: list, pivots: list, candles: list) -> list:
    """Один закрытый бар. Возвращает ноги, которые нужно сохранить."""
    dirty: list[dict] = []
    primary = [e for e in events if e.kind == "BOS" and e.stage == "primary"]
    secondary = [e for e in events if e.kind == "BOS" and e.stage != "primary"]
    if len({_dir_value(e.direction) for e in primary}) > 1:
        state["quality"] = "intrabar_order_unknown"
    primary.sort(key=lambda e: (_dir_value(e.direction), e.level_key))
    for event in primary:
        current = state["current"]
        direction = _dir_value(event.direction)
        same = (
            current is not None
            and current["state"] != "superseded"
            and current["direction"] == direction
        )
        corr = correction_pivot(direction, current["bos_at"], event, pivots) if same else None
        if same and corr is None:
            current["facts"].append({"key": event.level_key, "at": int(event.occurred_at)})
            current["as_of"] = int(candle.close_time)
            current["data_quality"] = state.get("quality") or current["data_quality"]
            dirty.append(current)
            continue
        if current is not None and current["state"] != "superseded":
            current["state"] = "superseded"
            current["superseded_at"] = int(event.occurred_at)
            current["as_of"] = int(event.occurred_at)
            dirty.append(current)
        origin = _origin_from_pivot(corr) if corr is not None else causal_origin(
            direction, event, pivots, candles,
        )
        dirty.append(_open_leg(state, event, pivots, candles, int(candle.close_time), origin))
    current = state["current"]
    if current is not None and current["state"] != "superseded":
        for event in secondary:
            if _dir_value(event.direction) != current["direction"]:
                continue
            current["facts"].append({"key": event.level_key, "at": int(event.occurred_at)})
        _extend_endpoint(current, candle, pivots)
        if current not in dirty:
            dirty.append(current)
    return dirty


def replay_events(events: list, pivots: list, candles: list, as_of: int) -> dict:
    """Полный проход по уже известным BOS. Будущие свечи и опоры не входят."""
    closed = sorted(
        (c for c in candles if c.closed and int(c.close_time) <= int(as_of)),
        key=lambda c: c.open_time,
    )
    grouped: dict[int, list] = {}
    for event in events:
        if event.kind != "BOS" or int(event.occurred_at) > int(as_of):
            continue
        grouped.setdefault(int(event.occurred_at), []).append(event)
    state = _new_state()
    seen: list[Candle] = []
    for candle in closed:
        seen.append(candle)
        known = [
            p for p in pivots
            if p.state == "confirmed" and int(p.confirmed_at) <= int(candle.close_time)
        ]
        apply_candle(state, candle, grouped.get(int(candle.close_time), []), known, seen)
    return state


class H1LegCursor:
    """Инкрементальный проход машин BOS. Повтор той же свечи ногу не дублирует."""

    def __init__(self, instrument_id: int, generation: int = 0, batch_id: int = 0):
        self.instrument_id = instrument_id
        self.generation = generation
        self.batch_id = batch_id
        self._clear()

    def _clear(self) -> None:
        self.bear = _SideScan(Direction.BEAR, 0)
        self.bull = _SideScan(Direction.BULL, 0)
        self.ps: list[PivotCandidate] = []
        self.seen: set[tuple] = set()
        self._roles: dict[tuple, str] = {}
        self.pi = 0
        self.last_open: Optional[int] = None
        self.state = _new_state()
        self._guard = False

    def advance(self, db, pivots, candles, as_of: int, cfg=None) -> None:
        if self._guard:
            return
        closed = sorted(
            (c for c in candles if c.closed and int(c.close_time) <= int(as_of)),
            key=lambda c: c.open_time,
        )
        if self.last_open is not None:
            opens = [int(c.open_time) for c in closed]
            if self.last_open not in opens:
                self._clear()
            else:
                # префикс оборван — машины больше не совпадают с историей
                idx = opens.index(self.last_open)
                if any(int(c.open_time) != opens[i] for i, c in enumerate(closed[: idx + 1])):
                    self._clear()
        fresh = []
        role_changed = False
        for pivot in pivots:
            if pivot.state != "confirmed" or int(pivot.confirmed_at) > int(as_of):
                continue
            key = (pivot.kind, int(pivot.pivot_at))
            if key in self.seen:
                if self._roles.get(key) != pivot.role:
                    role_changed = True
                continue
            fresh.append(pivot)
        if role_changed:
            self._guard = True
            self._clear()
            self._guard = False
            self.advance(db, pivots, candles, as_of, cfg)
            return
        fresh.sort(key=lambda p: (int(p.confirmed_at), int(p.pivot_at)))
        if self.ps and fresh and (
            (int(fresh[0].confirmed_at), int(fresh[0].pivot_at))
            < (int(self.ps[-1].confirmed_at), int(self.ps[-1].pivot_at))
        ):
            self._guard = True
            self._clear()
            self._guard = False
            self.advance(db, pivots, candles, as_of, cfg)
            return
        for pivot in fresh:
            self.ps.append(pivot)
            key = (pivot.kind, int(pivot.pivot_at))
            self.seen.add(key)
            self._roles[key] = pivot.role
        if self.last_open is None:
            start = 0
        else:
            start = [int(c.open_time) for c in closed].index(self.last_open) + 1
        seen: list[Candle] = closed[:start]
        for candle in closed[start:]:
            while self.pi < len(self.ps) and int(self.ps[self.pi].confirmed_at) <= int(candle.close_time):
                pivot = self.ps[self.pi]
                self.bear.absorb(pivot)
                self.bull.absorb(pivot)
                self.pi += 1
            self.bear.now_ms = int(as_of)
            self.bull.now_ms = int(as_of)
            emitted = self.bear.on_candle(candle) + self.bull.on_candle(candle)
            seen.append(candle)
            known = self.ps[: self.pi]
            dirty = apply_candle(self.state, candle, emitted, known, seen)
            for leg in dirty:
                _persist_leg(db, self.instrument_id, leg)
            self.last_open = int(candle.open_time)
        if closed:
            db.set_meta(
                f"h1:leg:scanned:{self.instrument_id}",
                str(int(closed[-1].close_time)),
            )
        db._commit()


def _evidence(leg: dict) -> str:
    origin = leg.get("origin") or {}
    return json.dumps({
        "facts": leg.get("facts") or [],
        "provenance": origin.get("provenance"),
        "origin_role": origin.get("role"),
        "data_quality": leg.get("data_quality") or "ok",
        "break_open": leg.get("break_open"),
        "endpoint_status": leg.get("endpoint_status"),
        "source_candle_refs": [
            v for v in (leg.get("origin_at"), leg.get("break_open"), leg.get("endpoint_at"))
            if v is not None
        ],
    }, ensure_ascii=False)


def _persist_leg(db, instrument_id: int, leg: dict) -> None:
    row = db.conn.execute(
        """SELECT id, as_of, state FROM h1_local_leg
           WHERE instrument_id=? AND trigger_bos_key=?""",
        (instrument_id, leg["trigger_bos_key"]),
    ).fetchone()
    payload = (
        leg["direction"], leg["origin_anchor_key"], leg["origin_price"], leg["origin_at"],
        leg["origin_known_at"], leg["bos_at"], leg["broken_anchor_key"], leg["state"],
        leg["endpoint_price"], leg["endpoint_at"], leg["endpoint_status"], int(leg["as_of"]),
        int(leg["revision"] or 1), leg["reason"], _evidence(leg), leg["superseded_at"],
        int(leg["epoch"] or 1),
    )
    if row is None:
        cur = db.conn.execute(
            """INSERT INTO h1_local_leg (
                   instrument_id, direction, origin_anchor_key, origin_price, origin_at,
                   origin_known_at, trigger_bos_key, bos_at, broken_anchor_key, state,
                   endpoint_price, endpoint_at, endpoint_status, as_of, revision, reason,
                   evidence, superseded_at, structure_epoch_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                instrument_id, leg["direction"], leg["origin_anchor_key"], leg["origin_price"],
                leg["origin_at"], leg["origin_known_at"], leg["trigger_bos_key"], leg["bos_at"],
                leg["broken_anchor_key"], leg["state"], leg["endpoint_price"], leg["endpoint_at"],
                leg["endpoint_status"], int(leg["as_of"]), int(leg["revision"] or 1), leg["reason"],
                _evidence(leg), leg["superseded_at"], int(leg["epoch"] or 1),
            ),
        )
        leg["id"] = int(cur.lastrowid)
    else:
        leg["id"] = int(row["id"])
        ahead = int(row["as_of"]) > int(leg["as_of"])
        if ahead and not (leg["state"] == "superseded" and row["state"] != "superseded"):
            pass
        else:
            db.conn.execute(
                """UPDATE h1_local_leg SET
                       direction=?, origin_anchor_key=?, origin_price=?, origin_at=?,
                       origin_known_at=?, bos_at=?, broken_anchor_key=?, state=?,
                       endpoint_price=?, endpoint_at=?, endpoint_status=?, as_of=?,
                       revision=?, reason=?, evidence=?, superseded_at=?,
                       structure_epoch_id=?
                   WHERE id=?""",
                (*payload, leg["id"]),
            )
    _persist_revision(db, leg)


def _persist_revision(db, leg: dict) -> None:
    action = leg.get("new_revision")
    if not action or not leg.get("revisions"):
        leg["new_revision"] = None
        return
    rev = leg["revisions"][-1]
    last = db.conn.execute(
        """SELECT * FROM h1_local_leg_revision
           WHERE leg_id=? ORDER BY revision DESC LIMIT 1""",
        (leg["id"],),
    ).fetchone()
    if last is not None and int(last["as_of"]) > int(rev["as_of"]):
        leg["new_revision"] = None
        return
    same = last is not None and _same_revision(dict(last), rev)
    if same:
        leg["new_revision"] = None
        return
    if last is not None and int(last["as_of"]) == int(rev["as_of"]):
        db.conn.execute(
            """UPDATE h1_local_leg_revision
               SET lower=?, upper=?, eq=?, endpoint_price=?, endpoint_at=?,
                   range_status=?, reason=?
               WHERE id=?""",
            (rev["lower"], rev["upper"], rev["eq"], rev["endpoint_price"], rev["endpoint_at"],
             rev["range_status"], rev["reason"], last["id"]),
        )
        leg["new_revision"] = None
        return
    number = 1 if last is None else int(last["revision"]) + 1
    rev["revision"] = number
    leg["revision"] = number
    db.conn.execute(
        """INSERT INTO h1_local_leg_revision (
               leg_id, revision, lower, upper, eq, endpoint_price, endpoint_at,
               range_status, as_of, reason
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (leg["id"], number, rev["lower"], rev["upper"], rev["eq"], rev["endpoint_price"],
         rev["endpoint_at"], rev["range_status"], int(rev["as_of"]), rev["reason"]),
    )
    leg["new_revision"] = None


def _head_as_of(db, instrument_id: int) -> Optional[int]:
    row = db.conn.execute(
        "SELECT MAX(as_of) AS as_of FROM h1_local_leg WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()
    if row is None or row["as_of"] is None:
        return None
    return int(row["as_of"])


def ensure_local_legs(db, cfg, instrument_id: int, candles, pivots, as_of: int) -> None:
    """Один проход, если в БД ещё нет ноги на последнюю закрытую свечу.

    Повторный вызов с тем же рядом строк не добавляет. Уведомлений нет.
    """
    latest = max(
        (int(c.close_time) for c in candles if c.closed and int(c.close_time) <= int(as_of)),
        default=0,
    )
    if not latest:
        return
    head = _head_as_of(db, instrument_id) or 0
    scanned = int(db.get_meta(f"h1:leg:scanned:{instrument_id}") or 0)
    if max(head, scanned) >= latest:
        return
    H1LegCursor(instrument_id).advance(db, pivots, candles, latest, cfg)


def _pivot_of(row) -> PivotCandidate:
    return PivotCandidate(
        instrument_id=row.instrument_id, price=row.price, kind=row.kind,
        pivot_at=row.pivot_at, candle_open_time=row.candle_open_time,
        confirmed_at=row.confirmed_at or 0, left=row.left, right=row.right,
        state=row.state, pivot_id=row.id, role=row.role or "none",
    )


def project_setup(db, cfg, instrument_id: int, as_of: int, *, mode: str = "current",
                   candles=None, pivots=None) -> Optional[dict[str, Any]]:
    """Снимок для API, графика и уведомления. Сам диапазон не выбирает по сценарию."""
    if candles is None:
        candles = db.get_candles(instrument_id, "H1", end_ms=as_of, closed_only=False)
    if pivots is None:
        pivots = [_pivot_of(p) for p in db.list_ltf_pivots(instrument_id)]
    ensure_local_legs(db, cfg, instrument_id, candles, pivots, as_of)
    return build_snapshot(db, cfg, instrument_id, as_of, mode, candles, pivots)


def local_leg_at(db, instrument_id: int, as_of: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        """SELECT * FROM h1_local_leg
           WHERE instrument_id=? AND bos_at<=?
             AND (superseded_at IS NULL OR superseded_at>?)
           ORDER BY bos_at DESC, id DESC LIMIT 1""",
        (instrument_id, int(as_of), int(as_of)),
    ).fetchone()
    return dict(row) if row is not None else None


def revision_at(db, leg_id: int, as_of: int) -> Optional[dict[str, Any]]:
    row = db.conn.execute(
        """SELECT * FROM h1_local_leg_revision
           WHERE leg_id=? AND as_of<=?
           ORDER BY revision DESC LIMIT 1""",
        (leg_id, int(as_of)),
    ).fetchone()
    return dict(row) if row is not None else None


def revisions_of(db, leg_id: int) -> list[dict[str, Any]]:
    return [
        dict(row) for row in db.conn.execute(
            "SELECT * FROM h1_local_leg_revision WHERE leg_id=? ORDER BY revision",
            (leg_id,),
        ).fetchall()
    ]


def _lookback_ms(cfg) -> int:
    bars = 20
    if cfg is not None:
        bars = int(getattr(cfg, "uncalibrated_consolidation_max_candles", 15) or 15) + 5
    return bars * H1_MS


def _trade_context(db, instrument_id: int, direction: str, as_of: int) -> tuple[bool, list[dict]]:
    row = db.conn.execute(
        """SELECT 1 FROM ltf_scenario s
           JOIN ltf_observation o ON o.id=s.observation_id
           WHERE o.instrument_id=? AND s.direction=?
             AND s.state IN ('range_pending','monitoring_entries')
           LIMIT 1""",
        (instrument_id, direction),
    ).fetchone()
    sources: list[dict] = []
    if row is not None:
        return True, sources
    try:
        from .htf_context import context_supports_direction, episode_sources
    except Exception:
        return False, sources
    episodes = db.conn.execute(
        """SELECT * FROM htf_context_episode
           WHERE instrument_id=? AND started_at<=?
             AND state IN ('awaiting_h1','confirmed')""",
        (instrument_id, int(as_of)),
    ).fetchall()
    for episode_row in episodes:
        episode = dict(episode_row)
        if episode.get("invalidated_at") and int(episode["invalidated_at"]) <= int(as_of):
            continue
        loaded = episode_sources(db, int(episode["id"]), as_of)
        if context_supports_direction(db, episode, loaded, direction):
            for source in loaded:
                if source.get("active", 1):
                    sources.append({
                        "zone_id": source.get("zone_id"),
                        "interaction": source.get("interaction"),
                        "interaction_at": source.get("interaction_at"),
                    })
            return True, sources
    return False, sources


def _overlap_interval(lower: float, upper: float, half_low: float, half_high: float):
    lo = max(lower, half_low)
    hi = min(upper, half_high)
    if lo <= hi:
        return lo, hi
    return None


def zone_against_leg(formed_at: int, confirmed: bool, lower: float, upper: float,
                     snap: dict, *, is_level: bool = False) -> str:
    """Причина зоны относительно уже построенного снимка ноги."""
    if snap.get("range_status") == "range_pending":
        return snap.get("reason") or "range_pending"
    window = snap.get("window_start")
    if window is not None and int(formed_at) < int(window):
        return "other_movement"
    if not confirmed:
        return "forming"
    if snap.get("lower") is None:
        return "range_pending"
    direction = Direction(snap["direction"])
    rng = RangeDraft(
        direction=direction, lower=snap["lower"], upper=snap["upper"], mid=snap["eq"],
        anchor_low_ref=None, anchor_high_ref=None, available_at=int(snap["as_of"]),
    )
    from ..config import DetectorConfig
    zone = LtfEntryZone(
        id=None, instrument_id=0, type="FVG", direction=direction,
        lower=lower, upper=upper, formed_at=int(formed_at),
        confirmed_at=int(snap["as_of"]) if confirmed else None,
    )
    decision = evaluate_entry(
        zone, direction, DetectorConfig(), rng,
        pd_status="provisional" if snap.get("range_status") == "provisional" else None,
    )
    if is_level:
        return decision.reason
    return decision.reason


def touch_is_retrospective(revisions: list[dict], direction: str, lower: float, upper: float,
                           touch_at: int) -> bool:
    """Касание на том же закрытии, которое впервые открыло допуск, — не вход.

    Область допуска берётся из ревизии, известной до этой свечи.
    """
    ordered = sorted(revisions, key=lambda r: (int(r["as_of"]), int(r.get("revision") or 0)))
    first = None
    was = False
    bull = direction == Direction.BULL.value
    for rev in ordered:
        if rev.get("lower") is None or rev.get("eq") is None or rev.get("upper") is None:
            was = False
            continue
        if rev.get("range_status") == "range_pending":
            was = False
            continue
        half_low, half_high = (rev["lower"], rev["eq"]) if bull else (rev["eq"], rev["upper"])
        admitted = _overlap_interval(lower, upper, half_low, half_high) is not None
        if admitted and not was and first is None:
            first = int(rev["as_of"])
        was = admitted
    return first is not None and int(touch_at) == first


def _candidates(db_cfg, leg: dict, rev: dict, candles, pivots, as_of: int, trade: bool) -> tuple[list, list, list]:
    from ..config import DetectorConfig
    cfg = db_cfg or DetectorConfig()
    if leg.get("origin_at") is None or rev.get("lower") is None:
        return [], [], [leg.get("reason") or "range_pending"]
    lookback = _lookback_ms(cfg)
    window = int(leg["origin_at"]) - lookback
    end_open = max(
        (int(c.open_time) for c in candles if c.closed and int(c.close_time) <= int(as_of)),
        default=int(leg["bos_at"]),
    )
    direction = Direction(leg["direction"])
    movement = MovementDraft(
        scenario_id=0, direction=direction, start_pivot_ref=int(leg["origin_at"]),
        end_pivot_ref=int(leg["endpoint_at"] or leg["origin_at"]),
        start_at=int(leg["origin_at"]), end_at=end_open, window_start=window,
        break_event_key=leg["trigger_bos_key"],
    )
    detection = detect_entry_zones(
        [c for c in candles if c.closed and int(c.close_time) <= int(as_of)],
        movement, pivots, direction, cfg,
    )
    rng = RangeDraft(
        direction=direction, lower=rev["lower"], upper=rev["upper"], mid=rev["eq"],
        anchor_low_ref=None, anchor_high_ref=None, available_at=int(rev["as_of"]),
    )
    pd_status = "provisional" if rev["range_status"] == "provisional" else None
    candidates = []
    regions = []
    reasons = []
    drafts = list(detection.zones)
    for rejected in detection.rejected:
        if rejected.get("type") == "OB" and "кандидат" in str(rejected.get("reason") or ""):
            drafts.append(type("F", (), {
                "type": "OB", "lower": None, "upper": None, "formed_at": rejected.get("formed_at"),
                "confirmed_at": None, "direction": direction, "validity": "fresh",
                "forming": True, "note": rejected.get("reason"),
            })())
    for draft in drafts:
        confirmed_at = getattr(draft, "confirmed_at", None)
        forming = bool(
            getattr(draft, "forming", False)
            or confirmed_at is None
            or int(confirmed_at) > int(as_of) + 1
        )
        if draft.lower is None or draft.upper is None:
            if forming and draft.formed_at is not None and int(draft.formed_at) >= window:
                candidates.append({
                    "type": draft.type, "lower": None, "upper": None,
                    "formed_at": draft.formed_at, "status": "forming",
                    "reason": "forming", "movement_id": leg.get("id"),
                    "admitted": None,
                })
                reasons.append("forming")
            continue
        if int(draft.formed_at) < window:
            continue
        zone = LtfEntryZone(
            id=None, instrument_id=int(leg.get("instrument_id") or 0), type=draft.type,
            direction=direction, lower=draft.lower, upper=draft.upper,
            formed_at=int(draft.formed_at),
            confirmed_at=None if forming else draft.confirmed_at,
            validity=getattr(draft, "validity", "fresh") or "fresh",
            max_test_depth=getattr(draft, "max_test_depth", 0.0) or 0.0,
            test_extreme=getattr(draft, "test_extreme", None),
        )
        if forming:
            reason = "forming"
            overlap = "none"
        else:
            decision = evaluate_entry(zone, direction, cfg, rng, pd_status=pd_status)
            reason = decision.reason
            overlap = decision.overlap
        half_low, half_high = (rev["lower"], rev["eq"]) if direction == Direction.BULL else (rev["eq"], rev["upper"])
        admitted = None if forming else _overlap_interval(draft.lower, draft.upper, half_low, half_high)
        row = {
            "type": draft.type,
            "direction": direction.value,
            "lower": draft.lower,
            "upper": draft.upper,
            "formed_at": int(draft.formed_at),
            "confirmed_at": None if forming else draft.confirmed_at,
            "status": "forming" if forming else "confirmed",
            "reason": reason,
            "overlap": overlap,
            "movement_id": leg.get("id"),
            "admitted": list(admitted) if admitted else None,
            "label": "предварительный PD" if reason == "eligible_provisional" else None,
        }
        candidates.append(row)
        allowed = reason in ("ok", "eligible_provisional") and admitted is not None and trade
        if allowed:
            regions.append(row)
        elif reason not in ("ok", "eligible_provisional"):
            reasons.append(reason)
    if not trade:
        reasons.append("context_missing")
    # уникальные, в стабильном порядке
    uniq = []
    for reason in reasons:
        if reason not in uniq:
            uniq.append(reason)
    return candidates, regions, uniq


def build_snapshot(db, cfg, instrument_id: int, as_of: int, mode: str,
                   candles, pivots) -> Optional[dict[str, Any]]:
    leg = local_leg_at(db, instrument_id, as_of)
    if leg is None:
        return None
    rev = revision_at(db, int(leg["id"]), as_of)
    if rev is None:
        rev = {
            "lower": None, "upper": None, "eq": None, "range_status": "range_pending",
            "as_of": int(leg["as_of"]), "revision": int(leg["revision"] or 0),
            "endpoint_price": leg.get("endpoint_price"), "endpoint_at": leg.get("endpoint_at"),
            "reason": leg.get("reason"),
        }
    evidence = {}
    try:
        evidence = json.loads(leg.get("evidence") or "{}")
    except json.JSONDecodeError:
        evidence = {}
    origin_at = leg.get("origin_at")
    window = int(origin_at) - _lookback_ms(cfg) if origin_at is not None else None
    trade, sources = _trade_context(db, instrument_id, leg["direction"], as_of)
    leg["instrument_id"] = instrument_id
    candidates, regions, reasons = _candidates(cfg, leg, rev, candles or [], pivots or [], as_of, trade)
    if rev.get("reason") and rev["reason"] not in reasons and rev.get("range_status") == "range_pending":
        reasons.insert(0, rev["reason"])
    preview = None
    for candle in candles or []:
        if candle.closed or int(candle.open_time) > int(as_of):
            continue
        if origin_at is not None and int(candle.open_time) < int(origin_at):
            continue
        preview = {
            "label": "текущая свеча",
            "open_time": int(candle.open_time),
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
    history = mode == "event" or leg.get("state") in ("superseded", "invalidated")
    later = db.conn.execute(
        """SELECT 1 FROM h1_local_leg
           WHERE instrument_id=? AND bos_at>? LIMIT 1""",
        (instrument_id, int(leg["bos_at"])),
    ).fetchone()
    if later is not None and mode == "event":
        history = True
    status = "analytics" if not trade else "trade"
    if rev.get("range_status") == "range_pending":
        status = "range_pending"
    bull = leg["direction"] == Direction.BULL.value
    return {
        "instrument_id": instrument_id,
        "as_of": int(as_of),
        "mode": mode,
        "snapshot_id": f"h1:{instrument_id}:{leg['id']}:{rev.get('revision')}:{as_of}",
        "movement_id": int(leg["id"]),
        "structure_epoch_id": leg.get("structure_epoch_id"),
        "direction": leg["direction"],
        "trigger_bos": {
            "key": leg["trigger_bos_key"],
            "at": leg["bos_at"],
            "broken_anchor_key": leg.get("broken_anchor_key"),
        },
        "origin_anchor": None if origin_at is None else {
            "key": leg.get("origin_anchor_key"),
            "price": leg.get("origin_price"),
            "at": origin_at,
            "known_at": leg.get("origin_known_at"),
            "provenance": evidence.get("provenance"),
        },
        "endpoint": {
            "price": rev.get("endpoint_price") if rev.get("endpoint_price") is not None else leg.get("endpoint_price"),
            "at": rev.get("endpoint_at") if rev.get("endpoint_at") is not None else leg.get("endpoint_at"),
            "status": (
                "confirmed" if rev.get("range_status") == "confirmed"
                else "provisional" if rev.get("range_status") == "provisional"
                else "unresolved"
            ),
            "label": (
                ("Потенциальный H" if bull else "Потенциальный L")
                if rev.get("range_status") != "confirmed"
                else ("Подтверждённый H" if bull else "Подтверждённый L")
            ),
        },
        "range_status": rev.get("range_status"),
        "range_revision": rev.get("revision"),
        "lower": rev.get("lower"),
        "upper": rev.get("upper"),
        "eq": rev.get("eq"),
        "context_sources": sources,
        "candidates": candidates,
        "eligible_regions": regions,
        "setup_status": status,
        "exclusion_reasons": reasons,
        "data_quality": evidence.get("data_quality") or "ok",
        "reason": rev.get("reason") or leg.get("reason"),
        "state": leg.get("state"),
        "history": history,
        "history_label": "история" if history else None,
        "window_start": window,
        "frame_from": None if origin_at is None else int(origin_at) - FRAME_PAD_BARS * H1_MS,
        "live_preview": preview,
        "label": "PD H1 текущего движения",
        "pd_label": "предварительный PD" if rev.get("range_status") == "provisional" else (
            "PD подтверждён" if rev.get("range_status") == "confirmed" else "PD не построен"
        ),
    }


def setup_card_lines(snap: Optional[dict]) -> list[str]:
    """Строки карточки из снимка. Цены не выдумываются вне snapshot."""
    if not snap:
        return []
    from ..notify.formatting import fmt_price_ru
    if snap.get("range_status") == "range_pending" or snap.get("lower") is None:
        return [f"PD H1 не построен: {snap.get('reason') or 'range_pending'}."]
    bull = snap.get("direction") == Direction.BULL.value
    origin = (snap.get("origin_anchor") or {}).get("price")
    end = (snap.get("endpoint") or {}).get("price")
    potential = (snap.get("endpoint") or {}).get("status") != "confirmed"
    lines = [
        "Текущее движение: {o} → {e}. {side} pivot {pot}.".format(
            o=fmt_price_ru(origin), e=fmt_price_ru(end),
            side="Верхний" if bull else "Нижний",
            pot="потенциальный" if potential else "подтверждён",
        ),
        "PD H1: {lo}–{hi}, 50%: {eq}.{extra}".format(
            lo=fmt_price_ru(snap["lower"]), hi=fmt_price_ru(snap["upper"]),
            eq=fmt_price_ru(snap["eq"]),
            extra=" Предварительный PD." if snap.get("range_status") == "provisional" else " PD подтверждён.",
        ),
    ]
    regions = snap.get("eligible_regions") or []
    if regions:
        rendered = ", ".join(
            f"{z['type']} {fmt_price_ru(z['admitted'][0])}–{fmt_price_ru(z['admitted'][1])}"
            + (" · предварительный PD" if z.get("reason") == "eligible_provisional" else "")
            for z in regions if z.get("admitted")
        )
        title = "Возможные области покупки на откате: " if bull else "Возможные области продажи на откате: "
        if rendered:
            lines.append(title + rendered)
    if "context_missing" in (snap.get("exclusion_reasons") or []):
        lines.append("BOS подтверждён, торговый контекст отсутствует. Это не готовый вход.")
    if snap.get("history"):
        lines.append("Исторический снимок. Диапазон на время события.")
    return lines


def entry_block_reason(db, event, now: int) -> Optional[str]:
    """Подавить входовой пакет, если нога уже не текущая или зона чужая.

    Обычный BOS и отмена сценария этим правилом не закрываются.
    """
    if getattr(event, "kind", None) not in ("entries_ready", "touch"):
        return None
    obs = db.get_ltf_observation(event.observation_id)
    if obs is None:
        return None
    current = local_leg_at(db, obs.instrument_id, now)
    if current is None:
        return None
    then = local_leg_at(db, obs.instrument_id, int(event.occurred_at))
    if then is not None and int(then["id"]) != int(current["id"]):
        return "superseded"
    if current.get("state") in ("superseded", "invalidated"):
        return str(current.get("state"))
    rev = revision_at(db, int(current["id"]), now)
    if rev is not None and rev.get("range_status") == "range_pending":
        return rev.get("reason") or current.get("reason") or "range_pending"
    zone_id = (event.payload or {}).get("entry_zone_id")
    zone = db.get_ltf_entry_zone(zone_id) if zone_id is not None else None
    if zone is None or current.get("origin_at") is None:
        if event.kind == "touch":
            return None
        return None
    from ..config import DetectorConfig
    window = int(current["origin_at"]) - _lookback_ms(DetectorConfig())
    if int(zone.formed_at) < window:
        return "other_movement"
    if event.kind == "touch" and rev is not None:
        rows = revisions_of(db, int(current["id"]))
        if touch_is_retrospective(
            rows, current["direction"], zone.lower, zone.upper, int(event.occurred_at),
        ):
            return "zone_became_available"
    return None
