"""Эндпоинт и обвязка метрик процесса (этап 1 perf-спеки — только наблюдение).

MetricsMiddleware — чистый ASGI: длительность и размер HTTP-ответов по
шаблону роута (scope["route"] заполняется роутером; для нематченных путей —
"unmatched"). metrics_lifespan — фоновый тикер лага event loop и процессных
гауджей (resource; на Windows модуля нет — гауджи просто не обновляются,
psutil в зависимостях нет и не добавляется).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from fastapi import Depends

from ..services.metrics import get_registry

log = logging.getLogger(__name__)

_TICK_SECONDS = 5.0


class MetricsMiddleware:
    """Замер HTTP-запросов: api_duration и api_response_bytes по роуту.

    Метки ограничены: шаблон роута (не сырой URL), метод, класс статуса —
    кардинальность не зависит от данных запроса."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        metrics = get_registry()
        started = time.monotonic()
        status = 500  # необработанное исключение уйдёт выше в ServerErrorMiddleware
        body_bytes = 0

        async def wrapped_send(message):
            nonlocal status, body_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                body_bytes += len(message.get("body") or b"")
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            route = scope.get("route")
            path = str(getattr(route, "path", None) or "unmatched")
            metrics.histogram("api_duration", time.monotonic() - started, {
                "route": path,
                "method": scope.get("method") if scope.get("method") in {"GET","POST","PUT","PATCH","DELETE","HEAD","OPTIONS"} else "other",
                "status_class": f"{status // 100}xx",
            })
            metrics.counter("api_response_bytes", body_bytes, {"route": path})


def _sample_process_gauges(metrics) -> None:
    """RSS/CPU процесса через resource (Unix); Windows — тихо пропускаем."""
    try:
        import resource
    except ImportError:
        return
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux: ru_maxrss в КиБ (прод — Docker/railway)
        metrics.gauge("process_peak_rss_bytes", float(usage.ru_maxrss) * 1024)
        try:
            from pathlib import Path
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            import os
            metrics.gauge("process_rss_bytes", pages * os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
        metrics.gauge("process_cpu_seconds", float(usage.ru_utime + usage.ru_stime))
    except Exception:
        log.debug("process gauges sample failed", exc_info=True)


async def _lag_ticker() -> None:
    metrics = get_registry()
    loop = asyncio.get_running_loop()
    expected = loop.time() + _TICK_SECONDS
    while True:
        await asyncio.sleep(_TICK_SECONDS)
        now = loop.time()
        metrics.gauge("event_loop_lag_s", max(0.0, now - expected))
        expected = now + _TICK_SECONDS
        _sample_process_gauges(metrics)


@contextlib.asynccontextmanager
async def metrics_lifespan(app):
    """Тикер живёт, пока приложение обслуживается; TestClient/ASGITransport
    без lifespan его не запускают — тесты не затрагиваются."""
    task = None
    try:
        task = asyncio.create_task(_lag_ticker())
    except RuntimeError:
        pass  # цикл не запущен — приложение собрано без сервера
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def register_metrics_routes(app, require_auth) -> None:
    """GET /api/diagnostics/metrics — снимок реестра (без секретов и URL)."""

    @app.get("/api/diagnostics/metrics", dependencies=[Depends(require_auth)])
    def diagnostics_metrics() -> dict[str, Any]:
        return get_registry().snapshot()
