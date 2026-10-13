"""Tracked configuration jobs with an atomic database profile and durable result."""
from __future__ import annotations
import asyncio
import copy
import json
import os
import uuid
from dataclasses import asdict, fields, replace
from pathlib import Path
from ..config import DetectorConfig, validate_detector_config, validate_detector_payload
from ..models import now_ms


class SettingsConflict(ValueError):
    pass


class InvalidSettings(ValueError):
    def __init__(self, errors):
        self.errors = errors


class SettingsJobs:
    def __init__(self, app, db, settings, engine, save_file):
        self.app, self.db, self.settings, self.engine = app, db, settings, engine
        self.save_file = save_file
        self.lock = asyncio.Lock()
        self.task = None
        active = db.get_meta("settings:active_profile")
        if active:
            values = json.loads(active)
            candidate = DetectorConfig(**values)
            errors = validate_detector_config(candidate)
            if errors:
                raise InvalidSettings(errors)
            self._install(candidate)
        else:
            db.set_meta("settings:active_profile", json.dumps(asdict(settings.detector)))
        old = self.status()
        from .runtime import _read_claim
        lease = _read_claim(db,"settings:owner") or {}
        if old and old.get("status") in {"queued", "running"} and lease.get("expires_at",0)<=now_ms():
            old.update(status="failed", finished_at=now_ms(), error="interrupted_by_restart")
            db.set_meta("settings:recalc", json.dumps(old))

    @property
    def version(self):
        return int(self.db.get_meta("settings:config_version") or 0)

    def status(self):
        raw = self.db.get_meta("settings:recalc")
        return json.loads(raw) if raw else None

    def _install(self, candidate):
        for field in fields(candidate):
            setattr(self.settings.detector, field.name, getattr(candidate, field.name))

    async def submit(self, payload):
        async with self.lock:
            payload = dict(payload)
            expected = payload.pop("expected_config_version", None)
            if expected is not None and type(expected) is not int:
                raise InvalidSettings({"expected_config_version":"Нужен целый номер версии"})
            if expected is not None and expected != self.version:
                raise SettingsConflict("config_version_changed")
            if self.task is not None and not self.task.done():
                raise SettingsConflict("settings_job_running")
            values, errors = validate_detector_payload(payload)
            candidate = replace(self.settings.detector, **values)
            errors.update(validate_detector_config(candidate))
            if errors:
                raise InvalidSettings(errors)
            from .runtime import claim_owner
            claim = await asyncio.to_thread(claim_owner,self.db,"settings:owner",
                {"instance_id":getattr(self.app.state,"instance_id",None),"role":"settings"})
            if not claim["owned"]:
                raise SettingsConflict("settings_job_running")
            from .runtime import _read_claim, CALC_OWNER, release_owner
            active_calc = _read_claim(self.db,CALC_OWNER) or {}
            if active_calc.get("expires_at",0)>now_ms() and active_calc.get("instance_id") != getattr(self.app.state,"instance_id",None):
                await asyncio.to_thread(release_owner,self.db,"settings:owner",claim["owner"])
                raise SettingsConflict("calc_owner_elsewhere")
            if expected is not None and expected != self.version:
                await asyncio.to_thread(release_owner,self.db,"settings:owner",claim["owner"])
                raise SettingsConflict("config_version_changed")
            base = DetectorConfig(**json.loads(self.db.get_meta("settings:active_profile")))
            candidate = replace(base, **values)
            errors = validate_detector_config(candidate)
            if errors:
                await asyncio.to_thread(release_owner,self.db,"settings:owner",claim["owner"])
                raise InvalidSettings(errors)
            job = {"job_id": uuid.uuid4().hex, "status": "queued", "started_at": now_ms(),
                   "finished_at": None, "requested_config_version": self.version + 1,
                   "effective_config_version": self.version, "error": None}
            await asyncio.to_thread(self.db.set_meta, "settings:recalc", json.dumps(job))
            self.task = asyncio.create_task(self._run(candidate, values, job,claim["owner"]))
            self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            return job, self.task

    async def _run(self, candidate, values, job, claim):
        from .runtime import lease_heartbeat, release_owner, assert_owner
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(lease_heartbeat(self.db,"settings:owner",claim,stop))
        old = DetectorConfig(**json.loads(self.db.get_meta("settings:active_profile")))
        need_resync = any(getattr(old, k) != getattr(candidate, k) for k in ("ltf_structure_left", "ltf_structure_right"))
        need_entries = any(getattr(old, k) != getattr(candidate, k) for k in ("ltf_entry_types", "entry_reuse_max_depth"))
        result = {"structure_resynced": False, "entries_reclassified": {}}
        def apply():
            job["status"] = "running"
            self.db.set_meta("settings:recalc", json.dumps(job))
            with self.db.batch_writes():
                assert_owner(self.db,"settings:owner",claim)
                if self.engine is not None:
                    stage = copy.copy(self.engine)
                    stage.cfg = candidate
                    for k in ("_ctx_sweep_cursor", "_h1_cursors"):
                        setattr(stage, k, {})
                    stage._batch = stage._bar_plan = None
                    if need_resync:
                        stage.resync_structure_params()
                        result["structure_resynced"] = True
                    if need_entries:
                        result["entries_reclassified"] = stage.reclassify_active_entries()
                # Database is the authoritative profile; the file is its export.
                self.save_file(self.settings, candidate)
                self.db.set_meta("settings:active_profile", json.dumps(asdict(candidate)))
                self.db.set_meta("settings:config_version", str(job["requested_config_version"]))
                # этап 6a (§13): применённый профиль — версия домена конфигурации
                self.db.bump_domain("seq:config")
                job.update(status="ready", finished_at=now_ms(), effective_config_version=job["requested_config_version"], result=result)
                self.db.set_meta("settings:recalc", json.dumps(job))
                assert_owner(self.db,"settings:owner",claim,check_expiry=False)
            return result
        sync = getattr(self.app.state, "data_sync", None)
        try:
            # Same lock as live calculations. Profile is installed before releasing it.
            if sync is not None:
                async with sync._calculation_lock:
                    work = asyncio.create_task(asyncio.to_thread(apply))
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        await work
                        self._install(candidate)
                        raise
                    self._install(candidate)
            else:
                work = asyncio.create_task(asyncio.to_thread(apply))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    await work
                    self._install(candidate)
                    raise
                self._install(candidate)
            return {"applied": sorted(values), **result, "recalc": job,
                    "detector": asdict(candidate), "config_version": self.version}
        except Exception as exc:
            self._install(old)
            try:
                await asyncio.to_thread(self.save_file, self.settings, old)
            finally:
                job.update(status="failed", finished_at=now_ms(), error=f"{type(exc).__name__}: {exc}")
                await asyncio.to_thread(self.db.set_meta, "settings:recalc", json.dumps(job))
            raise
        finally:
            stop.set()
            await asyncio.gather(heartbeat,return_exceptions=True)
            await asyncio.to_thread(release_owner,self.db,"settings:owner",claim)

    async def shutdown(self):
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
