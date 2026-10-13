"""Владелец расчёта, отпечаток профиля и диагностика без секретов (F35–F37)."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from typing import Any, Optional

from ..db import SCHEMA_VERSION
from ..models import now_ms
from ..adapters.base import TIMEFRAME_MS
from .htf_parent import parent_decision, policy_types
from .overview import _cursors, _parent_zones

CALC_OWNER = "runtime:calc_owner"
TELEGRAM_OWNER = "runtime:telegram_owner"
TELEGRAM_CONFLICT = "runtime:telegram_conflict"
LAST_ERROR = "runtime:last_error"


class Readiness:
    """Фазы готовности сервиса (этап 5, спека §11): web → realtime → history → calc.

    web — процесс поднят и отвечает (воркер прошёл ownership);
    realtime — запущены очереди и цикл котировок/WS (котировки текут,
    /api/ready.ready=true начинается здесь — история и расчёт продолжаются
    в фоне); history — первичный проход backfill завершён; calc — очередь
    расчётов дочитана после history. In-memory: в одном процессе живут и
    web, и воркер; дешёвый снимок для healthcheck Railway (/api/ready).
    """

    PHASES = ("web", "realtime", "history", "calc")
    READY_PHASE = "realtime"

    def __init__(self, clock=now_ms) -> None:
        self._clock = clock
        self.since = clock()
        self._reached: dict[str, dict[str, Any]] = {}

    def reach(self, phase: str, detail: Optional[str] = None) -> None:
        if phase not in self.PHASES:
            raise ValueError(f"unknown readiness phase: {phase!r}")
        if phase in self._reached:
            return  # идемпотентно: фаза фиксируется один раз
        self._reached[phase] = {"at": self._clock(), "detail": detail}

    @property
    def phase(self) -> str:
        reached = [p for p in self.PHASES if p in self._reached]
        return reached[-1] if reached else "starting"

    @property
    def ready(self) -> bool:
        return self.READY_PHASE in self._reached

    def snapshot(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "phase": self.phase,
            "phases": {p: self._reached.get(p) for p in self.PHASES},
            "since": self.since,
        }


def backfill_progress(db, scan_tfs) -> dict[str, Any]:
    """Прогресс фоновой догрузки истории (этап 5, §11) по чекпоинтам
    backfill:checkpoint:{iid}:{tf}: на канал — pending (чекпоинта нет),
    running (частично покрыт) или completed. Каналы без свечей и без
    чекпоинта считаются pending — на пустой БД индикаторы видны сразу."""
    channels: list[dict[str, Any]] = []
    completed = 0
    for ins in db.get_instruments(enabled_only=True):
        for tf in scan_tfs:
            raw = db.get_meta(f"backfill:checkpoint:{ins.id}:{tf}")
            cp: dict[str, Any] = {}
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        cp = data
                except json.JSONDecodeError:
                    cp = {}
            status = (
                "completed" if cp.get("completed")
                else "running" if cp
                else "pending"
            )
            completed += status == "completed"
            channels.append({
                "instrument_id": ins.id,
                "symbol": ins.symbol,
                "timeframe": tf,
                "status": status,
                "done_through_ms": cp.get("done_through_ms"),
                "updated_at": cp.get("updated_at"),
            })
    return {
        "total": len(channels),
        "completed": completed,
        "channels": channels,
    }


def profile_fingerprint(cfg) -> str:
    from dataclasses import asdict
    payload = asdict(cfg)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not proof of death
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    except SystemError:
        # Windows: os.kill(0) по освободившемуся pid может вернуть
        # SystemError вместо OSError — такой владелец тоже мёртв.
        return False
    return True


def _read_claim(db, key: str) -> Optional[dict[str, Any]]:
    raw = db.get_meta(key)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def claim_owner(db, key: str, identity: dict[str, Any], *, lease_ms: int = 120_000) -> dict[str, Any]:
    """Atomic lease claim. PID is diagnostic only, never the ownership key."""
    if not hasattr(db, "runtime_instance_id"):
        db.runtime_instance_id = uuid.uuid4().hex
    identity = {"host": socket.gethostname(), **identity,
                "instance_id": identity.get("instance_id") or db.runtime_instance_id}
    with db.batch_writes():
        current = _read_claim(db, key)
        stamp = now_ms()
        if current and current.get("instance_id") != identity["instance_id"] and current.get("expires_at", 0) > stamp:
            conflict = {"owned": False, "owner": current, "contender": identity}
            db.set_meta(key + ":conflict", json.dumps(conflict))
            return conflict
        generation = int((current or {}).get("owner_generation", 0))
        same = current and current.get("instance_id") == identity["instance_id"] and current.get("expires_at", 0) > stamp
        body = {**identity, "owner_generation": generation if same else generation + 1,
                "claimed_at": current["claimed_at"] if same else stamp,
                "heartbeat_at": stamp, "expires_at": stamp + lease_ms}
        db.set_meta(key, json.dumps(body))
        db.set_meta(key + ":conflict", "")
        return {"owned": True, "owner": body}


def assert_owner(db, key, claim, *, check_expiry=True):
    current = _read_claim(db, key) or {}
    if (current.get("instance_id") != claim.get("instance_id") or
            current.get("owner_generation") != claim.get("owner_generation") or
            (check_expiry and current.get("expires_at", 0) <= now_ms())):
        raise RuntimeError(f"owner_lease_lost:{key}")


def renew_owner(db, key, claim, *, lease_ms=120_000):
    with db.batch_writes():
        assert_owner(db, key, claim, check_expiry=False)
        stamp = now_ms()
        current = _read_claim(db, key)
        current.update(heartbeat_at=stamp, expires_at=stamp + lease_ms)
        db.set_meta(key, json.dumps(current))
        return current


def release_owner(db, key, claim):
    with db.batch_writes():
        try:
            assert_owner(db, key, claim, check_expiry=False)
        except RuntimeError:
            return
        current = _read_claim(db, key)
        current["expires_at"] = 0
        db.set_meta(key, json.dumps(current))


async def lease_heartbeat(db, key, claim, stop):
    import asyncio
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), 20)
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(renew_owner, db, key, claim)
            except Exception as exc:
                import sqlite3
                if isinstance(exc,sqlite3.OperationalError) and "locked" in str(exc):
                    continue  # an active unit of work still fences takeover
                raise


def record_telegram_conflict(db, message: str) -> None:
    db.set_meta(TELEGRAM_CONFLICT, json.dumps({
        "at": now_ms(),
        "message": message[:500],
    }, ensure_ascii=False))


def _parent_counts(db, instrument_id: int, cfg) -> dict[str, int]:
    counts: dict[str, int] = {}
    for zone in _parent_zones(db, instrument_id):
        reason = parent_decision(zone, cfg).reason
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _market_queue_diag(db, market_queue) -> dict[str, Any]:
    """Состояние очереди котировок (этап 2, §8): degraded-флаги и счётчики
    spool'а — из БД (видны и из web-роли), глубины in-memory очередей —
    из calc-процесса, когда очередь передана."""
    degraded: dict[str, Any] = {}
    for key, raw in db.meta_prefix("quote:degraded:").items():
        try:
            degraded[key.rsplit(":", 1)[1]] = json.loads(raw)
        except (ValueError, TypeError):
            degraded[key.rsplit(":", 1)[1]] = {"raw": raw[:200]}
    section: dict[str, Any] = {
        "degraded": degraded,
        "spool": {str(k): v for k, v in db.spool_counts().items()},
    }
    if market_queue is not None:
        section["queues"] = market_queue.diagnostics()
    return section


def _calc_queue_diag(db, calc_queue) -> dict[str, Any]:
    """Состояние очереди расчётов (этап 4, §9): persisted-строки calc_task
    (в т.ч. последняя failed с ошибкой — без секретов) видны и из web-роли;
    глубины/счётчики in-memory — из calc-процесса, когда очередь передана."""
    counts: dict[str, int] = {"pending": 0, "running": 0, "failed": 0}
    for r in db.conn.execute(
        "SELECT status, COUNT(*) AS n FROM calc_task GROUP BY status"
    ).fetchall():
        counts[r["status"]] = int(r["n"])
    row = db.conn.execute(
        "SELECT instrument_id, timeframe, task_kind, error, updated_at"
        " FROM calc_task WHERE status='failed'"
        " ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    section: dict[str, Any] = {
        "persisted": counts,
        "last_failed": (
            {
                "instrument_id": int(row["instrument_id"]),
                "timeframe": row["timeframe"],
                "task_kind": row["task_kind"],
                "error": row["error"],
                "updated_at": int(row["updated_at"]),
            }
            if row is not None else None
        ),
    }
    if calc_queue is not None:
        section["queue"] = calc_queue.diagnostics()
    return section


def _pipeline_diag(db, settings, calc_queue=None, realtime=None) -> dict[str, Any]:
    """Курсоры конвейера по каналам (этап 6a, §13): на (iid, tf) — received
    (последняя вставленная свеча, включая формирующую), processed
    (data_sync:processed:{iid}:{tf}), ожидание старейшего pending-задания
    calc_queue по каналу; на инструмент — возраст котировки из quote-meta.
    Плюс агрегат транспорта realtime, когда поток включён. Секретов нет."""
    cfg = settings.detector
    scan_tfs = tuple(
        t.strip()
        for t in str(getattr(cfg, "scan_timeframes", "")).split(",")
        if t.strip() in TIMEFRAME_MS
    ) or ("D1", "W1")
    waits: dict[str, float] = {}
    if calc_queue is not None:
        try:
            waits = calc_queue.diagnostics().get("pending_oldest_wait_s", {})
        except Exception:
            waits = {}
    now = now_ms()
    channels: list[dict[str, Any]] = []
    quotes: list[dict[str, Any]] = []
    for ins in db.get_instruments(enabled_only=True):
        detail = db.get_quote_detail(ins.id)
        basis = None
        if detail is not None:
            basis = (
                detail["event_at"] if detail["event_at"] is not None
                else detail["ts"]
            )
        quotes.append({
            "instrument_id": ins.id,
            "symbol": ins.symbol,
            "quote_at": basis,
            "quote_age_ms": (now - basis) if basis is not None else None,
        })
        for tf in dict.fromkeys((*scan_tfs, "H1")):
            head = db.last_candle(ins.id, tf, closed_only=False)
            processed = int(
                db.get_meta(f"data_sync:processed:{ins.id}:{tf}") or 0
            )
            channels.append({
                "instrument_id": ins.id,
                "symbol": ins.symbol,
                "timeframe": tf,
                "received": head.close_time if head is not None else None,
                "processed": processed or None,
                "calc_pending_wait_s": waits.get(f"{ins.id}:{tf}"),
            })
    section: dict[str, Any] = {"channels": channels, "quotes": quotes}
    if realtime is not None:
        links = realtime.status().get("instruments", {})
        section["transport"] = {
            "enabled": True,
            "connected": sum(1 for link in links.values() if link.get("connected")),
            "total": len(links),
            "reconnects": sum(
                int(link.get("reconnects") or 0) for link in links.values()
            ),
        }
    return section


def build_diagnostics(db, settings, *, role: str, instance_id: str,
                      started_at: int, build_id: str,
                      market_queue=None, calc_queue=None,
                      readiness=None, realtime=None) -> dict[str, Any]:
    """Защищённый снимок runtime. Токены и секреты не включаются."""
    cfg = settings.detector
    instruments = []
    for ins in db.get_instruments():
        last = db.last_candle(ins.id, "H1")
        cursors = _cursors(db, ins.id)
        instruments.append({
            "id": ins.id,
            "symbol": ins.symbol,
            "venue": ins.venue,
            "market_type": ins.market_type,
            "enabled": bool(ins.enabled),
            "ltf_analyze": bool(ins.ltf_analyze),
            "last_h1": last.close_time if last is not None else None,
            "structure_last_processed_h1": cursors["structure_last_processed_h1"],
            "scenario_last_processed_h1": cursors["scenario_last_processed_h1"],
            "cursor": cursors["cursor"],
            "allowed_types": sorted(policy_types(cfg)),
            "parents_by_reason": _parent_counts(db, ins.id, cfg),
            "replaying": db.get_meta(f"replaying:{ins.id}") == "1",
            "last_error": db.get_meta(f"runtime:last_error:{ins.id}"),
        })
    calc = _read_claim(db, CALC_OWNER)
    telegram = _read_claim(db, TELEGRAM_OWNER)
    conflict_raw = db.get_meta(TELEGRAM_CONFLICT)
    try:
        telegram_conflict = json.loads(conflict_raw) if conflict_raw else None
    except json.JSONDecodeError:
        telegram_conflict = {"message": "unreadable"}
    calc_conflict_raw = db.get_meta(CALC_OWNER + ":conflict")
    try:
        calc_conflict = json.loads(calc_conflict_raw) if calc_conflict_raw else None
    except json.JSONDecodeError:
        calc_conflict = None
    # Этап 5 (§11): фазы готовности и прогресс фонового backfill — тёплое
    # состояние UI видно из диагностики (в т.ч. из web-роли, по чекпоинтам)
    scan_tfs = tuple(
        t.strip()
        for t in str(getattr(cfg, "scan_timeframes", "")).split(",")
        if t.strip() in TIMEFRAME_MS
    ) or ("D1", "W1")
    return {
        "build_id": build_id,
        "started_at": started_at,
        "instance_id": instance_id,
        "process_role": role,
        "pid": os.getpid(),
        "schema_version": SCHEMA_VERSION,
        "storage_contract_version": 2,
        "rulesets": __import__("app.services.rulesets", fromlist=["registry"]).registry(settings),
        "config_version": int(db.get_meta("settings:config_version") or 0),
        "config_error": getattr(settings,"config_error",None),
        "calc_heartbeat": db.get_meta("runtime:calc_heartbeat"),
        "profile_fingerprint": profile_fingerprint(cfg),
        "allowed_context_types": sorted(policy_types(cfg)),
        "calc_owner": calc,
        "calc_conflict": calc_conflict,
        "telegram_owner": telegram,
        "telegram_conflict": telegram_conflict,
        "last_error": db.get_meta(LAST_ERROR),
        "state_version": db.get_state_seq(),
        "market_queue": _market_queue_diag(db, market_queue),
        "calc_queue": _calc_queue_diag(db, calc_queue),
        "pipeline": _pipeline_diag(db, settings, calc_queue=calc_queue,
                                   realtime=realtime),
        "readiness": readiness.snapshot() if readiness is not None else None,
        "backfill": backfill_progress(db, scan_tfs),
        "instruments": instruments,
    }
