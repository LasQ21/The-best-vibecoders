from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

import scoring

from .. import formatting as fmt
from .. import keyboards
from ..services import Services
from .common import send_tender

router = Router(name="start")

WELCOME = (
    "<b>Радар тендерів Prozorro</b>\n\n"
    "Я дивлюся на публічні закупівлі й показую, які тендери варто перевірити першими. "
    "Кожен отримує бал від 0 до 100 з чотирьох простих правил і оцінки моделі — "
    "і завжди з поясненням, чому він у списку.\n\n"
    "• /top — тендери з найвищим балом\n"
    "• /tender UA-2026-… — картка тендера\n"
    "• /buyer 43927048 — профіль замовника\n"
    "• /alerts — сповіщення про нові тендери з високим балом\n\n"
    "Можна просто надіслати код тендера, посилання з prozorro.gov.ua або ЄДРПОУ.\n\n"
    f"<i>{fmt.DISCLAIMER}</i>"
)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, services: Services) -> None:
    payload = (command.args or "").strip()
    if payload.startswith("t_"):
        await send_tender(message, services, payload[2:])
        return
    sub = services.storage.get_subscription(message.chat.id)
    await message.answer(WELCOME, reply_markup=keyboards.start_keyboard(services.settings, sub.active))


@router.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer(fmt.about_text(scoring.WEIGHTS, scoring.HIGH_SCORE_THRESHOLD))


@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery) -> None:
    await callback.message.answer(fmt.about_text(scoring.WEIGHTS, scoring.HIGH_SCORE_THRESHOLD))
    await callback.answer()
