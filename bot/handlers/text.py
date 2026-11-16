"""Текст без команди: код/посилання тендера, ЄДРПОУ або пошук замовника за назвою."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.types import Message

from .. import queries
from ..services import Services
from .common import search_buyer, send_buyer, send_tender

router = Router(name="text")
DIGITS_RE = re.compile(r"^\s*(\d{8}|\d{10})\s*$")


@router.message(F.text, ~F.text.startswith("/"), F.chat.type == "private")
async def any_text(message: Message, services: Services) -> None:
    text = message.text or ""
    if queries.looks_like_tender(text):
        await send_tender(message, services, text)
        return
    match = DIGITS_RE.match(text)
    if match:
        await send_buyer(message, services, match.group(1), message.chat.id)
        return
    if len(text.strip()) < 3:
        await message.answer("Надішліть код тендера (UA-2026-…), посилання з prozorro.gov.ua, "
                             "ЄДРПОУ або частину назви замовника.")
        return
    await search_buyer(message, services, text, message.chat.id)
