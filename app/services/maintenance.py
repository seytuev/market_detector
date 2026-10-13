"""Consistent backups and conservative retention; no market history deletion."""
from __future__ import annotations
import json
import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path
from ..models import now_ms


def backup(db, destination, settings=None):
    target = Path(destination).resolve()
    target.mkdir(parents=True, exist_ok=False)
    db_target = target / "htf_zones.db"
    with db.read_tx():
        profile = db.get_meta("settings:active_profile")
        version = db.get_state_seq()
        raw = sqlite3.connect(str(db_target))
        try:
            db.conn._conn.backup(raw)
            if raw.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
        finally:
            raw.close()
    if profile:
        (target / "settings.json").write_text(json.dumps({"detector":json.loads(profile)}, ensure_ascii=False, indent=2), encoding="utf-8")
    root = Path(db.path).parent
    media = root / "charts"
    if media.exists():
        shutil.copytree(media, target / "charts")
    labels = root / "labels.jsonl"
    if labels.exists():
        shutil.copy2(labels, target / "labels.jsonl")
    manifest = {"created_at":now_ms(), "state_version":version, "database":"htf_zones.db", "media_optional":True}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def retain(db, *, processed_days=30, job_days=30):
    stamp = now_ms()
    with db.batch_writes():
        receipts = db.conn.execute("DELETE FROM quote_spool WHERE status='processed' AND processed_at<?", (stamp-processed_days*86400_000,)).rowcount
        publications = db.conn.execute("DELETE FROM market_publication WHERE published_at IS NOT NULL AND published_at<?", (stamp-processed_days*86400_000,)).rowcount
        sessions = db.conn.execute("DELETE FROM browser_session WHERE expires_at<=?", (stamp,)).rowcount
        jobs = 0
        for key, raw in db.meta_prefix("data_sync:job:").items():
            try:
                job = json.loads(raw)
            except ValueError:
                continue
            if job.get("status") in {"completed","failed","cancelled","interrupted"} and job.get("finished_at",stamp) < stamp-job_days*86400_000:
                db.delete_meta(key)
                jobs += 1
    return {"processed_receipts":receipts, "published_batches":publications, "expired_sessions":sessions, "finished_jobs":jobs}


def disk_status(path):
    used = shutil.disk_usage(Path(path).resolve().parent)
    return {"free_bytes":used.free, "total_bytes":used.total, "degraded":used.free < max(256*1024**2,used.total*.05)}


async def run(db, settings, stop):
    import asyncio
    import logging
    import uuid
    from datetime import datetime, timezone
    from .runtime import CALC_OWNER, _read_claim
    from .metrics import get_registry
    log = logging.getLogger(__name__)
    def cycle():
        owner = _read_claim(db,CALC_OWNER) or {}
        if owner.get("instance_id") != getattr(db,"runtime_instance_id",None) or owner.get("expires_at",0)<=now_ms():
            return
        status = disk_status(db.path)
        get_registry().gauge("disk_free_bytes",status["free_bytes"])
        get_registry().gauge("disk_degraded",int(status["degraded"]))
        retain(db)
        if status["degraded"]:
            log.error("Disk space low: %s bytes free",status["free_bytes"])
            return
        last = int(db.get_meta("maintenance:last_backup_at") or 0)
        if settings.backup_enabled and now_ms()-last >= max(1,settings.backup_interval_hours)*3600_000:
            label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            destination = Path(db.path).resolve().parent / "backups" / label
            backup(db,destination,settings)
            db.set_meta("maintenance:last_backup_at",str(now_ms()))
    while not stop.is_set():
        if db.path != ":memory:":
            try:
                await asyncio.to_thread(cycle)
            except Exception:
                log.exception("Maintenance failed; market history retained")
        try:
            await asyncio.wait_for(stop.wait(),60)
        except asyncio.TimeoutError:
            pass
