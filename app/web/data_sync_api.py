"""Authenticated API for the existing worker's synchronization coordinator."""
from fastapi import Depends, HTTPException

from ..services.data_sync import JOB_KEY, SyncCooldown, SyncUnavailable, read_json


def register_data_sync_routes(app, db, require_auth):
    def coordinator():
        return getattr(app.state, "data_sync", None)

    @app.get("/api/data-sync/status", dependencies=[Depends(require_auth)])
    async def status():
        sync = coordinator()
        if sync is None:
            return {"available": False, "job": read_json(db, JOB_KEY),
                    "channels": [],
                    # §13 (этап 6a): версии доступны и без координатора
                    "versions": db.domain_versions(), "readiness": None}
        return sync.status()

    @app.post("/api/data-sync", status_code=202, dependencies=[Depends(require_auth)])
    async def start():
        sync = coordinator()
        if sync is None:
            raise HTTPException(503, detail="Обновление недоступно: обработчик данных не работает")
        try:
            return sync.start()
        except SyncUnavailable:
            raise HTTPException(503, detail="Обновление недоступно: обработчик данных не работает")
        except SyncCooldown as exc:
            raise HTTPException(429, detail={"error": "sync_cooldown", "next_allowed_at": exc.next_at})

    @app.get("/api/data-sync/{job_id}", dependencies=[Depends(require_auth)])
    async def job_status(job_id: str):
        job = read_json(db, f"data_sync:job:{job_id}")
        if job is None:
            raise HTTPException(404, detail="Задание обновления не найдено")
        return job
