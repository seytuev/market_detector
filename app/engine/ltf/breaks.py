"""Causal BOS/SMS projections and scenario cancellation.

The main HL/HH or LH/LL frame owns BOS. Every shift and continuation
inside its bounds remains SMS. Only a strict close can confirm a break;
SMS needs no confirmed lower high/higher low. Shared H1/D1/W1 rules:
see docs/BOS_SMS_rules_2026_10_14.md and market_structure.py.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Optional

from ...models import Candle, Direction
from ...models_ltf import PivotAbsorb, ScanTrace, TraceEntry
from .pivots import PivotCandidate


def bear_break(level: float, candle: Candle) -> bool:
    """§6: closed(H1) AND Close строго ниже уровня."""
    return candle.closed and candle.close < level


def bull_break(level: float, candle: Candle) -> bool:
    """§6: closed(H1) AND Close строго выше уровня."""
    return candle.closed and candle.close > level


def _ref(p: PivotCandidate) -> int:
    """Ссылка на pivot: id в БД, до материализации — pivot_at (ms)."""
    return p.pivot_id if p.pivot_id is not None else p.pivot_at


def _level_key(kind: str, stage: str, p: PivotCandidate) -> str:
    """Стабильный ключ уровня для дедупликации (§6: один слом на уровень/этап)."""
    return f"{kind.lower()}:{stage}:{p.role}:{_ref(p)}:{p.price!r}"


@dataclass
class StructureEventDraft:
    """Событие слома до записи в БД (слой БД создаёт LtfStructureEvent)."""

    kind: str                      # BOS | SMS
    stage: str                     # primary | secondary
    direction: Direction
    break_level: float
    break_candle_open_time: int
    occurred_at: int               # ms: закрытие пробойной свечи
    detected_at: int               # ms: когда алгоритм увидел (now_ms вызова)
    level_key: str
    ref_pivot_ids: list[int] = field(default_factory=list)
    accompanying: bool = False     # §6.5: SMS сопутствующий при BOS на той же свече
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CancellationSignal:
    """§6.5: противоположный подтверждённый слом отменяет активный сценарий."""

    pattern: str                   # reverse_bos | reverse_sms
    event: StructureEventDraft     # сам обратный слом


@dataclass
class StructureScanResult:
    events: list[StructureEventDraft]            # в направлении сценария, по времени
    cancellation: Optional[CancellationSignal] = None
    trace: Optional[ScanTrace] = None            # диагностика; None, если не запрошена


class _SideScan:
    """Directional view of the common close-driven nested structure machine."""
    def __init__(self, direction, now_ms, trace=None):
        from .market_structure import StructureMachine
        self.direction, self.now_ms, self.trace = direction, now_ms, trace
        self.machine = StructureMachine()

    def absorb(self, pivot):
        self.machine.absorb(pivot)

    def on_candle(self, candle):
        result = [e for e in self.machine.on_candle(candle, self.now_ms) if e.direction == self.direction]
        if self.trace is not None:
            for kind, check in (("BOS", "primary_bos"), ("SMS", "sms")):
                event = next((e for e in result if e.kind == kind), None)
                condition = self.machine.condition(self.direction, kind)
                refs = event.ref_pivot_ids if event else []
                if event and event.stage == "secondary" and kind == "BOS":
                    check = "secondary_bos"
                self.trace.entries.append(TraceEntry(
                    candle_open_time=candle.open_time, candle_close_time=candle.close_time,
                    direction=self.direction.value, check=check,
                    decision="accept" if event else "reject",
                    reason=None if event else ("close_not_beyond_level" if condition.get("level") is not None
                                               else "internal_not_set" if kind == "SMS" else "anchor_not_set"),
                    level_kind=kind, level_stage=event.stage if event else condition.get("stage"),
                    level_price=event.break_level if event else condition.get("level"),
                    ref_pivot_ids=refs,
                    anchor_pivot_id=refs[0] if refs else None,
                    anchor_price=event.evidence.get("anchor_price") if event else None,
                    ref_pivot_id=event.evidence.get("broken_pivot_id") if event else None,
                    ref_price=event.break_level if event else None,
                    internal_pivot_id=event.evidence.get("broken_pivot_id") if event and kind == "SMS" else None,
                    internal_price=event.break_level if event and kind == "SMS" else None))
        return result

    def _trace_entry(self, candle, check, decision, **kwargs):
        if self.trace is not None:
            self.trace.entries.append(TraceEntry(
                candle_open_time=candle.open_time, candle_close_time=candle.close_time,
                direction=self.direction.value, check=check, decision=decision, **kwargs))

    def armed_level(self):
        levels = self.machine.armed(self.direction)
        levels.sort(key=lambda item: (item[0] != "SMS", item[2].confirmed_at))
        return levels[0] if levels else None


class BreakCursor:
    """Инкрементальный эквивалент detect_breaks для пакетной обработки
    (replay/backlog): машины _SideScan поглощают pivots и обрабатывают свечи
    по мере поступления, без пересканирования истории на каждую свечу.

    Эквивалентность полному перескану обеспечивается инвариантами вызывающего:
    - avail растёт только добавлением новых подтверждённых pivots В КОНЕЦ
      (порядок по pivot_at; confirmed_at новых не меньше уже известных);
    - роли уже поглощённых pivots между вызовами не пересматриваются — при
      пересмотре ролей курсор пересоздаётся (StructureBatch инвалидирует).
    Свечи обрабатываются строго по open_time; пропущенные между вызовами
    свечи доигрываются из переданного списка (простой курсора, пока у
    наблюдения был активный сценарий, эквивалентен перескану на текущей
    свече — состояние машин детерминировано последовательностью).
    """

    def __init__(self, direction: Direction, since_ms: int = 0,
                 stop_on_cancellation: bool = True,
                 cancel_not_before_ms: Optional[int] = None):
        self.direction = direction
        self.since_ms = since_ms
        self.stop_on_cancellation = stop_on_cancellation
        self.cancel_not_before_ms = cancel_not_before_ms
        self.bear = _SideScan(Direction.BEAR, 0)
        self.bull = _SideScan(Direction.BULL, 0)
        self.target, self.reverse = (
            (self.bear, self.bull) if direction == Direction.BEAR
            else (self.bull, self.bear)
        )
        self.ps: list[PivotCandidate] = []   # по (confirmed_at, pivot_at)
        self.avail_len = 0
        self.pi = 0
        self.last_open_time: Optional[int] = None
        self.events_all: list[StructureEventDraft] = []
        self.cancellation: Optional[CancellationSignal] = None
        self.stopped = False

    def _absorb(self, avail: list[PivotCandidate], close_time: int) -> None:
        if len(avail) > self.avail_len:
            new = avail[self.avail_len:]
            self.avail_len = len(avail)
            if self.pi == 0 and not self.ps:
                self.ps = sorted(new, key=lambda p: (p.confirmed_at, p.pivot_at))
            else:
                # confirmed_at новых не меньше уже известных — строго в конец
                self.ps.extend(new)
        while self.pi < len(self.ps) and self.ps[self.pi].confirmed_at <= close_time:
            p = self.ps[self.pi]
            self.bear.absorb(p)
            self.bull.absorb(p)
            self.pi += 1

    def scan(
        self,
        avail: list[PivotCandidate],
        closed: list[Candle],
        open_times: list[int],
        now_ms: int,
    ) -> StructureScanResult:
        """Дообработать новые свечи и вернуть (все события, отмену) — тот же
        контракт, что у detect_breaks на полном списке свечей."""
        self.bear.now_ms = now_ms
        self.bull.now_ms = now_ms
        if not self.stopped:
            if self.last_open_time is None:
                start = 0
            else:
                start = bisect.bisect_right(open_times, self.last_open_time)
            for c in closed[start:]:
                if not c.closed:
                    continue
                self._absorb(avail, c.close_time)
                self.last_open_time = c.open_time
                emitted = self.target.on_candle(c)
                rev = self.reverse.on_candle(c)
                if c.open_time < self.since_ms:
                    continue
                self.events_all.extend(emitted)
                if rev and self.cancel_not_before_ms is not None:
                    rev = [e for e in rev
                           if e.occurred_at >= self.cancel_not_before_ms]
                if self.cancellation is None and rev:
                    first = rev[0]
                    self.cancellation = CancellationSignal(
                        pattern=("reverse_bos" if first.kind == "BOS"
                                 else "reverse_sms"),
                        event=first,
                    )
                if self.cancellation is not None and self.stop_on_cancellation:
                    # §13: факты на свече отмены уже записаны выше; дальше от
                    # этого сценария новые входные сигналы не публикуются (§6.5)
                    self.stopped = True
                    break
        self.events_all.sort(key=lambda e: (e.occurred_at, e.kind))
        return StructureScanResult(events=list(self.events_all),
                                   cancellation=self.cancellation)


def scan_instrument_structure(
    pivots: list[PivotCandidate],
    candles: list[Candle],
    now_ms: int,
    since_ms: int = 0,
) -> list[StructureEventDraft]:
    """Подтверждённые BOS/SMS инструмента, без сценария и без записи в БД.

    Те же машины ``_SideScan``, что у ``detect_breaks``: обе стороны на каждой
    закрытой свече, без остановки на обратном сломе. Событие появляется только
    из ``on_candle`` — закрытие H1 строго за вооружённым уровнем. Смена
    текстовой роли, тень, незакрытая свеча и Close == level событие не создают.
    Свечи раньше ``since_ms`` двигают состояние (уровень не переиздаётся позже),
    но в список не попадают. ``now_ms`` отсекает будущие свечи и опоры.
    """
    closed = sorted(
        (c for c in candles if c.closed and c.close_time <= now_ms),
        key=lambda c: c.open_time,
    )
    ps = sorted(
        (
            p for p in pivots
            if p.state == "confirmed" and p.confirmed_at <= now_ms
        ),
        key=lambda p: (p.confirmed_at, p.pivot_at),
    )
    bear = _SideScan(Direction.BEAR, now_ms)
    bull = _SideScan(Direction.BULL, now_ms)
    events: list[StructureEventDraft] = []
    pi = 0
    for c in closed:
        while pi < len(ps) and ps[pi].confirmed_at <= c.close_time:
            bear.absorb(ps[pi])
            bull.absorb(ps[pi])
            pi += 1
        emitted = bear.on_candle(c) + bull.on_candle(c)
        if c.open_time < since_ms:
            continue
        events.extend(emitted)
    events.sort(key=lambda e: (e.occurred_at, e.kind, e.direction.value, e.level_key))
    return events


def detect_breaks(
    pivots: list[PivotCandidate],
    candles: list[Candle],
    direction: Direction,
    now_ms: int,
    since_ms: int = 0,
    trace: Optional[ScanTrace] = None,
    stop_on_cancellation: bool = True,
    cancel_not_before_ms: Optional[int] = None,
) -> StructureScanResult:
    """Сканирует историю и возвращает события в направлении сценария
    плюс (возможный) сигнал обратной отмены.

    pivots — подтверждённые, с проставленными ролями (assign_roles);
    ambiguous отфильтровываются. Свечи раньше since_ms (например, до
    activated_at наблюдения) событий не дают, но участвуют в контексте:
    опоры, сформировавшиеся до касания HTF, — допустимый контекст (§4),
    а завершившийся до касания слом не выдаётся как новый сигнал.

    trace (опционально) — диагностика: по свече фиксируются решения
    accept/reject каждой проверки уровня с причиной и снапшотом состояния,
    поглощения pivots с ролями, context_only для свечей раньше since_ms.
    На события, отмену и порядок сканирования трассировка не влияет.

    stop_on_cancellation=True (по умолчанию, прод-поведение §13/§6.5): после
    первого обратного слома сканирование прекращается. False — режим чистой
    диагностики: машина продолжает оценивать уровни до конца истории,
    cancellation по-прежнему фиксирует ПЕРВЫЙ обратный слом.

    cancel_not_before_ms (§6.5: отменяет только слом ПОСЛЕ открытия
    сценария): обратные события с occurred_at раньше порога отмену не
    создают и скан не обрезают — в трассе помечаются skip с причиной
    reverse_before_trigger. Порог inclusive: слом на той же свече, что и
    триггер, отменяет. Пропущенный слом остаётся в broken_keys обратной
    машины — уровень, пробитый ДО рождения сценария, не может стать его
    отменой и позже. None (по умолчанию) — прежнее поведение.
    """
    closed = sorted((c for c in candles if c.closed and c.close_time <= now_ms), key=lambda c: c.open_time)
    ps = sorted(
        (p for p in pivots if p.state == "confirmed" and p.confirmed_at <= now_ms),
        key=lambda p: (p.confirmed_at, p.pivot_at),
    )
    if trace is None:
        # рабочий путь — инкрементальный курсор (та же траектория машин)
        cur = BreakCursor(direction, since_ms, stop_on_cancellation,
                          cancel_not_before_ms)
        return cur.scan(ps, closed, [c.open_time for c in closed], now_ms)
    bear = _SideScan(Direction.BEAR, now_ms, trace)
    bull = _SideScan(Direction.BULL, now_ms, trace)
    target, reverse = (
        (bear, bull) if direction == Direction.BEAR else (bull, bear)
    )
    if trace is not None:
        # незакрытые свечи в сломе не участвуют (§6) — фиксируем как skip
        for c in candles:
            if not c.closed and c.open_time >= since_ms:
                trace.entries.append(TraceEntry(
                    candle_open_time=c.open_time, candle_close_time=c.close_time,
                    direction=direction.value, check="scan", decision="skip",
                    reason="candle_not_closed",
                ))
    events: list[StructureEventDraft] = []
    cancellation: Optional[CancellationSignal] = None
    pi = 0
    ci = 0
    for ci, c in enumerate(closed):
        while pi < len(ps) and ps[pi].confirmed_at <= c.close_time:
            p = ps[pi]
            bear.absorb(p)
            bull.absorb(p)
            if trace is not None:
                for side in (bear, bull):
                    trace.absorptions.append(PivotAbsorb(
                        candle_close_time=c.close_time,
                        direction=side.direction.value, pivot_ref=_ref(p),
                        kind=p.kind, role=p.role, price=p.price,
                    ))
            pi += 1
        if c.open_time < since_ms:
            if trace is not None:
                for side in (bear, bull):
                    side._trace_entry(c, "scan", "context_only")
            target.trace = reverse.trace = None
            target.on_candle(c)
            reverse.on_candle(c)
            target.trace = reverse.trace = trace
            continue
        n0 = len(trace.entries) if trace is not None else 0
        events.extend(target.on_candle(c))
        rev = reverse.on_candle(c)
        if trace is not None:
            # pivots, подтверждённые, но ещё не доступные на этой свече
            pending = [_ref(p) for p in ps[pi:]]
            for e in trace.entries[n0:]:
                e.pending_pivots = pending
        if rev and cancel_not_before_ms is not None:
            # §6.5: отмена возможна только сломом ПОСЛЕ открытия сценария;
            # более ранние обратные сломы — история, скан продолжается
            skipped = [e for e in rev if e.occurred_at < cancel_not_before_ms]
            rev = [e for e in rev if e.occurred_at >= cancel_not_before_ms]
            if trace is not None:
                for e in skipped:
                    trace.entries.append(TraceEntry(
                        candle_open_time=c.open_time,
                        candle_close_time=c.close_time,
                        direction=reverse.direction.value, check="scan",
                        decision="skip", reason="reverse_before_trigger",
                        level_kind=e.kind, level_stage=e.stage,
                        level_price=e.break_level,
                    ))
        if cancellation is None and rev:
            first = rev[0]
            cancellation = CancellationSignal(
                pattern="reverse_bos" if first.kind == "BOS" else "reverse_sms",
                event=first,
            )
        if cancellation is not None and stop_on_cancellation:
            # §13: факты на свече отмены уже записаны выше; дальше от этого
            # сценария новые входные сигналы не публикуются (§6.5)
            break
    if cancellation is not None and stop_on_cancellation and trace is not None:
        for c in closed[ci + 1:]:
            trace.entries.append(TraceEntry(
                candle_open_time=c.open_time, candle_close_time=c.close_time,
                direction=target.direction.value, check="scan",
                decision="skip", reason="after_cancellation",
            ))
    events.sort(key=lambda e: (e.occurred_at, e.kind))
    return StructureScanResult(events=events, cancellation=cancellation,
                               trace=trace)


def expected_reverse_condition(
    pivots: list[PivotCandidate],
    candles: list[Candle],
    direction: Direction,
    now_ms: int,
    since_ms: int = 0,
    cancel_not_before_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Ожидаемое условие отмены из обратной машины BOS/SMS (F33).

    status=expected — уровень ещё не пробит; occurred — обратный слом
    уже случился; undefined — машина не вооружила уровень. side — сторона
    закрытия H1, которая отменит сценарий (или уже отменила).
    """
    cur = BreakCursor(
        direction, since_ms, stop_on_cancellation=True,
        cancel_not_before_ms=cancel_not_before_ms,
    )
    cur.bear.now_ms = now_ms
    cur.bull.now_ms = now_ms
    closed = sorted((c for c in candles if c.closed and c.close_time <= now_ms), key=lambda c: c.open_time)
    ps = sorted(
        (p for p in pivots if p.state == "confirmed" and p.confirmed_at <= now_ms),
        key=lambda p: (p.confirmed_at, p.pivot_at),
    )
    result = cur.scan(ps, closed, [c.open_time for c in closed], now_ms)
    side = "below" if cur.reverse.direction == Direction.BEAR else "above"
    if result.cancellation is not None:
        ev = result.cancellation.event
        return {
            "status": "occurred",
            "source": "reverse_machine",
            "kind": result.cancellation.pattern,
            "level": ev.break_level,
            "side": side,
            "pivot_id": ev.ref_pivot_ids[-1] if ev.ref_pivot_ids else None,
            "confirmed_at": ev.occurred_at,
        }
    armed = cur.reverse.armed_level()
    if armed is None:
        return {
            "status": "undefined",
            "source": "reverse_machine",
            "kind": None,
            "level": None,
            "side": None,
            "pivot_id": None,
            "confirmed_at": None,
        }
    kind, _stage, pivot = armed
    return {
        "status": "expected",
        "source": "reverse_machine",
        "kind": kind,
        "level": pivot.price,
        "side": side,
        "pivot_id": pivot.pivot_id,
        "pivot_at": pivot.pivot_at,
        "confirmed_at": pivot.confirmed_at,
    }


