"""Обробники чату. build_dispatcher() збирає роутери в правильному порядку."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Dispatcher
from aiogram.types import TelegramObject

from ..services import Services
from . import admin, alerts, buyer, inline, start, tender, text, top


class StateSyncMiddleware(BaseMiddleware):
    """Перед обробкою — свіжа SQLite з Blob (якщо змінилась), після — зберегти зміни."""

    def __init__(self, services: Services):
        self.services = services

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        self.services.refresh_db()
        try:
            return await handler(event, data)
        finally:
            await self.services.flush()


def build_dispatcher(services: Services) -> Dispatcher:
    dp = Dispatcher()
    dp["services"] = services
    dp.update.outer_middleware(StateSyncMiddleware(services))
    # text — останнім: він ловить будь-який текст без команди.
    for module in (start, admin, top, tender, buyer, alerts, inline, text):
        dp.include_router(module.router)
    return dp
