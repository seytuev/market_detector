"""§6: BOS/SMS — чистый детектор структурных сломов над историей H1.

Фиксация слома — закрытие H1 СТРОГО за уровнем: тень, незакрытая свеча и
Close == level сломом не являются (приёмка п.2). Один уровень/этап в
сценарии — одно событие: level_key стабилен, дедуп по нему (и UNIQUE в БД).

Стороны:
- медвежий контекст (§6.1, §6.3): исходно HH/HL; H_peak — последний HH,
  L_ref — предшествующий опорный HL. Close < L_ref → первичный BOS; далее
  L_first ниже L_ref, откат H_pullback < H_peak, Close < L_first → вторичный.
  SMS: внутренний L_internal выше L_ref, затем H_pullback < H_peak;
  Close < L_internal при ещё не пробитом L_ref → самостоятельный SMS.
- бычий (§6.2, §6.4): зеркально от последнего LL и опорного LH.

§6.5: одна свеча, пересекающая внутренний и внешний уровни, даёт оба факта —
основное событие BOS + сопутствующий SMS (accompanying=True). Противоположный
подтверждённый слом (BOS или SMS) возвращается как CancellationSignal с явным
видом паттерна (reverse_bos | reverse_sms); события на той же свече, что и
отмена, сохраняются (§13), дальше сценарий не публикует.
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
    """Машина одной стороны (BEAR ищет сломы вниз, BULL — вверх).

    Pivots поглощаются по мере подтверждения (confirmed_at <= закрытие
    текущей свечи): неподтверждённая опора в уровнях не участвует.
    """

    def __init__(self, direction: Direction, now_ms: int,
                 trace: Optional[ScanTrace] = None):
        self.direction = direction
        self.now_ms = now_ms
        self.trace = trace             # None — трассировка выключена (по умолчанию)
        # опорная пара: для BEAR — (h_peak=HH, l_ref=HL); для BULL — (l_bottom=LL, h_ref=LH)
        self.anchor: Optional[PivotCandidate] = None   # h_peak | l_bottom
        self.ref: Optional[PivotCandidate] = None      # l_ref | h_ref
        # SMS: внутренний экстремум + откат
        self.internal: Optional[PivotCandidate] = None  # internal_low | internal_high
        self.pullback: Optional[PivotCandidate] = None  # H_pullback | L_pullback
        # вторичный BOS: первый экстремум за ref + откат после него
        self.first: Optional[PivotCandidate] = None    # L_first | H_first
        self.pullback2: Optional[PivotCandidate] = None
        self.primary_at: Optional[int] = None          # open_time свечи первичного слома
        self.ref_broken = False
        self.secondary_done = False
        self.broken_keys: set[str] = set()             # §6: уровень/этап — один раз
        self._last_high: Optional[PivotCandidate] = None
        self._last_low: Optional[PivotCandidate] = None

    # ---- поглощение pivots ----

    def absorb(self, p: PivotCandidate) -> None:
        if self.direction == Direction.BEAR:
            self._absorb_bear(p)
        else:
            self._absorb_bull(p)
        if p.kind == "high":
            self._last_high = p
        else:
            self._last_low = p

    def _absorb_bear(self, p: PivotCandidate) -> None:
        if p.kind == "high" and p.role == "HH":
            # новый пик: опорный HL — low с ролью HL непосредственно перед ним;
            # уровни нового пика переоткрываются (новый level_key)
            self.anchor = p
            self.ref = (
                self._last_low
                if self._last_low is not None and self._last_low.role == "HL"
                else None
            )
            self.internal = None
            self.pullback = None
            self.first = None
            self.pullback2 = None
            self.primary_at = None
            self.ref_broken = False
            # защёлка этапа — на ногу (якорь), не на всю историю скана;
            # дубли уровня режут broken_keys и UNIQUE в БД
            self.secondary_done = False
        elif p.kind == "low" and p.role == "internal_low":
            if (
                self.anchor is not None and self.ref is not None
                and p.pivot_at > self.anchor.pivot_at
                and p.price > self.ref.price          # §6.3: L_internal выше L_ref
                and self.internal is None
            ):
                self.internal = p
        elif p.kind == "high" and self.internal is not None and self.pullback is None:
            if p.pivot_at > self.internal.pivot_at and p.price < self.anchor.price:
                self.pullback = p                     # H_pullback < H_peak (§6.3)
        # вторичный BOS (§6.1): L_first ниже L_ref, затем откат ниже H_peak
        if (
            self.primary_at is not None and p.kind == "low"
            and self.ref is not None and self.first is None
            and p.pivot_at >= self.primary_at and p.price < self.ref.price
        ):
            self.first = p
        if (
            self.first is not None and p.kind == "high" and self.pullback2 is None
            and p.pivot_at > self.first.pivot_at and p.price < self.anchor.price
        ):
            self.pullback2 = p                        # H_pullback не обязан быть ниже L_ref

    def _absorb_bull(self, p: PivotCandidate) -> None:
        if p.kind == "low" and p.role == "LL":
            self.anchor = p
            self.ref = (
                self._last_high
                if self._last_high is not None and self._last_high.role == "LH"
                else None
            )
            self.internal = None
            self.pullback = None
            self.first = None
            self.pullback2 = None
            self.primary_at = None
            self.ref_broken = False
            self.secondary_done = False      # защёлка этапа — на ногу, не на скан
        elif p.kind == "high" and p.role == "internal_high":
            if (
                self.anchor is not None and self.ref is not None
                and p.pivot_at > self.anchor.pivot_at
                and p.price < self.ref.price          # §6.4: H_internal ниже H_ref
                and self.internal is None
            ):
                self.internal = p
        elif p.kind == "low" and self.internal is not None and self.pullback is None:
            if p.pivot_at > self.internal.pivot_at and p.price > self.anchor.price:
                self.pullback = p                     # L_pullback > L_bottom (§6.4)
        # вторичный BOS (§6.2): H_first выше H_ref, затем откат выше L_bottom
        if (
            self.primary_at is not None and p.kind == "high"
            and self.ref is not None and self.first is None
            and p.pivot_at >= self.primary_at and p.price > self.ref.price
        ):
            self.first = p
        if (
            self.first is not None and p.kind == "low" and self.pullback2 is None
            and p.pivot_at > self.first.pivot_at and p.price > self.anchor.price
        ):
            self.pullback2 = p

    # ---- свечи ----

    def _draft(
        self, kind: str, stage: str, level_pivot: PivotCandidate,
        candle: Candle, refs: list[PivotCandidate], accompanying: bool,
    ) -> StructureEventDraft:
        return StructureEventDraft(
            kind=kind, stage=stage, direction=self.direction,
            break_level=level_pivot.price,
            break_candle_open_time=candle.open_time,
            occurred_at=candle.close_time,
            detected_at=self.now_ms,
            level_key=_level_key(kind, stage, level_pivot),
            ref_pivot_ids=[_ref(r) for r in refs],
            accompanying=accompanying,
            evidence={
                "anchor_price": self.anchor.price if self.anchor else None,
                "ref_price": self.ref.price if self.ref else None,
                "close": candle.close,
                # §5/§12: пробитый уровень с происхождением — pivot машины,
                # его роль и подтверждение на момент события
                "broken_pivot_id": _ref(level_pivot),
                "role_at_event": level_pivot.role,
                "level_price": level_pivot.price,
                "pivot_confirmed_at": level_pivot.confirmed_at,
                "candle_close_time": candle.close_time,
                "close_price": candle.close,
            },
        )

    # ---- трассировка (диагностика; при trace=None — нулевая стоимость) ----

    def _trace_entry(
        self, c: Candle, check: str, decision: str,
        reason: Optional[str] = None,
        level_pivot: Optional[PivotCandidate] = None,
        kind: Optional[str] = None, stage: Optional[str] = None,
        refs: tuple[PivotCandidate, ...] = (),
        snap: Optional[dict[str, Any]] = None,
    ) -> None:
        if self.trace is None:
            return
        s = snap if snap is not None else self._state_snapshot()
        self.trace.entries.append(TraceEntry(
            candle_open_time=c.open_time, candle_close_time=c.close_time,
            direction=self.direction.value, check=check, decision=decision,
            reason=reason, level_kind=kind, level_stage=stage,
            level_price=level_pivot.price if level_pivot is not None else None,
            ref_pivot_ids=[_ref(r) for r in refs], **s,
        ))

    def _state_snapshot(self) -> dict[str, Any]:
        """Состояние машины (цены + ссылки pivots) для снапшота в трассе."""
        def put(d: dict[str, Any], name: str, p: Optional[PivotCandidate]) -> None:
            d[f"{name}_price"] = p.price if p is not None else None
            d[f"{name}_pivot_id"] = _ref(p) if p is not None else None
        out: dict[str, Any] = {}
        put(out, "anchor", self.anchor)
        put(out, "ref", self.ref)
        put(out, "internal", self.internal)
        put(out, "pullback", self.pullback)
        put(out, "first", self.first)
        put(out, "pullback2", self.pullback2)
        out["ref_broken"] = self.ref_broken
        out["primary_at"] = self.primary_at
        return out

    def armed_level(self) -> Optional[tuple[str, str, PivotCandidate]]:
        """Следующий уровень, который on_candle принял бы при закрытии за ним.

        Тот же порядок, что у расчёта слома: первичный BOS, затем SMS,
        затем вторичный BOS. Уже пробитый уровень не вооружён.
        """
        if self.anchor is not None and self.ref is not None and not self.ref_broken:
            key = _level_key("BOS", "primary", self.ref)
            if key not in self.broken_keys:
                return ("BOS", "primary", self.ref)
        if (
            self.internal is not None and self.pullback is not None
            and not self.ref_broken
        ):
            key = _level_key("SMS", "primary", self.internal)
            if key not in self.broken_keys:
                return ("SMS", "primary", self.internal)
        if (
            self.primary_at is not None and not self.secondary_done
            and self.first is not None and self.pullback2 is not None
        ):
            key = _level_key("BOS", "secondary", self.first)
            if key not in self.broken_keys:
                return ("BOS", "secondary", self.first)
        return None

    def on_candle(self, c: Candle) -> list[StructureEventDraft]:
        out: list[StructureEventDraft] = []
        brk = bear_break if self.direction == Direction.BEAR else bull_break
        ref_broken_before = self.ref_broken
        snap = self._state_snapshot() if self.trace is not None else None

        primary: Optional[StructureEventDraft] = None
        # первичный BOS: закрытие строго за опорным уровнем
        if self.anchor is None:
            primary_reason = "anchor_not_set"
        elif self.ref is None:
            primary_reason = "ref_not_set"
        elif self.ref_broken:
            primary_reason = "ref_already_broken"
        elif not brk(self.ref.price, c):
            primary_reason = "close_not_beyond_level"
        else:
            primary_reason = None
        if primary_reason is None:
            key = _level_key("BOS", "primary", self.ref)
            if key not in self.broken_keys:
                primary = self._draft("BOS", "primary", self.ref, c,
                                      [self.anchor, self.ref], accompanying=False)
                out.append(primary)
                self.broken_keys.add(key)
                self._trace_entry(c, "primary_bos", "accept",
                                  level_pivot=self.ref, kind="BOS",
                                  stage="primary",
                                  refs=(self.anchor, self.ref), snap=snap)
            else:
                self._trace_entry(c, "primary_bos", "reject",
                                  reason="duplicate_level_key",
                                  level_pivot=self.ref, kind="BOS",
                                  stage="primary", snap=snap)
            self.ref_broken = True
            self.primary_at = c.open_time
        else:
            self._trace_entry(c, "primary_bos", "reject",
                              reason=primary_reason,
                              level_pivot=self.ref if primary_reason not in (
                                  "anchor_not_set", "ref_not_set") else None,
                              kind="BOS", stage="primary", snap=snap)

        # SMS: самостоятельный при не пробитом ранее опорном уровне (§6.3/§6.4);
        # на одной свече с BOS — сопутствующий факт (§6.5)
        if self.internal is None:
            sms_reason = "internal_not_set"
        elif self.pullback is None:
            sms_reason = "pullback_missing"
        elif ref_broken_before:
            sms_reason = "ref_already_broken"
        elif not brk(self.internal.price, c):
            sms_reason = "close_not_beyond_level"
        else:
            sms_reason = None
        if sms_reason is None:
            key = _level_key("SMS", "primary", self.internal)
            if key not in self.broken_keys:
                out.append(self._draft(
                    "SMS", "primary", self.internal, c,
                    [self.anchor, self.internal, self.pullback],
                    accompanying=primary is not None,
                ))
                self.broken_keys.add(key)
                self._trace_entry(c, "sms", "accept",
                                  level_pivot=self.internal, kind="SMS",
                                  stage="primary",
                                  refs=(self.anchor, self.internal,
                                        self.pullback), snap=snap)
            else:
                self._trace_entry(c, "sms", "reject",
                                  reason="duplicate_level_key",
                                  level_pivot=self.internal, kind="SMS",
                                  stage="primary", snap=snap)
        else:
            self._trace_entry(c, "sms", "reject", reason=sms_reason,
                              level_pivot=self.internal if sms_reason in (
                                  "ref_already_broken", "close_not_beyond_level"
                              ) else None,
                              kind="SMS", stage="primary", snap=snap)

        # вторичный BOS: только после сформированного отката — непрерывный
        # импульс через уровень без отката вторым этапом не является (§6.1)
        if self.primary_at is None:
            secondary_reason = "primary_not_fired"
        elif self.secondary_done:
            secondary_reason = "secondary_done"
        elif self.first is None:
            secondary_reason = "first_not_set"
        elif self.pullback2 is None:
            secondary_reason = "pullback_missing"
        elif not brk(self.first.price, c):
            secondary_reason = "close_not_beyond_level"
        else:
            secondary_reason = None
        if secondary_reason is None:
            key = _level_key("BOS", "secondary", self.first)
            if key not in self.broken_keys:
                out.append(self._draft("BOS", "secondary", self.first, c,
                                       [self.anchor, self.first, self.pullback2],
                                       accompanying=False))
                self.broken_keys.add(key)
                self._trace_entry(c, "secondary_bos", "accept",
                                  level_pivot=self.first, kind="BOS",
                                  stage="secondary",
                                  refs=(self.anchor, self.first,
                                        self.pullback2), snap=snap)
            else:
                self._trace_entry(c, "secondary_bos", "reject",
                                  reason="duplicate_level_key",
                                  level_pivot=self.first, kind="BOS",
                                  stage="secondary", snap=snap)
            self.secondary_done = True
        else:
            self._trace_entry(c, "secondary_bos", "reject",
                              reason=secondary_reason,
                              level_pivot=self.first if secondary_reason == (
                                  "close_not_beyond_level") else None,
                              kind="BOS", stage="secondary", snap=snap)

        if self.ref is not None and brk(self.ref.price, c):
            self.ref_broken = True
        return out


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
                if c.open_time < self.since_ms:
                    continue
                self.events_all.extend(self.target.on_candle(c))
                rev = self.reverse.on_candle(c)
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
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    ps = sorted(
        (p for p in pivots if p.state == "confirmed"),
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
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    ps = sorted(
        (p for p in pivots if p.state == "confirmed"),
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
    """Первичный BOS в том состоянии, в котором on_candle его примет."""
    bear = side.direction == Direction.BEAR
    base = {
        "kind": "BOS", "stage": "primary", "timeframe": "H1",
        "requires_close": True, "strict": True,
        "side": _side_word(side.direction),
        "source": "structure_machine",
    }
    if side.anchor is None:
        missing = (
            "нет подтверждённого максимума"
            if bear else "нет подтверждённого минимума"
        )
        return {**base, "status": "unavailable", "level": None, "missing": missing,
                "opens_scenario": False}
    if side.ref is None:
        missing = (
            "после максимума нет подтверждённой опоры"
            if bear else "после минимума нет подтверждённой опоры"
        )
        return {**base, "status": "waiting_prerequisite", "level": None,
                "missing": missing, "anchor": _pivot_view(side.anchor),
                "opens_scenario": False}
    key = _level_key("BOS", "primary", side.ref)
    occurred = bool(side.ref_broken or key in side.broken_keys)
    return {
        **base,
        "status": "occurred" if occurred else "ready",
        "level": side.ref.price,
        "pivot": _pivot_view(side.ref),
        "anchor": _pivot_view(side.anchor),
        "opens_scenario": not occurred,
    }


def _sms_condition(side: _SideScan) -> dict[str, Any]:
    """SMS: уровень внутреннего экстремума не заменяет отсутствующий откат."""
    bear = side.direction == Direction.BEAR
    base = {
        "kind": "SMS", "stage": "primary", "timeframe": "H1",
        "requires_close": True, "strict": True,
        "side": _side_word(side.direction),
        "source": "structure_machine",
    }
    if side.internal is None:
        missing = "нет подтверждённого внутреннего экстремума"
        return {**base, "status": "unavailable", "level": None, "missing": missing,
                "opens_scenario": False}
    key = _level_key("SMS", "primary", side.internal)
    if key in side.broken_keys:
        status, missing = "occurred", None
    elif side.pullback is None:
        status = "waiting_prerequisite"
        missing = "сначала нужен подтверждённый откат"
    elif side.ref_broken:
        status, missing = "superseded", "опорный уровень уже пробит"
    else:
        status, missing = "ready", None
    return {
        **base,
        "status": status,
        "level": side.internal.price,
        "pivot": _pivot_view(side.internal),
        "pullback": _pivot_view(side.pullback) if side.pullback else None,
        "missing": missing,
        "opens_scenario": status == "ready",
    }


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
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
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
