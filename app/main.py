"""Точка входа сервиса HTF Zones: веб + воркер + Telegram-бот в одном процессе.

Запуск: python -m app.main
Секреты — только ENV (§11 п.8), см. .env.example. Файл .env в корне проекта
подхватывается автоматически (load_dotenv); явно заданный ENV приоритетнее.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from .adapters.binance import BinanceSpotAdapter
from .adapters.hyperliquid import HyperliquidSpotAdapter
from .config import load_detector_config, load_settings
from .db import Database
from .engine.ltf import LtfEngine
from .notify.ltf_queue import LtfDispatcher
from .notify.queue import EventDispatcher
from .notify.telegram import LogSender, TelegramSender, build_application
from .web.api import create_app
from .worker import Worker

log = logging.getLogger(__name__)


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
    """Long polling кнопок/заметок. Без токена — пропуск (§9)."""
    application = build_application(settings, db)
    if application is None:
        log.info("TELEGRAM_TOKEN не задан — бот отключён, доставка в лог")
        return
    base_url = settings.effective_base_url()
    if "127.0.0.1" in base_url or "localhost" in base_url:
        log.warning(
            "Ссылки из Telegram ведут на %s (localhost) — с других устройств "
            "не откроются; задайте HTF_PUBLIC_BASE_URL", base_url,
        )
    async with application:
        await application.start()
        await application.updater.start_polling()
        log.info("Telegram-бот запущен (polling)")
        await asyncio.Event().wait()  # живём до остановки процесса


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    load_dotenv()
    settings = load_settings()
    # ENV-переопределения детектора — базовый слой; далее create_app применит
    # переопределения из data/settings.json (файл приоритетнее), и воркер
    # получит тот же итоговый конфиг, что виден в веб-настройках
    settings.detector = load_detector_config()
    db = Database(settings.db_path)

    adapters = {
        "binance": BinanceSpotAdapter(settings.binance_base_url),
        "hyperliquid": HyperliquidSpotAdapter(settings.hyperliquid_base_url),
    }
    if settings.telegram_token:
        sender = TelegramSender(
            settings.telegram_token,
            settings.telegram_chat_id,
            db,
            site_base_url=settings.effective_base_url(),
        )
    else:
        sender = LogSender()
    # §11 п.7: снимки зон для Telegram складываем рядом с БД
    charts_dir = str(Path(settings.db_path).parent / "charts")
    dispatcher = EventDispatcher(
        db, settings.detector, sender, charts_dir=charts_dir,
        chat_id=settings.telegram_chat_id or None,
    )

    ltf_engine = LtfEngine(db, settings.detector)   # LTF Confirmations (этапы B–E)
    app = create_app(db, settings, ltf_engine=ltf_engine)
    # доставка LTF-уведомлений тем же транспортом, что у HTF (§11)
    ltf_dispatcher = LtfDispatcher(db, settings.detector, sender, settings=settings)
    worker = Worker(
        db, settings, settings.detector, adapters, dispatcher,
        broadcast=app.state.ws_hub.broadcast,
        ltf_engine=ltf_engine,
        ltf_dispatcher=ltf_dispatcher,
    )

    server = uvicorn.Server(
        uvicorn.Config(app=app, host=settings.host, port=settings.port,
                       log_level="info")
    )
    try:
        await asyncio.gather(
            server.serve(),
            worker.run(),
            run_bot_polling(settings, db),
        )
    finally:
        worker.stop()
        db.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
