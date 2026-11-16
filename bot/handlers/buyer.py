from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .. import keyboards
from ..services import Services
from .common import search_buyer, send_buyer

router = Router(name="buyer")


@router.message(Command("buyer"))
async def cmd_buyer(message: Message, command: CommandObject, services: Services) -> None:
    await search_buyer(message, services, command.args or "", message.chat.id)


@router.callback_query(F.data.startswith(keyboards.CB_BUYER))
async def cb_buyer(callback: CallbackQuery, services: Services) -> None:
    await send_buyer(callback.message, services, callback.data[len(keyboards.CB_BUYER):],
                     callback.message.chat.id)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CB_FOLLOW))
async def cb_follow(callback: CallbackQuery, services: Services) -> None:
    edrpou = callback.data[len(keyboards.CB_FOLLOW):]
    services.storage.follow_buyer(callback.message.chat.id, edrpou)
    sub = services.storage.get_subscription(callback.message.chat.id)
    note = "" if sub.active else " Увімкніть сповіщення: /alerts"
    await callback.answer(f"Стежимо за {edrpou}: сповіщення від балу "
                          f"{int(sub.follow_min_score)}.{note}", show_alert=not sub.active)


@router.callback_query(F.data.startswith(keyboards.CB_UNFOLLOW))
async def cb_unfollow(callback: CallbackQuery, services: Services) -> None:
    edrpou = callback.data[len(keyboards.CB_UNFOLLOW):]
    services.storage.follow_buyer(callback.message.chat.id, edrpou, follow=False)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.buyer_keyboard(services.settings, edrpou, False))
    except Exception:
        pass
    await callback.answer("Більше не стежимо")


@router.callback_query(F.data.startswith(keyboards.CB_MUTE))
async def cb_mute(callback: CallbackQuery, services: Services) -> None:
    edrpou = callback.data[len(keyboards.CB_MUTE):]
    services.storage.mute_buyer(callback.message.chat.id, edrpou)
    await callback.answer("Тендери цього замовника більше не надсилатимемо. "
                          "Повернути можна в дашборді, екран «Сповіщення».", show_alert=True)


@router.callback_query(F.data.startswith(keyboards.CB_UNMUTE))
async def cb_unmute(callback: CallbackQuery, services: Services) -> None:
    services.storage.mute_buyer(callback.message.chat.id, callback.data[len(keyboards.CB_UNMUTE):],
                                mute=False)
    await callback.answer("Замовника повернуто")
