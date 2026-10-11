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
   на каждой следующей закрытой H1 действующего сценария — добор OB/FVG
   дальнейшего движения, без нового слома и без отсечки по свече BOS/SMS;
6. события LtfEvent с дедуп-ключами §11.5 (insert_ltf_event отбрасывает дубли).

Если снятие и отмена приходятся на одно закрытие, журнал хранит оба факта,
но от отменённого сценария новый вход не публикуется (§13.4).
Live и replay используют один детерминированный путь (_process_candle).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from ...config import DetectorConfig
from ...db import Database
from ...models import Direction, Zone, ZoneStatus, ZoneType, now_ms as _real_now_ms
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
from .context import context_flags, scenario_context
from ..depth import reaches_depth
from .eligibility import (
    REASON_LEVEL_BROKEN,
    activation_cut_for,
    admitted_scenario_entries,
    evaluate_entry,
)
from .entries import (
    H1_MS,
    MovementDraft,
    build_movement,
    detect_entry_zones,
    fvg_filled,
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

log = logging.getLogger(__name__)


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
        self._bar_plan: Optional[dict] = None
        # Инкрементальная локальная нога H1. Полный проход — один раз на пакет,
        # дальше только новая свеча. События и уведомления отсюда не создаются.
        self._h1_cursors: dict[int, Any] = {}

    # ------------------------------------------------------------------ #
    # Запуск наблюдения (§4)
    # ------------------------------------------------------------------ #

    def on_htf_zone_touched(
        self, instrument_id: int, zone: Zone, occurred_at: int,
        interaction: Optional[str] = None, evidence: Optional[dict] = None,
    ) -> Optional[LtfObservation]:
        """Открыть (или вернуть существующее) наблюдение за HTF-зоной.

        Идемпотентно по UNIQUE(zone_id, cycle_id): повторное HTF-событие
        и подавление Telegram не влияют на наблюдение (§4).
        interaction/evidence — основание контакта для источника эпизода
        (правила v1 §3/§10.1: touch/level_cross/crossed_between_observations
        или служебный manual_watch).

        Правила v1 §7: контакт фиксируется как zone_activation с состоянием
        зоны ДО неё; зона, отработанная ≥ порога глубины/заполнения ДО новой
        независимой активации (R16/R18), — не свежее основание: активация
        пишется, но новое наблюдение не открывается (None). unknown
        (needs_replay) наблюдение не блокирует (R25).
        """
        depth_before, fill_before = self._activation_pre_state(zone)
        worked = self._historically_worked(zone, depth_before, fill_before)
        obs = self.db.get_ltf_observation_by_zone(zone.id, zone.cycle_id)
        if obs is not None and obs.state == "closed_stale":
            # §8/R24: техническая автоархивация не закрывает торговый
            # контекст — новый рыночный факт восстанавливает наблюдение;
            # связь с активацией сохраняется (новая не создаётся), дубли
            # событий гасятся дедуп-ключами
            self.db.update_ltf_observation(
                obs.id, state="waiting_structure", updated_at=occurred_at,
            )
            obs = self.db.get_ltf_observation(obs.id)
        if obs is None and not worked:
            obs_evidence: dict[str, Any] = {
                "zone_type": zone.type.value, "timeframe": zone.timeframe,
                "zone_lower": zone.lower, "zone_upper": zone.upper,
            }
            # §3/Q18: доказанное время контакта (реальная котировка) идёт и в
            # наблюдение — скан сломов допускает подтверждение на той же H1
            if (evidence or {}).get("order_proven"):
                obs_evidence["order_proven"] = True
            obs = self.db.insert_ltf_observation(LtfObservation(
                id=None, instrument_id=instrument_id, zone_id=zone.id,
                zone_version=int(zone.evidence.get("boundary_version", 1)),
                cycle_id=zone.cycle_id, direction=zone.direction,
                state="waiting_structure", activated_at=occurred_at,
                created_at=occurred_at, updated_at=occurred_at,
                evidence=obs_evidence,
            ))
        from ...services.htf_context import record_interaction
        episode = record_interaction(
            self.db, self.cfg, instrument_id, zone, occurred_at,
            interaction=interaction, evidence=evidence,
        )
        if self.db.get_open_zone_activation(zone.id, zone.cycle_id) is None:
            self.db.create_zone_activation(
                zone.id, zone.cycle_id, occurred_at,
                episode_id=int(episode["id"]) if episode else None,
                observation_id=obs.id if obs is not None else None,
                depth_before=depth_before, fill_before=fill_before,
                fresh_for_new_activation=not worked and self._known_fresh(
                    zone, depth_before, fill_before
                ),
                created_at=occurred_at,
            )
        return obs

    def _activation_pre_state(self, zone: Zone):
        """Состояние HTF-зоны ДО активации (§7): максимум самостоятельных
        тестов OB / заполнение FVG по визитам. None — unknown (R25:
        needs_replay, история до визита не восстанавливается)."""
        if getattr(zone, "needs_replay", False):
            return None, None
        if zone.type == ZoneType.FVG:
            visits = self.db.get_visits(zone.id, zone.cycle_id)
            depth = max((v.max_depth for v in visits), default=None)
            return depth, (depth >= 1.0 if depth is not None else None)
        if zone.is_level:
            return 0.0, False
        return (zone.max_test_depth if zone.has_tests else 0.0), None

    def _historically_worked(self, zone: Zone, depth_before, fill_before) -> bool:
        """§7: отработанность ДО активации. unknown не блокирует (R25)."""
        if zone.type == ZoneType.FVG:
            return fill_before is True
        if zone.is_level:
            return False
        return (
            depth_before is not None
            and depth_before >= self.cfg.entry_reuse_max_depth
        )

    def _known_fresh(self, zone: Zone, depth_before, fill_before) -> bool:
        """fresh_for_new_activation: unknown — не считается свежим (R25)."""
        if zone.type == ZoneType.FVG:
            return fill_before is not None and not fill_before
        if zone.is_level:
            return True
        return (
            depth_before is not None
            and depth_before < self.cfg.entry_reuse_max_depth
        )

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
        key = f"ltf:h1:last_close:{instrument_id}"
        last_done = int(self.db.get_meta(key) or 0)
        last = self.db.last_candle(instrument_id, "H1")
        if last is None:
            return LtfTickResult()
        from ...services.htf_ideas import (
            ideas_checkpoint_fresh, mark_ideas_checkpoint, reconcile_ideas,
        )
        if last.close_time <= last_done:
            # §10.3: новых закрытий нет, но есть необработанная ревизия уже
            # закрытых свечей — дешёвый сигнал (одна строка MAX), без чтения
            # всей серии на каждом тике
            if self.db.latest_candle_revision_at(instrument_id, "H1") <= int(
                self.db.get_meta(f"ltf:h1:revision_done:{instrument_id}") or 0
            ):
                # Пропущенные OB/FVG уже обработанного контекста добираются
                # один раз, без текущих уведомлений.
                self._recover_context_zones_once(instrument_id)
                # Идеи пересчитываем только если снимок устарел; серию H1
                # заново не читаем.
                if not ideas_checkpoint_fresh(self.db, instrument_id):
                    reconcile_ideas(self.db, instrument_id, self.cfg, now)
                    from ...services.liquidity_anchors import refresh_liquidity_anchors
                    refresh_liquidity_anchors(self.db, instrument_id)
                    mark_ideas_checkpoint(self.db, instrument_id)
                return LtfTickResult()
        closed = self.db.get_candles(instrument_id, "H1")
        result = LtfTickResult()
        if not closed:
            return result
        # F10: отпечаток OHLC уже обработанных свечей — курсор
        # ltf:h1:last_close знает только время закрытия; расхождение OHLC
        # при том же open_time фиксируется как ревизия исходных данных
        fp_key = f"ltf:h1:ohlc:{instrument_id}"
        try:
            fingerprint = json.loads(self.db.get_meta(fp_key) or "{}")
        except json.JSONDecodeError:
            fingerprint = {}
        revisions, fp_dirty = self._detect_h1_revisions(
            instrument_id, closed, last_done, fingerprint
        )
        # §10.3 (R46/R47): ревизия уже обработанных свечей — причинный
        # пересчёт с момента самой ранней ревизии. До пересчёта — снимок
        # активных сценариев для уведомления об исправлении
        pre_scenarios: dict[int, str] = {}
        if revisions:
            earliest = min(r["open_time"] for r in revisions)
            idx0 = next(
                (i for i, c in enumerate(closed) if c.open_time >= earliest),
                len(closed),
            )
            last_done = closed[idx0 - 1].close_time if idx0 > 0 else 0
            for obs in self.db.list_ltf_observations(instrument_id=instrument_id):
                sc = self.db.get_active_ltf_scenario(obs.id)
                if sc is not None:
                    pre_scenarios[sc.id] = sc.state
        self._batch = StructureBatch() if self.scan_cursors else None
        try:
            for idx, c in enumerate(closed):
                if not c.closed or c.close_time <= last_done:
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
                fingerprint[str(c.open_time)] = [c.open, c.high, c.low, c.close]
                fp_dirty = True
        finally:
            self._batch = None
            if fp_dirty:
                self.db.set_meta(fp_key, json.dumps(fingerprint))
        if revisions:
            self._emit_correction_events(
                instrument_id, revisions, pre_scenarios, now, grace_ms, result,
            )
            self.db.set_meta(
                f"ltf:h1:revision_done:{instrument_id}",
                str(self.db.latest_candle_revision_at(instrument_id, "H1")),
            )
        self._recover_context_zones_once(instrument_id)
        reconcile_ideas(self.db, instrument_id, self.cfg, now)
        from ...services.liquidity_anchors import refresh_liquidity_anchors
        refresh_liquidity_anchors(self.db, instrument_id)
        mark_ideas_checkpoint(self.db, instrument_id)
        return result

    def _detect_h1_revisions(
        self, instrument_id: int, closed: list, last_done: int,
        fingerprint: dict,
    ) -> tuple[list[dict], bool]:
        """F10/§10.3: свеча с open_time <= курсора, чей OHLC изменился
        относительно обработанного, — ревизия исходных данных. Факт пишется
        в лог и candle_revision (источник истины; дедуп по целевому OHLC —
        upsert-путь и этот детектор сходятся в одной строке), отпечаток
        обновляется, чтобы та же ревизия не фиксировалась повторно.
        Опубликованная история молча не стирается; пересчёт — с момента
        самой ранней ревизии (вызывающий). Возвращает (список ревизий,
        признак изменения отпечатка)."""
        revisions: list[dict] = []
        dirty = False
        detected = _real_now_ms()
        for c in closed:
            if not c.closed or c.close_time > last_done:
                continue
            key = str(c.open_time)
            bar = [c.open, c.high, c.low, c.close]
            prev = fingerprint.get(key)
            if prev is None:
                fingerprint[key] = bar
                dirty = True
            elif prev != bar:
                revision = {
                    "open_time": c.open_time, "previous": prev, "current": bar,
                    "detected_at": detected,
                }
                log.warning(
                    "LTF %s: ревизия уже обработанной H1 open_time=%s "
                    "OHLC %s -> %s; пересчёт зависимостей с этого момента (§10.3)",
                    instrument_id, c.open_time, prev, bar,
                )
                self.db._record_candle_revision(
                    instrument_id, "H1", c.open_time, prev, bar, detected,
                )
                self.db.set_meta(
                    f"ltf:h1:revision:{instrument_id}", json.dumps(revision)
                )
                fingerprint[key] = bar
                revisions.append(revision)
                dirty = True
        return revisions, dirty

    def _emit_correction_events(
        self, instrument_id: int, revisions: list[dict],
        pre_scenarios: dict[int, str], now: int, grace_ms: int,
        result: LtfTickResult,
    ) -> None:
        """R46 (§10.3): пересчёт изменил уже активный сигнал — отдельное
        уведомление об исправлении с причиной (candle_revision) и ссылкой на
        исходный сценарий. Неизменённые факты не пересылаются (дедуп-ключ),
        массовой повторной рассылки истории нет: режим по лагу, как у всех."""
        earliest = min(r["open_time"] for r in revisions)
        rev_opens = sorted(r["open_time"] for r in revisions)
        lag = max(0, now - earliest)
        for sc_id, prev_state in pre_scenarios.items():
            sc = self.db.get_ltf_scenario(sc_id)
            if sc is None or sc.state == prev_state:
                continue  # состояние не изменилось — исправление мимо
            obs = self.db.get_ltf_observation(sc.observation_id)
            trigger = (
                next((e for e in self.db.list_ltf_structure_events(sc.id)
                      if e.id == sc.trigger_event_id), None)
                if sc.trigger_event_id else None
            )
            fact_ref = (
                f"исходный факт: {trigger.kind} {trigger.break_level}"
                if trigger is not None else f"сценарий {sc.id}"
            )
            opens = ", ".join(str(o) for o in rev_opens)
            text = (
                f"Исправление данных (H1): сценарий {sc.id} "
                f"{prev_state} → {sc.state}. Причина: ревизия закрытых "
                f"свечей ({opens}). Исходный факт сохранён в журнале: {fact_ref}."
            )
            self._emit(
                obs.id if obs is not None else 0, sc.id, "correction", {
                    "fact": "scenario_corrected",
                    "reason": "candle_revision",
                    "revision_open_times": rev_opens,
                    "scenario_id": sc.id,
                    "previous_state": prev_state,
                    "new_state": sc.state,
                    "text": text,
                },
                earliest, now,
                f"correction:{sc.id}:{'_'.join(str(o) for o in rev_opens)}",
                _processing_mode(lag, grace_ms), lag, result,
            )

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
            from ...services.htf_ideas import mark_ideas_checkpoint, reconcile_ideas
            reconcile_ideas(self.db, obs.instrument_id, self.cfg, closed[-1].close_time)
            from ...services.liquidity_anchors import refresh_liquidity_anchors
            refresh_liquidity_anchors(self.db, obs.instrument_id)
            mark_ideas_checkpoint(self.db, obs.instrument_id)
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
        # Локальная нога инструмента. Не создаёт событий и не выбирает PD сценария.
        from ...services.h1_setup import H1LegCursor
        batch = self._batch
        gen = batch.structure_gen if batch is not None else 0
        batch_id = id(batch) if batch is not None else 0
        cursor = self._h1_cursors.get(instrument_id)
        if cursor is None:
            cursor = H1LegCursor(instrument_id, generation=gen, batch_id=batch_id)
            self._h1_cursors[instrument_id] = cursor
        else:
            # Новый пакет начинает structure_gen с единицы. Это не смена ролей:
            # машина ноги продолжает префикс. Смену роли видит сам курсор.
            cursor.generation = gen
            cursor.batch_id = batch_id
        cursor.advance(self.db, avail, up_to, candle.close_time, self.cfg)
        from ...services.htf_context import apply_context_bar, plan_context_bar
        self._bar_plan = plan_context_bar(
            self.db, self.cfg, instrument_id, candle, avail, up_to, now,
        )
        try:
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
            apply_context_bar(
                self, instrument_id, candle, avail, up_to, now,
                processing_mode, detection_lag_ms, result, self._bar_plan,
            )
        finally:
            self._bar_plan = None

    def _scenario_start(self, sc) -> int:
        """Рыночное время старта сценария: occurred_at триггера (fallback —
        created_at). Свечи раньше сценарию не принадлежат: в replay активный
        сценарий читается из БД до своей рыночной истории и НЕ должен
        поглощать сломы, диапазоны и касания прошлых эпох (§13: replay не
        меняет прошлое — в т.ч. чужое)."""
        trig = next(
            (e for e in self.db.list_ltf_structure_events(sc.id)
             if e.id == sc.trigger_event_id),
            None,
        )
        return trig.occurred_at if trig is not None else sc.created_at

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
            if candle.close_time < self._scenario_start(sc):
                continue  # свеча раньше триггера сценария — чужая эпоха (§13)
            cur = self.db.get_current_ltf_range(sc.id)
            ver = cur.version if cur is not None else 0
            entries = [
                e for e in self.db.list_ltf_scenario_entries(sc.id, state="fresh")
                if e.range_version == ver
            ]
            # §6.3 (правила v1): касание отслеживается по полному диапазону
            # зоны — строки вне половины PD получают тот же touch-флоу
            entries += [
                e for e in self.db.list_ltf_scenario_entries(
                    sc.id, state="out_of_range")
                if e.range_version == ver
            ]
            tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
            if (obs.evidence or {}).get("reversal_episode_id"):
                self._scan_reversal_visits(
                    obs, sc, candle, now, processing_mode, detection_lag_ms, result,
                )
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
                if (obs.evidence or {}).get("reversal_episode_id") and not zone.is_level:
                    continue
                # §8.5: касание проверяется по полному диапазону зоны;
                # §9 (Этап 5): закрытие за уровнем БЕЗ касания (ценовой разрыв
                # через уровень) — уровень пройден, терминальный исход
                if not touch_bar(zone.lower, zone.upper, candle):
                    if (zone.is_level
                            and (cur is None
                                 or candle.close_time >= cur.available_at)
                            and self._level_close_beyond(zone, candle)):
                        self._mark_level_broken(obs, sc, zone, candle, now,
                                                processing_mode,
                                                detection_lag_ms, result)
                    continue
                # replay/backlog: выбор версии происходит после прохода
                # касаний той же свечи. Свеча, на которой версия стала
                # доступна, и всё более раннее к этой версии не относятся
                # (ТЗ §3: повторный выбор не воскрешает исторические тесты;
                # §13: replay не меняет прошлое).
                if cur is not None and candle.close_time <= cur.available_at:
                    continue
                self._on_entry_touched(obs, sc, entry, zone, candle, tests,
                                       now, processing_mode, detection_lag_ms,
                                       result)
            # §9 (Этап 5): уровни с незавершённой историей тестов (касались
            # раньше, терминального исхода нет) — строгое закрытие за уровнем
            # на этой свече финализирует исход: уровень пройден без возврата
            watched = {
                t.entry_zone_id for t in tests
                if t.state not in ("confirmed", "failed")
            }
            for zid in watched:
                zone = self.db.get_ltf_entry_zone(zid)
                if zone is None or not zone.is_level:
                    continue
                if self._level_close_beyond(zone, candle):
                    self._mark_level_broken(obs, sc, zone, candle, now,
                                            processing_mode, detection_lag_ms,
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
        # повторному выбору считается отдельно по max_test_depth.
        # Объект обновляется синхронно с БД (F5): оценка ниже идёт по
        # состоянию ПОСЛЕ перехода, иначе первый глубокий тест (>= порога)
        # сохранял бы reason=ok вместо tested_too_deep
        first_test_at = zone.first_test_at or candle.close_time
        self.db.update_ltf_entry_zone(
            zone.id,
            first_test_at=first_test_at,
            validity="tested",
        )
        zone.first_test_at = first_test_at
        zone.validity = "tested"
        self._accumulate_test_depth(zone, candle)
        # reason пересчитывается ПОСЛЕ накопления глубины: глубокий тест
        # (>= 90%) сразу закрывает допуск (tested_too_deep), мелкий —
        # оставляет зону допустимой к перевыбору (ok), ТЗ §13: тест
        # обработан до обновления пригодности следующего входа
        cur = self.db.get_current_ltf_range(sc.id)
        rng_draft = self._row_to_draft(cur, sc.direction) if cur else None
        ev = self._evaluate_zone(
            obs, sc, zone, rng_draft,
            self.db.list_ltf_movements(sc.id), tests,
        )
        self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
            id=None, scenario_id=sc.id, entry_zone_id=zone.id,
            range_version=entry.range_version, eligible=ev.eligible,
            overlap=ev.overlap, state="tested", reason=ev.reason,
            added_at=entry.added_at, updated_at=now,
        ))
        result.touches.append(zone.id)
        # §6.3 (правила v1): PD — не фильтр; положение в половине диапазона
        # (half) — описательная характеристика, допуска оно не ограничивает
        payload: dict[str, Any] = {
            "entry_zone_id": zone.id, "scenario_id": sc.id, "type": zone.type,
            "lower": zone.lower, "upper": zone.upper, "mid": zone.mid,
            "candle_open_time": candle.open_time,
            "half": zone_half(zone.lower, zone.upper, zone.is_level,
                              cur, sc.direction.value),
        }
        # §11.5: ключ касания — entry_zone_id + сценарий, БЕЗ range_version
        self._emit(obs.id, sc.id, "touch", payload,
                   candle.close_time, now, f"touch:{zone.id}:{sc.id}",
                   processing_mode, detection_lag_ms, result)
        # §7/§9: FVG впервые заполнился в текущем активирующем визите —
        # основание сохранено, наблюдение продолжается (R19)
        if (zone.type == "FVG" and not zone.is_level
                and reaches_depth(zone, zone.test_extreme, 1.0)):
            from ...services.htf_context import emit_context_transition
            emit_context_transition(
                self.db, self.cfg, obs.instrument_id,
                fact="fvg_filled_current",
                text="Текущий FVG полностью заполнен; наблюдение реакции H1 продолжается",
                occurred_at=candle.close_time,
                dedupe_key=f"ctxtr:{sc.id}:fvgfilled:{zone.id}",
                observation_id=obs.id, scenario_id=sc.id, detected_at=now,
                processing_mode=processing_mode,
                detection_lag_ms=detection_lag_ms,
            )

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
        # §9 (Этап 5): confirmed — снятие с возвратом; failed — строгое
        # закрытие за уровнем (уровень пройден без возврата). Оба исхода
        # терминальны: уровень сразу теряет пригодность на текущей версии,
        # не дожидаясь пересчёта диапазона, и не воскресает (п.17)
        terminal_reason = {
            "confirmed": "swept_level",
            "failed": REASON_LEVEL_BROKEN,
        }.get(outcome)
        if terminal_reason is not None:
            cur = self.db.get_current_ltf_range(sc.id)
            ver = cur.version if cur is not None else 0
            for e in self.db.list_ltf_scenario_entries(sc.id):
                if e.entry_zone_id != zone.id or e.range_version != ver:
                    continue
                self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                    id=None, scenario_id=sc.id, entry_zone_id=zone.id,
                    range_version=e.range_version, eligible=e.eligible,
                    overlap=e.overlap, state="tested", reason=terminal_reason,
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
    # (2b) §9 (Этап 5): уровень, пройденный закрытием без возврата
    # ------------------------------------------------------------------ #

    @staticmethod
    def _level_close_beyond(zone: LtfEntryZone, candle) -> bool:
        """Строгое закрытие за уровнем: BSL — Close > K, SSL — Close < K."""
        if not candle.closed:
            return False
        if zone.type == "BSL":
            return candle.close > zone.lower
        return candle.close < zone.lower

    def _level_passed_candle(
        self, zone_type: str, level: float, formed_at: int, up_to: list
    ):
        """§9 (Этап 5): первая закрытая свеча ПОСЛЕ рождения экстремума,
        закрепившаяся за уровнем без возврата (BSL: Close > K; SSL: Close < K).
        Рождение экстремума и свечи до него — не ретест (приёмка п.11)."""
        for c in up_to:
            if not c.closed or c.open_time <= formed_at:
                continue
            if zone_type == "BSL" and c.close > level:
                return c
            if zone_type == "SSL" and c.close < level:
                return c
        return None

    def _fvg_fill_scan(
        self, zone: LtfEntryZone, up_to: list
    ) -> Optional[tuple[int, float]]:
        """§10 (Этап 6): первое полное перекрытие FVG по истории закрытых
        свечей (формирующая тройка тестом не является, §9/п.11). Возвращает
        (first_test_at, extreme) или None. Вызывается на границах версий
        диапазона: перекрытие, случившееся пока зона была вне выбора,
        не остаётся незамеченным."""
        after = zone.formed_at
        fvg_candles = zone.evidence.get("fvg_candles")
        if fvg_candles:
            after = fvg_candles[-1]
        first: Optional[int] = None
        extreme: Optional[float] = None
        for c in up_to:
            if not c.closed or c.open_time <= after:
                continue
            if not touch_bar(zone.lower, zone.upper, c):
                continue
            if first is None:
                first = c.open_time
            cur = c.low if zone.direction == Direction.BULL else c.high
            extreme = merge_test_extreme(zone.direction, extreme, cur)
            if reaches_depth(zone, extreme, 1.0):
                return first, extreme
        return None

    def _mark_level_broken(
        self, obs, sc, zone: LtfEntryZone, candle, now: int,
        processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> Optional[LtfLiquidityTest]:
        """Терминальный исход «уровень пройден без возврата» (broken_without
        _reclaim): строка теста state='failed', зона tested, привязки текущей
        версии — reason level_broken, событие sweep_failed. Идемпотентно:
        терминальный тест (confirmed/failed) по зоне уже есть — no-op."""
        tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
        if any(
            t.entry_zone_id == zone.id and t.state in ("confirmed", "failed")
            for t in tests
        ):
            return None
        # зона должна быть подтверждена к моменту свечи (§13: replay не
        # меняет прошлое) — более раннее «прохождение» ловится при создании
        if zone.confirmed_at and zone.confirmed_at > candle.close_time:
            return None
        t = LtfLiquidityTest(
            id=None, entry_zone_id=zone.id, scenario_id=sc.id,
            level=zone.lower, touch_at=candle.close_time,
            candle_open_time=candle.open_time, state="failed",
            close_price=candle.close, resolved_at=candle.close_time,
        )
        t.id = self.db.insert_ltf_liquidity_test(t)
        # пройденный уровень — не «свежая нетронутая ликвидность» (§9)
        self.db.update_ltf_entry_zone(
            zone.id,
            first_test_at=zone.first_test_at or candle.close_time,
            validity="tested",
        )
        zone.first_test_at = zone.first_test_at or candle.close_time
        zone.validity = "tested"
        cur = self.db.get_current_ltf_range(sc.id)
        ver = cur.version if cur is not None else 0
        for e in self.db.list_ltf_scenario_entries(sc.id):
            if e.entry_zone_id != zone.id or e.range_version != ver:
                continue
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=zone.id,
                range_version=e.range_version, eligible=e.eligible,
                overlap=e.overlap, state="tested", reason=REASON_LEVEL_BROKEN,
                added_at=e.added_at, updated_at=now,
            ))
        result.sweeps.append(t.id)
        self._emit(obs.id, sc.id, "sweep_failed", {
            "liquidity_test_id": t.id, "entry_zone_id": zone.id,
            "level": zone.lower, "close_price": candle.close,
            "outcome": "failed",
            "candle_open_time": candle.open_time,
        }, candle.close_time, now, f"sweep:{zone.id}:{candle.open_time}",
            processing_mode, detection_lag_ms, result)
        return t

    # ------------------------------------------------------------------ #
    # (3) Сценарии: открытие, события, отмена (§6)
    # ------------------------------------------------------------------ #

    def _latest_zone_interaction(self, zone_id: Optional[int]) -> Optional[int]:
        """id htf_context_source последнего контакта зоны (связь сценария с
        причинным HTF-контактом, §3/§6.2)."""
        if zone_id is None:
            return None
        row = self.db.conn.execute(
            """SELECT id FROM htf_context_source
               WHERE zone_id=? ORDER BY interaction_at DESC, id DESC LIMIT 1""",
            (zone_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    @staticmethod
    def _observation_scan_since(obs, up_to: list) -> int:
        """Начало скана сломов наблюдения. Контакт с доказанным временем
        (order_proven — реальная котировка, §3/Q18, R34) допускает слом на
        той же H1: свеча контакта включается в скан целиком. Без доказанного
        внутрисвечного порядка — прежняя отсечка activated_at."""
        since = obs.activated_at
        if (obs.evidence or {}).get("order_proven"):
            for c in up_to:
                if c.open_time <= since <= c.close_time:
                    return c.open_time
        return since

    def _maybe_open_scenario(
        self, obs, avail: list[PivotCandidate], up_to: list, candle,
        now: int, processing_mode: str, detection_lag_ms: int,
        result: LtfTickResult,
    ) -> None:
        """Первичный BOS/SMS в направлении HTF-родителя открывает сценарий
        (SMS самостоятелен, §6.3). Невалидный родитель новых сценариев не
        порождает (ТЗ 06.10.2026 §10 T21); уже активный сценарий этот путь
        не трогает (R33)."""
        zone = self.db.get_zone(obs.zone_id)
        if zone is not None and zone.market_validity != "active":
            return
        # stop_on_cancellation=False: обратный слом отменяет АКТИВНЫЙ сценарий
        # (§6.5); ждущему наблюдению отменять нечего, scan.cancellation здесь
        # не используется, а обрезка скана навсегда блокировала бы открытие
        # (и переоткрытие после отмены) при любом встречном сломе в окне
        since = self._observation_scan_since(obs, up_to)
        if self._batch is not None:
            # пакетная обработка (replay/backlog): курсорный скан — та же
            # траектория машин, что у полного перескана на эту свечу
            cur = self._batch.cursor(
                ("open", obs.id), obs.direction, since,
                stop_on_cancellation=False, cancel_not_before_ms=None,
            )
            scan = cur.scan(self._batch.avail, self._batch.closed,
                            self._batch.open_times, now)
        else:
            scan = detect_breaks(avail, up_to, obs.direction, now,
                                 since_ms=since,
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
        # §6.1 (правила v1): BOS и SMS равноправны как триггер. На одной
        # свече — детерминированный выбор по доступности уровня (раньше
        # подтверждённый pivot, затем level_key); тип в выборе не участвует
        primaries.sort(key=lambda e: (
            e.evidence.get("pivot_confirmed_at") or 0, e.level_key, e.kind,
        ))
        main = primaries[0]
        sc = self.db.insert_ltf_scenario(LtfScenario(
            id=None, observation_id=obs.id, direction=obs.direction,
            trigger=main.kind, stage="primary", state="range_pending",
            structural_epoch_id=self._structural_epoch(obs),
            last_processed_close=candle.close_time,
            created_at=now, updated_at=now,
        ))
        self.db.update_ltf_observation(obs.id, state="active", updated_at=now)
        result.scenarios_created.append(sc.id)
        se_ids: dict[str, int] = {}
        for e in primaries:
            se_ids[e.level_key] = self._insert_structure_event(sc.id, e, now).id
        # §6.2: якорь рабочего PD — триггерное причинное событие (BOS|SMS),
        # связь с HTF-контактом; первая ревизия PD
        self.db.update_ltf_scenario(
            sc.id, trigger_event_id=se_ids[main.level_key],
            origin_break_event_id=se_ids[main.level_key],
            pd_anchor_structure_key=main.level_key,
            pd_anchor_kind=main.kind,
            htf_interaction_id=self._latest_zone_interaction(obs.zone_id),
            updated_at=now,
        )
        sc.trigger_event_id = se_ids[main.level_key]
        self.db.insert_pd_revision(
            sc.id, main.level_key, main.kind, [], candle.close_time,
            created_at=now,
        )
        sc.origin_break_event_id = se_ids[main.level_key]
        # §11.2/п.18: диапазон и зоны готовы на закрытии слома — одно
        # объединённое событие, иначе range_pending и позже дополнение
        self._update_range(sc, up_to, now, result, avail=avail, obs=obs,
                           processing_mode=processing_mode,
                           detection_lag_ms=detection_lag_ms)
        self._build_entries(obs, sc, main, se_ids[main.level_key], avail,
                            up_to, now, result,
                            processing_mode=processing_mode,
                            detection_lag_ms=detection_lag_ms)
        # §7 (Этап 4): второй проход — причинное движение появляется только
        # в _build_entries, поэтому диапазон origin_reversal от его якоря
        # строится здесь, на том же закрытии слома; при валидной
        # continuation-паре первый проход уже создал версию, а этот — no-op
        self._update_range(sc, up_to, now, result, avail=avail, obs=obs,
                           processing_mode=processing_mode,
                           detection_lag_ms=detection_lag_ms)
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
        if candle.close_time < self._scenario_start(sc):
            # replay: свеча раньше триггера — сломы/диапазоны/касания прошлых
            # эпох этому сценарию не принадлежат (§13: replay не меняет прошлое)
            return
        cancel = sync.cancellations.get(sc.id)
        fresh = [
            e for e in sync.events.get(sc.id, [])
            if e.occurred_at == candle.close_time
        ]
        if cancel is not None:
            # §13.4/п.19: журнал хранит оба факта (события и обратный слом)
            for e in fresh:
                self._insert_structure_event(sc.id, e, now)
            rev = cancel.event
            self._insert_structure_event(sc.id, rev, now)
            if not self._qualified_reverse(obs, sc, rev):
                # §6.1/R31: обратный слом без контекстного основания — НЕ
                # отмена; локальное структурное изменение / возможная
                # ликвидность (пометка, не cancel)
                self._emit(
                    obs.id, sc.id, "context_update", {
                        "fact": "local_structure_change", "scenario_id": sc.id,
                        "kind": rev.kind, "break_level": rev.break_level,
                        "break_candle_open_time": rev.break_candle_open_time,
                        "note": "обратный слом без контекстного основания — сценарий не отменён",
                    }, rev.occurred_at, now,
                    f"context:{sc.id}:localbreak:{rev.level_key}",
                    processing_mode, detection_lag_ms, result,
                )
                # §9: сообщение — коррекция без встречного основания
                from ...services.htf_context import (
                    _dir_ru,
                    emit_context_transition,
                )
                dir_txt = _dir_ru("bull" if sc.direction == Direction.BEAR else "bear")
                ctx_txt = "медвежьего" if sc.direction == Direction.BEAR else "бычьего"
                emit_context_transition(
                    self.db, self.cfg, obs.instrument_id,
                    fact="local_structure_change",
                    text=(
                        f"{rev.kind} H1 {dir_txt} без встречного HTF-основания. "
                        f"Коррекция внутри {ctx_txt} контекста"
                    ),
                    occurred_at=rev.occurred_at,
                    dedupe_key=f"ctxtr:{sc.id}:localbreak:{rev.level_key}",
                    observation_id=obs.id, scenario_id=sc.id, detected_at=now,
                    processing_mode=processing_mode,
                    detection_lag_ms=detection_lag_ms,
                )
            else:
                # §5/§12: уровень отмены — хранимый факт с происхождением
                # (опорный pivot обратной машины из evidence события-слома)
                rev_pivot_id = rev.evidence.get("broken_pivot_id")
                rev_confirmed_at = rev.evidence.get("pivot_confirmed_at")
                self.db.update_ltf_scenario(
                    sc.id, state="cancelled", cancellation_reason=cancel.pattern,
                    cancelled_at=now, updated_at=now,
                    reverse_break_level_price=rev.break_level,
                    reverse_break_pivot_id=rev_pivot_id,
                    reverse_break_confirmed_at=rev_confirmed_at,
                    last_processed_close=candle.close_time,
                )
                self.db.update_ltf_observation(obs.id, state="waiting_structure",
                                               updated_at=now)
                # §7: квалифицированная отмена завершает активацию зоны
                for act in self.db.list_zone_activations(obs.zone_id, obs.cycle_id):
                    if act["ended_at"] is None:
                        self.db.update_zone_activation(
                            int(act["id"]), ended_at=rev.occurred_at,
                            end_reason="qualified_reverse_break",
                            current_activation_valid=False,
                        )
                # §11.5: scenario_id + cancellation_event_id (level_key слома).
                # Журнал остаётся по сценарию. Карточка склеивается ключом перехода.
                cancel_payload = {
                    "scenario_id": sc.id, "reason": cancel.pattern,
                    "break_level": rev.break_level,
                    "break_candle_open_time": rev.break_candle_open_time,
                    "reverse_break_pivot_id": rev_pivot_id,
                    "reverse_break_confirmed_at": rev_confirmed_at,
                }
                cancel_payload.update(self._market_transition_fields(obs, rev))
                self._emit(obs.id, sc.id, "cancellation", cancel_payload,
                           rev.occurred_at, now, f"cancellation:{sc.id}:{rev.level_key}",
                           processing_mode, detection_lag_ms, result)
                result.cancellations.append(sc.id)
                return

        if self._cancel_on_protected_level(
            obs, sc, candle, now, processing_mode, detection_lag_ms, result,
        ):
            return

        self.db.update_ltf_scenario(
            sc.id, last_processed_close=candle.close_time,
        )
        se_ids = [(e, self._insert_structure_event(sc.id, e, now).id)
                  for e in fresh]
        # (4) диапазон: новая подтверждённая опора → версия; старые события
        # сохраняют прежнюю геометрию (§7)
        created_range = self._update_range(sc, up_to, now, result, avail=avail,
                                           obs=obs,
                                           processing_mode=processing_mode,
                                           detection_lag_ms=detection_lag_ms)
        # §6.2/R40: последний причинный BOS/SMS обновляет якорь рабочего PD
        # на равных (тип не участвует в выборе); ревизия PD сохраняется
        if se_ids:
            causal = [e for e, _ in se_ids]
            causal.sort(key=lambda e: (
                e.evidence.get("pivot_confirmed_at") or 0, e.level_key, e.kind,
            ))
            anchor = causal[0]
            rng_now = created_range or self.db.get_current_ltf_range(sc.id)
            anchors = [
                a for a in (
                    (rng_now.anchor_low_pivot_id, rng_now.anchor_high_pivot_id)
                    if rng_now is not None else ()
                )
                if a is not None
            ]
            self.db.update_ltf_scenario(
                sc.id, pd_anchor_structure_key=anchor.level_key,
                pd_anchor_kind=anchor.kind, updated_at=now,
            )
            self.db.insert_pd_revision(
                sc.id, anchor.level_key, anchor.kind, anchors,
                candle.close_time,
                range_version=rng_now.version if rng_now is not None else None,
                created_at=now,
            )
        # Продолжение в том же направлении не открывает новое движение.
        # OB/FVG действующего контекста добираются отдельно, на каждой
        # закрытой H1, в том числе без нового BOS/SMS.
        for e, se_id in se_ids:
            self._emit_structure_event(obs, sc, e, se_id, now,
                                       processing_mode, detection_lag_ms,
                                       result)
        # §18: обновление контекста — до entries_ready, чтобы допуск FVG
        # вне Premium увидел свежие факты этого закрытия
        self._update_scenario_context(obs, sc, avail, up_to, now,
                                      processing_mode, detection_lag_ms,
                                      result)
        self._extend_reversal_zones(
            obs, sc, avail, up_to, candle, now, result,
            processing_mode, detection_lag_ms,
        )
        self._scan_context_zones(obs, sc, avail, up_to, candle, now, result)
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
        head = up_to[-1] if up_to else None
        # F1: момент решения — закрытие обрабатываемой свечи, а не время
        # обнаружения: при catchup HTF-факт будущего (зона/тест, ещё
        # неизвестные на эту H1) не должен подтверждать старое событие
        event_as_of = head.close_time if head is not None and head.closed else now
        ctx = scenario_context(
            self.db, self._scenario_instrument(sc), sc.direction,
            avail, up_to, since, as_of=event_as_of,
            sweep_only_after=self._ctx_sweep_cursor.get(sc.id),
        )
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

    def _structural_epoch(self, obs) -> int:
        """§5/§12: эпоха структуры наблюдения — 1 + число отмен обратным
        сломом. Выводится из строк сценариев: replay даёт ту же эпоху
        (детерминизм §13), хранить счётчик в наблюдении не нужно."""
        return 1 + sum(
            1
            for s in self.db.list_ltf_scenarios(observation_id=obs.id)
            if s.cancellation_reason in ("reverse_bos", "reverse_sms")
        )

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
            kind=row.kind,
        )

    def _origin_start_ref(self, sc) -> Optional[int]:
        """§7 (Этап 4): start-pivot причинного движения первичного слома —
        якорь-источник диапазона origin_reversal (никогда «максимум окна»)."""
        if sc.origin_movement_id is None:
            return None
        mv = self.db.get_ltf_movement(sc.origin_movement_id)
        return mv.start_pivot_id if mv is not None else None

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
        obs=None, processing_mode: str = "unknown", detection_lag_ms: int = 0,
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
        policy = self.cfg.ltf_range_anchor_policy
        origin_ref = self._origin_start_ref(sc)
        # пакетная обработка: draft детерминирован (avail, prev_draft); avail
        # растёт добавлением, поэтому (len(avail), origin_ref, отпечаток prev)
        # — полный ключ (policy — константа конфига; origin_ref меняется
        # между первым и вторым проходом при открытии сценария). Пересмотр
        # ролей инвалидирует range_memo (StructureBatch).
        # Мемо только когда опоры — avail структурного профиля 3/3 (транзитный
        # расчёт при ином профиле меняется каждую свечу и не мемоизируется).
        memo_key = None
        if (
            self._batch is not None and avail is not None
            and (self.cfg.ltf_structure_left, self.cfg.ltf_structure_right)
            == (RANGE_PIVOT_LEFT, RANGE_PIVOT_RIGHT)
        ):
            memo_key = (
                sc.id, len(pivots), origin_ref,
                None if prev_draft is None else (
                    prev_draft.lower, prev_draft.upper,
                    prev_draft.anchor_low_ref, prev_draft.anchor_high_ref,
                    prev_draft.available_at, prev_draft.kind,
                ),
            )
            if memo_key in self._batch.range_memo:
                draft = self._batch.range_memo[memo_key]
            else:
                draft = range_recalc(
                    prev_draft, pivots, sc.direction, now,
                    origin_start_ref=origin_ref, anchor_policy=policy,
                )
                self._batch.range_memo[memo_key] = draft
        else:
            draft = range_recalc(
                prev_draft, pivots, sc.direction, now,
                origin_start_ref=origin_ref, anchor_policy=policy,
            )
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
            kind=draft.kind,
            anchor_policy=policy,
            structural_epoch_id=sc.structural_epoch_id,
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
        if obs is None:
            obs = self.db.get_ltf_observation(sc.observation_id)
        for zid in zone_ids:
            zone = self.db.get_ltf_entry_zone(zid)
            if zone is None:
                continue
            # §9 (Этап 5): уровень, пройденный закрытием до этой версии (в т.ч.
            # за окно подтверждения опор и пока был вне выбора), рождается /
            # перепривязывается уже пройденным — «свежим» он не становится
            if zone.is_level and not any(
                t.entry_zone_id == zid and t.state in ("confirmed", "failed")
                for t in tests
            ):
                breach = self._level_passed_candle(
                    zone.type, zone.lower, zone.formed_at, up_to
                )
                if breach is not None:
                    t = self._mark_level_broken(
                        obs, sc, zone, breach, now,
                        processing_mode, detection_lag_ms, result,
                    )
                    if t is not None:
                        tests.append(t)
            # §10 (Этап 6): FVG, полностью перекрытый пока был вне выбора,
            # на новой версии не возвращается в fresh — терминальный fvg_filled
            if zone.type == "FVG" and not fvg_filled(zone):
                fill = self._fvg_fill_scan(zone, up_to)
                if fill is not None:
                    first_test, extreme = fill
                    self.db.update_ltf_entry_zone(
                        zone.id, validity="tested",
                        first_test_at=zone.first_test_at or first_test,
                        max_test_depth=1.0, test_extreme=extreme,
                    )
                    zone.validity = "tested"
                    zone.first_test_at = zone.first_test_at or first_test
                    zone.max_test_depth = 1.0
                    zone.test_extreme = extreme
            ev = self._evaluate_zone(obs, sc, zone, draft, movements, tests)
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

    def _market_transition_fields(self, obs, rev) -> dict[str, Any]:
        """Один слом — один ключ карточки. Журнал сценария остаётся своим."""
        instrument = self.db.get_instrument(obs.instrument_id)
        if instrument is None:
            return {}
        from ...services.htf_context import anchor_key, remember_break, transition_key
        anchor = anchor_key(
            rev.evidence.get("broken_pivot_id"), rev.evidence.get("role_at_event"),
        )
        direction = rev.direction.value
        key = transition_key(instrument, rev.break_candle_open_time, direction)
        fields = {
            "market_transition_key": key,
            "market_break_key": key + "|" + anchor,
            "anchor_key": anchor,
            "break_direction": direction,
        }
        plan = self._bar_plan or {}
        if plan.get("transition_key") == key and plan.get("wait_reason") and not plan.get("confirm"):
            fields["long_wait_reason"] = plan["wait_reason"]
        remember_break(
            self.db, instrument, rev, anchor, rev.occurred_at,
            {"anchor": anchor, "scenario_observation": obs.id},
        )
        return fields

    def _annotate_reversal_event(self, obs, sc, event, payload: dict[str, Any]) -> None:
        from ...services.htf_context import reversal_projection
        plan = self._bar_plan or {}
        payload["reversal_confirmed"] = True
        payload["break_direction"] = sc.direction.value
        if plan.get("transition_key") and plan.get("direction") == sc.direction.value:
            payload["market_transition_key"] = plan["transition_key"]
        view = reversal_projection(
            self.db, obs.instrument_id, event.occurred_at, self.cfg,
        )
        if view and view.get("pd"):
            pd = view["pd"]
            payload["pd"] = pd
            payload["range"] = {
                "lower": pd["L"], "upper": pd["H"], "mid": pd["EQ"],
                "version": pd["version"], "kind": "reversal_leg",
            }
            payload["range_pending"] = False
        payload["htf_sources"] = (view or {}).get("sources") or []

    def _evaluate_zone(self, obs, sc, zone, draft, movements, tests):
        from ...services.htf_context import reversal_range_for
        rng, status = reversal_range_for(self.db, obs, draft, sc.direction)
        return evaluate_entry(
            zone, sc.direction, self.cfg, rng,
            movements=movements, liquidity_tests=tests, pd_status=status,
            cut=activation_cut_for(self.db, obs, zone, self.cfg),
        )

    def _reversal_start_pivot(self, obs, avail: list[PivotCandidate]):
        evidence = obs.evidence or {}
        ref = evidence.get("protected_pivot_ref")
        opened = evidence.get("protected_candle_open")
        kind = "low" if obs.direction == Direction.BULL else "high"
        for pivot in avail:
            if ref is not None and (pivot.pivot_id == ref or pivot.pivot_at == ref):
                return pivot
        for pivot in avail:
            if pivot.kind == kind and opened is not None and pivot.pivot_at == opened:
                return pivot
        return None

    def _qualified_reverse(self, obs, sc, rev) -> bool:
        """§6.1 (правила v1): квалификация обратного BOS/SMS для отмены.

        Основание — противоположный HTF-источник с доказанным контактом до
        слома ЛИБО опора, сформированная структурой HTF-контекста (pivot,
        подтверждённый после активации; геометрическое нахождение вне
        HTF-зоны не мешает — R32). Слом pivot «на пустом месте» (до
        контакта и без встречного HTF) отменой не является (R31)."""
        pivot_confirmed = rev.evidence.get("pivot_confirmed_at")
        if pivot_confirmed is not None and int(pivot_confirmed) >= int(obs.activated_at):
            return True
        from ...services.htf_context import opposing_contact_before
        return opposing_contact_before(
            self.db, obs.instrument_id, sc.direction, int(rev.occurred_at),
        )

    def _cancel_on_protected_level(
        self, obs, sc, candle, now, processing_mode, detection_lag_ms, result,
    ) -> bool:
        """Закрытие за исходным LL/HH — пометка, не отмена. Фитиль только пишется.

        Правила v1 §6.1 (R31): пробой защищённого LL/HH без контекстного
        основания сам по себе сценарий НЕ отменяет — это локальное
        структурное изменение / возможная ликвидность. Отмена — только
        квалифицированный обратный BOS/SMS (_qualified_reverse)."""
        if not (obs.evidence or {}).get("reversal_episode_id"):
            return False
        from ...services.htf_context import load_episode
        episode = load_episode(self.db, int(obs.evidence["reversal_episode_id"]))
        if episode is None or episode.get("protected_price") is None:
            return False
        price = float(episode["protected_price"])
        kind = episode.get("protected_kind")
        if sc.direction == Direction.BULL and kind == "ll":
            breached = candle.close < price
            wicked = candle.low < price and not breached
        elif sc.direction == Direction.BEAR and kind == "hh":
            breached = candle.close > price
            wicked = candle.high > price and not breached
        else:
            return False
        if wicked:
            self._emit(
                obs.id, sc.id, "context_update",
                {"kind": "protected_wick", "price": price, "candle_open_time": candle.open_time},
                candle.close_time, now,
                f"wick-protected:{sc.id}:{candle.open_time}",
                processing_mode, detection_lag_ms, result,
            )
            return False
        if not breached:
            return False
        self._emit(
            obs.id, sc.id, "context_update", {
                "fact": "local_structure_change", "scenario_id": sc.id,
                "protected_kind": kind, "price": price,
                "candle_open_time": candle.open_time,
                "note": "пробой защищённого уровня без контекстного основания — сценарий не отменён (R31)",
            },
            candle.close_time, now, f"context:{sc.id}:protected_broken",
            processing_mode, detection_lag_ms, result,
        )
        return False

    def _announced_key(self, scenario_id: int) -> str:
        return f"ltf:announced_entries:{scenario_id}"

    def _mark_announced(self, scenario_id: int, zone_ids: list[int]) -> None:
        """Зона уже известна контексту. Повторно как текущий сигнал не уходит.

        Журнал и карточка BOS/SMS не переписываются: id только скрывает зону
        от следующего entries_ready. Доставка исторического факта идёт
        прежним путём delayed/replay, отдельного события здесь нет.
        """
        if not zone_ids:
            return
        current = self._announced_entry_ids(scenario_id)
        current.update(int(z) for z in zone_ids)
        self.db.set_meta(
            self._announced_key(scenario_id),
            json.dumps(sorted(current)),
        )

    def _origin_movement_draft(self, sc) -> Optional[tuple[LtfMovement, MovementDraft]]:
        """Движение триггерного слома. Конец ноги остаётся свечой BOS/SMS:
        поздние зоны ссылаются на него, но не сдвигают его."""
        movements = self.db.list_ltf_movements(sc.id)
        if not movements:
            return None
        row = next(
            (m for m in movements if m.id == sc.origin_movement_id),
            movements[0],
        )
        lookback = (self.cfg.uncalibrated_consolidation_max_candles + 5) * H1_MS
        draft = MovementDraft(
            scenario_id=sc.id,
            direction=sc.direction,
            start_pivot_ref=row.start_pivot_id,
            end_pivot_ref=row.end_pivot_id,
            start_at=row.start_at,
            end_at=row.end_at,
            window_start=row.start_at - lookback,
            break_event_key=str(row.break_event_id or ""),
            source_candle_ids=list(row.source_candle_ids or []),
            provenance_status=row.provenance_status,
        )
        return row, draft

    def _scan_context_zones(
        self, obs, sc, avail, up_to, candle, now, result: LtfTickResult,
        *, recover: bool = False,
    ) -> None:
        """OB/FVG действующего контекста по свечам, уже закрытым к ``candle``.

        Отменённый и закрытый сценарий новых зон не получает. Повтор той же
        свечи не создаёт второй прямоугольник и не меняет первое
        подтверждение. Зона, подтверждённая раньше этой свечи, не становится
        текущим сигналом.
        """
        if sc.state in ("cancelled", "closed"):
            return
        built = self._origin_movement_draft(sc)
        if built is None:
            return
        row, movement = built
        det = detect_entry_zones(up_to, movement, avail, sc.direction, self.cfg)
        linked: dict[tuple, int] = {}
        for zid in self._scenario_zone_ids(sc.id):
            z0 = self.db.get_ltf_entry_zone(zid)
            if z0 is not None:
                linked[(z0.type, z0.direction, z0.lower, z0.upper, z0.formed_at)] = z0.id
        market: dict[tuple, LtfEntryZone] = {}
        for z0 in self.db.list_ltf_entry_zones(instrument_id=obs.instrument_id):
            market[(z0.type, z0.direction, z0.lower, z0.upper, z0.formed_at)] = z0
        rng = self.db.get_current_ltf_range(sc.id)
        rng_draft = self._row_to_draft(rng, sc.direction) if rng else None
        ver = rng.version if rng else 0
        movements = self.db.list_ltf_movements(sc.id)
        tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
        historical: list[int] = []
        for z in det.zones:
            if z.type not in ("OB", "FVG"):
                continue
            # Подтверждение — граница закрытия третьей свечи (close_time + 1).
            # Будущая тройка в префикс этой свечи не входит.
            if z.confirmed_at > candle.close_time + 1:
                continue
            key = (z.type, z.direction, z.lower, z.upper, z.formed_at)
            if key in linked:
                continue
            ez = market.get(key)
            if ez is None:
                ez = self.db.insert_ltf_entry_zone(LtfEntryZone(
                    id=None, instrument_id=obs.instrument_id, type=z.type,
                    direction=z.direction, lower=z.lower, upper=z.upper,
                    formed_at=z.formed_at, confirmed_at=z.confirmed_at,
                    movement_id=row.id, first_test_at=z.first_test_at,
                    validity=z.validity, max_test_depth=z.max_test_depth,
                    test_extreme=z.test_extreme, evidence=z.evidence,
                ))
                market[key] = ez
            linked[key] = ez.id
            result.entry_zones_processed.append(ez.id)
            # Один прямоугольник на инструмент. movement_id строки — нога
            # сценария, который записал её первым. Для допуска этого
            # сценария происхождение уже проверено детектором по его ноге;
            # чужой id не делает свою зону «без происхождения». Строку не
            # переписываем.
            known_movements = {m.id for m in movements}
            judged = ez
            if ez.movement_id and ez.movement_id not in known_movements:
                judged = replace(ez, movement_id=row.id)
            ev = self._evaluate_zone(obs, sc, judged, rng_draft, movements, tests)
            # Связь доступна с фактического подтверждения, не со времени догона.
            added_at = z.confirmed_at if (
                recover or z.confirmed_at <= candle.close_time
            ) else candle.close_time
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=ez.id,
                range_version=ver, eligible=ev.eligible, overlap=ev.overlap,
                state=ev.state, reason=ev.reason,
                added_at=added_at, updated_at=added_at,
            ))
            if recover or z.confirmed_at <= candle.close_time:
                historical.append(ez.id)
        if historical:
            self._mark_announced(sc.id, historical)

    def rescan_active_context_zones(
        self, instrument_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Добрать пропущенные OB/FVG действующих сценариев по сохранённым H1.

        Повтор идемпотентен: чужие и уже записанные объекты не дублируются,
        первое подтверждение не сдвигается, ручные поля существующих строк
        не переписываются. Новые строки не создают текущих уведомлений.
        """
        added: list[dict[str, Any]] = []
        scenarios = 0
        for obs in self.db.list_ltf_observations(instrument_id=instrument_id):
            if obs.state not in ("waiting_structure", "active"):
                continue
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is None or sc.state in ("cancelled", "closed"):
                continue
            candles = [
                c for c in self.db.get_candles(obs.instrument_id, "H1")
                if c.closed
            ]
            if not candles:
                continue
            scenarios += 1
            before = set(self._scenario_zone_ids(sc.id))
            candle = candles[-1]
            self._scan_context_zones(
                obs, sc, [], candles, candle, candle.close_time,
                LtfTickResult(), recover=True,
            )
            for zid in self._scenario_zone_ids(sc.id) - before:
                zone = self.db.get_ltf_entry_zone(zid)
                if zone is None or zone.type not in ("OB", "FVG"):
                    continue
                added.append({
                    "id": zone.id,
                    "scenario_id": sc.id,
                    "type": zone.type,
                    "direction": zone.direction.value,
                    "lower": zone.lower,
                    "upper": zone.upper,
                    "formed_at": zone.formed_at,
                    "confirmed_at": zone.confirmed_at,
                    "validity": zone.validity,
                    "relation": (zone.evidence or {}).get("relation"),
                    "break_at": (zone.evidence or {}).get("break_at"),
                    "base_at": (zone.evidence or {}).get("base_at"),
                })
        return {"scenarios": scenarios, "zones_added": len(added), "added": added}

    def _recover_context_zones_once(self, instrument_id: int) -> None:
        key = f"ltf:context_zones_rescan:v1:{instrument_id}"
        if self.db.get_meta(key) == "1":
            return
        if self.db.last_candle(instrument_id, "H1") is None:
            return
        stats = self.rescan_active_context_zones(instrument_id)
        self.db.set_meta(key, "1")
        if stats["zones_added"]:
            log.info(
                "LTF %s: восстановлены OB/FVG действующего контекста: %s",
                instrument_id, stats["zones_added"],
            )

    def _extend_reversal_zones(
        self, obs, sc, avail, up_to, candle, now, result,
        processing_mode, detection_lag_ms,
    ) -> None:
        """После BOS нога ещё растёт до подтверждения верхней опоры."""
        if sc.state in ("cancelled", "closed"):
            return
        if not (obs.evidence or {}).get("reversal_episode_id"):
            return
        from ...services.htf_context import latest_leg, refresh_reversal_leg
        refresh_reversal_leg(self.db, self.cfg, obs, avail, up_to, candle)
        leg = latest_leg(self.db, int(obs.evidence["reversal_episode_id"]))
        if leg is not None and int(leg["frozen"]):
            return
        start = self._reversal_start_pivot(obs, avail)
        if start is None:
            return
        lookback = (self.cfg.uncalibrated_consolidation_max_candles + 5) * H1_MS
        fake = StructureEventDraft(
            kind="BOS", stage="primary", direction=sc.direction,
            break_level=candle.close, break_candle_open_time=candle.open_time,
            occurred_at=candle.close_time, detected_at=now, level_key="reversal-extend",
        )
        mv = build_movement(
            sc.id, avail, up_to, fake, sc.direction, lookback, start_pivot=start,
        )
        if mv is None:
            return
        det = detect_entry_zones(up_to, mv, avail, sc.direction, self.cfg)
        movements = self.db.list_ltf_movements(sc.id)
        movement_id = movements[-1].id if movements else 0
        rng = self.db.get_current_ltf_range(sc.id)
        ver = rng.version if rng else 0
        tests = self.db.list_ltf_liquidity_tests(scenario_id=sc.id)
        existing = {}
        for zid in self._scenario_zone_ids(sc.id):
            z0 = self.db.get_ltf_entry_zone(zid)
            if z0 is not None:
                existing[(z0.type, z0.lower, z0.upper)] = zid
        for z in det.zones:
            if (z.type, z.lower, z.upper) in existing:
                continue
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
            ev = self._evaluate_zone(obs, sc, ez, None, movements, tests)
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=ez.id,
                range_version=ver, eligible=ev.eligible, overlap=ev.overlap,
                state=ev.state, reason=ev.reason, added_at=now, updated_at=now,
            ))

    def _scan_reversal_visits(
        self, obs, sc, candle, now, processing_mode, detection_lag_ms, result,
    ) -> None:
        """Один алерт на непрерывный визит в допустимый участок discount."""
        import json
        from ...services.htf_context import (
            _premium_segment, entry_segment, latest_leg, tick_size,
        )
        leg = latest_leg(self.db, int(obs.evidence["reversal_episode_id"]))
        if leg is None or sc.direction not in (Direction.BULL, Direction.BEAR):
            return
        instrument = self.db.get_instrument(obs.instrument_id)
        tick = tick_size(instrument) if instrument is not None else 0
        cur = self.db.get_current_ltf_range(sc.id)
        ver = cur.version if cur is not None else 0
        for entry in self.db.list_ltf_scenario_entries(sc.id):
            if entry.range_version != ver:
                continue
            if entry.reason not in ("ok", "eligible_provisional"):
                continue
            zone = self.db.get_ltf_entry_zone(entry.entry_zone_id)
            if zone is None or zone.is_level:
                continue
            if zone.confirmed_at and zone.confirmed_at > candle.close_time:
                continue
            if sc.direction == Direction.BULL:
                segment = entry_segment(zone.lower, zone.upper, leg["low"], leg["eq"], tick=tick)
            else:
                segment = _premium_segment(zone.lower, zone.upper, leg["eq"], leg["high"], tick)
            if segment is None:
                continue
            lo, hi = segment
            key = f"htf_ctx:visit:{sc.id}:{zone.id}"
            state = json.loads(self.db.get_meta(key) or '{"inside": false, "n": 0}')
            outside = candle.high < lo or candle.low > hi
            hits = candle.high >= lo and candle.low <= hi
            if outside or not hits:
                if state.get("inside"):
                    state["inside"] = False
                    self.db.set_meta(key, json.dumps(state))
                continue
            if state.get("inside"):
                continue
            from .entries import entry_reusable
            if zone.validity == "tested" and not entry_reusable(zone, self.cfg):
                state["inside"] = True
                self.db.set_meta(key, json.dumps(state))
                continue
            n = int(state.get("n") or 0) + 1
            state["inside"] = True
            state["n"] = n
            self.db.set_meta(key, json.dumps(state))
            self.db.update_ltf_entry_zone(
                zone.id, first_test_at=zone.first_test_at or candle.close_time,
                validity="tested",
            )
            self._accumulate_test_depth(zone, candle)
            self._emit(
                obs.id, sc.id, "touch",
                {"entry_zone_id": zone.id, "scenario_id": sc.id, "type": zone.type,
                 "lower": lo, "upper": hi, "zone_lower": zone.lower, "zone_upper": zone.upper,
                 "candle_open_time": candle.open_time, "visit": n,
                 "admissible": True},
                candle.close_time, now, f"touch:{zone.id}:{sc.id}:visit:{n}",
                processing_mode, detection_lag_ms, result,
            )
            result.touches.append(zone.id)

    # ------------------------------------------------------------------ #
    # (5) Entry Zones причинного движения (§8, §9)
    # ------------------------------------------------------------------ #

    def _build_entries(
        self, obs, sc, main: StructureEventDraft, se_id: int,
        avail: list[PivotCandidate], up_to: list, now: int,
        result: LtfTickResult,
        processing_mode: str = "unknown", detection_lag_ms: int = 0,
    ) -> None:
        # ТЗ §8/п.04: отменённый/закрытый сценарий не получает новых
        # движений и Entry Zones
        if sc.state in ("cancelled", "closed"):
            return
        lookback = (self.cfg.uncalibrated_consolidation_max_candles + 5) * H1_MS
        start_pivot = None
        if (obs.evidence or {}).get("reversal_episode_id"):
            start_pivot = self._reversal_start_pivot(obs, avail)
        mv = build_movement(
            sc.id, avail, up_to, main, sc.direction, lookback, start_pivot=start_pivot,
        )
        if mv is None:
            return
        movement_id = self.db.insert_ltf_movement(LtfMovement(
            id=None, scenario_id=sc.id, start_pivot_id=mv.start_pivot_ref,
            end_pivot_id=mv.end_pivot_ref, start_at=mv.start_at,
            end_at=mv.end_at, break_event_id=se_id, confirmed_at=now,
            source_candle_ids=mv.source_candle_ids,
            provenance_status=mv.provenance_status,
        ))
        # §5/§12: движение триггерного слома — исходное движение сценария
        if sc.origin_movement_id is None and se_id == sc.origin_break_event_id:
            self.db.update_ltf_scenario(sc.id, origin_movement_id=movement_id)
            sc.origin_movement_id = movement_id
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
            # §9 (Этап 5): уровень, пройденный закрытием после рождения
            # экстремума (в т.ч. за окно из 3 правых свечей подтверждения),
            # рождается пройденным — «свежей нетронутой ликвидностью» не
            # становится никогда
            if ez.is_level:
                breach = self._level_passed_candle(
                    ez.type, ez.lower, ez.formed_at, up_to
                )
                if breach is not None:
                    t = self._mark_level_broken(
                        obs, sc, ez, breach, now,
                        processing_mode, detection_lag_ms, result,
                    )
                    if t is not None:
                        tests.append(t)
            # ТЗ §10: пригодность — конъюнкция evaluate_entry (тип, sweep,
            # движение, глубина тестов, половина диапазона); протестированная
            # зона с глубиной СТРОГО < 90% допускается к повторному выбору
            ev = self._evaluate_zone(obs, sc, ez, rng_draft, movements, tests)
            self.db.upsert_ltf_scenario_entry(LtfScenarioEntry(
                id=None, scenario_id=sc.id, entry_zone_id=ez.id,
                range_version=ver, eligible=ev.eligible, overlap=ev.overlap,
                state=ev.state, reason=ev.reason, added_at=now, updated_at=now,
            ))

        self.db.set_meta(f"htf_idea:discovered:{sc.id}", str(main.occurred_at))

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
                "version": rng.version, "kind": rng.kind}

    def _entry_payload(
        self, sc, entry: LtfScenarioEntry, zone: LtfEntryZone, rng,
    ) -> dict[str, Any]:
        return {
            "entry_zone_id": zone.id, "type": zone.type, "lower": zone.lower,
            "upper": zone.upper, "mid": zone.mid, "overlap": entry.overlap,
            "confirmed_at": zone.confirmed_at,
            # §6.3: половина диапазона зоны — описательная характеристика
            "half": zone_half(zone.lower, zone.upper, zone.is_level,
                              rng, sc.direction.value),
        }

    def _entry_candidates(
        self, sc
    ) -> list[tuple[LtfScenarioEntry, LtfEntryZone]]:
        """Зоны, допущенные к выбору на текущей версии диапазона (ver=0 до
        пары): свежие и повторно выбранные протестированные с глубиной
        тестов < 90% (ТЗ §3 — переклассификация в _update_range); §6.3:
        положение в PD допуск не ограничивает.
        Отбор делегирован admitted_scenario_entries — единому источнику
        правила допуска (L01)."""
        return [
            (e, zone)
            for e, zone, _fe in admitted_scenario_entries(
                self.db, sc.id, cfg=self.cfg
            )
        ]

    def _announced_entry_ids(self, scenario_id: int) -> set[int]:
        """Уже сообщённые зоны (§11.5): id из payload прошлых bos/sms/
        entries_ready того же сценария и зоны, восстановленные без нового
        сигнала."""
        announced: set[int] = set()
        for ev in self.db.list_ltf_events(scenario_id=scenario_id, limit=1000):
            if ev.kind not in ("bos", "sms", "entries_ready"):
                continue
            for item in ev.payload.get("entries", []):
                announced.add(item["entry_zone_id"])
        raw = self.db.get_meta(self._announced_key(scenario_id))
        if raw:
            try:
                announced.update(int(z) for z in json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
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
        reversal = bool((obs.evidence or {}).get("reversal_episode_id"))
        fresh = self._entry_candidates(sc) if (rng is not None or reversal) else []
        payload: dict[str, Any] = {
            "scenario_id": sc.id, "structure_event_id": se_id,
            "direction": sc.direction.value, "kind": e.kind, "stage": e.stage,
            "break_level": e.break_level,
            "break_candle_open_time": e.break_candle_open_time,
            # цена закрытия пробойной H1 — из evidence слома (§5.4 ТЗ 07.10.2026)
            "close": e.evidence.get("close"),
            "range": self._range_payload(rng) if rng else None,
            "range_pending": rng is None,
            "movement": self._movement_payload(sc.id, se_id),
            # при range_pending зоны копятся кандидатами и не анонсируются —
            # о готовых сообщит entries_ready (§11.2)
            "entries": [
                self._entry_payload(sc, en, z, rng)
                for en, z in fresh
            ],
        }
        if reversal:
            self._annotate_reversal_event(obs, sc, e, payload)
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
        fresh = [(en, z) for en, z in self._entry_candidates(sc)
                 if z.id not in announced]
        if fresh:
            ids = sorted(z.id for _, z in fresh)
            self._emit(obs.id, sc.id, "entries_ready", {
                "scenario_id": sc.id, "range": self._range_payload(rng),
                "entries": [
                    self._entry_payload(sc, en, z, rng)
                    for en, z in fresh
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
        """Инвалидация HTF-родителя — фиксация факта, НЕ отмена сценария.

        Правила v1 §6.1 (R33): рыночная неактуальность зоны (invalid,
        конверсия, пробой) — отдельное состояние зоны; автоматическая
        отмена LTF-сценария по статусу родителя запрещена. Отмена — только
        квалифицированный обратный BOS/SMS (_qualified_reverse). Факт
        прохода по-прежнему идёт в машину контекста §4 (note_source_outcome)
        рыночным временем (F13). True — родитель инвалидирован (факт
        записан). Тест 90% (WORKED) и запрет Breaker фактом не являются.
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
        ) or (
            zone.market_validity == "invalid"
        )
        if not invalidated:
            return False
        now = _real_now_ms()
        # F13: рыночное время факта инвалидации — из доказательства зоны
        # (или конца рисунка), а не wall-clock обнаружения; режим — по лагу
        # обнаружения, как у остального потока: восстановленная историческая
        # инвалидация не выглядит свежим live-событием
        evidence = zone.evidence or {}
        raw_at = evidence.get("invalidated_at") or zone.display_until
        occurred = int(raw_at) if raw_at is not None else now
        lag = max(0, now - occurred)
        mode = _processing_mode(lag, self.cfg.ltf_live_grace_seconds * 1000)
        sc = self.db.get_active_ltf_scenario(obs.id)
        if sc is not None:
            # информационный факт (не cancellation): зона invalid отдельно
            self._emit(obs.id, sc.id, "context_update", {
                "fact": "htf_invalidated", "scenario_id": sc.id,
                "invalidated_at": occurred,
            }, occurred, now, f"context:{sc.id}:htf_invalidated",
                mode, lag, LtfTickResult())
        # R33: наблюдение и активный сценарий НЕ закрываются по статусу
        # родителя; новые сценарии от невалидной зоны не открываются
        # (блок в _maybe_open_scenario, ТЗ 06.10.2026 §10 T21)
        self.db.update_ltf_observation(obs.id, updated_at=now)
        from ...services.htf_context import note_source_outcome
        note_source_outcome(self.db, zone, occurred, cfg=self.cfg)
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
        from ...services.htf_context import close_episodes_for_scenario
        close_episodes_for_scenario(self.db, scenario_id, now)

    # ------------------------------------------------------------------ #
    # Автоархивация неактивных наблюдений и resync pivots
    # ------------------------------------------------------------------ #

    def archive_stale_observations(
        self, now_ms: Optional[int] = None
    ) -> list[int]:
        """Техническая очистка наблюдений без активности (closed_stale).

        Правила v1 §8 (R24): автоархивация — НЕ срок торгового контекста.
        Наблюдение с АКТИВНЫМ сценарием не архивируется вовсе (прежняя
        отмена сценария причиной 'stale' убрана): контекст и активация
        закрываются только квалифицированным сломом/переходами §4/§5.
        zone_activation и эпизод архивация не трогает; новый рыночный факт
        восстанавливает наблюдение (on_htf_zone_touched → waiting_structure).

        Наблюдения без активности дольше ltf_observation_stale_days →
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
        from ...services.htf_ideas import project_ideas
        idea_cache = {}
        for obs in candidates:
            if obs.instrument_id not in idea_cache:
                idea_cache[obs.instrument_id] = project_ideas(
                    self.db, obs.instrument_id, self.cfg, now,
                )[0]
            if any(i["observation_id"] == obs.id and i["state"] != "closed"
                   for i in idea_cache[obs.instrument_id]):
                continue
            sc = self.db.get_active_ltf_scenario(obs.id)
            if sc is not None:
                # R24: живой сценарий — не техническая архивация; контекст
                # не закрывается таймером
                continue
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
                ev = self._evaluate_zone(obs, sc, zone, rng_draft, movements, tests)
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