def _pivot_view(p: PivotCandidate) -> dict[str, Any]:
    return {
        "id": p.pivot_id,
        "price": p.price,
        "pivot_at": p.pivot_at,
        "confirmed_at": p.confirmed_at,
        "role": p.role,
    }


def _side_word(direction: Direction) -> str:
    return "below" if direction == Direction.BEAR else "above"


def _bos_condition(side: _SideScan) -> dict[str, Any]:
    return side.machine.condition(side.direction, "BOS")


def _sms_condition(side: _SideScan) -> dict[str, Any]:
    return side.machine.condition(side.direction, "SMS")


def expected_structure_conditions(
    pivots: list[PivotCandidate],
    candles: list[Candle],
    direction: Direction,
    now_ms: int,
    since_ms: int = 0,
) -> dict[str, Any]:
    """Следующие BOS и SMS из той же машины, которая регистрирует слом.

    Свечи проигрываются по порядку, pivots поглощаются только после
    confirmed_at. Уже подтверждённые к now_ms pivots без последующей свечи
    вооружают следующее условие и не создают исторический слом.
    """
    cur = BreakCursor(direction, since_ms, stop_on_cancellation=False)
    closed = sorted((c for c in candles if c.closed and c.close_time <= now_ms), key=lambda c: c.open_time)
    ps = sorted(
        (p for p in pivots if p.state == "confirmed" and p.confirmed_at <= now_ms),
        key=lambda p: (p.confirmed_at, p.pivot_at),
    )
    cur.scan(ps, closed, [c.open_time for c in closed], now_ms)
    cur._absorb(ps, now_ms)
    side = cur.target
    bos = _bos_condition(side)
    sms = _sms_condition(side)
    ready = [
        name for name, cond in (("BOS", bos), ("SMS", sms))
        if cond.get("opens_scenario")
    ]
    return {
        "direction": direction.value,
        "bos": bos,
        "sms": sms,
        "opens_scenario": ready,
        "either": len(ready) == 2,
    }
