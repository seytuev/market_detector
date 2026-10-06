"""Оркестратор LTF Confirmations: последовательность обработки закрытия H1 (§13).

На каждой новой закрытой H1-свече:
1. свечи берутся из БД (только closed);
2. касания опубликованных свежих зон и закрытие liquidity-тестов (§9/§10);
3. структура: sync_structure (pivots/роли) → BOS/SMS по активным сценариям,
   открытие сценария при первичном сломе в направлении HTF-родителя,
   обратный слом → отмена (§6.5);
4. диапазон: range_recalc → новая версия LtfRange, пересчёт применимости
   ScenarioEntry (§7);
5. при первичном BOS/SMS: movement + Entry Zones + свежесть (§8/§9);
6. события LtfEvent с дедуп-ключами §11.5 (insert_ltf_event отбрасывает дубли).

Если снятие и отмена приходятся на одно закрытие, журнал хранит оба факта,
но от отменённого сценария новый вход не публикуется (§13.4).
Live и replay используют один детерминированный путь (_process_candle).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ...config import DetectorConfig
from ...db import Database
from ...models import Direction, Zone, ZoneStatus, now_ms as _real_now_ms
from ...models_ltf import (
    RULE_VERSION_LTF,
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
    LtfMovement,
    LtfObservation,
    LtfRange,
    LtfScenario,
    LtfScenarioEntry,
    LtfStructureEvent,
)
from .breaks import StructureEventDraft, detect_breaks
from .context import context_complete, context_flags, scenario_context
from .eligibility import (
    ADMISSION_CONTEXT,
    REASON_OUTSIDE_PD,
    admitted_scenario_entries,
    entry_reason,
    evaluate_entry,
)
from .entries import (
    H1_MS,
    build_movement,
    detect_entry_zones,
    merge_test_extreme,
    test_depth_of,
    touch_bar,
)
from .liquidity import resolve_sweep
from .pivots import PivotCandidate, confirmed_pivots, find_h1_pivots
from .ranges import (
    RANGE_PIVOT_LEFT,
    RANGE_PIVOT_RIGHT,
    RangeDraft,
    range_recalc,
    zone_half,
)
from .roles import assign_roles
from .structure import StructureBatch, sync_structure

# §4/§15: инвалидация родителя — конверсия в Breaker или архив по пробою;
# WORKED (90%) инвалидацией OB не является (приёмка п.17)
_INVALIDATING_REASONS = ("breaker_broken", "prb_broken", "jumped_through", "swept")

# состояния наблюдения, из которых возможна автоархивация по неактивности
_STALE_ARCHIVABLE_STATES = ("waiting_structure", "active", "paused_data")


def _processing_mode(lag_ms: int, grace_ms: int) -> str:
    """F01/A01: происхождение события по лагу обнаружения. В пределах
    grace-окна живого опроса — 'live' (доставляется); дольше — 'catchup'
    (догоняющий бэклог, подавлен как replay). 'replay' задаёт вызывающий
    код явно (исторический прогон), здесь не вычисляется."""
    return "live" if lag_ms <= grace_ms else "catchup"


@dataclass
class LtfTickResult:
    """Итог обработки (одной или нескольких свечей)."""

    processed: int = 0
    last_candle_open_time: Optional[int] = None
    pivots_new: int = 0
    scenarios_created: list[int] = field(default_factory=list)
    cancellations: list[int] = field(default_factory=list)
    ranges_created: list[int] = field(default_factory=list)
    entry_zones_processed: list[int] = field(default_factory=list)
    touches: list[int] = field(default_factory=list)      # entry_zone_id
    sweeps: list[int] = field(default_factory=list)       # liquidity_test_id
    events: list[LtfEvent] = field(default_factory=list)  # только новые (created)


class LtfEngine:
    """Оркестратор LTF-модуля. Живёт поверх Database и DetectorConfig."""

    def __init__(self, db: Database, cfg: DetectorConfig, *,
                 scan_cursors: bool = True):
        self.db = db
        self.cfg = cfg
        # scan_cursors=True (прод): пакетная обработка свечей (replay/backlog)
        # использует StructureBatch — инкрементальные pivots/avail/роли и
        # курсоры сканирования сломов; детерминированно эквивалентно полному
        # пересчёту на каждую свечу (проверяется test_ltf_replay_perf).
        # False — полный пересчёт на свечу (поведение до оптимизации).
        self.scan_cursors = scan_cursors
        self._batch: Optional[StructureBatch] = None
        # §18: курсор инкрементального сканирования контр-снятий по сценарию
        # (scenario_id → close_time последней проверенной свечи). Кандидаты
        # (pivots, подтверждённые к триггеру) и уже просканированные закрытия
        # неизменны, поэтому пересканировать историю на каждой свече не нужно;
        # после перезапуска процесса курсор пуст — один полный прогон, повторы
        # гасятся дедупом событий. Без курсора replay всей H1-истории шёл
        # десятки часов и голодал веб/API (GIL + замок БД).
        self._ctx_sweep_cursor: dict[int, int] = {}

    # ------------------------------------------------------------------ #
    # Запуск наблюдения (§4)
    # ------------------------------------------------------------------ #

    def on_htf_zone_touched(
        self, instrument_id: int, zone: Zone, occurred_at: int
    ) -> LtfObservation:
        """Открыть (или вернуть существующее) наблюдение за HTF-зоной.

        Идемпотентно по UNIQUE(zone_id, cycle_id): повторное HTF-событие
        и подавление Telegram не влияют на наблюдение (§4).
        """
        return self.db.insert_ltf_observation(LtfObservation(
            id=None, instrument_id=instrument_id, zone_id=zone.id,
            zone_version=int(zone.evidence.get("boundary_version", 1)),
            cycle_id=zone.cycle_id, direction=zone.direction,
            state="waiting_structure", activated_at=occurred_at,
            created_at=occurred_at, updated_at=occurred_at,
            evidence={
                "zone_type": zone.type.value, "timeframe": zone.timeframe,
                "zone_lower": zone.lower, "zone_upper": zone.upper,
            },
        ))

    # ------------------------------------------------------------------ #
    # Обработка закрытий H1 (§13)
    # ------------------------------------------------------------------ #

    def process_h1_close(
        self, instrument_id: int, now_ms: Optional[int] = None
    ) -> LtfTickResult:
        """Обрабатывает все ещё не обработанные закрытые H1-свечи инструмента
        (инкрементально, через meta-курсор — перезапуск ничего не дублирует)."""
        now = now_ms if now_ms is not None else _real_now_ms()
        grace_ms = self.cfg.ltf_live_grace_seconds * 1000
        closed = self.db.get_candles(instrument_id, "H1")
        result = LtfTickResult()
        if not closed:
            return result
        key = f"ltf:h1:last_close:{instrument_id}"
        last_done = int(self.db.get_meta(key) or 0)
        self._batch = StructureBatch() if self.scan_cursors else None
        try:
            for idx, c in enumerate(closed):
                if c.close_time <= last_done:
                    continue
                # F01/A01: лаг обнаружения относительно закрытия свечи. В
                # пределах grace-окна события — live (доставляются: любой лаг
                # опроса, даже миллисекунды, не подавляет свежий сигнал);
                # дольше — catchup: массовый догоняющий бэклог не уходит как
                # текущий (§13)
                lag = max(0, now - c.close_time)
                self._process_candle(
                    instrument_id, closed, idx,
                    detected_at=now,
                    processing_mode=_processing_mode(lag, grace_ms),
                    detection_lag_ms=lag, result=result,
                )
                self.db.set_meta(key, str(c.close_time))
        finally:
            self._batch = None
        return result

    def replay_observation(self, observation_id: int) -> LtfTickResult:
        """§13: детерминированный прогон по сохранённым свечам тем же кодом.

        detected_at = закрытие свечи, события processing_mode='replay'
        (delayed=True — в доставку не идут). Повторный replay
        не дублирует pivots/события/зоны/диапазоны (UNIQUE-ключи + дедуп)
        и не воскрешает tested-зоны (состояние читается из БД).
        """
        result = LtfTickResult()
        obs = self.db.get_ltf_observation(observation_id)
        if obs is None:
            return result
        closed = self.db.get_candles(obs.instrument_id, "H1")
        # «сейчас» replay — голова истории, а не реальное время: иначе
        # detection_lag_ms событий зависел бы от wall-clock и повторный
        # replay давал бы другое состояние (§13: replay детерминирован)
        now = closed[-1].close_time if closed else _real_now_ms()
        self._batch = StructureBatch() if self.scan_cursors else None
        try:
            for idx, c in enumerate(closed):
                self._process_candle(
                    obs.instrument_id, closed, idx,
                    detected_at=c.close_time, processing_mode="replay",
                    detection_lag_ms=max(0, now - c.close_time), result=result,
                    # роли в БД переписываем только на голове истории — иначе
                    # каждый replay прокручивает промежуточные роли против
                    # финальных и множит строки ltf_pivot_role_log (§13: replay
                    # не должен менять прошлое)
                    persist_roles=idx == len(closed) - 1,
                )
        finally:
            self._batch = None
        if closed:
            self.db.set_meta(
                f"ltf:h1:last_close:{obs.instrument_id}", str(closed[-1].close_time)
            )
        return result

    def _process_candle(
        self, instrument_id: int, closed: list, idx: int,
        detected_at: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
        persist_roles: bool = True,
    ) -> None:
        candle = closed[idx]
        up_to = closed[: idx + 1]
        now = detected_at
        result.processed += 1
        result.last_candle_open_time = candle.open_time

        # (2) касания опубликованных зон и исходы liquidity-тестов (§13.2)
        self._process_touches(instrument_id, candle, now,
                              processing_mode, detection_lag_ms, result)

        # (3) структура: pivots/роли идемпотентно в БД; события — далее
        sync = sync_structure(self.db, self.cfg, instrument_id, up_to, now,
                              persist_roles=persist_roles, batch=self._batch)
        result.pivots_new += sync.pivots_new
        # роли — из только что выполненного пересчёта (in-memory), а не из БД:
        # в replay роли в БД пишутся только на голове истории
        avail = sync.avail
        for obs in self.db.list_ltf_observations(instrument_id=instrument_id):
            if obs.state not in ("waiting_structure", "active"):
                continue
            if obs.activated_at > candle.close_time:
                continue  # наблюдение начнётся позже этой свечи (§4)
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is None:
                self._maybe_open_scenario(obs, avail, up_to, candle, now,
                                          processing_mode, detection_lag_ms,
                                          result)
            else:
                self._process_active_scenario(obs, sc, sync, avail, up_to,
                                              candle, now, processing_mode,
                                              detection_lag_ms, result)

    # ------------------------------------------------------------------ #
    # (2) Касания и liquidity-тесты (§9, §10)
    # ------------------------------------------------------------------ #

    def _process_touches(
        self, instrument_id: int, candle, now: int,
        processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        for obs in self.db.list_ltf_observations(instrument_id=instrument_id):
            if obs.state not in ("waiting_structure", "active"):
                continue
            if obs.activated_at > candle.close_time:
                continue
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is None:
                continue
            cur = self.db.get_current_ltf_range(sc.id)
            ver = cur.version if cur is not None else 0
            entries = [
                e for e in self.db.list_ltf_scenario_entries(sc.id, state="fresh")
                if e.range_version == ver
            ]
            # §18: контекстно допущенные FVG вне Premium (state out_of_range)
            # получают тот же touch-флоу, что и свежие зоны
            if context_complete(self._scenario_context(sc.id)):
                entries += [
                    e for e in self.db.list_ltf_scenario_entries(
                        sc.id, state="out_of_range")
                    if e.range_version == ver
                    and entry_reason(e) == REASON_OUTSIDE_PD
                ]
            tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
            for entry in entries:
                zone = self.db.get_ltf_entry_zone(entry.entry_zone_id)
                if zone is None:
                    continue
                if entry.state == "out_of_range" and zone.type != "FVG":
                    continue  # §18: допуск вне Premium — только для FVG
                # replay/backlog: зона должна быть подтверждена к моменту свечи,
                # иначе позднее созданная зона ловила бы ложные «исторические»
                # касания (§13: восстановление не меняет прошлое)
                if zone.confirmed_at and zone.confirmed_at > candle.close_time:
                    continue
                # §8.5: касание проверяется по полному диапазону зоны
                if not touch_bar(zone.lower, zone.upper, candle):
                    continue
                # replay/backlog: версия диапазона подтверждена позже этой
                # свечи — касания до выбора зоны на текущей версии не
                # переисполняются (ТЗ §3: повторный выбор не воскрешает
                # исторические тесты; §13: replay не меняет прошлое)
                if cur is not None and candle.close_time < cur.available_at:
                    continue
                self._on_entry_touched(obs, sc, entry, zone, candle, tests,
                                       now, processing_mode, detection_lag_ms,
                                       result)
            # закрытие тестов, начатых внутри этой свечи (live-путь, §10/п.14)
            for t in self.db.list_ltf_liquidity_tests(state="awaiting_close",
                                                      scenario_id=sc.id):
                if t.candle_open_time != candle.open_time:
                    continue
                zone = self.db.get_ltf_entry_zone(t.entry_zone_id)
                if zone is None:
                    continue
                outcome = resolve_sweep(zone.type, t.level, candle)
                if outcome is not None:
                    self._resolve_liquidity_test(obs, sc, t.id, zone, t.level,
                                                 candle, outcome, now,
                                                 processing_mode,
                                                 detection_lag_ms, result)

    def _on_entry_touched(
        self, obs, sc, entry: LtfScenarioEntry, zone: LtfEntryZone, candle,
        tests: list[LtfLiquidityTest], now: int,
        processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        # §9: касание потребляет текущий выбор зоны; отказ доставки не
        # отменяет факт касания — состояние пишется до события. ТЗ §3:
        # validity="tested" — факт истории, а не запрет навсегда: допуск к
        # повторному выбору считается отдельно по max_test_depth
        self.db.update_ltf_entry_zone(
            zone.id,
            first_test_at=zone.first_test_at or candle.close_time,
            validity="tested",
        )
        self._accumulate_test_depth(zone, candle)
        # reason пересчитывается ПОСЛЕ накопления глубины: глубокий тест
        # (>= 90%) сразу закрывает допуск (tested_too_deep), мелкий —
        # оставляет зону допустимой к перевыбору (ok), ТЗ §13: тест
        # обработан до обновления пригодности следующего входа
        cur = self.db.get_current_ltf_range(sc.id)
        rng_draft = self._row_to_draft(cur, sc.direction) if cur else None
        ev = evaluate_entry(
            zone, sc.direction, self.cfg, rng_draft,
            movements=self.db.list_ltf_movements(sc.id),
            liquidity_tests=tests,
        )
        self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=zone.id,
            range_version=entry.range_version, eligible=ev.eligible,
            overlap=ev.overlap, state="tested", reason=ev.reason,
            added_at=entry.added_at, updated_at=now,
        ))
        result.touches.append(zone.id)
        # §18: допущенная вне Premium FVG аннотируется и в touch — факт
        # «не в Premium» отображается, но не блокирует
        flags = self._scenario_context(sc.id)
        payload: dict[str, Any] = {
            "entry_zone_id": zone.id, "scenario_id": sc.id, "type": zone.type,
            "lower": zone.lower, "upper": zone.upper, "mid": zone.mid,
            "candle_open_time": candle.open_time,
            "half": zone_half(zone.lower, zone.upper, zone.is_level,
                              cur, sc.direction.value),
        }
        if (zone.type == "FVG" and ev.reason == REASON_OUTSIDE_PD
                and context_complete(flags)):
            payload["outside_premium"] = True
            payload["context"] = self._context_brief(flags)
        # §11.5: ключ касания — entry_zone_id + сценарий, БЕЗ range_version
        self._emit(obs.id, sc.id, "touch", payload,
                   candle.close_time, now, f"touch:{zone.id}:{sc.id}",
                   processing_mode, detection_lag_ms, result)

        if zone.type not in ("BSL", "SSL"):
            return  # OB/FVG: касание зафиксировано, зона в истории (§9)
        # BSL/SSL: убираем из свежих, но проверяем начатую свечу до закрытия
        # (§9/§10); один тест на (зона, свеча)
        if any(t.entry_zone_id == zone.id and t.candle_open_time == candle.open_time
               for t in tests):
            return
        tid = self.db.insert_ltf_liquidity_test(LtfLiquidityTest(
            id=None, entry_zone_id=zone.id, scenario_id=sc.id, level=zone.lower,
            touch_at=candle.close_time, candle_open_time=candle.open_time,
        ))
        outcome = resolve_sweep(zone.type, zone.lower, candle)
        if outcome is not None:  # свеча уже закрыта — исход известен сразу
            self._resolve_liquidity_test(obs, sc, tid, zone, zone.lower,
                                         candle, outcome, now,
                                         processing_mode, detection_lag_ms,
                                         result)

    def _accumulate_test_depth(self, zone: LtfEntryZone, candle) -> None:
        """ТЗ §3/§4: глубина теста накапливается за всю историю (максимум);
        мелкий поздний тест не стирает более глубокий прежний. Уровни
        (BSL/SSL, W=0) глубины не имеют."""
        if zone.is_level:
            return
        cur = candle.low if zone.direction == Direction.BULL else candle.high
        extreme = merge_test_extreme(zone.direction, zone.test_extreme, cur)
        depth = test_depth_of(zone, extreme)
        self.db.update_ltf_entry_zone(
            zone.id, max_test_depth=depth, test_extreme=extreme
        )
        zone.max_test_depth = depth
        zone.test_extreme = extreme

    def _resolve_liquidity_test(
        self, obs, sc, test_id: int, zone: LtfEntryZone, level: float, candle,
        outcome: str, now: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        confirmed = outcome == "confirmed"
        self.db.update_ltf_liquidity_test(
            test_id, state=outcome, close_price=candle.close,
            sweep_at=candle.close_time if confirmed else None,
            resolved_at=candle.close_time,
        )
        if confirmed:
            # снятый уровень сразу теряет пригодность на текущей версии,
            # не дожидаясь следующего пересчёта диапазона (приёмка п.17)
            cur = self.db.get_current_ltf_range(sc.id)
            ver = cur.version if cur is not None else 0
            for e in self.db.list_ltf_scenario_entries(sc.id):
                if e.entry_zone_id != zone.id or e.range_version != ver:
                    continue
                self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                    id=None, scenario_id=sc.id, entry_zone_id=zone.id,
                    range_version=e.range_version, eligible=e.eligible,
                    overlap=e.overlap, state="tested", reason="swept_level",
                    added_at=e.added_at, updated_at=now,
                ))
        result.sweeps.append(test_id)
        # §11.5: level_id + sweep_candle_id
        self._emit(obs.id, sc.id,
                   "sweep_confirmed" if confirmed else "sweep_failed", {
                       "liquidity_test_id": test_id, "entry_zone_id": zone.id,
                       "level": level, "close_price": candle.close,
                       "outcome": outcome,
                       "candle_open_time": candle.open_time,
                   }, candle.close_time, now,
                   f"sweep:{zone.id}:{candle.open_time}",
                   processing_mode, detection_lag_ms, result)

    # ------------------------------------------------------------------ #
    # (3) Сценарии: открытие, события, отмена (§6)
    # ------------------------------------------------------------------ #

    def _maybe_open_scenario(
        self, obs, avail: list[PivotCandidate], up_to: list, candle,
        now: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        """Первичный BOS/SMS в направлении HTF-родителя открывает сценарий
        (SMS самостоятелен, §6.3)."""
        # stop_on_cancellation=False: обратный слом отменяет АКТИВНЫЙ сценарий
        # (§6.5); ждущему наблюдению отменять нечего, scan.cancellation здесь
        # не используется, а обрезка скана навсегда блокировала бы открытие
        # (и переоткрытие после отмены) при любом встречном сломе в окне
        if self._batch is not None:
            # пакетная обработка (replay/backlog): курсорный скан — та же
            # траектория машин, что у полного перескана на эту свечу
            cur = self._batch.cursor(
                ("open", obs.id), obs.direction, obs.activated_at,
                stop_on_cancellation=False, cancel_not_before_ms=None,
            )
            scan = cur.scan(self._batch.avail, self._batch.closed,
                            self._batch.open_times, now)
        else:
            scan = detect_breaks(avail, up_to, obs.direction, now,
                                 since_ms=obs.activated_at,
                                 stop_on_cancellation=False)
        primaries = [
            e for e in scan.events
            if e.stage == "primary" and e.occurred_at == candle.close_time
        ]
        if not primaries:
            return
        # replay/повтор: слом, уже записанный в сценарии наблюдения, новый
        # сценарий не открывает
        covered = {
            (ev.level_key, ev.stage)
            for sc0 in self.db.list_ltf_scenarios(observation_id=obs.id)
            for ev in self.db.list_ltf_structure_events(sc0.id)
        }
        primaries = [e for e in primaries if (e.level_key, e.stage) not in covered]
        if not primaries:
            return
        # §11.5-дедуп по level_key не ловит вариант того же движения с другой
        # опорой (иная пара anchor/ref при пересчёте ролей): повторный replay
        # открывал второй сценарий ВНУТРИ окна уже существующего (прошлая
        # отмена лежит в БД раньше своей рыночной даты). Окна — в рыночном
        # времени (occurred_at): [триггер, отмена); пересечение = дубль.
        windows = self._scenario_windows(obs)
        primaries = [
            e for e in primaries
            if not any(start <= e.occurred_at < end for start, end in windows)
        ]
        if not primaries:
            return
        # §6.5: BOS и SMS на одной свече — основной тип BOS
        main = next((e for e in primaries if e.kind == "BOS"), primaries[0])
        sc = self.db.insert_ltf_scenario(LtfScenario(
            id=None, observation_id=obs.id, direction=obs.direction,
            trigger=main.kind, stage="primary", state="range_pending",
            created_at=now, updated_at=now,
        ))
        self.db.update_ltf_observation(obs.id, state="active", updated_at=now)
        result.scenarios_created.append(sc.id)
        se_ids: dict[str, int] = {}
        for e in primaries:
            se_ids[e.level_key] = self._insert_structure_event(sc.id, e, now).id
        self.db.update_ltf_scenario(
            sc.id, trigger_event_id=se_ids[main.level_key], updated_at=now,
        )
        # §11.2/п.18: диапазон и зоны готовы на закрытии слома — одно
        # объединённое событие, иначе range_pending и позже дополнение
        self._update_range(sc, up_to, now, result, avail=avail)
        self._build_entries(obs, sc, main, se_ids[main.level_key], avail,
                            up_to, now, result)
        # §18: контекст (снятие SSL/BSL + тест 50% D1 FVG) — до публикации
        # входов, чтобы допуск вне Premium применился уже в событии слома
        self._update_scenario_context(obs, sc, avail, up_to, now,
                                      processing_mode, detection_lag_ms,
                                      result)
        self._emit_structure_event(obs, sc, main, se_ids[main.level_key],
                                   now, processing_mode, detection_lag_ms,
                                   result)

    def _process_active_scenario(
        self, obs, sc, sync, avail: list[PivotCandidate], up_to: list, candle,
        now: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        cancel = sync.cancellations.get(sc.id)
        fresh = [
            e for e in sync.events.get(sc.id, [])
            if e.occurred_at == candle.close_time
        ]
        if cancel is not None:
            # §13.4/п.19: журнал хранит оба факта, но новый входной сигнал
            # от отменённого сценария не публикуется
            for e in fresh:
                self._insert_structure_event(sc.id, e, now)
            rev = cancel.event
            self._insert_structure_event(sc.id, rev, now)
            self.db.update_ltf_scenario(
                sc.id, state="cancelled", cancellation_reason=cancel.pattern,
                cancelled_at=now, updated_at=now,
            )
            self.db.update_ltf_observation(obs.id, state="waiting_structure",
                                           updated_at=now)
            # §11.5: scenario_id + cancellation_event_id (level_key слома)
            self._emit(obs.id, sc.id, "cancellation", {
                "scenario_id": sc.id, "reason": cancel.pattern,
                "break_level": rev.break_level,
                "break_candle_open_time": rev.break_candle_open_time,
            }, rev.occurred_at, now, f"cancellation:{sc.id}:{rev.level_key}",
                processing_mode, detection_lag_ms, result)
            result.cancellations.append(sc.id)
            return

        se_ids = [(e, self._insert_structure_event(sc.id, e, now).id)
                  for e in fresh]
        # (4) диапазон: новая подтверждённая опора → версия; старые события
        # сохраняют прежнюю геометрию (§7)
        created_range = self._update_range(sc, up_to, now, result, avail=avail)
        # §6.5: продолжение в том же направлении — фиксируем событие, но новые
        # зоны последующих движений не добавляем (§8.1)
        for e, se_id in se_ids:
            self._emit_structure_event(obs, sc, e, se_id, now,
                                       processing_mode, detection_lag_ms,
                                       result)
        # §18: обновление контекста — до entries_ready, чтобы допуск FVG
        # вне Premium увидел свежие факты этого закрытия
        self._update_scenario_context(obs, sc, avail, up_to, now,
                                      processing_mode, detection_lag_ms,
                                      result)
        # §11.5: дополнение — только при ещё не сообщённых свежих зонах,
        # не при каждом изменении M
        self._maybe_entries_ready(obs, sc, candle.close_time, now,
                                  processing_mode, detection_lag_ms,
                                  result,
                                  range_created=created_range is not None,
                                  suppress=bool(se_ids))

    # ------------------------------------------------------------------ #
    # (3b) Контекст сценария §18: снятие SSL/BSL + тест 50% D1 FVG
    # ------------------------------------------------------------------ #

    def _update_scenario_context(
        self, obs, sc, avail: list[PivotCandidate], up_to: list,
        now: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        """§18: новые факты контекста (снятие контр-уровня, тест 50% D1 FVG)
        — события context_update с дедупом по уровню/зоне; отдельно не
        доставляются, читаются агрегацией (_scenario_context)."""
        if sc.state in ("cancelled", "closed"):
            self._ctx_sweep_cursor.pop(sc.id, None)
            return
        trig = next(
            (e for e in self.db.list_ltf_structure_events(sc.id)
             if e.id == sc.trigger_event_id),
            None,
        )
        since = trig.occurred_at if trig is not None else sc.created_at
        ctx = scenario_context(
            self.db, self._scenario_instrument(sc), sc.direction,
            avail, up_to, since, as_of=now,
            sweep_only_after=self._ctx_sweep_cursor.get(sc.id),
        )
        head = up_to[-1] if up_to else None
        if head is not None and head.closed:
            self._ctx_sweep_cursor[sc.id] = max(
                head.close_time, self._ctx_sweep_cursor.get(sc.id, 0)
            )
        for s in ctx["counter_swept"]:
            self._emit(obs.id, sc.id, "context_update", {
                "fact": "counter_sweep", "scenario_id": sc.id, **s,
            }, s["swept_at"], now,
                f"context:{sc.id}:sweep:{s['pivot_ref']}",
                processing_mode, detection_lag_ms, result)
        f = ctx["htf_fvg50"]
        if f is not None and f.get("confirmed", False):
            # L02: неподтверждённый геометрический fallback не эмитится —
            # контекстное исключение §18 он не включает
            self._emit(obs.id, sc.id, "context_update", {
                "fact": "htf_fvg50", "scenario_id": sc.id, **f,
            }, f.get("tested_at") or now, now,
                f"context:{sc.id}:fvg50:{f['zone_id']}",
                processing_mode, detection_lag_ms, result)

    def _scenario_context(self, scenario_id: int) -> dict[str, Any]:
        """§18: агрегированный контекст сценария из событий context_update."""
        return context_flags(
            self.db.list_ltf_events(scenario_id=scenario_id, limit=1000)
        )

    @staticmethod
    def _context_brief(flags: dict[str, Any]) -> dict[str, Any]:
        """Краткий словарь контекста для payload зоны (§18)."""
        f = flags["htf_fvg50"]
        return {
            "counter_swept": bool(flags["counter_swept"]),
            "htf_fvg50": (
                {"zone_id": f["zone_id"], "tf": f["tf"]} if f else None
            ),
        }

    def _scenario_windows(self, obs) -> list[tuple[int, float]]:
        """Окна [открытие; отмена) сценариев наблюдения в рыночном времени.

        Начало — occurred_at триггера (created_at, если запись триггера не
        найдена); конец — occurred_at обратного слома-отмены (cancelled_at
        для ручного/родительского закрытия); None — сценарий жив (окно
        бесконечно). Используется как фильтр дублей при открытии (§11.5
        по смыслу: одно окно — один сценарий направления).
        """
        windows: list[tuple[int, float]] = []
        for sc0 in self.db.list_ltf_scenarios(observation_id=obs.id):
            if sc0.direction != obs.direction:
                continue
            events = self.db.list_ltf_structure_events(sc0.id)
            trig = next((e for e in events if e.id == sc0.trigger_event_id), None)
            start = trig.occurred_at if trig is not None else sc0.created_at
            end: float = float("inf")
            if sc0.state in ("cancelled", "closed"):
                rev = [e for e in events if e.direction != obs.direction]
                end = (min(e.occurred_at for e in rev) if rev
                       else (sc0.cancelled_at or float("inf")))
            windows.append((start, end))
        return windows

    def _insert_structure_event(
        self, scenario_id: int, e: StructureEventDraft, now: int
    ) -> LtfStructureEvent:        return self.db.insert_ltf_structure_event(LtfStructureEvent(
            id=None, scenario_id=scenario_id, kind=e.kind, stage=e.stage,
            direction=e.direction, break_level=e.break_level,
            break_candle_open_time=e.break_candle_open_time,
            occurred_at=e.occurred_at, detected_at=now,
            ref_pivot_ids=e.ref_pivot_ids, accompanying=e.accompanying,
            level_key=e.level_key, evidence=e.evidence,
        ))

    # ------------------------------------------------------------------ #
    # (4) Диапазон Premium/Discount (§7)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_draft(row: LtfRange, direction: Direction) -> RangeDraft:
        return RangeDraft(
            direction=direction, lower=row.lower, upper=row.upper, mid=row.mid,
            anchor_low_ref=row.anchor_low_pivot_id,
            anchor_high_ref=row.anchor_high_pivot_id,
            available_at=row.available_at,
        )

    def _range_pivots(
        self, instrument_id: int, up_to: list, now: int,
        avail: Optional[list[PivotCandidate]] = None,
    ) -> list[PivotCandidate]:
        """Опоры диапазона подтверждаются РОВНО тремя правыми закрытыми
        свечами (RANGE_PIVOT_LEFT/RIGHT = 3/3; ТЗ «LTF Current Setup» §3/§9 —
        неизменяемое правило). Настройка cfg.ltf_range_right на расчёт
        диапазона НЕ влияет: оставлена в конфиге для совместимости
        (settings.json мог содержать 0 — это давало мгновенное
        «подтверждение» опор, NULL-якоря и тысячи версий диапазона).
        При структурном профиле 3/3 переиспользуются свежие pivots текущего
        sync (якоря = id ltf_pivot); иначе — транзитный расчёт 3/3 с
        разрешением якорей по материализованным pivots БД, чтобы
        anchor_*_pivot_id версий персистились и дедуп версий работал."""
        structural = (self.cfg.ltf_structure_left, self.cfg.ltf_structure_right)
        if structural == (RANGE_PIVOT_LEFT, RANGE_PIVOT_RIGHT):
            # роли — из свежего пересчёта sync.avail, не из БД (в replay роли
            # в БД пишутся только на голове истории)
            if avail is not None:
                return avail
            return [
                p for p in self._db_pivots(instrument_id)
                if p.state == "confirmed" and p.confirmed_at
                and p.confirmed_at <= now
            ]
        cands = find_h1_pivots(up_to, RANGE_PIVOT_LEFT, RANGE_PIVOT_RIGHT)
        pivots = confirmed_pivots(cands, now)
        res = assign_roles(pivots)
        for i, p in enumerate(pivots):
            p.role = res.roles[i]
        self._attach_db_pivot_ids(instrument_id, pivots)
        return pivots

    def _attach_db_pivot_ids(
        self, instrument_id: int, pivots: list[PivotCandidate]
    ) -> None:
        """Подставить pivot_id материализованных в ltf_pivot экстремумов
        (совпадение по pivot_at/kind/price). Транзитный расчёт 3/3 при
        нестандартном структурном профиле тогда тоже даёт персистентные
        якоря версий, а не NULL."""
        by_key = {
            (p.pivot_at, p.kind, p.price): p.id
            for p in self.db.list_ltf_pivots(instrument_id)
        }
        for p in pivots:
            if p.pivot_id is None:
                p.pivot_id = by_key.get((p.pivot_at, p.kind, p.price))

    def _update_range(
        self, sc, up_to: list, now: int, result: LtfTickResult,
        avail: Optional[list[PivotCandidate]] = None,
    ) -> Optional[LtfRange]:
        # ТЗ §8/п.04: отменённый/закрытый сценарий не получает новых рабочих
        # версий диапазона и пересчёта привязок (исторический replay
        # идемпотентен и сюда не доходит — live-путь берёт только активные)
        if sc.state in ("cancelled", "closed"):
            return None
        pivots = self._range_pivots(self._scenario_instrument(sc), up_to, now,
                                    avail=avail)
        prev = self.db.get_current_ltf_range(sc.id)
        prev_draft = self._row_to_draft(prev, sc.direction) if prev else None
        # пакетная обработка: draft детерминирован (avail, prev_draft); avail
        # растёт добавлением, поэтому (len(avail), отпечаток prev) — полный
        # ключ. Пересмотр ролей инвалидирует range_memo (StructureBatch).
        # Мемо только когда опоры — avail структурного профиля 3/3 (транзитный
        # расчёт при ином профиле меняется каждую свечу и не мемоизируется).
        memo_key = None
        if (
            self._batch is not None and avail is not None
            and (self.cfg.ltf_structure_left, self.cfg.ltf_structure_right)
            == (RANGE_PIVOT_LEFT, RANGE_PIVOT_RIGHT)
        ):
            memo_key = (
                sc.id, len(pivots),
                None if prev_draft is None else (
                    prev_draft.lower, prev_draft.upper,
                    prev_draft.anchor_low_ref, prev_draft.anchor_high_ref,
                    prev_draft.available_at,
                ),
            )
            if memo_key in self._batch.range_memo:
                draft = self._batch.range_memo[memo_key]
            else:
                draft = range_recalc(prev_draft, pivots, sc.direction, now)
                self._batch.range_memo[memo_key] = draft
        else:
            draft = range_recalc(prev_draft, pivots, sc.direction, now)
        if draft is None:
            return None
        # якоря — id ltf_pivot: pivots текущего sync (профиль 3/3) уже с id,
        # транзитные — с id, подставленными _attach_db_pivot_ids; None лишь
        # когда экстремум в БД не материализован
        low_id = (
            draft.anchor_low_ref
            if draft.anchor_low_ref and self.db.get_ltf_pivot(draft.anchor_low_ref)
            else None
        )
        high_id = (
            draft.anchor_high_ref
            if draft.anchor_high_ref and self.db.get_ltf_pivot(draft.anchor_high_ref)
            else None
        )
        # §9/п.08: семантический дедуп версий в ОБОИХ путях — та же геометрия
        # с тем же available_at — ТА ЖЕ версия; повторная обработка тех же
        # свечей (replay/backlog/догон) новую версию не создаёт. Без него
        # каждый прогон дописывал бы старые геометрии как новые версии.
        if any(
            (r.lower, r.upper, r.anchor_low_pivot_id, r.anchor_high_pivot_id,
             r.available_at) == (draft.lower, draft.upper, low_id, high_id,
                                 draft.available_at)
            for r in self.db.list_ltf_ranges(sc.id)
        ):
            return None
        row_id = self.db.insert_ltf_range(LtfRange(
            id=None, scenario_id=sc.id,
            version=(prev.version + 1) if prev else 1,
            lower=draft.lower, upper=draft.upper, mid=draft.mid,
            anchor_low_pivot_id=low_id,
            anchor_high_pivot_id=high_id,
            available_at=draft.available_at,
            prev_version_id=prev.id if prev else None,
        ))
        result.ranges_created.append(row_id)
        if sc.state == "range_pending":
            self.db.update_ltf_scenario(sc.id, state="monitoring_entries",
                                        updated_at=now)
            sc.state = "monitoring_entries"
        # §7/п.09: якорь пары — кандидат BSL/SSL только при СМЕНЕ опорного
        # pivot пары; смена диапазона с прежним pivot новый уровень не создаёт
        anchor_zone_id = None
        if prev_draft is None or (
            self._pair_anchor_ref(sc.direction, draft)
            != self._pair_anchor_ref(sc.direction, prev_draft)
        ):
            anchor_zone_id = self._add_range_anchor_level(sc, draft, pivots,
                                                          up_to, now, result)
        # пересчёт применимости всех зон сценария на новой версии диапазона;
        # зоны сохраняют исходные цены, меняется только фильтр (§7).
        # ТЗ §10: пригодность — конъюнкция evaluate_entry; невалидная зона
        # не возвращается в fresh, снятый BSL/SSL не воскресает (п.17),
        # tested < 90% допускается к повторному выбору (ТЗ §3)
        movements = self.db.list_ltf_movements(sc.id)
        tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
        zone_ids = self._scenario_zone_ids(sc.id)
        if anchor_zone_id is not None:
            zone_ids.add(anchor_zone_id)
        for zid in zone_ids:
            zone = self.db.get_ltf_entry_zone(zid)
            if zone is None:
                continue
            ev = evaluate_entry(
                zone, sc.direction, self.cfg, draft,
                movements=movements, liquidity_tests=tests,
            )
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=zid,
                range_version=(prev.version + 1) if prev else 1,
                eligible=ev.eligible, overlap=ev.overlap, state=ev.state,
                reason=ev.reason, added_at=now, updated_at=now,
            ))
        return self.db.get_current_ltf_range(sc.id)

    @staticmethod
    def _pair_anchor_ref(direction: Direction, draft: RangeDraft) -> Optional[int]:
        """Опорный pivot пары, дающий BSL (bear) / SSL (bull): последний
        LH медвежьей / HL бычьей пары (§7)."""
        return (
            draft.anchor_high_ref if direction == Direction.BEAR
            else draft.anchor_low_ref
        )

    def _add_range_anchor_level(
        self, sc, draft: RangeDraft, avail: list[PivotCandidate], up_to: list,
        now: int, result: LtfTickResult,
    ) -> Optional[int]:
        """§7: последний LH (bear) / HL (bull) пары — кандидат BSL/SSL."""
        bear = sc.direction == Direction.BEAR
        anchor_ref = draft.anchor_high_ref if bear else draft.anchor_low_ref
        pivot = next(
            (p for p in avail if (p.pivot_id or p.pivot_at) == anchor_ref), None
        )
        if pivot is None:
            return None
        type_ = "BSL" if bear else "SSL"
        # ТЗ §11: идентичность уровня — опорный pivot (evidence["pivot_ref"]),
        # а не цена: два разных экстремума могут иметь одинаковую цену.
        # Уже существующая зона этого pivot не дублируется и не воскресает —
        # её пригодность (в т.ч. снятие) переоценит _update_range (п.17)
        for zid in self._scenario_zone_ids(sc.id):
            z = self.db.get_ltf_entry_zone(zid)
            if (
                z is not None and z.type == type_
                and z.evidence.get("pivot_ref") == anchor_ref
            ):
                return zid
        first_test = None
        for c in up_to:
            if c.open_time <= pivot.pivot_at:
                continue
            if c.high >= pivot.price and c.low <= pivot.price:
                first_test = c.open_time
                break
        movements = self.db.list_ltf_movements(sc.id)
        ez = self.db.insert_ltf_entry_zone(LtfEntryZone(
            id=None, instrument_id=self._scenario_instrument(sc),
            type=type_, direction=sc.direction, lower=pivot.price,
            upper=pivot.price, formed_at=pivot.pivot_at,
            confirmed_at=pivot.confirmed_at,
            movement_id=movements[-1].id if movements else 0,
            first_test_at=first_test,
            validity="tested" if first_test is not None else "fresh",
            evidence={"range_anchor": True, "pivot_ref": anchor_ref},
        ))
        result.entry_zones_processed.append(ez.id)
        return ez.id

    def _scenario_instrument(self, sc) -> int:
        obs = self.db.get_ltf_observation(sc.observation_id)
        return obs.instrument_id if obs else 0

    def _scenario_zone_ids(self, scenario_id: int) -> set[int]:
        return {
            e.entry_zone_id
            for e in self.db.list_ltf_scenario_entries(scenario_id)
        }

    # ------------------------------------------------------------------ #
    # (5) Entry Zones причинного движения (§8, §9)
    # ------------------------------------------------------------------ #

    def _build_entries(
        self, obs, sc, main: StructureEventDraft, se_id: int,
        avail: list[PivotCandidate], up_to: list, now: int,
        result: LtfTickResult,
    ) -> None:
        # ТЗ §8/п.04: отменённый/закрытый сценарий не получает новых
        # движений и Entry Zones
        if sc.state in ("cancelled", "closed"):
            return
        lookback = (self.cfg.uncalibrated_consolidation_max_candles + 5) * H1_MS
        mv = build_movement(sc.id, avail, up_to, main, sc.direction, lookback)
        if mv is None:
            return
        movement_id = self.db.insert_ltf_movement(LtfMovement(
            id=None, scenario_id=sc.id, start_pivot_id=mv.start_pivot_ref,
            end_pivot_id=mv.end_pivot_ref, start_at=mv.start_at,
            end_at=mv.end_at, break_event_id=se_id, confirmed_at=now,
            source_candle_ids=mv.source_candle_ids,
            provenance_status=mv.provenance_status,
        ))
        det = detect_entry_zones(up_to, mv, avail, sc.direction, self.cfg)
        rng = self.db.get_current_ltf_range(sc.id)
        rng_draft = self._row_to_draft(rng, sc.direction) if rng else None
        ver = rng.version if rng else 0
        movements = self.db.list_ltf_movements(sc.id)
        tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
        # уровни якорей диапазона уже могут быть зонами сценария — не дублируем
        existing: dict[tuple[str, float, float], int] = {}
        for zid in self._scenario_zone_ids(sc.id):
            z0 = self.db.get_ltf_entry_zone(zid)
            if z0 is not None:
                existing[(z0.type, z0.lower, z0.upper)] = zid
        for z in det.zones:
            dup_id = existing.get((z.type, z.lower, z.upper))
            if dup_id is not None:
                ez = self.db.get_ltf_entry_zone(dup_id)
            else:
                ez = self.db.insert_ltf_entry_zone(LtfEntryZone(
                    id=None, instrument_id=obs.instrument_id, type=z.type,
                    direction=z.direction, lower=z.lower, upper=z.upper,
                    formed_at=z.formed_at, confirmed_at=z.confirmed_at,
                    movement_id=movement_id, first_test_at=z.first_test_at,
                    validity=z.validity, max_test_depth=z.max_test_depth,
                    test_extreme=z.test_extreme, evidence=z.evidence,
                ))
                existing[(ez.type, ez.lower, ez.upper)] = ez.id
            result.entry_zones_processed.append(ez.id)
            # ТЗ §10: пригодность — конъюнкция evaluate_entry (тип, sweep,
            # движение, глубина тестов, половина диапазона); протестированная
            # зона с глубиной СТРОГО < 90% допускается к повторному выбору
            ev = evaluate_entry(
                ez, sc.direction, self.cfg, rng_draft,
                movements=movements, liquidity_tests=tests,
            )
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=ez.id,
                range_version=ver, eligible=ev.eligible, overlap=ev.overlap,
                state=ev.state, reason=ev.reason, added_at=now, updated_at=now,
            ))

    # ------------------------------------------------------------------ #
    # (6) События LtfEvent (§11)
    # ------------------------------------------------------------------ #

    def _emit(
        self, observation_id: int, scenario_id: Optional[int], kind: str,
        payload: dict[str, Any], occurred_at: int, detected_at: int,
        dedupe_key: str, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> LtfEvent:
        # F01/A01: legacy delayed — производный от происхождения: всё, что не
        # live (catchup/replay/unknown), gates доставки подавляют без изменений
        ev, created = self.db.insert_ltf_event(LtfEvent(
            id=None, observation_id=observation_id, scenario_id=scenario_id,
            kind=kind, payload=payload, occurred_at=occurred_at,
            detected_at=detected_at, dedupe_key=dedupe_key,
            delayed=processing_mode != "live",
            processing_mode=processing_mode, detection_lag_ms=detection_lag_ms,
        ))
        if created:
            result.events.append(ev)
        return ev

    @staticmethod
    def _range_payload(rng: LtfRange) -> dict[str, Any]:
        return {"lower": rng.lower, "upper": rng.upper, "mid": rng.mid,
                "version": rng.version}

    def _entry_payload(
        self, sc, entry: LtfScenarioEntry, zone: LtfEntryZone, rng,
        flags: dict[str, Any], outside_premium: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "entry_zone_id": zone.id, "type": zone.type, "lower": zone.lower,
            "upper": zone.upper, "mid": zone.mid, "overlap": entry.overlap,
            "confirmed_at": zone.confirmed_at,
            # §18: половина диапазона зоны — всегда; допуск вне Premium —
            # явная пометка + краткий контекст
            "half": zone_half(zone.lower, zone.upper, zone.is_level,
                              rng, sc.direction.value),
        }
        if outside_premium:
            payload["outside_premium"] = True
            payload["context"] = self._context_brief(flags)
        return payload

    def _entry_candidates(
        self, sc
    ) -> list[tuple[LtfScenarioEntry, LtfEntryZone, bool]]:
        """Зоны, допущенные к выбору на текущей версии диапазона (ver=0 до
        пары): свежие и повторно выбранные протестированные с глубиной
        тестов < 90% (ТЗ §3 — переклассификация в _update_range).
        Отбор делегирован admitted_scenario_entries — единому источнику
        правила допуска (L01); третий элемент кортежа — признак контекстного
        допуска §18 (admission_basis == context_exception)."""
        return [
            (e, zone, fe.admission_basis == ADMISSION_CONTEXT)
            for e, zone, fe in admitted_scenario_entries(self.db, sc.id)
        ]

    def _announced_entry_ids(self, scenario_id: int) -> set[int]:
        """Уже сообщённые зоны (§11.5): id из payload прошлых bos/sms/
        entries_ready того же сценария."""
        announced: set[int] = set()
        for ev in self.db.list_ltf_events(scenario_id=scenario_id, limit=1000):
            if ev.kind not in ("bos", "sms", "entries_ready"):
                continue
            for item in ev.payload.get("entries", []):
                announced.add(item["entry_zone_id"])
        return announced

    def _movement_payload(
        self, scenario_id: int, break_event_id: int
    ) -> Optional[dict[str, Any]]:
        """Причинное движение слома (§8.1) для payload события: своё у
        первичного BOS/SMS, у вторичного/accompanying — последнее движение
        сценария (поиск зон идёт от первичного, §8). None — движение не
        построилось (нет стартового pivot)."""
        mvs = self.db.list_ltf_movements(scenario_id)
        if not mvs:
            return None
        mv = next(
            (m for m in reversed(mvs) if m.break_event_id == break_event_id),
            mvs[-1],
        )
        start = self.db.get_ltf_pivot(mv.start_pivot_id)
        end = self.db.get_ltf_pivot(mv.end_pivot_id)
        return {
            "start_price": start.price if start is not None else None,
            "end_price": end.price if end is not None else None,
            "start_at": mv.start_at,
            "end_at": mv.end_at,
            "candles": len(mv.source_candle_ids),
            "provenance_status": mv.provenance_status,
        }

    def _emit_structure_event(
        self, obs, sc, e: StructureEventDraft, se_id: int, now: int,
        processing_mode: str, detection_lag_ms: int, result: LtfTickResult,
    ) -> None:
        """Событие bos/sms (§11.1/§11.2). При готовых диапазоне и зонах —
        одно объединённое сообщение (приёмка п.18)."""
        rng = self.db.get_current_ltf_range(sc.id)
        fresh = self._entry_candidates(sc) if rng is not None else []
        flags = self._scenario_context(sc.id)
        payload: dict[str, Any] = {
            "scenario_id": sc.id, "structure_event_id": se_id,
            "direction": sc.direction.value, "kind": e.kind, "stage": e.stage,
            "break_level": e.break_level,
            "break_candle_open_time": e.break_candle_open_time,
            "range": self._range_payload(rng) if rng else None,
            "range_pending": rng is None,
            "movement": self._movement_payload(sc.id, se_id),
            # при range_pending зоны копятся кандидатами и не анонсируются —
            # о готовых сообщит entries_ready (§11.2)
            "entries": [
                self._entry_payload(sc, en, z, rng, flags, out)
                for en, z, out in fresh
            ],
        }
        # §11.5: scenario_id + structure_event_id
        self._emit(obs.id, sc.id, e.kind.lower(), payload, e.occurred_at, now,
                   f"{e.kind.lower()}:{sc.id}:{se_id}",
                   processing_mode, detection_lag_ms, result)

    def _maybe_entries_ready(
        self, obs, sc, occurred_at: int, now: int,
        processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult, range_created: bool, suppress: bool,
    ) -> None:
        """§11.2: дополнение при появлении ещё не сообщённых подходящих зон
        (включая повторно выбранные протестированные, ТЗ §3); иначе при
        первой готовой паре — range_ready (ожидание зон).
        occurred_at — закрытие текущей свечи (рыночное время), now — когда
        алгоритм увидел (при backlog/replay они различаются, §13)."""
        if suppress:
            return  # объединённое событие bos/sms на этом закрытии уже всё несёт
        rng = self.db.get_current_ltf_range(sc.id)
        if rng is None:
            return
        announced = self._announced_entry_ids(sc.id)
        flags = self._scenario_context(sc.id)
        fresh = [(en, z, out) for en, z, out in self._entry_candidates(sc)
                 if z.id not in announced]
        if fresh:
            ids = sorted(z.id for _, z, _ in fresh)
            self._emit(obs.id, sc.id, "entries_ready", {
                "scenario_id": sc.id, "range": self._range_payload(rng),
                "entries": [
                    self._entry_payload(sc, en, z, rng, flags, out)
                    for en, z, out in fresh
                ],
            }, occurred_at, now, f"entries_ready:{sc.id}:{','.join(map(str, ids))}",
                processing_mode, detection_lag_ms, result)
        elif range_created and rng.version == 1:
            self._emit(obs.id, sc.id, "range_ready", {
                "scenario_id": sc.id, "range": self._range_payload(rng),
                "note": "подходящих свежих Entry Zones пока нет",
            }, occurred_at, now, f"range_ready:{sc.id}:{rng.version}",
                processing_mode, detection_lag_ms, result)

    # ------------------------------------------------------------------ #
    # Жизненный цикл родителя и ручное завершение (§4, §11.4)
    # ------------------------------------------------------------------ #

    def check_parent_validity(self, zone: Zone) -> bool:
        """Инвалидация HTF-родителя → HTF_INVALIDATED (§4).

        True — родитель инвалидирован и наблюдение закрыто. Тест 90%
        (WORKED) и запрет Breaker наблюдение НЕ закрывают (приёмка п.17);
        конверсия в Breaker = подтверждённый пробой в обратную сторону.
        """
        obs = self.db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
        if obs is None or obs.state in (
            "closed_by_parent", "closed_by_user", "closed_stale"
        ):
            return False
        invalidated = zone.status == ZoneStatus.CONVERTED or (
            zone.status == ZoneStatus.ARCHIVED
            and zone.end_reason is not None
            and zone.end_reason.startswith(_INVALIDATING_REASONS)
        )
        if not invalidated:
            return False
        now = _real_now_ms()
        sc = self.db.get_active_ltf_scenario(obs.id)
        if sc is not None:
            self.db.update_ltf_scenario(
                sc.id, state="cancelled", cancellation_reason="HTF_INVALIDATED",
                cancelled_at=now, updated_at=now,
            )
            self._emit(obs.id, sc.id, "cancellation", {
                "scenario_id": sc.id, "reason": "HTF_INVALIDATED",
            }, now, now, f"cancellation:{sc.id}:htf_invalidated",
                "live", 0, LtfTickResult())
        self.db.update_ltf_observation(obs.id, state="closed_by_parent",
                                       updated_at=now)
        return True

    def close_scenario_manually(self, scenario_id: int) -> None:
        """Ручное завершение сценария (§12): HTF-зона не трогается,
        наблюдение возвращается в ожидание нового подтверждения."""
        sc = self.db.get_ltf_scenario(scenario_id)
        if sc is None or sc.state in ("cancelled", "closed"):
            return
        now = _real_now_ms()
        self.db.update_ltf_scenario(
            sc.id, state="closed", cancellation_reason="manual",
            cancelled_at=now, updated_at=now,
        )
        self.db.update_ltf_observation(sc.observation_id,
                                       state="waiting_structure",
                                       updated_at=now)
        self._emit(sc.observation_id, sc.id, "cancellation", {
            "scenario_id": sc.id, "reason": "manual",
        }, now, now, f"cancellation:{sc.id}:manual", "live", 0, LtfTickResult())

    # ------------------------------------------------------------------ #
    # Автоархивация неактивных наблюдений и resync pivots
    # ------------------------------------------------------------------ #

    def archive_stale_observations(
        self, now_ms: Optional[int] = None
    ) -> list[int]:
        """Наблюдения без активности дольше ltf_observation_stale_days →
        история (closed_stale), тот же путь записи, что у closed_by_parent.

        Активность — updated_at наблюдения; для наблюдения с живым сценарием
        учитывается и время последнего структурного события сценария (оно
        свежее updated_at, который движок трогает только при смене состояния).
        Идемпотентно: повторный проход видит только уже закрытые состояния.
        0 в настройке — автоархивация выключена."""
        days = self.cfg.ltf_observation_stale_days
        if days <= 0:
            return []
        now = now_ms if now_ms is not None else _real_now_ms()
        cutoff = now - days * 86_400_000
        archived: list[int] = []
        candidates = self.db.list_stale_ltf_observations(
            _STALE_ARCHIVABLE_STATES, cutoff
        )
        for obs in candidates:
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is not None:
                events = self.db.list_ltf_structure_events(sc.id)
                if events and max(e.occurred_at for e in events) >= cutoff:
                    continue  # сценарий жив: сломы были недавно
                self.db.update_ltf_scenario(
                    sc.id, state="cancelled", cancellation_reason="stale",
                    cancelled_at=now, updated_at=now,
                )
                self._emit(obs.id, sc.id, "cancellation", {
                    "scenario_id": sc.id, "reason": "stale",
                }, now, now, f"cancellation:{sc.id}:stale",
                    "live", 0, LtfTickResult())
            self.db.update_ltf_observation(obs.id, state="closed_stale",
                                           updated_at=now)
            archived.append(obs.id)
        return archived

    def reclassify_active_entries(
        self, now_ms: Optional[int] = None
    ) -> dict[int, int]:
        """Лёгкий пересчёт привязок активных сценариев (ТЗ §10, приёмка п.14).

        Применяется при смене ltf_entry_types через /api/settings: reason/
        state/eligible/overlap строк ТЕКУЩЕЙ версии диапазона пересчитываются
        через evaluate_entry — без replay свечей, без новых событий и
        уведомлений. Строки прошлых версий и сами зоны не трогаются
        (история сохраняется); невалидная зона не восстанавливается
        переключением фильтра. Возвращает {scenario_id: число изменённых
        строк}."""
        now = now_ms if now_ms is not None else _real_now_ms()
        out: dict[int, int] = {}
        for obs in self.db.list_ltf_observations():
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is None:
                continue
            rng = self.db.get_current_ltf_range(sc.id)
            rng_draft = self._row_to_draft(rng, sc.direction) if rng else None
            ver = rng.version if rng is not None else 0
            movements = self.db.list_ltf_movements(sc.id)
            tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
            changed = 0
            for e in self.db.list_ltf_scenario_entries(sc.id):
                if e.range_version != ver:
                    continue  # история версий не переписывается
                zone = self.db.get_ltf_entry_zone(e.entry_zone_id)
                if zone is None:
                    continue
                ev = evaluate_entry(
                    zone, sc.direction, self.cfg, rng_draft,
                    movements=movements, liquidity_tests=tests,
                )
                if (ev.reason, ev.state, ev.eligible, ev.overlap) == (
                    e.reason, e.state, e.eligible, e.overlap
                ):
                    continue
                self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                    id=None, scenario_id=sc.id, entry_zone_id=zone.id,
                    range_version=ver, eligible=ev.eligible,
                    overlap=ev.overlap, state=ev.state, reason=ev.reason,
                    added_at=e.added_at, updated_at=now,
                ))
                changed += 1
            if changed:
                out[sc.id] = changed
        return out

    def resync_structure_params(
        self, now_ms: Optional[int] = None
    ) -> dict[int, int]:
        """Перестроить pivots по текущим ltf_structure_left/right.

        L03: изменение параметров создаёт новую версию расчёта
        (calc_version); прежние pivots не удаляются, а помечаются
        superseded_by — ссылки исторических сценариев, диапазонов и движений
        (anchor_*_pivot_id, start/end_pivot_id) остаются разрешимыми, старые
        BOS/SMS читаются со своими опорами. Детекция запускается заново по
        сохранённым закрытым H1-свечам через sync_structure (та же вставка и
        роли, что в рабочем цикле). События, диапазоны и зоны — исторические
        записи и не пересчитываются. Если версия параметров не изменилась,
        перестройка не выполняется (идемпотентно). Возвращает
        {instrument_id: число новых pivots}."""
        now = now_ms if now_ms is not None else _real_now_ms()
        cv_id, created = self.db.get_or_create_calc_version(
            "ltf_structure",
            {"left": self.cfg.ltf_structure_left,
             "right": self.cfg.ltf_structure_right},
            RULE_VERSION_LTF, now,
        )
        if not created:
            return {}  # параметры не менялись — перестройка не требуется
        instrument_ids = sorted(
            {o.instrument_id for o in self.db.list_ltf_observations()}
        )
        out: dict[int, int] = {}
        for iid in instrument_ids:
            self.db.supersede_ltf_pivots(iid, cv_id)
            candles = self.db.get_candles(iid, "H1")
            sync = sync_structure(self.db, self.cfg, iid, candles, now)
            out[iid] = sync.pivots_new
        return out

    # ------------------------------------------------------------------ #

    def _db_pivots(self, instrument_id: int) -> list[PivotCandidate]:
        return [
            PivotCandidate(
                instrument_id=p.instrument_id, price=p.price, kind=p.kind,
                pivot_at=p.pivot_at, candle_open_time=p.candle_open_time,
                confirmed_at=p.confirmed_at or 0, left=p.left, right=p.right,
                state=p.state, pivot_id=p.id, role=p.role,
            )
            for p in self.db.list_ltf_pivots(instrument_id)
        ]
