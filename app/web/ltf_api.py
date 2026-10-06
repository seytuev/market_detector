"""HTTP API окна LTF Confirmations (LTF-спека §3, §12, §13).

Маршруты: список наблюдений с фильтрами (§3.2), карточка (§3.3), слои
графика (§3.5), таблица Entry Zones с dist-формулами (§3.4), журнал,
ручное завершение сценария, разметка Entry Zones (ревью). Все за
owner-авторизацией; сериализация — to_dict() моделей плюс агрегация из
репозиториев Database.

ТЗ «LTF Current Setup» §14 — read model «текущая ситуация по активу»:
GET /api/ltf/instruments (одна строка на instrument_id, этап считает
сервер), GET /api/ltf/instruments/{id}/current (InstrumentCurrentView:
котировка, data_state, контексты с реальным is_price_inside_now,
не-отменённый current_scenario, диапазон, подходящие зоны, counts,
state_version), POST select-context (ручной выбор контекста, §7).
Сами вычисления read model вынесены в app/services/overview.py (нужны
Telegram-боту без HTTP); эндпоинты только делегируют.
Старые маршруты наблюдений (история, журнал, разметка) сохранены.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel

from ..db import Database
from ..engine.ltf.context import context_complete
from ..engine.ltf.eligibility import evaluate_final
from ..models import now_ms
from ..models_ltf import (
    LTF_LEGACY_DECISIONS,
    LTF_REVIEW_DECISIONS,
    LtfEntryZone,
    LtfReview,
    LtfReviewAssessment,
    LtfScenario,
)
from ..services.overview import (
    _ACTIVE_STATES,
    _ENTRY_ORDER,
    _context_flags,
    _entry_row,
    _expected_levels,
    _fresh_entries,
    _instrument_brief,
    _last_cancellation,
    _scenario_block,
    _state_version,
    _zone_brief,
    instrument_current,
    instruments_overview,
    observation_chart_layers,
)

# вкладка «history» списка наблюдений (§3.2; «active» — в services/overview)
_HISTORY_STATES = {"closed_by_parent", "closed_by_user", "closed_stale"}


class LtfSelectContextIn(BaseModel):
    """Ручной выбор HTF-контекста инструмента (§7)."""
    observation_id: int


class LtfReviewIn(BaseModel):
    """Решение ревью Entry Zone (аналог ReviewIn HTF, §15.3 HTF-спеки).

    Оценка только фиксируется: validity зоны, границы и привязки
    ltf_scenario_entry не меняются. fix_boundaries требует lower/upper —
    исправленные границы записываются в разметку, зона остаётся как была.
    """
    decision: str
    text: str = ""
    scenario_id: Optional[int] = None  # контекст: из какого сценария оцениваем
    reason_code: Optional[str] = None  # LTF-специфичный код причины
    lower: Optional[float] = None
    upper: Optional[float] = None
    evidence_source: str = "manual_ui"


def export_ltf_label(
    db: Database,
    zone: LtfEntryZone,
    decision: str,
    text: str,
    settings,
    *,
    assessment: LtfReviewAssessment,
    scenario_id: Optional[int] = None,
) -> str:
    """Пишет оценку Entry Zone в ltf_labels.jsonl рядом с БД (append-only).

    Отдельный от HTF-разметки (labels.jsonl) датасет: полный снимок зоны,
    контекст сценария/наблюдения/родительской HTF-зоны, все привязки
    ltf_scenario_entry этой зоны и OHLC исходных свечей. Версия 1.
    """
    ins = db.get_instrument(zone.instrument_id)
    entries = db.list_ltf_scenario_entries_by_zone(zone.id)

    # контекст сценария: явный scenario_id или последняя привязка зоны
    if scenario_id is None and entries:
        scenario_id = entries[-1].scenario_id
    scenario = db.get_ltf_scenario(scenario_id) if scenario_id else None
    observation = (
        db.get_ltf_observation(scenario.observation_id)
        if scenario is not None else None
    )

    # OHLC исходных свечей основания (fvg_candles / base_candles — open_time)
    source_ohlc = []
    source_ids = (
        zone.evidence.get("fvg_candles")
        or zone.evidence.get("base_candles")
        or []
    )
    if source_ids:
        by_open = {
            c.open_time: c
            for c in db.get_candles(zone.instrument_id, "H1")
        }
        for t in source_ids:
            c = by_open.get(t)
            if c:
                source_ohlc.append({
                    "open_time": t, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                })

    now = now_ms()
    record = {
        "purpose": "разметка LTF Entry Zones для ИИ-анализа (dev-режим)",
        "labels_version": 1,
        "exported_at": now,  # время снимка; не подменяет reviewed_at
        "decision": decision,
        "comment": text,
        "author": "owner",
        "instrument": _instrument_brief(db, ins.id) if ins else None,
        "entry_zone": zone.to_dict(),
        # контекст: сценарий оценки, наблюдение и родительская HTF-зона
        "scenario_id": scenario_id,
        "scenario": scenario.to_dict() if scenario is not None else None,
        "observation": observation.to_dict() if observation is not None else None,
        "parent_zone": (
            _zone_brief(db, observation.zone_id)
            if observation is not None else None
        ),
        "scenario_entries": [e.to_dict() for e in entries],
        "source_candles_ohlc": source_ohlc,
        # поля ревью
        "review_id": assessment.review_id,
        "reviewed_at": assessment.reviewed_at,
        "assessed_as_of": assessment.assessed_as_of,
        "review_decision": assessment.review_decision,
        "geometry_verdict": assessment.geometry_verdict,
        "lifecycle_verdict": assessment.lifecycle_verdict,
        "reason_code": assessment.reason_code,
        "requires_clarification": bool(assessment.requires_clarification),
        "corrected_lower": assessment.corrected_lower,
        "corrected_upper": assessment.corrected_upper,
    }
    path = Path(settings.db_path).parent / "ltf_labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


def _active_or_last_scenario(
    db: Database, observation_id: int
) -> Optional[LtfScenario]:
    sc = db.get_active_ltf_scenario(observation_id)
    if sc is not None:
        return sc
    scenarios = db.list_ltf_scenarios(observation_id=observation_id)
    return scenarios[-1] if scenarios else None


def register_ltf_routes(app, db: Database, settings, require_auth, ltf_engine=None) -> None:
    """Регистрирует маршруты окна LTF на существующем приложении."""
    from fastapi import Depends

    # ------------------------- наблюдения (§3.2) -------------------------

    @app.get("/api/ltf/observations", dependencies=[Depends(require_auth)])
    def ltf_observations(
        tab: str = "active",
        instrument_id: Optional[int] = None,
        htf_tf: Optional[str] = None,
        direction: Optional[str] = None,
        trigger: Optional[str] = None,
        entry_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        observations = db.list_ltf_observations(instrument_id=instrument_id)
        out = []
        for obs in observations:
            if tab == "active" and obs.state not in _ACTIVE_STATES:
                continue
            if tab == "history" and obs.state not in _HISTORY_STATES:
                continue
            if direction is not None and obs.direction.value != direction:
                continue
            zone = db.get_zone(obs.zone_id)
            if htf_tf is not None and (zone is None or zone.timeframe != htf_tf):
                continue
            sc = _active_or_last_scenario(db, obs.id)
            if trigger is not None and (sc is None or sc.trigger != trigger):
                continue
            fresh = _fresh_entries(db, sc.id) if sc is not None else []
            if entry_type is not None and not any(
                z.type == entry_type for _, z in fresh
            ):
                continue
            out.append({
                **obs.to_dict(),
                "instrument": _instrument_brief(db, obs.instrument_id),
                "parent_zone": _zone_brief(db, obs.zone_id),
                "scenario": sc.to_dict() if sc is not None else None,
                "fresh_entries": len(fresh),
            })
        out.sort(key=lambda o: o["activated_at"], reverse=True)
        return out

    # ------------------------- карточка (§3.3) -------------------------

    @app.get("/api/ltf/observations/{observation_id}",
             dependencies=[Depends(require_auth)])
    def ltf_observation_card(observation_id: int) -> dict[str, Any]:
        obs = db.get_ltf_observation(observation_id)
        if obs is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        scenarios = db.list_ltf_scenarios(observation_id=obs.id)
        # ТЗ «LTF Current Setup» §8/§4.4: active_scenario — только живой
        # сценарий; отменённый доступен в истории (scenarios/last_cancellation
        # и журнале), а в текущей карточке — ожидание нового сценария
        sc = db.get_active_ltf_scenario(obs.id)
        sc_block = _scenario_block(db, sc) if sc is not None else None
        return {
            "observation": obs.to_dict(),
            "instrument": _instrument_brief(db, obs.instrument_id),
            "parent_zone": _zone_brief(db, obs.zone_id),
            "active_scenario": sc_block,
            "awaiting_new_scenario": (
                sc is None and obs.state in _ACTIVE_STATES
            ),
            "last_cancellation": _last_cancellation(db, obs.id),
            "scenarios": [s.to_dict() for s in scenarios],
            "expected": _expected_levels(db, obs, settings),
            "state_version": _state_version(db, obs, sc),
        }

    # ------------------------- слои графика (§3.5) -------------------------

    @app.get("/api/ltf/observations/{observation_id}/chart",
             dependencies=[Depends(require_auth)])
    def ltf_observation_chart(observation_id: int) -> dict[str, Any]:
        # сборка слоёв — app/services/overview.py (используется и ботом);
        # F04: чтение в одной read-транзакции, state_version — из того же
        # счётчика state_seq, что и /current (клиент сверяет версии пакета)
        with db.read_tx():
            layers = observation_chart_layers(db, settings, observation_id)
            seq = db.get_state_seq()
        if layers is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        return {"state_version": seq, **layers}

    # ------------------- read model «LTF Current Setup» (§14) -------------------

    @app.get("/api/ltf/instruments", dependencies=[Depends(require_auth)])
    def ltf_instruments() -> dict[str, Any]:
        """Левая панель «Активы» (§4.2): одна строка на instrument_id
        (symbol/venue/market), сколько бы Observation ни было у инструмента.
        eligible_count/stage — тем же циклом допуска, что /current (§13).
        Вычисление — app/services/overview.py. Чтение — в одной
        read-транзакции (D01): state_version — из того же счётчика
        state_seq, что у /current (клиент сверяет версии пакета)."""
        with db.read_tx():
            rows = instruments_overview(db, settings)
            seq = db.get_state_seq()
        return {"state_version": seq, "instruments": rows}

    @app.get("/api/ltf/instruments/{instrument_id}/current",
             dependencies=[Depends(require_auth)])
    def ltf_instrument_current(instrument_id: int) -> dict[str, Any]:
        """InstrumentCurrentView (§14): согласованный снимок «здесь и сейчас».
        current_scenario — только не отменённый; отменённый — в истории
        (journal/observations). После reconnect тот же снимок: отменённый
        сценарий текущим не удерживается.
        Вычисление — app/services/overview.py. Чтение — в одной
        read-транзакции (D01): серия запросов снимка видит одну версию
        состояния."""
        with db.read_tx():
            data = instrument_current(db, settings, instrument_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Инструмент не найден")
        return data

    @app.post("/api/ltf/instruments/{instrument_id}/select-context",
              dependencies=[Depends(require_auth)])
    def ltf_select_context(
        instrument_id: int, body: LtfSelectContextIn
    ) -> dict[str, Any]:
        """Ручной выбор контекста (§7): сохраняется в meta и удерживается,
        пока контекст доступен; ушедший в историю — fallback-политика."""
        obs = db.get_ltf_observation(body.observation_id)
        if obs is None or obs.instrument_id != instrument_id:
            raise HTTPException(status_code=404, detail="Контекст не найден")
        if obs.state not in _ACTIVE_STATES:
            raise HTTPException(
                status_code=409,
                detail="Контекст в истории — ручной выбор недоступен",
            )
        db.set_meta(f"ltf:selected_context:{instrument_id}", str(obs.id))
        return {"selected_context_id": obs.id}

    # ------------------------- журнал наблюдения (§3.2) -------------------------

    @app.get("/api/ltf/observations/{observation_id}/journal",
             dependencies=[Depends(require_auth)])
    def ltf_observation_journal(observation_id: int) -> dict[str, Any]:
        """Все события наблюдения, включая события отменённых сценариев
        (§6.5: отмена не стирает историю); нужен, когда активного
        сценария ещё/уже нет. F04: конверт {state_version, events} —
        версия из того же state_seq, чтение в одной read-транзакции."""
        with db.read_tx():
            obs = db.get_ltf_observation(observation_id)
            events = (
                db.list_ltf_events(observation_id=obs.id, limit=1000)
                if obs is not None else []
            )
            seq = db.get_state_seq()
        if obs is None:
            raise HTTPException(status_code=404, detail="Наблюдение не найдено")
        return {
            "state_version": seq,
            "events": [
                e.to_dict()
                for e in sorted(events, key=lambda e: (e.occurred_at, e.id))
            ],
        }

    # ------------------------- таблица Entry Zones (§3.4) -------------------------

    @app.get("/api/ltf/scenarios/{scenario_id}/entries",
             dependencies=[Depends(require_auth)])
    def ltf_scenario_entries(
        scenario_id: int, price: Optional[float] = None,
        include_all: bool = False, view: str = "eligible",
    ) -> dict[str, Any]:
        """Таблица Entry Zones сценария.

        view (ТЗ «LTF Current Setup» §10/§12): eligible — только подходящие
        (reason == "ok", по умолчанию); excluded — исключённые с причиной
        (reason != "ok"); history — все строки всех версий (история причин).
        invalid-зоны скрыты во всех представлениях (рыночно неактуальны).
        F04: конверт {state_version, entries} — версия из того же state_seq,
        чтение в одной read-транзакции."""
        if view not in ("eligible", "excluded", "history"):
            raise HTTPException(
                status_code=400,
                detail="view: eligible | excluded | history",
            )
        with db.read_tx():
            sc = db.get_ltf_scenario(scenario_id)
            rows = []
            if sc is not None:
                current = db.get_current_ltf_range(sc.id)
                entries = [
                    e for e in db.list_ltf_scenario_entries(sc.id)
                    if e.state != "invalid"
                ]
                if current is not None:
                    ver = current.version
                elif entries:
                    # диапазона ещё нет — последняя версия среди строк
                    # сценария, иначе фильтр по несуществующей v0 скрывал
                    # бы всё
                    ver = max(e.range_version for e in entries)
                else:
                    ver = 0
                allow_outside = context_complete(_context_flags(db, sc.id))
                for e in entries:
                    z = db.get_ltf_entry_zone(e.entry_zone_id)
                    if z is None:
                        continue
                    admitted = evaluate_final(
                        e, z, allow_outside=allow_outside
                    ).eligible_now
                    if view == "eligible" and not admitted:
                        continue
                    if view == "excluded" and admitted:
                        continue
                    if (view != "history" and not include_all
                            and e.range_version != ver):
                        continue
                    rows.append(_entry_row(db, e, z, sc, price))
                rows.sort(key=lambda r: (_ENTRY_ORDER.get(r["type"], 9),
                                         r["confirmed_at"] or 0))
            seq = db.get_state_seq()
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        return {"state_version": seq, "entries": rows}

    # ------------------------- журнал (§3.2) -------------------------

    @app.get("/api/ltf/scenarios/{scenario_id}/journal",
             dependencies=[Depends(require_auth)])
    def ltf_scenario_journal(scenario_id: int) -> dict[str, Any]:
        """F04: конверт {state_version, events} — версия из того же
        state_seq, чтение в одной read-транзакции."""
        with db.read_tx():
            sc = db.get_ltf_scenario(scenario_id)
            merged: dict[int, Any] = {}
            if sc is not None:
                merged = {
                    e.id: e
                    for e in db.list_ltf_events(scenario_id=sc.id, limit=1000)
                }
                for e in db.list_ltf_events(observation_id=sc.observation_id,
                                            limit=1000):
                    merged.setdefault(e.id, e)
            seq = db.get_state_seq()
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        return {
            "state_version": seq,
            # §5/§12: причинная цепочка сценария (эпоха, уровень отмены)
            "scenario": sc.to_dict(),
            "events": [
                e.to_dict()
                for e in sorted(merged.values(),
                                key=lambda e: (e.occurred_at, e.id))
            ],
        }

    # ------------------------- ручное завершение (§12) -------------------------

    @app.post("/api/ltf/scenarios/{scenario_id}/close",
              dependencies=[Depends(require_auth)])
    def ltf_scenario_close(scenario_id: int) -> dict[str, Any]:
        sc = db.get_ltf_scenario(scenario_id)
        if sc is None:
            raise HTTPException(status_code=404, detail="Сценарий не найден")
        if sc.state in ("cancelled", "closed"):
            raise HTTPException(status_code=409,
                                detail="Сценарий уже завершён")
        if ltf_engine is None:
            raise HTTPException(status_code=503, detail="LTF-модуль выключен")
        # родительская HTF-зона не трогается (§12) — внутри метода движка
        ltf_engine.close_scenario_manually(scenario_id)
        return db.get_ltf_scenario(scenario_id).to_dict()

    # ------------------------- разметка Entry Zones -------------------------

    @app.post("/api/ltf/entry-zones/{entry_zone_id}/review",
              dependencies=[Depends(require_auth)])
    def ltf_entry_zone_review(
        entry_zone_id: int, body: LtfReviewIn
    ) -> dict[str, Any]:
        """Оценка Entry Zone с раздельными вердиктами (аналог §15.3 HTF).

        Оценка ТОЛЬКО фиксируется: validity зоны, границы, first_test_at и
        привязки ltf_scenario_entry не меняются, движок не перезапускается.
        """
        zone = db.get_ltf_entry_zone(entry_zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Entry Zone не найдена")
        # обратная совместимость старых решений (как на HTF-ревью, R13)
        decision = LTF_LEGACY_DECISIONS.get(body.decision, body.decision)
        if decision not in LTF_REVIEW_DECISIONS:
            raise HTTPException(
                status_code=400,
                detail="decision: correct | now_irrelevant | fix_boundaries | "
                       "wrong_base | wrong_type | no_context | wrong "
                       "(legacy: confirmed | rejected | corrected)",
            )
        # wrong без кода причины — 'unknown', а не выдуманная причина
        reason_code = body.reason_code or (
            "unknown" if decision == "wrong" else decision
        )
        now = now_ms()
        # replay не нужен — состояние не меняем; фиксируем только опору
        # оценки: open_time последней закрытой H1-свечи инструмента
        last_h1 = db.last_candle(zone.instrument_id, "H1")
        assessed_as_of = last_h1.open_time if last_h1 is not None else now
        geometry_verdict = "unknown"
        requires_clarification = False
        corrected_lower: Optional[float] = None
        corrected_upper: Optional[float] = None

        if decision in ("correct", "now_irrelevant"):
            geometry_verdict = "valid"
        elif decision in ("wrong_base", "wrong_type", "wrong"):
            geometry_verdict = "invalid"
        elif decision == "fix_boundaries":
            if body.lower is None or body.upper is None:
                raise HTTPException(
                    status_code=422,
                    detail="fix_boundaries требует lower и upper",
                )
            geometry_verdict = "needs_correction"
            corrected_lower = float(body.lower)
            corrected_upper = float(body.upper)
        elif decision == "no_context":
            # нет контекста — невозможность оценки, а не ошибка геометрии
            requires_clarification = True

        # жизненный цикл зоны на LTF — первое касание (§9 LTF-спеки)
        lifecycle_verdict = "tested" if zone.first_test_at else None

        # решение сохраняем как нажато (история не теряется)
        review = LtfReview(
            id=None, entry_zone_id=entry_zone_id,
            scenario_id=body.scenario_id, decision=body.decision,
            text=body.text, created_at=now,
        )
        review.id = db.add_ltf_review(review)
        assessment = LtfReviewAssessment(
            id=None, entry_zone_id=entry_zone_id, review_id=review.id,
            review_decision=decision, geometry_verdict=geometry_verdict,
            lifecycle_verdict=lifecycle_verdict, reason_code=reason_code,
            evidence_source=body.evidence_source,
            assessed_as_of=assessed_as_of, reviewed_at=now,
            requires_clarification=requires_clarification,
            corrected_lower=corrected_lower, corrected_upper=corrected_upper,
        )
        assessment.id = db.add_ltf_assessment(assessment)

        # зону не трогаем; разметка — отдельным файлом ltf_labels.jsonl
        labels_file = export_ltf_label(
            db, zone, body.decision, body.text or "", settings,
            assessment=assessment, scenario_id=body.scenario_id,
        )
        return {
            "entry_zone": zone.to_dict(),
            "labels_file": labels_file,
            "review": review.to_dict(),
            "assessment": assessment.to_dict(),
            "reviews": [r.to_dict() for r in db.get_ltf_reviews(entry_zone_id)],
        }
