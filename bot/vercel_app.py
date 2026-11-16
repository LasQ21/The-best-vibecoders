"""Точка входу для Vercel ([tool.vercel] entrypoint = "bot.vercel_app:app").

Той самий FastAPI-застосунок, що й у процесі, плюс вебхук Telegram (/tg/webhook) і
/cron/poll для зовнішнього планувальника. Long polling на Vercel неможливий:
функція живе лише поки обробляє запит.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import ConfigError, load_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def _build() -> FastAPI:
    os.environ.setdefault("VERCEL", "1")
    settings = load_settings()
    from .services import build_services
    from .web.server import create_app

    services = build_services(settings)
    dispatcher = None
    if services.bot is not None:
        from .handlers import build_dispatcher
        dispatcher = build_dispatcher(services)
    else:
        logger.warning("BOT_TOKEN не задано: працює лише дашборд без авторизації Telegram")
    return create_app(services, dispatcher)


try:
    app = _build()
except ConfigError as error:  # покажемо причину замість 500 без пояснень
    logger.error("Помилка налаштувань: %s", error)
    _reason = str(error)
    app = FastAPI()

    @app.get("/{path:path}")
    async def misconfigured(path: str):
        return JSONResponse({"error": "misconfigured", "detail": _reason}, status_code=500)
