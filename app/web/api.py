"""Web API и WebSocket HTF Zones (§9, §10, §11 спеки).

Фабрика ``create_app(db, settings, event_bus=None)`` собирает FastAPI-приложение:
REST /api/* (закрыто токеном владельца), WebSocket /ws, статика на /.
Запуск: ``uvicorn --factory app.web.api:create_app_from_env`` или из воркера —
``create_app(db, settings)`` и ``uvicorn.Config(app=app, ...)``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import (
    DETECTOR_DEPRECATED_FIELDS,
    DETECTOR_FIELD_GROUPS,
    DetectorConfig,
    Settings,
    load_settings,
    parse_bool,
    validate_detector_config,
    validate_detector_payload,
)
from ..db import Database
from ..engine import Scanner
from ..engine.scanner import review_replay_start_ms
from ..services.journal import JOURNAL_KINDS, collect_journal
from ..services.zone_groups import union_find_groups
from ..models import (
    TIMEFRAME_MINUTES,
    BoundaryCorrection,
    Direction,
    Event,
    EventKind,
    Instrument,
    InnerLevel,
    Review,
    ReviewAssessment,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from .auth import check_ws_token, make_auth_dependency

APP_VERSION = "0.1.0"
_STATIC_DIR = Path(__file__).with_name("static")

logger = logging.getLogger(__name__)


class _NoCacheStaticFiles(StaticFiles):
    """Статика без heuristic-кэша: Cache-Control: no-cache заставляет браузер
    ревалидировать при каждой загрузке (ETag/Last-Modified → дешёвый 304),
    иначе после обновления ltf.js/ltf.html пользователи видят старую копию."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

# Статусы, которые по умолчанию интересуют интерфейс (остальные — по фильтру)
_VISIBLE_STATUSES = [
    ZoneStatus.CANDIDATE,
    ZoneStatus.ACTIVE,
    ZoneStatus.WEAKENED,
    ZoneStatus.WORKED,
    ZoneStatus.CONVERTED,
    ZoneStatus.TAKEN,
]

# §15.3 (R13): обратная совместимость старых решений ревью
_LEGACY_DECISIONS = {
    "confirmed": "correct",
    "rejected": "wrong",
    "corrected": "fix_boundaries",
}
_VALID_DECISIONS = {
    "correct",          # размечено верно
    "now_irrelevant",   # верная форма, но сейчас неактуально
    "fix_boundaries",   # поправить границы (нужны lower/upper)
    "wrong_base",       # другое основание
    "already_breaker",  # уже превратился в Breaker
    "no_context",       # недостаточно истории/контекста
    "wrong_type",       # неверный тип/форма
    "wrong",            # legacy rejected (reason_code обязателен, иначе unknown)
}

# Статусы завершённого жизненного цикла (§15.1.1): ревью их не воскрешает
_TERMINAL_STATUSES = {
    ZoneStatus.WORKED,
    ZoneStatus.ARCHIVED,
    ZoneStatus.TAKEN,
    ZoneStatus.CONVERTED,
}


# ---------------------------------------------------------------------------
# Сериализация
# ---------------------------------------------------------------------------

def instrument_to_dict(ins: Instrument) -> dict[str, Any]:
    return {
        "id": ins.id,
        "asset": ins.asset,
        "venue": ins.venue,
        "market_type": ins.market_type,
        "symbol": ins.symbol,
        "quote_asset": ins.quote_asset,
        "precision": ins.precision,
        "enabled": ins.enabled,
        "ltf_analyze": ins.ltf_analyze,
    }


def _unconfirmed_reason(z: Zone) -> Optional[str]:
    """Причина отсутствия автоматического подтверждения (ТЗ 06.10.2026 §13):
    нарушена хронология / не найден внешний FVG / недостающие свечи."""
    if z.confirmed_at is not None:
        return None
    if z.evidence.get("integrity") == "inconsistent":
        return "timeline_violation"
    if z.needs_replay:
        return "data_incomplete"
    return "no_external_fvg"


def zone_to_dict(z: Zone) -> dict[str, Any]:
    """Зона для API: середина, возраст, evidence (объяснение обнаружения, §13).

    display-поля (§15.1.3, §15.1.7): рисунок начинается от display_from
    (для FVG — средняя свеча тройки) и заканчивается в display_until
    (момент завершения состояния), не тянется до текущей цены.
    """
    return {
        "id": z.id,
        "instrument_id": z.instrument_id,
        "type": z.type.value,
        "direction": z.direction.value,
        "timeframe": z.timeframe,
        "lower": z.lower,
        "upper": z.upper,
        "mid": z.mid,
        "width": z.width,
        "is_level": z.is_level,
        "status": z.status.value,
        "cycle_id": z.cycle_id,
        "source": z.source,
        "formed_at": z.formed_at,
        "confirmed_at": z.confirmed_at,
        "created_at": z.created_at,
        # начало рисунка: display_from (FVG — средняя свеча) или formed_at
        "display_from": z.display_from or z.formed_at,
        # конец рисунка: None — зона живая, рисуется до края графика
        "display_until": z.display_until,
        "end_reason": z.end_reason,
        # §6: закрытие за дальней границей есть, ждём новый FVG пробоя
        "breaker_pending": z.breakout_close_at is not None,
        # §6/§15.6: предшествующий тест >50% навсегда запретил Breaker
        "breaker_forbidden": z.breaker_forbidden,
        "age_days": round((now_ms() - z.formed_at) / 86_400_000, 1),
        "source_candles": z.source_candles,
        # ТЗ «Единый движок» §7: якорь ручной зоны на графике и типовое
        # правило (ob/fvg) для source=manual
        "anchor_time": z.anchor_time,
        "zone_type": z.zone_type,
        "name": z.evidence.get("name", ""),
        "comment": z.evidence.get("comment", ""),
        "boundary_version": z.evidence.get("boundary_version", 1),
        # ТЗ 06.10.2026 §4 (T09): основание подтверждения — FVG, только
        # ручное одобрение владельца или отсутствует
        "confirmation_state": (
            "fvg_confirmed" if z.confirmed_at is not None
            else "manual_only" if z.evidence.get("manual_confirmation_only")
            else "unconfirmed"
        ),
        # ТЗ 07.10.2026 §3/§11: единый canonical state — актуальные зоны
        # рисуются сразу, статус candidate означает только очередь ревью
        "relevant": z.is_currently_relevant(),
        # ТЗ 07.10.2026 §7 (T10): предпочтение владельца для рабочего входа
        "entry_preference": (
            "preferred" if z.evidence.get("preferred_for_entry")
            else "superseded" if z.evidence.get("superseded_for_entry_by")
            else None
        ),
        "supersedes_for_entry": z.evidence.get("supersedes_for_entry"),
        "superseded_for_entry_by": z.evidence.get("superseded_for_entry_by"),
        "market_validity": z.market_validity,
        "evidence": z.evidence,
        "rule_version": z.rule_version,
    }


def inner_level_to_dict(lv: InnerLevel) -> dict[str, Any]:
    """ТЗ «Единый движок» §5: внутренний уровень ликвидности после теста OB."""
    return {
        "id": lv.id,
        "parent_ob_id": lv.parent_ob_id,
        "instrument_id": lv.instrument_id,
        "timeframe": lv.timeframe,
        "kind": lv.kind,
        "price": lv.price,
        "pivot_time": lv.pivot_time,
        "confirmed_at": lv.confirmed_at,
        "status": lv.status,
        "taken_at": lv.taken_at,
        "source_test_id": lv.source_test_id,
        "created_at": lv.created_at,
    }


def event_to_dict(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "zone_id": e.zone_id,
        "cycle_id": e.cycle_id,
        "kind": e.kind.value,
        "occurred_at": e.occurred_at,
        "detected_at": e.detected_at,
        "price": e.price,
        "depth": e.depth,
        "delayed": e.delayed,
        "evidence": e.evidence,
    }


