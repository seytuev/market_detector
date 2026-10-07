"""HTTP API окна «Альткоины» (ТЗ 07.10.2026 §16–§18).

Маршруты: таблица сетапов с фильтрами и ранжированием (§16/§17),
детальная карточка сетапа и forming-кандидата (график + «Почему найдено»),
статус дневного прогона (§18), ручной пересчёт (POST, auth + Origin —
как у остальных мутаций, проверка внутри require_auth).

Вычисления — app/services/alt_overview.py (read model); эндпоинты только
делегируют. alt_runner — дневной runner модуля; None (тесты/выключенный
модуль) — чтение работает, ручной пересчёт отвечает 503. В create_app
передаётся как app.state.alt_runner (runner конструируется после приложения
в main.py), поэтому эндпоинт берёт его в момент запроса.

Ручной пересчёт не порождает исторический спам (§18): антиспам первичной
загрузки живёт в runner (meta alt:loaded:{asset}:{source}); ручной прогон
— обычный live/catchup, события идут стандартным outbox-путём с дедупом
UNIQUE(setup_id, event_type, source_event_id).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import Depends, HTTPException

from ..db import Database
from ..services.alt_overview import (
    BUCKETS,
    alt_candidate_detail,
    alt_run_status,
    alt_setup_detail,
    alt_setups_table,
)

log = logging.getLogger(__name__)


def register_alt_routes(app, db: Database, settings, require_auth,
                        alt_runner=None) -> None:
    """Регистрирует маршруты окна «Альткоины» на существующем приложении."""

    def _runner():
        return alt_runner or getattr(app.state, "alt_runner", None)

    # ------------------------- таблица (§16/§17) -------------------------

    @app.get("/api/alt/setups", dependencies=[Depends(require_auth)])
    def alt_setups(
        bucket: str = "eligible",
        venue: Optional[str] = None,
        rank_min: Optional[int] = None,
        rank_max: Optional[int] = None,
        age_min: Optional[int] = None,
        age_max: Optional[int] = None,
        dd_min: Optional[float] = None,
        dd_max: Optional[float] = None,
        structure: Optional[str] = None,
    ) -> dict[str, Any]:
        if bucket not in BUCKETS:
            raise HTTPException(
                status_code=400,
                detail="bucket: " + " | ".join(BUCKETS),
            )
        with db.read_tx():
            return alt_setups_table(
                db, settings, bucket=bucket, venue=venue,
                rank_min=rank_min, rank_max=rank_max,
                age_min=age_min, age_max=age_max,
                dd_min=dd_min, dd_max=dd_max, structure=structure,
            )

    # ------------------------- деталь / «Почему найдено» (§16) -------------------------

    @app.get("/api/alt/setup/{setup_id}", dependencies=[Depends(require_auth)])
    def alt_setup(setup_id: int) -> dict[str, Any]:
        with db.read_tx():
            data = alt_setup_detail(db, setup_id, settings)
        if data is None:
            raise HTTPException(status_code=404, detail="Сетап не найден")
        return data

    @app.get("/api/alt/candidate/{candidate_id}",
             dependencies=[Depends(require_auth)])
    def alt_candidate(candidate_id: int) -> dict[str, Any]:
        with db.read_tx():
            data = alt_candidate_detail(db, candidate_id, settings)
        if data is None:
            raise HTTPException(status_code=404, detail="Диапазон не найден")
        return data

    # ------------------------- статус прогона (§18) -------------------------

    @app.get("/api/alt/run-status", dependencies=[Depends(require_auth)])
    def alt_status() -> dict[str, Any]:
        with db.read_tx():
            return alt_run_status(db, settings.alt_config)

    # ------------------------- ручной пересчёт -------------------------

    @app.post("/api/alt/recalc", dependencies=[Depends(require_auth)])
    async def alt_recalc() -> dict[str, Any]:
        """Ручной запуск дневного прогона (trigger="manual", в фоне).

        Без исторического спама: первичная загрузка каждого актива уже
        отработала с антиспамом runner'а; повторный прогон идемпотентен
        (дедуп событий по стабильным source_event_id). Блокировка — как у
        штатного расписания: активный "running" не даёт второго прогона.
        """
        runner = _runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="ALT-модуль выключен")
        if db.get_running_alt_run() is not None:
            raise HTTPException(
                status_code=409, detail="Прогон уже выполняется",
            )

        async def _run() -> None:
            try:
                await runner.run_daily(trigger="manual")
            except Exception:
                log.exception("alt: ручной пересчёт упал")

        asyncio.get_running_loop().create_task(_run())
        return {"status": "started", "trigger": "manual"}
