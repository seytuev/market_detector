"""Материализация структуры H1 в БД: pivots, роли, детекция событий по
активным сценариям инструмента.

Этап B1: sync_structure идемпотентно записывает pivots и пересмотры ролей,
а события/отмены ВОЗВРАЩАЕТ — запись LtfStructureEvent/LtfRange и оркестрация
сценариев относятся к этапу B2. Дедуп заложен через has_ltf_structure_event:
уже записанные в БД события из результата отфильтровываются.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, Optional

from ...config import DetectorConfig
from ...db import Database
from ...models import Candle
from ...models_ltf import LtfPivot
from .breaks import (
    BreakCursor,
    CancellationSignal,
    StructureEventDraft,
    StructureScanResult,
    detect_breaks,
)
from .pivots import PivotCandidate, confirmed_pivots, find_h1_pivots, pivot_candidates_at
from .roles import RoleChange, assign_roles


@dataclass
class StructureSyncResult:
    pivots_total: int                     # всего найдено на истории
    pivots_new: int                       # сколько новых записано в ltf_pivot
    role_changes: list[RoleChange]        # применённые к БД пересмотры ролей
    events: dict[int, list[StructureEventDraft]] = field(default_factory=dict)
    cancellations: dict[int, CancellationSignal] = field(default_factory=dict)
    scans: dict[int, StructureScanResult] = field(default_factory=dict)
    # pivots, доступные на now_ms, со СВЕЖИМИ ролями этого пересчёта (in-memory
    # роли применены всегда, даже когда persist_roles=False) — потребителям
    # внутри того же тика нужны именно они, а не сохранённые в БД значения
    avail: list[PivotCandidate] = field(default_factory=list)


class StructureBatch:
    """Состояние пакетной обработки H1 одного инструмента (replay/backlog).

    Устраняет квадратичный пересчёт на каждую свечу при ДЕТЕРМИНИРОВАННОЙ
    эквивалентности полному пересчёту:
    - pivots: новая закрытая свеча на хвосте создаёт не более одной новой
      позиции-кандидата (len-1-right) — остальные окна не меняются;
      несовпадение префикса истории → полный пересчёт (fallback);
    - avail: новые кандидаты подтверждены сразу (их подтверждающая свеча —
      последняя), поэтому avail растёт строго добавлением в конец;
    - роли: assign_roles детерминирован над последовательностью avail;
      пересчёт только при росте avail. Пересмотр роли уже поглощённого
      pivot → инвалидация всех курсоров (они пересоздаются полным прогоном
      на текущей свече — ровно то, что сделал бы перескан);
    - курсоры BreakCursor: та же траектория машин _SideScan, что у
      перескана на каждую свечу (см. docstring BreakCursor).
    """

    def __init__(self) -> None:
        self._fp: Optional[tuple] = None      # (left, right, len, last_open)
        self.closed: list[Candle] = []
        self.cands: list[PivotCandidate] = []
        self.new_cands: list[PivotCandidate] = []
        self.avail: list[PivotCandidate] = []
        self._roles: list[str] = []
        self._existing_fp: Optional[int] = None
        self.existing: dict[tuple, Any] = {}
        self.cursors: dict[tuple, BreakCursor] = {}
        self._cursor_params: dict[tuple, tuple] = {}
        self.open_times: list[int] = []
        self.range_memo: dict[tuple, Any] = {}

    # ---- pivots/avail/роли (инкрементально) ----

    def _reset(self, candles: list[Candle], left: int, right: int) -> None:
        self.closed = sorted((c for c in candles if c.closed),
                             key=lambda c: c.open_time)
        self.cands = find_h1_pivots(self.closed, left, right)
        self.avail = []
        self._roles = []
        self.cursors.clear()
        self._cursor_params.clear()
        self.open_times = [c.open_time for c in self.closed]
        self._fp = (left, right, len(self.closed),
                    self.closed[-1].open_time if self.closed else None)

    def pivots_for(
        self, candles: list[Candle], left: int, right: int
    ) -> list[PivotCandidate]:
        """Кандидаты pivots по закрытым свечам; инкрементально по хвосту."""
        self.new_cands = []
        if self._fp is None or self._fp[0] != left or self._fp[1] != right:
            self._reset(candles, left, right)
            self.new_cands = list(self.cands)
            return self.cands
        _, _, prev_len, prev_last = self._fp
        closed = self.closed
        n_new_raw = len(candles)
        # префикс совпадает, если на стыке та же свеча (open_time уникальны
        # в рамках инструмента/ТФ) — иначе полный пересчёт
        same_prefix = (
            n_new_raw >= prev_len
            and (prev_len == 0
                 or (len(candles) > 0 and len(closed) > 0
                     and closed[prev_len - 1].open_time == prev_last
                     and candles[prev_len - 1].open_time == prev_last))
        )
        if not same_prefix:
            self._reset(candles, left, right)
            self.new_cands = list(self.cands)
            return self.cands
        for c in candles[prev_len:]:
            if not c.closed:
                continue
            closed.append(c)
            self.open_times.append(c.open_time)
            pos = len(closed) - 1 - right
            if pos >= left:
                self.new_cands.extend(
                    pivot_candidates_at(closed, pos, left, right)
                )
        self.cands.extend(self.new_cands)
        self._fp = (left, right, len(closed),
                    closed[-1].open_time if closed else None)
        return self.cands

    def avail_for(self, now_ms: int) -> list[PivotCandidate]:
        """Подтверждённые на now_ms pivots; новые кандидаты подтверждены
        сразу (confirmed_at = закрытие последней свечи ≤ now) — append-only."""
        for p in self.new_cands:
            if p.state == "confirmed" and p.confirmed_at <= now_ms:
                self.avail.append(p)
        return self.avail

    def roles_for(self):
        """Роли avail (пересчёт только при росте). Пересмотр роли старого
        pivot → инвалидация курсоров и мемо диапазонов."""
        if len(self.avail) == len(self._roles):
            return None
        res = assign_roles(self.avail)
        old = self._roles
        revised = any(res.roles[i] != old[i] for i in range(len(old)))
        self._roles = res.roles
        if revised:
            self.cursors.clear()
            self._cursor_params.clear()
            self.range_memo.clear()
        return res

    def existing_for(self, db: Database, instrument_id: int) -> dict:
        """(kind, pivot_at) → строка ltf_pivot; перечитывается только при
        изменении числа pivots в БД (append-only внутри пакета)."""
        rows = db.list_ltf_pivots(instrument_id)
        if self._existing_fp != len(rows):
            self.existing = {(p.kind, p.pivot_at): p for p in rows}
            self._existing_fp = len(rows)
        return self.existing

    # ---- курсоры сканирования сломов ----

    def cursor(
        self,
        key: tuple,
        direction,
        since_ms: int,
        stop_on_cancellation: bool,
        cancel_not_before_ms: Optional[int],
    ) -> BreakCursor:
        params = (direction, since_ms, stop_on_cancellation,
                  cancel_not_before_ms)
        cur = self.cursors.get(key)
        if cur is not None and self._cursor_params.get(key) != params:
            cur = None  # параметры скана изменились — пересоздать
        if cur is None:
            cur = BreakCursor(direction, since_ms, stop_on_cancellation,
                              cancel_not_before_ms)
            self.cursors[key] = cur
            self._cursor_params[key] = params
        return cur


def sync_structure(
    db: Database,
    cfg: DetectorConfig,
    instrument_id: int,
    candles: list[Candle],
    now_ms: int,
    persist_roles: bool = True,
    batch: Optional[StructureBatch] = None,
) -> StructureSyncResult:
    """Синхронизирует структуру H1 инструмента с БД (идемпотентно).

    persist_roles=False (replay истории): роли пересчитываются и применяются
    к avail в памяти, но БД не переписывается на каждой свече — иначе каждый
    replay заново прокручивает промежуточные роли (none→HL→internal_*) против
    сохранённых финальных и засоряет ltf_pivot_role_log миллионами строк.
    Запись ролей на ГОЛОВЕ истории (последняя свеча прогона) остаётся —
    она же исправляет расхождения.

    batch (replay/backlog): инкрементальные pivots/avail/роли и курсоры
    сканирования — детерминированно эквивалентно полному пересчёту на каждую
    свечу (см. StructureBatch). candles ожидается растущим списком закрытых
    свечей (up_to), по одной новой свече на вызов.
    """
    if batch is not None:
        return _sync_structure_batch(
            db, cfg, instrument_id, candles, now_ms, persist_roles, batch
        )
    cands = find_h1_pivots(candles, cfg.ltf_structure_left, cfg.ltf_structure_right)

    # --- pivots → БД; идемпотентность по (instrument_id, kind, pivot_at) ---
    existing = {
        (p.kind, p.pivot_at): p for p in db.list_ltf_pivots(instrument_id)
    }
    new_count = 0
    for c in cands:
        row = existing.get((c.kind, c.pivot_at))
        if row is None:
            c.pivot_id = db.insert_ltf_pivot(LtfPivot(
                id=None, instrument_id=c.instrument_id, price=c.price,
                kind=c.kind, pivot_at=c.pivot_at, confirmed_at=c.confirmed_at,
                role="none", left=c.left, right=c.right,
                candle_open_time=c.candle_open_time, state=c.state,
            ))
            new_count += 1
        else:
            c.pivot_id = row.id
            c.role = row.role

    # --- роли: только доступные на now_ms подтверждённые pivots (§5.1) ---
    avail = confirmed_pivots(cands, now_ms)
    applied: list[RoleChange] = []
    if avail:
        res = assign_roles(avail)
        # в БД пишем только финальную роль каждого pivot и только если она
        # отличается от сохранённой: промежуточные пересмотры внутри одного
        # пересчёта (none→HL→internal_low) не были состояниями БД и не должны
        # порождать строки лога при каждом sync
        for i, p in enumerate(avail):
            new_role = res.roles[i]
            if new_role == "none" or p.role == new_role:
                continue  # роль не присвоена (§5.2) или не изменилась
            if persist_roles:
                db.update_ltf_pivot_role(p.pivot_id, new_role, changed_at=now_ms)
            applied.append(RoleChange(i, p.role, new_role))
            p.role = new_role

    # --- события по живым сценариям инструмента ---
    result = StructureSyncResult(
        pivots_total=len(cands), pivots_new=new_count, role_changes=applied,
        avail=avail,
    )
    for obs in db.list_ltf_observations(instrument_id=instrument_id):
        if obs.state not in ("waiting_structure", "active"):
            continue
        sc = db.get_active_ltf_scenario(obs.id)
        if sc is None:
            continue
        # §6.5: отменяет только обратный слом ПОСЛЕ триггера сценария;
        # без записанного триггера — момент открытия (created_at)
        cancel_not_before = sc.created_at
        for ev in db.list_ltf_structure_events(sc.id):
            if ev.id == sc.trigger_event_id:
                cancel_not_before = ev.occurred_at
                break
        scan = detect_breaks(avail, candles, sc.direction, now_ms,
                             since_ms=obs.activated_at,
                             cancel_not_before_ms=cancel_not_before)
        # отсекаем уже записанные в БД события (UNIQUE scenario/level/stage)
        fresh = [
            e for e in scan.events
            if not db.has_ltf_structure_event(sc.id, e.level_key, e.stage)
        ]
        result.scans[sc.id] = scan
        if fresh:
            result.events[sc.id] = fresh
        if scan.cancellation is not None:
            result.cancellations[sc.id] = scan.cancellation
    return result


def _sync_structure_batch(
    db: Database,
    cfg: DetectorConfig,
    instrument_id: int,
    candles: list[Candle],
    now_ms: int,
    persist_roles: bool,
    batch: StructureBatch,
) -> StructureSyncResult:
    """Batch-ветка sync_structure: те же записи в БД и тот же результат, но
    без квадратичного пересчёта (инкрементальные pivots/avail/роли, курсоры).

    Эквивалентность one-shot пути по построению:
    - содержимое cands/avail и применённые к объектам роли совпадают
      (assign_roles детерминирован; эффективная роль объекта после цикла —
      res.roles[i], а при res=="none" — роль из БД на момент создания
      кандидата, что внутри пакета совпадает с ролью из БД на текущую свечу:
      запись ролей идёт только на голове пакета);
    - курсоры BreakCursor дают ту же траекторию машин _SideScan, что
      перескан на каждую свечу; при пересмотре ролей курсоры инвалидируются
      и пересоздаются полным прогоном на текущей свече.
    """
    cands = batch.pivots_for(candles, cfg.ltf_structure_left,
                             cfg.ltf_structure_right)

    # --- pivots → БД; идемпотентность по (instrument_id, kind, pivot_at) ---
    # в пакете проверяются только новые кандидаты этой свечи: прежние уже
    # материализованы (existing внутри пакета растёт только добавлением)
    existing = batch.existing_for(db, instrument_id)
    new_count = 0
    for c in batch.new_cands:
        row = existing.get((c.kind, c.pivot_at))
        if row is None:
            c.pivot_id = db.insert_ltf_pivot(LtfPivot(
                id=None, instrument_id=c.instrument_id, price=c.price,
                kind=c.kind, pivot_at=c.pivot_at, confirmed_at=c.confirmed_at,
                role="none", left=c.left, right=c.right,
                candle_open_time=c.candle_open_time, state=c.state,
            ))
            row = db.get_ltf_pivot(c.pivot_id)
            if row is not None:
                existing[(c.kind, c.pivot_at)] = row
            new_count += 1
        else:
            c.pivot_id = row.id
            c.role = row.role

    # --- роли: пересчёт только при росте avail; пересмотр старых ролей
    # инвалидирует курсоры (внутри roles_for) ---
    avail = batch.avail_for(now_ms)
    applied: list[RoleChange] = []
    res = batch.roles_for()
    if res is not None:
        for i, p in enumerate(avail):
            new_role = res.roles[i]
            if new_role == "none" or p.role == new_role:
                continue  # роль не присвоена (§5.2) или не изменилась
            if persist_roles:
                db.update_ltf_pivot_role(p.pivot_id, new_role, changed_at=now_ms)
            applied.append(RoleChange(i, p.role, new_role))
            p.role = new_role

    # --- события по живым сценариям инструмента (курсоры вместо перескана) ---
    result = StructureSyncResult(
        pivots_total=len(cands), pivots_new=new_count, role_changes=applied,
        avail=avail,
    )
    for obs in db.list_ltf_observations(instrument_id=instrument_id):
        if obs.state not in ("waiting_structure", "active"):
            continue
        sc = db.get_active_ltf_scenario(obs.id)
        if sc is None:
            continue
        # §6.5: отменяет только обратный слом ПОСЛЕ триггера сценария;
        # без записанного триггера — момент открытия (created_at)
        cancel_not_before = sc.created_at
        for ev in db.list_ltf_structure_events(sc.id):
            if ev.id == sc.trigger_event_id:
                cancel_not_before = ev.occurred_at
                break
        cur = batch.cursor(
            ("sc", sc.id), sc.direction, obs.activated_at,
            stop_on_cancellation=True, cancel_not_before_ms=cancel_not_before,
        )
        scan = cur.scan(avail, batch.closed, batch.open_times, now_ms)
        # отсекаем уже записанные в БД события (UNIQUE scenario/level/stage)
        fresh = [
            e for e in scan.events
            if not db.has_ltf_structure_event(sc.id, e.level_key, e.stage)
        ]
        result.scans[sc.id] = scan
        if fresh:
            result.events[sc.id] = fresh
        if scan.cancellation is not None:
            result.cancellations[sc.id] = scan.cancellation
    return result
