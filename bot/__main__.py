"""Запуск: python -m bot (з кореня репозиторію).

Один процес і один цикл подій: uvicorn (дашборд), aiogram long polling і цикл
опитування Prozorro в одному asyncio.gather.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import ConfigError, load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # aiohttp пише URL запиту з токеном у DEBUG — не підвищуємо рівень.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as error:
        print(f"Помилка налаштувань: {error}", file=sys.stderr)
        sys.exit(2)
    if not settings.bot_token and not settings.dev_auth_bypass:
        print("BOT_TOKEN порожній: заповніть .env (див. .env.example)", file=sys.stderr)
        sys.exit(2)
    asyncio.run(run(settings))


async def run(settings) -> None:
    import uvicorn

    from .handlers import build_dispatcher
    from .ingest import poll_forever
    from .services import build_services, setup_bot_profile
    from .web.server import create_app

    services = build_services(settings)
    dispatcher = build_dispatcher(services)
    web = create_app(services)
    server = uvicorn.Server(uvicorn.Config(web, host=settings.host, port=settings.port,
                                           log_level="warning", lifespan="on"))
    # Знімок вантажимо в потоці: цикл подій у цей час відповідає на запити.
    asyncio.get_running_loop().create_task(services.snapshots.get())

    bot = services.bot
    if bot is None:
        # Розробка дашборда без Telegram: DEV_AUTH_BYPASS=1, BOT_ENV=dev, без токена.
        logging.getLogger(__name__).info("Без бота: лише дашборд http://%s:%s/app",
                                         settings.host, settings.port)
        await server.serve()
        return
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    services.extra["bot_username"] = me.username
    try:
        await setup_bot_profile(services)
    except Exception:
        logging.getLogger(__name__).exception("Не вдалося налаштувати команди й кнопку меню")
    logging.getLogger(__name__).info("Бот @%s, дашборд http://%s:%s/app", me.username,
                                     settings.host, settings.port)

    async def polling():
        await dispatcher.start_polling(bot, handle_signals=False,
                                       allowed_updates=dispatcher.resolve_used_update_types())

    await asyncio.gather(server.serve(), polling(),
                         poll_forever(services.poller, settings.poll_interval_sec))


if __name__ == "__main__":
    main()
