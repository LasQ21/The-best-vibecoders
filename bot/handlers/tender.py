from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .. import keyboards
from ..services import Services
from .common import send_tender

router = Router(name="tender")


@router.message(Command("tender"))
async def cmd_tender(message: Message, command: CommandObject, services: Services) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Надішліть код тендера, id або посилання: "
                             "<code>/tender UA-2026-06-26-011657-a</code>")
        return
    await send_tender(message, services, query)


@router.callback_query(F.data.startswith(keyboards.CB_TENDER))
async def cb_tender(callback: CallbackQuery, services: Services) -> None:
    await send_tender(callback.message, services, callback.data[len(keyboards.CB_TENDER):])
    await callback.answer()
