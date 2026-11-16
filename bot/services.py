"""Збирання всіх частин бота в один об'єкт Services.

Один і той самий Services використовують обидва режими: процес (__main__) і Vercel.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import alerts as alerts_mod
from . import formatting as fmt
from . import keyboards
from .config import ALERT_WINDOW_HOURS, Settings
from .persist import LocalPersistence, make_persistence
from .snapshot import SnapshotStore
from .storage import Storage

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    ("start", "Про бота й дашборд"),
    ("top", "Тендери з найвищим балом"),
    ("tender", "Картка тендера: код, id або посилання"),
    ("buyer", "Замовник: ЄДРПОУ або назва"),
    ("alerts", "Сповіщення: стан і поріг"),
    ("about", "Як рахується бал"),
]
ADMIN_CHATS_KEY = "admin_chats"
ADMIN_COMMANDS = [("update", "Позачерговий цикл опитування"), ("status", "Стан опитування"),
                  ("demo_alert", "Демо-сповіщення")]


@dataclass
class ProcessResult:
    candidates: int = 0
    sent: int = 0
    collapsed: int = 0
    blocked: int = 0


class AlertService:
    def __init__(self, services: "Services"):
        self.services = services

    def card(self, row) -> tuple[str, object]:
        settings = self.services.settings
        return (fmt.tender_card(row, header=fmt.alert_header()),
                keyboards.tender_keyboard(settings, row, alert=True))

    def collapsed(self, plan: alerts_mod.ChatPlan) -> tuple[str, object]:
        return (fmt.collapsed_text(len(plan.collapsed), plan.min_score),
                keyboards.collapsed_keyboard(self.services.settings, plan.min_score))

    async def process(self, changed: pd.DataFrame, now: datetime) -> ProcessResult:
        candidates = alerts_mod.candidates_from(changed, now, ALERT_WINDOW_HOURS)
        result = ProcessResult(candidates=len(candidates))
        if candidates.empty or self.services.sender is None:
            return result
        subs = self.services.storage.active_subscriptions()
        plans = alerts_mod.plan_alerts(candidates, subs, self.services.storage, now)
        outcome = await alerts_mod.dispatch(plans, self.services.sender, self.services.storage,
                                            self.card, self.collapsed)
        result.sent, result.collapsed, result.blocked = outcome.sent, outcome.collapsed, outcome.blocked
        return result


@dataclass
class Services:
    settings: Settings
    storage: Storage
    persistence: LocalPersistence
    snapshots: SnapshotStore
    bot: object | None = None
    sender: alerts_mod.AlertSender | None = None
    alerts: AlertService | None = None
    poller: object | None = None
    extra: dict = field(default_factory=dict)

    def admin_chat_ids(self) -> set[int]:
        """chat_id з ADMIN_IDS плюс запам'ятовані для адміністраторів, вказаних юзернеймом."""
        remembered = self.storage.get_json(ADMIN_CHATS_KEY, {}) or {}
        return set(self.settings.admin_ids) | {int(chat_id) for chat_id in remembered.values()}

    def is_admin_chat(self, chat_id: int | None, username: str | None, private: bool = True) -> bool:
        if chat_id is not None and chat_id in self.admin_chat_ids():
            return True
        return private and self.settings.is_admin(None, username)

    async def remember_admin(self, chat_id: int, username: str | None) -> bool:
        """Адміністратор за юзернеймом написав боту: запам'ятати його chat_id.

        Telegram не дає боту писати людині за юзернеймом, лише за chat_id, тому
        службові повідомлення приходитимуть після першого /start. True — новий.
        """
        if not username or not self.settings.is_admin(None, username):
            return False
        remembered = self.storage.get_json(ADMIN_CHATS_KEY, {}) or {}
        key = username.lower()
        if remembered.get(key) == chat_id:
            return False
        remembered[key] = chat_id
        self.storage.set_json(ADMIN_CHATS_KEY, remembered)
        if self.bot is not None:
            try:
                await set_admin_commands(self.bot, chat_id)
            except Exception as error:
                logger.warning("Команди адміністратора %s: %s", chat_id, error)
        return True

    async def notify_admins(self, text: str) -> None:
        if self.bot is None:
            logger.warning("Адміністраторам: %s", text)
            return
        for chat_id in self.admin_chat_ids():
            try:
                await self.bot.send_message(chat_id, text)
            except Exception as error:
                logger.warning("Не вдалося написати адміністратору %s: %s", chat_id, error)

    def refresh_db(self) -> None:
        """На Vercel: якщо інший екземпляр оновив SQLite — перечитати."""
        if self.persistence.refresh_db():
            self.storage.reopen()

    async def flush(self) -> None:
        await asyncio.to_thread(self.persistence.flush)


def make_bot(settings: Settings):
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import LinkPreviewOptions

    return Bot(settings.bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))


def telegram_sender(bot) -> alerts_mod.AlertSender:
    from aiogram.exceptions import TelegramForbiddenError

    async def send(chat_id: int, text: str, markup) -> None:
        try:
            await bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramForbiddenError as error:
            raise alerts_mod.Blocked(str(error)) from error

    return alerts_mod.AlertSender(send)


def build_services(settings: Settings, with_bot: bool = True) -> Services:
    persistence = make_persistence(settings)
    persistence.prepare()
    storage = Storage(persistence.db_path, on_change=persistence.db_changed)
    services = Services(settings=settings, storage=storage, persistence=persistence,
                        snapshots=SnapshotStore(persistence.data_dir))
    if with_bot and settings.bot_token:
        services.bot = make_bot(settings)
        services.sender = telegram_sender(services.bot)
    services.alerts = AlertService(services)
    from .ingest import Poller
    services.poller = Poller(services)
    return services


async def set_admin_commands(bot, chat_id: int) -> None:
    from aiogram.types import BotCommand, BotCommandScopeChat

    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS + ADMIN_COMMANDS],
        scope=BotCommandScopeChat(chat_id=chat_id))


async def setup_bot_profile(services: Services) -> None:
    """Команди й кнопка меню, що відкриває дашборд."""
    from aiogram.types import BotCommand, MenuButtonDefault, MenuButtonWebApp, WebAppInfo

    bot = services.bot
    settings = services.settings
    if bot is None:
        return
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    for admin in services.admin_chat_ids():
        try:
            await set_admin_commands(bot, admin)
        except Exception as error:
            logger.warning("Команди адміністратора %s: %s", admin, error)
    if settings.has_webapp:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Дашборд", web_app=WebAppInfo(url=settings.webapp_url)))
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
