"""Точка входа сервиса HTF Zones: веб + воркер + Telegram-бот в одном процессе.

Запуск: python -m app.main
Секреты — только ENV (§11 п.8), см. .env.example. Файл .env в корне проекта
подхватывается автоматически (load_dotenv); явно заданный ENV приоритетнее.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .adapters.binance import BinanceSpotAdapter
from .adapters.bybit import BybitSpotAdapter
from .adapters.coinmarketcap import CoinMarketCapAdapter
from .adapters.hyperliquid import HyperliquidSpotAdapter
from .alt.runner import AltRunner
from .config import load_alt_config, load_detector_config, load_settings
from .db import Database
from .engine.ltf import LtfEngine
from .notify.alt_queue import AltDispatcher
from .notify.ltf_queue import LtfDispatcher
from .notify.queue import EventDispatcher
from .notify.telegram import LogSender, TelegramSender, build_application
from .web.api import create_app
from .worker import Worker

log = logging.getLogger(__name__)


class ShutdownAwareServer(uvicorn.Server):
    """Begin worker shutdown as soon as SIGTERM arrives, before HTTP drains."""
    def __init__(self, config, shutdown_requested):
        super().__init__(config)
        self.shutdown_requested = shutdown_requested

    def handle_exit(self, sig, frame):
        self.shutdown_requested.set()
        # This process owns several services. Uvicorn's default handler
        # re-raises SIGTERM when HTTP finishes, killing the process before
        # the calculation writer can release its lease. async_main drains
        # the services and exits after that cleanup itself.
        if self.should_exit and sig == signal.SIGINT:
            self.force_exit = True
        else:
            self.should_exit = True


async def finish_calculation_shutdown(db, worker, worker_tasks):
    """Release the role after its writers stop, before unrelated cleanup."""
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    lease_task = getattr(worker, '_lease_task', None)
    if lease_task is not None:
        await asyncio.gather(lease_task, return_exceptions=True)
    from .services.runtime import CALC_OWNER, release_owner
    claim = getattr(worker, '_calc_claim', None)
    if claim:
        await asyncio.to_thread(release_owner, db, CALC_OWNER, claim)


def load_dotenv(path: Path | None = None) -> None:
    """Подхватывает KEY=VALUE из .env в корне проекта. Переменные, уже
    заданные в окружении процесса, не переопределяются (реальный ENV важнее).
    Позволяет запускать просто `python -m app.main` без `source .env`."""
    path = path or Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def run_bot_polling(settings, db) -> None:
    """Long polling кнопок/заметок. Без токена — пропуск (§9).

    Один владелец getUpdates на базу. Пока чужая lease действует, ждём;
    Conflict от Telegram пишется в состояние сервиса, без
    изменения рыночных фактов."""
    application = build_application(settings, db)
    if application is None:
        log.info("TELEGRAM_TOKEN не задан — бот отключён, доставка в лог")
        return
    from .services.runtime import (
        TELEGRAM_OWNER,
        wait_for_owner,
        record_telegram_conflict,
        lease_heartbeat,
        release_owner,
    )
    stop = getattr(db, "runtime_stop", asyncio.Event())
    claim = await wait_for_owner(db, TELEGRAM_OWNER, {
        "pid": os.getpid(), "role": "telegram",
    }, stop)
    if claim is None:
        return

    async def _on_telegram_error(_update, context) -> None:
        err = getattr(context, "error", None)
        text = str(err) if err is not None else ""
        name = err.__class__.__name__ if err is not None else ""
        if "Conflict" in name or "Conflict" in text:
            record_telegram_conflict(db, text or name)

    heartbeat = asyncio.create_task(lease_heartbeat(db, TELEGRAM_OWNER, claim, stop))
    waiter = asyncio.create_task(stop.wait())
    try:
        application.add_error_handler(_on_telegram_error)
        base_url = settings.effective_base_url()
        if "127.0.0.1" in base_url or "localhost" in base_url:
            log.warning(
                "Ссылки из Telegram ведут на %s (localhost) — с других устройств "
                "не откроются; задайте HTF_PUBLIC_BASE_URL", base_url,
            )
        async with application:
            started = polling = False
            try:
                await application.start()
                started = True
                await application.updater.start_polling()
                polling = True
                log.info("Telegram-бот запущен (polling)")
                done, _ = await asyncio.wait([heartbeat, waiter], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                try:
                    if polling:
                        await application.updater.stop()
                finally:
                    if started:
                        await application.stop()
    finally:
        heartbeat.cancel()
        waiter.cancel()
        await asyncio.gather(heartbeat, waiter, return_exceptions=True)
        await asyncio.to_thread(release_owner, db, TELEGRAM_OWNER, claim)


def check_deploy_token(settings) -> None:
    """§7.6: запуск со стандартным dev-token на не-loopback хосте запрещён —
    иначе сервис с известным токеном доступен сети. Локальный режим
    (127.0.0.1/localhost/::1) остаётся явно разрешённым."""
    if settings.auth_token == "dev-token" and settings.host not in (
        "127.0.0.1", "localhost", "::1",
    ):
        raise SystemExit(
            f"Отказ запуска: HTF_HOST={settings.host}, а токен — стандартный "
            "dev-token. Задайте HTF_AUTH_TOKEN (§7.6) либо верните "
            "loopback-host для локального режима."
        )


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Telegram places its bot credential in the URL logged by httpx at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    settings = load_settings()
    check_deploy_token(settings)
    # ENV-переопределения детектора — базовый слой; далее create_app применит
    # переопределения из data/settings.json (файл приоритетнее), и воркер
    # получит тот же итоговый конфиг, что виден в веб-настройках
    settings.detector = load_detector_config()
    settings.alt_config = load_alt_config()
    from .services.postgres_migration import ensure_postgres
    await asyncio.to_thread(ensure_postgres, settings)
    db = await asyncio.to_thread(Database, settings.database_target)
    if db.is_postgres:
        log.info("база PostgreSQL подключена; файлы приложения: %s", Path(settings.db_path).parent)
    elif settings.db_path != ":memory:":
        try:
            db_bytes = Path(settings.db_path).stat().st_size
        except OSError:
            db_bytes = -1
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip().rstrip("/")
        if mount:
            log.info("база %s (%s байт), том %s", settings.db_path, db_bytes, mount)
        else:
            log.info("база %s (%s байт)", settings.db_path, db_bytes)
    from .services.htf_profile import (
        migrate_saved_htf_profile,
        migrate_saved_htf_profile_v3,
    )
    migrate_saved_htf_profile(db, settings)
    migrate_saved_htf_profile_v3(db, settings)

    adapters = {
        "binance": BinanceSpotAdapter(settings.binance_base_url, trust_env=settings.market_trust_env),
        "hyperliquid": HyperliquidSpotAdapter(settings.hyperliquid_base_url, trust_env=settings.market_trust_env),
        "bybit": BybitSpotAdapter(settings.bybit_base_url, trust_env=settings.market_trust_env),
    }
    if settings.telegram_token:
        sender = TelegramSender(
            settings.telegram_token,
            settings.telegram_chat_id,
            db,
            site_base_url=settings.effective_base_url(),
            auth_token=settings.auth_token,
        )
    else:
        sender = LogSender()
    from .events import publish as events_publish
    events_publish.wire(db, sender, settings)
    # §11 п.7: снимки зон для Telegram складываем рядом с БД
    charts_dir = str(Path(settings.db_path).parent / "charts")
    dispatcher = EventDispatcher(
        db, settings.detector, sender, charts_dir=charts_dir,
        chat_id=settings.telegram_chat_id or None,
    )

    dispatcher.defer_flush = True
    dispatcher.outbox.inline_delivery = False
    ltf_engine = LtfEngine(db, settings.detector)   # LTF Confirmations (этапы B–E)
    app = create_app(db, settings, ltf_engine=ltf_engine)
    # доставка LTF-уведомлений тем же транспортом, что у HTF (§11)
    ltf_dispatcher = LtfDispatcher(db, settings.detector, sender, settings=settings)
    # «Altcoins D1 accumulation»: без CMC_API_KEY runner конструируется,
    # но run_daily завершается no_universe (DATA_PENDING, §3 ТЗ)
    cmc_adapter = CoinMarketCapAdapter(settings.cmc_base_url, settings.cmc_api_key)
    alt_runner = AltRunner(
        db, settings, adapters, cmc_adapter,
        broadcast=app.state.ws_hub.broadcast,
    )
    app.state.alt_runner = alt_runner  # маршрут /api/alt/recalc берёт его здесь
    # доставка alt_event в Telegram тем же транспортом (§18 ТЗ 07.10.2026)
    alt_dispatcher = AltDispatcher(db, settings, sender)
    worker = Worker(
        db, settings, settings.detector, adapters, dispatcher,
        broadcast=app.state.ws_hub.broadcast,
        ltf_engine=ltf_engine,
        ltf_dispatcher=ltf_dispatcher,
        alt_runner=alt_runner,
        alt_dispatcher=alt_dispatcher,
    )
    app.state.data_sync = worker.data_sync
    app.state.worker = worker
    worker.instance_id = app.state.instance_id
    db.runtime_instance_id = app.state.instance_id
    db.runtime_stop = worker._stop

    shutdown_requested = asyncio.Event()
    server = ShutdownAwareServer(
        uvicorn.Config(app=app, host=settings.host, port=settings.port,
                       log_level="info"), shutdown_requested,
    )
    from .services.maintenance import run as run_maintenance
    tasks = [asyncio.create_task(server.serve(), name="http"),
             asyncio.create_task(shutdown_requested.wait(), name="shutdown"),
             asyncio.create_task(worker.run(), name="worker"),
             asyncio.create_task(dispatcher.outbox.run(worker._stop), name="outbox"),
             asyncio.create_task(run_maintenance(db,settings,worker._stop), name="maintenance")]
    if settings.telegram_token:
        tasks.append(asyncio.create_task(run_bot_polling(settings, db), name="telegram"))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        app.state.stopping = True
        worker.stop()
        server.should_exit = True
        if worker.realtime is not None:
            await worker.realtime.stop()
        for task in list(worker._ws_background):
            task.cancel()
        await asyncio.gather(*list(worker._ws_background), return_exceptions=True)
        for task in tasks:
            if task.get_name() == "worker" and not task.done():
                task.cancel()
        await app.state.settings_jobs.shutdown()
        await worker.market_queue.stop()
        await worker.calc_queue.stop()
        await worker.data_sync.shutdown()
        await finish_calculation_shutdown(
            db, worker, [task for task in tasks if task.get_name() == 'worker'])
        shutdown_requested.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in [*adapters.values(), cmc_adapter]:
            client = getattr(adapter, "client", None) or getattr(adapter, "_client", None)
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()
        db.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