def review_to_dict(r: Review) -> dict[str, Any]:
    return {
        "id": r.id,
        "zone_id": r.zone_id,
        "decision": r.decision,
        "author": r.author,
        "text": r.text,
        "boundary_version": r.boundary_version,
        "created_at": r.created_at,
    }


def assessment_to_dict(a: ReviewAssessment) -> dict[str, Any]:
    """§15.3: раздельная оценка геометрии и актуальности (R01)."""
    return {
        "id": a.id,
        "zone_id": a.zone_id,
        "review_id": a.review_id,
        "review_decision": a.review_decision,
        "geometry_verdict": a.geometry_verdict,
        "lifecycle_verdict": a.lifecycle_verdict,
        "reason_code": a.reason_code,
        "evidence_source": a.evidence_source,
        "assessed_as_of": a.assessed_as_of,
        "reviewed_at": a.reviewed_at,
        "requires_clarification": a.requires_clarification,
    }


def boundary_correction_to_dict(c: BoundaryCorrection) -> dict[str, Any]:
    return {
        "id": c.id,
        "zone_id": c.zone_id,
        "boundary_version": c.boundary_version,
        "original_lower": c.original_lower,
        "original_upper": c.original_upper,
        "corrected_lower": c.corrected_lower,
        "corrected_upper": c.corrected_upper,
        "anchor_candle_open_time": c.anchor_candle_open_time,
        "reason": c.reason,
        "created_at": c.created_at,
    }


def export_label(db: Database, zone: Zone, decision: str, text: str,
                 settings: Settings,
                 assessment: Optional[ReviewAssessment] = None,
                 correction: Optional[BoundaryCorrection] = None) -> str:
    """Пишет решение пользователя в labels.jsonl рядом с БД (append-only,
    сырые строки неизменны — R13).

    Это разметка для ИИ-анализа и выведения правил §14: полный снимок зоны,
    связей и исходных свечей с OHLC — отдельно от базы, чтобы разметку можно
    было забрать/передать независимо. Версия 2 (§15.3): review_id, времена
    reviewed_at/assessed_as_of, раздельные вердикты геометрии/актуальности,
    код причины, исходные/исправленные границы и display-поля зоны.
    """
    ins = db.get_instrument(zone.instrument_id)
    rel = db.get_relation(zone.id)
    relation = None
    if rel is not None:
        relation = {
            "parent_ob_id": rel.parent_ob_id,
            "confirming_fvg_id": rel.confirming_fvg_id,
            "predecessor_ob_id": rel.predecessor_ob_id,
            "confirming_fvg": None,
        }
        if rel.confirming_fvg_id:
            fvg = db.get_zone(rel.confirming_fvg_id)
            if fvg:
                relation["confirming_fvg"] = zone_to_dict(fvg)

    source_ohlc = []
    if zone.source_candles:
        by_open = {
            c.open_time: c
            for c in db.get_candles(zone.instrument_id, zone.timeframe)
        }
        for t in zone.source_candles:
            c = by_open.get(t)
            if c:
                source_ohlc.append({
                    "open_time": t, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                })

    now = now_ms()
    record = {
        "purpose": "разметка для ИИ-анализа и выведения правил §14 (dev-режим)",
        "labels_version": 2,
        "exported_at": now,  # время снимка; не подменяет reviewed_at (R13)
        # фактическая версия правил конкретной зоны (ТЗ §10), а не константа
        "rule_version": zone.rule_version,
        "decision": decision,
        "comment": text,
        "author": "owner",
        "instrument": instrument_to_dict(ins) if ins else None,
        "zone": zone_to_dict(zone),
        "relation": relation,
        "source_candles_ohlc": source_ohlc,
        # §15.3: раздельная оценка и контекст ревью
        "review_id": assessment.review_id if assessment else None,
        "reviewed_at": assessment.reviewed_at if assessment else now,
        "assessed_as_of": assessment.assessed_as_of if assessment else now,
        "review_decision": assessment.review_decision if assessment else decision,
        "geometry_verdict": assessment.geometry_verdict if assessment else "unknown",
        "lifecycle_verdict": assessment.lifecycle_verdict if assessment else None,
        "reason_code": assessment.reason_code if assessment else "",
        "requires_clarification": (
            bool(assessment.requires_clarification) if assessment else False
        ),
        # display-поля зоны на момент ревью (§15.1.3/§15.1.7)
        "display_from": zone.display_from or zone.formed_at,
        "display_until": zone.display_until,
        "end_reason": zone.end_reason,
        # ТЗ 06.10.2026 §3.6/§11: явная временная семантика снимка —
        # разница в 1 мс между display_until (boundary) и assessed_as_of
        # (close_time) — следствие конвенции, а не будущие данные; срез
        # zone — текущее состояние БД на exported_at, а не на reviewed_at
        "time_semantics": {
            "event_time": "exclusive close boundary = open_time + tf = close_time + 1ms",
            "assessed_as_of": "close_time последней закрытой свечи ТФ зоны",
            "reviewed_at": "момент нажатия оценки пользователем",
            "snapshot_scope": "current_db_state_at_export",
        },
        # §15.2: исходные и исправленные границы (для fix_boundaries)
        "original_lower": correction.original_lower if correction else None,
        "original_upper": correction.original_upper if correction else None,
        "corrected_lower": correction.corrected_lower if correction else None,
        "corrected_upper": correction.corrected_upper if correction else None,
        "anchor_candle_open_time": (
            correction.anchor_candle_open_time if correction else None
        ),
    }
    # ТЗ §10: слой нормализации старого ошибочного паттерна (wrong_type +
    # lifecycle-комментарий) — отдельным полем, сырые вердикты не трогаем
    normalized = _normalization_hint(
        record["review_decision"], record["geometry_verdict"], text)
    if normalized is not None:
        record["normalized"] = normalized
    path = Path(settings.db_path).parent / "labels.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Тела запросов
# ---------------------------------------------------------------------------

class InstrumentIn(BaseModel):
    asset: str
    venue: str
    symbol: str
    market_type: str = "spot"
    quote_asset: str = "USDT"
    precision: int = 8


class ManualZoneIn(BaseModel):
    """Ручная зона (§10, ТЗ §7): диапазон L/U либо один уровень ``level``.

    ``anchor_time`` (ms) — выбранное пользователем начало зоны на графике
    (может быть историческим); по умолчанию — момент создания. ``zone_type``
    — типовое правило для ручной зоны: ob | fvg | None (без правила; для
    level-зон допустимо None)."""
    instrument_id: int
    direction: str = "bull"
    lower: Optional[float] = None
    upper: Optional[float] = None
    level: Optional[float] = None
    timeframe: str = "D1"
    name: str = ""
    comment: str = ""
    anchor_time: Optional[int] = None
    zone_type: Optional[str] = None


class ZonePatchIn(BaseModel):
    """Правка границ/названия ручной работы (§10)."""
    lower: Optional[float] = None
    upper: Optional[float] = None
    name: Optional[str] = None
    comment: Optional[str] = None


class LtfAnalyzeIn(BaseModel):
    """Галочка «Анализировать» в настройках LTF."""
    analyze: bool


class ReviewIn(BaseModel):
    """Решение ревью (§15.3, R13).

    Новые коды: correct | now_irrelevant | fix_boundaries | wrong_base |
    already_breaker | no_context | wrong_type. Обратная совместимость:
    confirmed → correct, corrected → fix_boundaries, rejected → отклонение
    с reason_code (по умолчанию 'unknown').
    """
    decision: str
    text: str = ""
    lower: Optional[float] = None
    upper: Optional[float] = None
    reason_code: Optional[str] = None          # машинный код причины (R13)
    anchor_candle_open_time: Optional[int] = None  # свеча-якорь поправки (§15.2)
    evidence_source: str = "manual_ui"


class PreferEntryIn(BaseModel):
    """ТЗ 07.10.2026 §7 (T10): предпочтение владельца для рабочего входа.

    supersedes_zone_id — какую связанную (например, более широкую
    охватывающую) зону эта зона замещает для входа; None — снять пометку.
    """
    supersedes_zone_id: Optional[int] = None
    comment: str = ""


# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------

class WsHub:
    """Простой hub: множество подключений + broadcast() для воркера (§11 п.6).

    ``broadcast`` — синхронный метод: его может дёргать фоновый воркер
    из своего потока, отправка планируется в цикле событий приложения.
    """

    def __init__(self, state_seq_provider=None) -> None:
        self.connections: set[WebSocket] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # D01: провайдер монотонной версии состояния (db.get_state_seq);
        # каждое сообщение несёт версию изменения
        self._state_seq_provider = state_seq_provider

    async def connect(self, ws: WebSocket) -> None:
        self._loop = asyncio.get_running_loop()
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.connections.discard(ws)

    async def _broadcast_async(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.connections.discard(ws)

    def broadcast(self, message: dict[str, Any]) -> None:
        """Трансляция {type: price|event|zone|ltf, state_seq, ...} всем
        подключённым клиентам."""
        if self._loop is None or not self.connections:
            return
        if self._state_seq_provider is not None:
            try:
                message = {
                    **message,
                    "state_seq": self._state_seq_provider(),
                }
            except Exception:
                pass  # версия — дополнение, не должна ронять доставку
        asyncio.run_coroutine_threadsafe(self._broadcast_async(message), self._loop)


# ---------------------------------------------------------------------------
# Настройки детектора: файл data/settings.json (§10)
# ---------------------------------------------------------------------------

def _settings_path(settings: Settings) -> Path:
    parent = Path(settings.db_path).parent
    if str(parent) in ("", "."):
        parent = Path("data")
    return parent / "settings.json"


def _apply_detector_payload(cfg: DetectorConfig, payload: dict[str, Any]) -> list[str]:
    """Применяет известные поля DetectorConfig; возвращает список применённых."""
    known = {f.name: type(f.default) for f in fields(cfg)}
    applied: list[str] = []
    for key, value in payload.items():
        if key not in known:
            continue
        try:
            # bool — подкласс int: строки/числа приводим аккуратно
            # ("false" из ENV/JSON не должна превращаться в True)
            if known[key] is bool:
                setattr(cfg, key, parse_bool(value))
            else:
                setattr(cfg, key, known[key](value))
            applied.append(key)
        except (ValueError, TypeError) as exc:
            # A05: битое значение в settings.json не роняет старт, но
            # видно в журнале (поле, значение, причина)
            logger.warning(
                "settings.json: поле %s со значением %r пропущено (%s)",
                key, value, exc,
            )
    return applied


def _load_detector_from_file(settings: Settings) -> None:
    path = _settings_path(settings)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _apply_detector_payload(settings.detector, payload.get("detector", payload))
    except (json.JSONDecodeError, OSError) as exc:
        # битый файл настроек не должен ронять старт — но и не прячется
        logger.warning("settings.json не прочитан (%s); используются значения по умолчанию", exc)


def _save_detector_to_file(
    settings: Settings, detector: Optional[DetectorConfig] = None
) -> Path:
    """Атомарная запись настроек (A05): через временный файл и os.replace,
    чтобы сбой записи не оставлял обрезанный settings.json."""
    path = _settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"detector": asdict(detector or settings.detector)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# A05: задание пересчёта после смены настроек — статус в meta БД
# ---------------------------------------------------------------------------

_RECALC_META_KEY = "settings:recalc"
# потолок ожидания пересчёта в запросе; дольше — ответ со status=running,
# задание продолжается в фоне и видно через GET /api/settings
_RECALC_WAIT_SECONDS = 25.0


def _get_recalc_status(db: Database) -> Optional[dict[str, Any]]:
    raw = db.get_meta(_RECALC_META_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _set_recalc_status(db: Database, status: str, *, started_at: int,
                       finished_at: Optional[int] = None,
                       error: Optional[str] = None,
                       result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    recalc: dict[str, Any] = {"status": status, "started_at": started_at,
                              "finished_at": finished_at,
                              "error": error, "result": result}
    db.set_meta(_RECALC_META_KEY, json.dumps(recalc, ensure_ascii=False))
    return recalc


# ---------------------------------------------------------------------------
# Фабрика приложения
# ---------------------------------------------------------------------------

def create_app(
    db: Database,
    settings: Settings,
    event_bus: Optional[Callable[[Callable[[dict[str, Any]], None]], Any]] = None,
    ltf_engine=None,
    alt_runner=None,
) -> FastAPI:
    """Собирает FastAPI-приложение поверх репозитория Database.

    event_bus — необязательный callable, который вызывается один раз с
    ``hub.broadcast``: воркер подписывается на него снаружи и шлёт
    сообщения {type: price|event|zone|ltf|alt, ...}. Hub доступен как
    ``app.state.ws_hub``. ltf_engine — движок окна LTF (ручное завершение
    сценариев); None — LTF-маршруты работают в режиме чтения.
    alt_runner — дневной runner модуля «Альткоины»; None — ALT-маршруты
    работают в режиме чтения (ручной пересчёт отвечает 503). В main.py
    runner конструируется после приложения и ставится в
    ``app.state.alt_runner`` — эндпоинт пересчёта читает его в момент
    запроса.
    """
    _load_detector_from_file(settings)
    # A05: recalc-задание в статусе running при старте — процесс умер посреди
    # пересчёта (безопасное завершение после краша/перезапуска)
    _stale_recalc = _get_recalc_status(db)
    if _stale_recalc is not None and _stale_recalc.get("status") == "running":
        _set_recalc_status(
            db, "failed", started_at=_stale_recalc.get("started_at") or now_ms(),
            finished_at=now_ms(), error="interrupted_by_restart",
        )
    require_auth = make_auth_dependency(settings)
    hub = WsHub(state_seq_provider=db.get_state_seq)

    app = FastAPI(title="LevelFrame", version=APP_VERSION)
    app.state.db = db
    app.state.settings = settings
    app.state.ws_hub = hub
    app.state.event_bus = event_bus
    app.state.ltf_engine = ltf_engine
    app.state.alt_runner = alt_runner
    if callable(event_bus):
        event_bus(hub.broadcast)

    from .alt_api import register_alt_routes
    from .ltf_api import register_ltf_routes

    register_ltf_routes(app, db, settings, require_auth, ltf_engine)
    register_alt_routes(app, db, settings, require_auth, alt_runner)

    # ------------------------- instruments -------------------------

    @app.get("/api/instruments", dependencies=[Depends(require_auth)])
    def list_instruments() -> list[dict[str, Any]]:
        return [instrument_to_dict(i) for i in db.get_instruments()]

    @app.post("/api/instruments", status_code=201, dependencies=[Depends(require_auth)])
    def add_instrument(body: InstrumentIn) -> dict[str, Any]:
        ins = Instrument(
            id=None, asset=body.asset, venue=body.venue, market_type=body.market_type,
            symbol=body.symbol, quote_asset=body.quote_asset, precision=body.precision,
            enabled=True,
        )
        ins_id = db.upsert_instrument(ins)
        db.set_instrument_enabled(ins_id, True)
        return instrument_to_dict(db.get_instrument(ins_id))

    @app.post("/api/instruments/{instrument_id}/toggle", dependencies=[Depends(require_auth)])
    def toggle_instrument(instrument_id: int) -> dict[str, Any]:
        ins = db.get_instrument(instrument_id)
        if ins is None:
            raise HTTPException(status_code=404, detail="Инструмент не найден")
        db.set_instrument_enabled(instrument_id, not ins.enabled)
        return instrument_to_dict(db.get_instrument(instrument_id))

    @app.post("/api/instruments/{instrument_id}/ltf-analyze",
              dependencies=[Depends(require_auth)])
    def set_instrument_ltf_analyze(
        instrument_id: int, payload: LtfAnalyzeIn
    ) -> dict[str, Any]:
        """Галочка «Анализировать» (настройки LTF): наблюдения на подтверждённые
        OB D1/W1 открываются без касания. Снятие не закрывает уже открытые."""
        ins = db.get_instrument(instrument_id)
        if ins is None:
            raise HTTPException(status_code=404, detail="Инструмент не найден")
        db.set_instrument_ltf_analyze(instrument_id, payload.analyze)
        return instrument_to_dict(db.get_instrument(instrument_id))

    # ------------------------- zones -------------------------

    @app.get("/api/zones", dependencies=[Depends(require_auth)])
    def list_zones(
        instrument_id: Optional[int] = None,
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        statuses = _parse_enum_list(status, ZoneStatus, "status")
        types = _parse_enum_list(type, ZoneType, "type")
        zones = db.get_zones(instrument_id=instrument_id, statuses=statuses, types=types)
        return [zone_to_dict(z) for z in zones]

    @app.get("/api/zones/grouped", dependencies=[Depends(require_auth)])
    def grouped_zones(instrument_id: int) -> dict[str, Any]:
        """Визуальное объединение пересекающихся ACTIVE-зон (§10).

        Только для отображения: исходные зоны, их границы, середины и
        правила уведомлений НЕ пересчитываются и не меняются.
        """
        zones = [z for z in db.get_zones(instrument_id=instrument_id,
                                         statuses=[ZoneStatus.ACTIVE])
                 if z.is_currently_relevant()]  # ТЗ 06.10.2026 §13 (T21)
        groups = union_find_groups(zones)
        return {
            "instrument_id": instrument_id,
            "groups": [
                {
                    "id": min(z.id for z in members),
                    "lower": min(z.lower for z in members),
                    "upper": max(z.upper for z in members),
                    "mid": (min(z.lower for z in members) + max(z.upper for z in members)) / 2,
                    "zone_ids": [z.id for z in members],
                    "zones": [zone_to_dict(z) for z in members],
                }
                for members in groups
            ],
        }

    @app.post("/api/zones/manual", status_code=201, dependencies=[Depends(require_auth)])
    def create_manual_zone(body: ManualZoneIn) -> dict[str, Any]:
        """Ручная зона (§10, ТЗ §7): type=manual, source='manual', сразу ACTIVE.

        Начало видимости — anchor_time (выбор пользователя, может быть
        историческим): сохраняется в anchor_time и display_from. confirmed_at
        НЕ проставляется: ручное создание не доказывает алгоритмическое
        подтверждение, видимость обеспечивает якорь."""
        ins = db.get_instrument(body.instrument_id)
        if ins is None:
            raise HTTPException(status_code=404, detail="Инструмент не найден")
        if body.level is not None:
            lower = upper = float(body.level)
        else:
            if body.lower is None or body.upper is None:
                raise HTTPException(status_code=400, detail="Нужны lower/upper или level")
            lower, upper = sorted((float(body.lower), float(body.upper)))
        try:
            direction = Direction(body.direction)
        except ValueError:
            raise HTTPException(status_code=400, detail="direction: bull | bear")
        if body.timeframe not in TIMEFRAME_MINUTES:
            raise HTTPException(status_code=400, detail="timeframe: H1 | H4 | D1 | W1")
        if body.zone_type not in (None, "ob", "fvg"):
            raise HTTPException(status_code=400, detail="zone_type: ob | fvg | null")
        now = now_ms()
        anchor = int(body.anchor_time) if body.anchor_time is not None else now
        zone = Zone(
            id=None, instrument_id=ins.id, type=ZoneType.MANUAL, direction=direction,
            timeframe=body.timeframe, lower=lower, upper=upper,
            formed_at=now, confirmed_at=None, status=ZoneStatus.ACTIVE,
            source="manual", created_at=now,
            display_from=anchor, anchor_time=anchor, zone_type=body.zone_type,
            evidence={
                "name": body.name,
                "comment": body.comment,
                "boundary_version": 1,
            },
        )
        zone_id = db.insert_zone(zone)
        created = db.get_zone(zone_id)
        hub.broadcast({"type": "zone", "action": "created", "zone": zone_to_dict(created)})
        return zone_to_dict(created)

    @app.get("/api/zones/{zone_id}", dependencies=[Depends(require_auth)])
    def zone_detail(zone_id: int) -> dict[str, Any]:
        zone = db.get_zone(zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Зона не найдена")
        rel = db.get_relation(zone_id)
        relation = None
        if rel is not None:
            relation = {
                "parent_ob_id": rel.parent_ob_id,
                "confirming_fvg_id": rel.confirming_fvg_id,
                "predecessor_ob_id": rel.predecessor_ob_id,
                "visual_group_id": rel.visual_group_id,
                # компактные карточки связанных объектов для панели деталей
                "parent_ob": _zone_brief(db, rel.parent_ob_id),
                "confirming_fvg": _zone_brief(db, rel.confirming_fvg_id),
                "predecessor_ob": _zone_brief(db, rel.predecessor_ob_id),
            }
        # Визиты: в репозитории нет выборки по зоне — читаем напрямую
        visits = [
            {
                "id": r["id"], "cycle_id": r["cycle_id"],
                "entered_at": r["entered_at"], "exited_at": r["exited_at"],
                "max_depth": r["max_depth"], "observed": bool(r["observed"]),
            }
            for r in db.conn.execute(
                "SELECT * FROM visit WHERE zone_id=? ORDER BY entered_at DESC", (zone_id,)
            ).fetchall()
        ]
        return {
            "zone": zone_to_dict(zone),
            "instrument": instrument_to_dict(db.get_instrument(zone.instrument_id))
            if db.get_instrument(zone.instrument_id) else None,
            "relation": relation,
            "visits": visits,
            # ТЗ §5: внутренние уровни ликвидности после тестов OB
            # (все статусы — снятые остаются в истории)
            "inner_levels": [
                inner_level_to_dict(lv)
                for lv in db.list_inner_levels(parent_ob_id=zone_id)
            ],
            "events": [event_to_dict(e) for e in db.get_events(zone_id=zone_id)],
            "reviews": [review_to_dict(r) for r in db.get_reviews(zone_id)],
            # §15.3: раздельные оценки ревью и история правок границ
            "assessments": [assessment_to_dict(a) for a in db.get_assessments(zone_id)],
            "boundary_corrections": [
                boundary_correction_to_dict(c)
                for c in db.get_boundary_corrections(zone_id)
            ],
        }

    @app.get("/api/zones/{zone_id}/inner-levels", dependencies=[Depends(require_auth)])
    def zone_inner_levels(zone_id: int) -> list[dict[str, Any]]:
        """ТЗ §5: внутренние уровни ликвидности зоны (все статусы,
        снятые остаются в истории)."""
        if db.get_zone(zone_id) is None:
            raise HTTPException(status_code=404, detail="Зона не найдена")
        return [
            inner_level_to_dict(lv)
            for lv in db.list_inner_levels(parent_ob_id=zone_id)
        ]

    @app.patch("/api/zones/{zone_id}", dependencies=[Depends(require_auth)])
    def patch_zone(zone_id: int, body: ZonePatchIn) -> dict[str, Any]:
        """Правка границ/названия (§10): версионирование границ, история
        событий НЕ сбрасывается, старые границы уходят в evidence.history."""
        zone = db.get_zone(zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Зона не найдена")
        if body.lower is not None or body.upper is not None:
            if body.lower is None or body.upper is None:
                raise HTTPException(status_code=400, detail="Границы меняются парой: lower и upper")
            _apply_boundary_change(
                db, zone, lower=float(body.lower), upper=float(body.upper),
                text="Ручная правка границ",
            )
        if body.name is not None or body.comment is not None:
            zone = db.get_zone(zone_id)
            evidence = dict(zone.evidence)
            if body.name is not None:
                evidence["name"] = body.name
            if body.comment is not None:
                evidence["comment"] = body.comment
            _update_evidence(db, zone, evidence)
        updated = db.get_zone(zone_id)
        hub.broadcast({"type": "zone", "action": "updated", "zone": zone_to_dict(updated)})
        return zone_to_dict(updated)

    # ------------------------- review кандидатов (§10, §15.3) -------------------------

    @app.post("/api/zones/{zone_id}/review", dependencies=[Depends(require_auth)])
    def review_zone(zone_id: int, body: ReviewIn) -> dict[str, Any]:
        """Решение ревью с раздельной оценкой геометрии и актуальности
        (§15.1.1, R01/R13).

        Решение НЕ переводит объект безусловно в active: `correct` активирует
        только живого кандидата, завершённая зона сохраняет свой статус
        (§15.5: approve не воскрешает отработанный объект). Перед оценкой
        состояние воспроизводится по истории свечей (R02/§15.1.2).
        """
        zone = db.get_zone(zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Зона не найдена")
        # обратная совместимость старых решений (R13)
        decision = _LEGACY_DECISIONS.get(body.decision, body.decision)
        if decision not in _VALID_DECISIONS:
            raise HTTPException(
                status_code=400,
                detail="decision: correct | now_irrelevant | fix_boundaries | wrong_base | "
                       "already_breaker | no_context | wrong_type "
                       "(legacy: confirmed | rejected | corrected)",
            )
        # rejected без кода причины — 'unknown', а не выдуманная причина (R13)
        reason_code = body.reason_code or (
            "unknown" if decision == "wrong" else decision
        )
        now = now_ms()
        # R02/§15.1.2: актуальность — по воспроизведённой истории от подтверждения
        # до момента ревью, а не по «застывшему» статусу в БД. replay идемпотентен:
        # по свежему инструменту это дёшевый прогон, по отстающему — догоняет
        # касания/глубины/пробои до assessed_as_of. Окно — от появления зоны
        # (для OB formed_at — первая свеча базы) с запасом в несколько свечей:
        # состояние до окна уже накоплено live-трекингом, а replay аддитивен,
        # поэтому полный прогон всей истории инструмента не нужен (на H1 это
        # часы ожидания на клик ревью).
        start_ms = review_replay_start_ms(zone)
        Scanner(db, settings.detector).replay_instrument(
            zone.instrument_id, start_ms=start_ms, timeframes={zone.timeframe}
        )
        zone = db.get_zone(zone_id)
        assert zone is not None  # replay не удаляет зоны
        tf_candles = db.get_candles(zone.instrument_id, zone.timeframe)
        # состояние воспроизведено до закрытия последней свечи ТФ (§15.1.2)
        assessed_as_of = tf_candles[-1].close_time if tf_candles else now
        finished = _zone_finished(zone)
        geometry_verdict = "unknown"
        lifecycle_verdict: Optional[str] = None
        requires_clarification = False
        correction: Optional[BoundaryCorrection] = None

        if decision == "correct":
            geometry_verdict = "valid"
            if finished:
                # §15.5: верная геометрия, но жизненный цикл завершён —
                # статус не меняем, объект не возвращается в активные
                lifecycle_verdict = "completed"
            else:
                if zone.status == ZoneStatus.CANDIDATE:
                    db.update_zone(zone_id, status=ZoneStatus.ACTIVE)
                    if zone.confirmed_at is None:
                        # ТЗ 06.10.2026 §4 (T09): ручное одобрение геометрии
                        # не создаёт external_fvg/confirmed_at без
                        # доказательств — отмечаем, что подтверждение
                        # только ручное; расхождение остаётся явным
                        evidence = dict(zone.evidence)
                        evidence["manual_confirmation_only"] = True
                        _update_evidence(db, zone, evidence)
                # ZONE_CONFIRMED_BY_USER — только при correct на живой зоне
                db.insert_event(Event(
                    id=None, zone_id=zone_id, cycle_id=zone.cycle_id,
                    kind=EventKind.ZONE_CONFIRMED_BY_USER, occurred_at=now,
                    detected_at=now, price=zone.mid,
                    evidence={"author": "owner", "text": body.text},
                ))
        elif decision == "now_irrelevant":
            # §15.1.1: верная форма, но сейчас неактуальна — статус не меняем
            geometry_verdict = "valid"
            lifecycle_verdict = "completed"
        elif decision in ("wrong_type", "wrong_base", "wrong"):
            hint = _lifecycle_comment_hint(body.text)
            if hint:
                # ТЗ §10: lifecycle-замечание («не отработан», «уже
                # тестирован» и т.п.) — не ошибка геометрии: зона НЕ
                # отклоняется, geometry_verdict не invalid, заполняются
                # lifecycle_verdict и reason_code
                reason_code, lifecycle_verdict = hint
            else:
                geometry_verdict = "invalid"
                db.update_zone(zone_id, status=ZoneStatus.REJECTED)
        elif decision == "no_context":
            # нет контекста — невозможность ревью, а не ошибка геометрии (§15.4)
            requires_clarification = True
        elif decision == "already_breaker":
            geometry_verdict = "valid"
            lifecycle_verdict = "converted"
        elif decision == "fix_boundaries":
            if body.lower is None or body.upper is None:
                raise HTTPException(
                    status_code=400, detail="fix_boundaries требует lower и upper"
                )
            geometry_verdict = "needs_correction"
            _, _, correction = _apply_boundary_change(
                db, zone, lower=float(body.lower), upper=float(body.upper),
                text=body.text or "Исправление границ кандидата",
                decision=body.decision,
                anchor_candle_open_time=body.anchor_candle_open_time,
            )

        # review — как прежде (decision сохраняем как нажато, история не теряется)
        if correction is None:
            review_id = _add_review(db, zone_id, body.decision, body.text)
        else:
            review_id = db.get_reviews(zone_id)[-1].id

        # ТЗ 07.10.2026 §9/§11 (T13): ручное исключение из новых входов по
        # комментарию («глубокий/множественный тест») — market_validity НЕ
        # трогаем: рыночное завершение подтверждается только свечами,
        # выдуманного close_beyond нет. Оценка владельца не сбрасывает
        # историю тестов/пробоев (T17)
        if reason_code in _MANUAL_ENTRY_EXCLUSION_REASONS:
            cur = db.get_zone(zone_id)
            updates: dict = {}
            if cur.entry_eligible:
                updates["entry_eligible"] = False
            evidence = dict(cur.evidence)
            evidence["manual_entry_exclusion"] = reason_code
            evidence["manual_entry_exclusion_review_id"] = review_id
            evidence["manual_entry_exclusion_comment"] = body.text or ""
            _update_evidence(db, cur, evidence)
            if updates:
                db.update_zone(zone_id, **updates)
        # review_assessment — раздельная оценка (§12, §15.3); текст не дублируем
        assessment = ReviewAssessment(
            id=None, zone_id=zone_id, review_id=review_id,
            review_decision=decision, geometry_verdict=geometry_verdict,
            lifecycle_verdict=lifecycle_verdict, reason_code=reason_code,
            evidence_source=body.evidence_source,
            assessed_as_of=assessed_as_of, reviewed_at=now,
            requires_clarification=requires_clarification,
        )
        assessment.id = db.add_assessment(assessment)

        updated = db.get_zone(zone_id)
        # разметка для ИИ-анализа правил §14 — отдельным файлом labels.jsonl (v2)
        labels_file = export_label(db, updated, body.decision, body.text or "",
                                   settings, assessment=assessment,
                                   correction=correction)
        hub.broadcast({"type": "zone", "action": "reviewed", "zone": zone_to_dict(updated)})
        return {"zone": zone_to_dict(updated),
                "labels_file": labels_file,
                "assessment": assessment_to_dict(assessment),
                "boundary_correction": (
                    boundary_correction_to_dict(correction) if correction else None
                ),
                "reviews": [review_to_dict(r) for r in db.get_reviews(zone_id)]}

    @app.post("/api/zones/{zone_id}/prefer-entry", dependencies=[Depends(require_auth)])
    def prefer_entry(zone_id: int, body: PreferEntryIn) -> dict[str, Any]:
        """ТЗ 07.10.2026 §7 (T10): предпочтительная рабочая зона для входа.

        Ссылка preferred_entry_zone/supersedes_for_entry — не удаление
        родителя: обе зоны сохраняют независимые lifecycle и историю тестов
        (эталон ETH №342 предпочтительна, широкая №340 — связанная
        историческая/контекстная). Глубины считаются от собственных W каждой
        зоны (T11). Автоматического ранжирования «самая узкая/поздняя» нет.
        """
        zone = db.get_zone(zone_id)
        if zone is None:
            raise HTTPException(status_code=404, detail="Зона не найдена")
        evidence = dict(zone.evidence)
        old_superseded = evidence.get("supersedes_for_entry")
        if body.supersedes_zone_id is None:
            evidence.pop("preferred_for_entry", None)
            evidence.pop("supersedes_for_entry", None)
            evidence.pop("entry_preference_comment", None)
        else:
            other = db.get_zone(body.supersedes_zone_id)
            if other is None:
                raise HTTPException(status_code=404,
                                    detail="Замещаемая зона не найдена")
            if other.id == zone.id:
                raise HTTPException(status_code=400,
                                    detail="Зона не может замещать саму себя")
            evidence["preferred_for_entry"] = True
            evidence["supersedes_for_entry"] = other.id
            if body.comment:
                evidence["entry_preference_comment"] = body.comment
            # обратная пометка на замещаемой зоне (родитель не удаляется)
            other_ev = dict(other.evidence)
            other_ev["superseded_for_entry_by"] = zone.id
            _update_evidence(db, other, other_ev)
        _update_evidence(db, zone, evidence)
        # снятие обратной пометки с прежней замещаемой зоны
        if body.supersedes_zone_id is None and old_superseded is not None:
            prev = db.get_zone(old_superseded)
            if prev is not None and \
                    prev.evidence.get("superseded_for_entry_by") == zone.id:
                prev_ev = dict(prev.evidence)
                prev_ev.pop("superseded_for_entry_by", None)
                _update_evidence(db, prev, prev_ev)
        updated = db.get_zone(zone_id)
        hub.broadcast({"type": "zone", "action": "updated",
                       "zone": zone_to_dict(updated)})
        return zone_to_dict(updated)

    @app.get("/api/candidates", dependencies=[Depends(require_auth)])
    def list_candidates(instrument_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Очередь ручной проверки (§10): кандидаты без ревью, с объяснением
        обнаружения из evidence. Проверенные зоны (решение зафиксировано
        в review) из очереди уходят, даже если статус остался candidate."""
        out = []
        for z in db.get_unreviewed_candidates(instrument_id=instrument_id):
            ins = db.get_instrument(z.instrument_id)
            out.append({
                **zone_to_dict(z),
                "instrument": instrument_to_dict(ins) if ins else None,
                "explanation": z.evidence,  # объяснение обнаружения (§13)
                # ТЗ 06.10.2026 §13: отсутствие подтверждения — явный статус
                # с причиной, а не неконкретное «ожидание»
                "unconfirmed_reason": _unconfirmed_reason(z),
            })
        return out

    # ------------------------- events / candles -------------------------

    @app.get("/api/events", dependencies=[Depends(require_auth)])
    def list_events(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        out = []
        for e in db.get_events(limit=limit):
            zone = db.get_zone(e.zone_id)
            ins = db.get_instrument(zone.instrument_id) if zone else None
            out.append({
                **event_to_dict(e),
                "zone": zone_to_dict(zone) if zone else None,
                "instrument": instrument_to_dict(ins) if ins else None,
            })
        return out

    @app.get("/api/journal", dependencies=[Depends(require_auth)])
    def journal(
        kind: str = Query(default="all"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """Единая хронология раздела «Журнал» (план ребрендинга §6.D):
        рынок (HTF+LTF события) / решения пользователя / доставка,
        смешанные по времени, свежие первыми. Пагинации нет — limit."""
        if kind not in JOURNAL_KINDS:
            raise HTTPException(
                status_code=400,
                detail="kind: all | market | decisions | delivery",
            )
        return collect_journal(db, kind=kind, limit=limit)

    @app.get("/api/candles", dependencies=[Depends(require_auth)])
    def list_candles(
        instrument_id: int,
        timeframe: str,
        limit: int = Query(default=300, ge=1, le=5000),
    ) -> list[dict[str, Any]]:
        """Свечи для графика в формате lightweight-charts (time — unix sec)."""
        candles = db.get_candles(
            instrument_id, timeframe, closed_only=False)[-limit:]
        return [
            {"time": c.open_time // 1000, "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "closed": c.closed}
            for c in candles
        ]

    # ------------------------- настройки (§10) -------------------------

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    def get_settings() -> dict[str, Any]:
        """DetectorConfig; uncalibrated-поля помечены отдельным списком (§14).
        Секреты процесса сюда не попадают (§11 п.8).
        L04: groups — представление/доставка/анализ/эксперимент/deprecated;
        устаревшие поля отдаются (миграционная совместимость), но не
        редактируются."""
        return {
            "detector": asdict(settings.detector),
            "uncalibrated": [
                f.name for f in fields(DetectorConfig) if f.name.startswith("uncalibrated_")
            ],
            "groups": {
                f.name: DETECTOR_FIELD_GROUPS.get(f.name, "analysis")
                for f in fields(DetectorConfig)
            },
            "deprecated": sorted(DETECTOR_DEPRECATED_FIELDS),
            # A05: статус последнего задания пересчёта (или null, если не было)
            "recalc": _get_recalc_status(db),
            # Признак без секрета: токен и chat id в ответ не попадают (§11 п.8)
            "telegram_configured": bool(
                settings.telegram_token and settings.telegram_chat_id),
        }

    @app.post("/api/settings", dependencies=[Depends(require_auth)])
    async def save_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """L04: строгая схема и атомарное применение. Любая ошибка (неизвестное
        или устаревшее поле, тип, диапазон, перечисление, зависимость порогов)
        — 422 с ошибками по полям; память и файл настроек не меняются.

        A05: порядок применения — валидация → атомарная запись файла → память →
        пересчёт (tracked-задание, meta settings:recalc). Сбой записи файла —
        500, память не тронута; сбой пересчёта — откат памяти и файла к прежнему
        конфигу, задание помечается failed, ответ 500."""
        values, errors = validate_detector_payload(payload)
        if not errors:
            candidate = replace(settings.detector, **values)
            errors = validate_detector_config(candidate)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_settings", "fields": errors},
            )
        # файл — первым: при ошибке записи память ещё не изменена
        try:
            path = _save_detector_to_file(settings, candidate)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "settings_save_failed", "reason": str(exc)},
            )
        # DetectorConfig разделён по ссылке с движком/воркером — применяем
        # по полям, чтобы объект оставался тем же
        old_values = {key: getattr(settings.detector, key) for key in values}
        for key, value in values.items():
            setattr(settings.detector, key, value)
        applied = sorted(values)
        # смена профиля l/r структурных pivots: новая версия расчёта (L03),
        # прежние опоры помечаются superseded и сохраняются (движок делит
        # DetectorConfig с настройками и уже видит новые значения)
        need_resync = ltf_engine is not None and (
            settings.detector.ltf_structure_left != old_values.get(
                "ltf_structure_left", settings.detector.ltf_structure_left)
            or settings.detector.ltf_structure_right != old_values.get(
                "ltf_structure_right", settings.detector.ltf_structure_right)
        )
        # ТЗ «LTF Current Setup» §10/п.14: смена ltf_entry_types — лёгкий
        # пересчёт привязок активных сценариев (без replay свечей и без
        # новых событий/уведомлений; история строк сохраняется)
        need_reclassify = ltf_engine is not None and (
            settings.detector.ltf_entry_types != old_values.get(
                "ltf_entry_types", settings.detector.ltf_entry_types)
        )
        if not (need_resync or need_reclassify):
            return {"applied": applied, "saved_to": str(path),
                    "structure_resynced": False,
                    "entries_reclassified": {},
                    "detector": asdict(settings.detector)}

        def _run_recalc() -> dict[str, Any]:
            result: dict[str, Any] = {"structure_resynced": False,
                                      "entries_reclassified": {}}
            if need_resync:
                ltf_engine.resync_structure_params()
                result["structure_resynced"] = True
            if need_reclassify:
                result["entries_reclassified"] = ltf_engine.reclassify_active_entries()
            return result

        started = now_ms()
        _set_recalc_status(db, "running", started_at=started)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_recalc), timeout=_RECALC_WAIT_SECONDS)
        except asyncio.TimeoutError:
            # длинный пересчёт: задание живёт дальше в потоке, статус —
            # running в meta и виден через GET /api/settings
            return {"applied": applied, "saved_to": str(path),
                    "structure_resynced": need_resync,
                    "entries_reclassified": {},
                    "recalc": {"status": "running"},
                    "detector": asdict(settings.detector)}
        except Exception as exc:
            # откат: память — к прежним значениям, файл — к прежнему конфигу
            for key, value in old_values.items():
                setattr(settings.detector, key, value)
            try:
                _save_detector_to_file(settings)
            except OSError:
                logger.warning("откат settings.json не записался", exc_info=True)
            _set_recalc_status(db, "failed", started_at=started,
                               finished_at=now_ms(), error=str(exc))
            raise HTTPException(
                status_code=500,
                detail={"error": "settings_recalc_failed", "reason": str(exc)},
            )
        _set_recalc_status(db, "ready", started_at=started,
                           finished_at=now_ms(), result=result)
        return {"applied": applied, "saved_to": str(path),
                "structure_resynced": result["structure_resynced"],
                "entries_reclassified": result["entries_reclassified"],
                "recalc": {"status": "ready"},
                "detector": asdict(settings.detector)}

    @app.get("/api/labels", dependencies=[Depends(require_auth)])
    def labels() -> dict[str, Any]:
        """Русские формулировки для UI — тот же источник, что у Telegram
        и снимков зон (app/texts_ru.py), чтобы тексты не расходились."""
        from ..texts_ru import (
            DIRECTION_RU,
            KIND_RU,
            LTF_CANCELLATION_RU,
            LTF_EVENT_KIND_RU,
            LTF_OBSERVATION_STATE_RU,
            LTF_REVIEW_DECISION_RU,
            LTF_REVIEW_REASON_RU,
            LTF_SCENARIO_STATE_RU,
            LTF_TYPE_RU,
            STATUS_RU,
            TYPE_RU,
        )

        return {
            "event_kinds": {k.value: v for k, v in KIND_RU.items()},
            "types": TYPE_RU,
            "directions": DIRECTION_RU,
            "statuses": STATUS_RU,
            # окно LTF (LTF-спека §3)
            "ltf_event_kinds": LTF_EVENT_KIND_RU,
            "ltf_entry_types": LTF_TYPE_RU,
            "ltf_observation_states": LTF_OBSERVATION_STATE_RU,
            "ltf_scenario_states": LTF_SCENARIO_STATE_RU,
            "ltf_cancellation_reasons": LTF_CANCELLATION_RU,
            # разметка Entry Zones
            "ltf_review_decisions": LTF_REVIEW_DECISION_RU,
            "ltf_review_reasons": LTF_REVIEW_REASON_RU,
        }

    @app.get("/api/export/labels", dependencies=[Depends(require_auth)])
    def download_labels() -> FileResponse:
        """Скачивание разметки проверки (labels.jsonl, §13/§15) одним файлом —
        та же append-only запись, что пишет export_label() при каждом ревью."""
        path = Path(settings.db_path).parent / "labels.jsonl"
        if not path.exists():
            raise HTTPException(status_code=404,
                                detail="Проверенных решений пока нет")
        return FileResponse(path, media_type="application/x-ndjson",
                            filename="labels.jsonl")

    @app.get("/api/export/reviews", dependencies=[Depends(require_auth)])
    def export_reviews() -> Response:
        """Полная выгрузка данных проверок одним JSON: разметка labels.jsonl
        (богатые снимки зон с OHLC, если файл существует) + все записи БД —
        review, review_assessment, boundary_correction. Разметка живёт только
        в labels.jsonl, а решения — в БД; выгружаем оба источника, чтобы
        ничего не потерять. Пустые списки — валидный ответ (200)."""
        labels: list[Any] = []
        labels_path = Path(settings.db_path).parent / "labels.jsonl"
        if labels_path.exists():
            for line in labels_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    labels.append(json.loads(line))
        reviews = db.get_reviews()
        zone_ids = {r.zone_id for r in reviews}
        payload = {
            "format": "htf-review-export",
            "version": 1,
            "exported_at": now_ms(),
            "labels": labels,
            "reviews": [review_to_dict(r) for r in reviews],
            "review_assessments": [
                assessment_to_dict(a)
                for zid in sorted(zone_ids) for a in db.get_assessments(zid)
            ],
            "boundary_corrections": [
                boundary_correction_to_dict(c)
                for zid in sorted(zone_ids)
                for c in db.get_boundary_corrections(zid)
            ],
            # ТЗ 06.10.2026 §11: нормализация отдельным слоем — latest-view
            # по reviewed_at и сохранённые конфликты; сырые массивы выше
            # неизменны (R13)
            "normalized": _export_normalized_layer(db, reviews),
        }
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="reviews.json"'},
        )

    # ------------------------- health (§11: для деплоя, открыт) -------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        now = now_ms()
        # свежесть — только по включённым ТФ поиска (scan_timeframes)
        scan_tfs = {
            t.strip() for t in settings.detector.scan_timeframes.split(",")
            if t.strip() in TIMEFRAME_MINUTES
        } or set(TIMEFRAME_MINUTES)
        freshness = []
        for ins in db.get_instruments(enabled_only=True):
            for tf, minutes in TIMEFRAME_MINUTES.items():
                if tf not in scan_tfs:
                    continue
                candle = db.last_candle(ins.id, tf)
                if candle is None:
                    continue
                age_ms = now - candle.close_time
                freshness.append({
                    "instrument_id": ins.id,
                    "symbol": ins.symbol,
                    "timeframe": tf,
                    "last_open_time": candle.open_time,
                    "age_ms": age_ms,
                    # устарело: последняя закрытая свеча старше двух периодов ТФ
                    "stale": age_ms > 2 * minutes * 60_000,
                })
        active = len(db.get_zones(statuses=[ZoneStatus.ACTIVE]))
        return {
            "time": now,
            "version": APP_VERSION,
            "active_zones": active,
            "candle_freshness": freshness,
        }

    # ------------------------- WebSocket -------------------------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        if not await check_ws_token(websocket, settings):
            return
        await hub.connect(websocket)
        try:
            while True:
                # входящие сообщения не используются — соединение держим живым
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    # ------------------------- статика -------------------------

    app.mount("/", _NoCacheStaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app


# ---------------------------------------------------------------------------
# Внутренние помощники
# ---------------------------------------------------------------------------

def _parse_enum_list(raw: Optional[str], enum_cls, name: str) -> Optional[list]:
    """Парсит query-параметр вида ``active,weakened`` в список enum-значений."""
    if not raw:
        return None
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(enum_cls(part))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Недопустимое значение {name}: {part}")
    return values or None


def _zone_finished(zone: Zone) -> bool:
    """§15.1.1: зона завершена — заполнен display_until либо
    терминальный статус. Ревью такой зоны не возвращает её в активные."""
    return zone.display_until is not None or zone.status in _TERMINAL_STATUSES


# ТЗ «Единый движок» §10: lifecycle-замечания в свободном комментарии —
# это НЕ ошибка геометрии. Ключевые фразы → (reason_code, lifecycle_verdict).
# Порядок важен: отрицания («не отработан») проверяются раньше общих слов.
_LIFECYCLE_COMMENT_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("not_worked_out", "relevant", (
        "не отработан", "не отработана", "не отработано",
        "не является отработанным", "не является отработанной",
    )),
    # ТЗ 07.10.2026 §9/§11 (T13): глубокий/множественный тест — ручное
    # исключение из НОВЫХ входов с конкретной причиной, без выдуманного
    # close_beyond; непробитый OB сохраняет market_validity.
    # «множественный» проверяется раньше «глубокого» (№215: «Глубокий и
    # множественный тест»)
    ("repeated_test_manual_exclusion", "completed", (
        "множественный тест", "множественные тесты",
    )),
    ("deep_test_entry_excluded", "completed", (
        "протестирован", "глубокий тест", "90%", "90 %",
    )),
    ("already_tested", "tested", (
        "уже тестирован", "уже тестировал", "тестировался", "тестировалась",
        "был протестирован", "была протестирована", "частичный тест",
    )),
    ("already_completed", "completed", (
        "уже снят", "уже снята", "перекрыт", "перекрыта",
        "был пробит", "была пробита", "не актуал", "неактуал",
        "прошит", "прошита", "прошиты", "прошито",
        # ТЗ 06.10.2026 §11 (№551): «потерял актуальность в …» — lifecycle,
        # а не ошибка геометрии
        "потерял актуальность", "потеряла актуальность",
        "утратил актуальность", "утратила актуальность",
    )),
]

# Причины already_completed, означающие ручное исключение из новых входов
# (ТЗ 07.10.2026 §11: разбиение на close_beyond / deep_test_entry_excluded /
# repeated_test_manual_exclusion / unresolved)
_MANUAL_ENTRY_EXCLUSION_REASONS = {
    "deep_test_entry_excluded", "repeated_test_manual_exclusion",
}


def _lifecycle_comment_hint(text: str) -> Optional[tuple[str, str]]:
    """Распознаёт lifecycle-замечание в комментарии ревью.

    Возвращает (reason_code, lifecycle_verdict) либо None, если комментарий
    не про жизненный цикл."""
    low = (text or "").lower()
    for code, verdict, needles in _LIFECYCLE_COMMENT_RULES:
        if any(n in low for n in needles):
            return code, verdict
    return None


def _normalization_hint(
    decision: str, geometry_verdict: str, text: str,
) -> Optional[dict[str, Any]]:
    """Слой нормализации старого ошибочного паттерна (ТЗ §10): ранние
    lifecycle-замечания (review_id 131–134) писались как wrong_type +
    geometry_verdict=invalid. Для новой записи с таким слепком добавляем
    нормализованные вердикты отдельным объектом; исходные поля вердиктов
    и комментарий задним числом не переписываем (R13)."""
    if geometry_verdict != "invalid":
        return None
    if decision not in ("wrong_type", "wrong_base", "wrong", "rejected"):
        return None
    hint = _lifecycle_comment_hint(text)
    if hint is None:
        return None
    code, verdict = hint
    return {
        # ТЗ 06.10.2026 §11 (№551): вердикт/комментарий противоречат друг
        # другу — строка не используется как образец ошибки геометрии
        "semantic_conflict": True,
        "geometry_verdict": "unknown",
        "lifecycle_verdict": verdict,
        "reason_code": code,
        "explanation": (
            "lifecycle-замечание, ошибочно размеченное как неверная геометрия "
            "(старый паттерн: отклонение + geometry_verdict=invalid)"
        ),
    }


def _export_normalized_layer(db: Database, reviews: list[Review]) -> list[dict[str, Any]]:
    """ТЗ 06.10.2026 §11 (T16): latest-view оценок и журнал конфликтов.

    По каждой зоне — последняя оценка по reviewed_at (она действует), плюс:
    - verdict_changed: прошлые оценки с иными вердиктами (№130: удаление
      текста не доказывает отсутствие пробоя — конфликт сохраняется);
    - semantic_conflict: комментарий о потере актуальности при вердикте
      correct (не доказательство lifecycle-события, хранится для аудита).
    """
    by_zone: dict[int, list[Review]] = {}
    for r in reviews:
        by_zone.setdefault(r.zone_id, []).append(r)
    out: list[dict[str, Any]] = []
    for zid in sorted(by_zone):
        assessments = db.get_assessments(zid)
        if not assessments:
            continue
        latest = max(assessments, key=lambda a: (a.reviewed_at or 0, a.id or 0))
        conflicts: list[dict[str, Any]] = []
        for a in assessments:
            if a.id == latest.id:
                continue
            if (a.review_decision, a.geometry_verdict, a.lifecycle_verdict) != (
                latest.review_decision, latest.geometry_verdict,
                latest.lifecycle_verdict,
            ):
                conflicts.append({
                    "kind": "verdict_changed",
                    "assessment": assessment_to_dict(a),
                })
        for r in by_zone[zid]:
            if r.decision in ("correct", "confirmed") and \
                    _lifecycle_comment_hint(r.text or ""):
                conflicts.append({
                    "kind": "semantic_conflict",
                    "review": review_to_dict(r),
                    "note": "комментарий о потере актуальности при вердикте "
                            "correct — не доказательство пробоя, сохранён для аудита",
                })
        if conflicts or len(assessments) > 1:
            out.append({
                "zone_id": zid,
                "latest_assessment": assessment_to_dict(latest),
                "conflicts": conflicts,
            })
    return out


def _zone_brief(db: Database, zone_id: Optional[int]) -> Optional[dict[str, Any]]:
    if zone_id is None:
        return None
    zone = db.get_zone(zone_id)
    return zone_to_dict(zone) if zone else None


def _update_evidence(db: Database, zone: Zone, evidence: dict[str, Any]) -> None:
    """Запись evidence с сохранением source_candles (db.update_zone сериализует
    dict как есть, а source_candles живут внутри evidence-json)."""
    evidence["source_candles"] = zone.source_candles
    db.update_zone(zone.id, evidence=evidence)


def _apply_boundary_change(
    db: Database, zone: Zone, *, lower: float, upper: float, text: str,
    decision: str = "corrected", anchor_candle_open_time: Optional[int] = None,
) -> tuple[int, int, BoundaryCorrection]:
    """Изменение границ с версионированием (§10, §15.2): старые границы — в
    evidence.history, версия растёт, запись review с boundary_version и запись
    boundary_correction с точной свечой-якорем (R07/R08: приблизительное число
    не подменяет проверяемую поправку). История событий зоны не трогается.
    Возвращает (review_id, new_version, correction)."""
    if lower > upper:
        lower, upper = upper, lower
    reviews = db.get_reviews(zone.id)
    version = max([r.boundary_version for r in reviews], default=1) + 1
    now = now_ms()
    correction = BoundaryCorrection(
        id=None, zone_id=zone.id, boundary_version=version,
        original_lower=zone.lower, original_upper=zone.upper,
        corrected_lower=lower, corrected_upper=upper,
        anchor_candle_open_time=anchor_candle_open_time,
        reason=text, created_at=now,
    )
    correction.id = db.add_boundary_correction(correction)
    evidence = dict(zone.evidence)
    history = list(evidence.get("history", []))
    history.append({
        "lower": zone.lower,
        "upper": zone.upper,
        "replaced_by_version": version,
        "changed_at": now,
    })
    evidence["history"] = history
    evidence["boundary_version"] = version
    _update_evidence(db, zone, evidence)
    db.update_zone(zone.id, lower=lower, upper=upper)
    review = Review(
        id=None, zone_id=zone.id, decision=decision, text=text,
        boundary_version=version, created_at=now,
    )
    return db.add_review(review), version, correction


def _add_review(db: Database, zone_id: int, decision: str, text: str) -> int:
    review = Review(
        id=None, zone_id=zone_id, decision=decision, text=text,
        boundary_version=1, created_at=now_ms(),
    )
    return db.add_review(review)


def create_app_from_env() -> FastAPI:
    """Фабрика для uvicorn из командной строки:
    ``uvicorn --factory app.web.api:create_app_from_env --host 127.0.0.1 --port 8000``"""
    settings = load_settings()
    db = Database(settings.db_path)
    return create_app(db, settings)
