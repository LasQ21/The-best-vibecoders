"""Спільні дії обробників: показати тендер, замовника, пошук."""

from __future__ import annotations

from aiogram.types import Message

from .. import formatting as fmt
from .. import keyboards, queries
from ..services import Services

NOT_FOUND_TENDER = ("Такого тендера немає в даних. Бот бачить цивільні тендери зі статусом "
                    "«завершено» або «визначено переможця» за останні півроку.")
NOT_FOUND_BUYER = "Замовника не знайдено. Спробуйте ЄДРПОУ (8 цифр) або частину назви."
LOADING = "Завантажую дані…"


async def send_tender(message: Message, services: Services, query: str) -> None:
    snapshot = await services.snapshots.get()
    tender_id = queries.find_tender_id(snapshot, query)
    if tender_id is None:
        await message.answer(NOT_FOUND_TENDER)
        return
    row = queries.get_tender(snapshot, tender_id)
    header = "Тендер до перевірки" if fmt.safe_float(row.get("total_score"), 0) >= snapshot.threshold \
        else "Тендер"
    await message.answer(fmt.tender_card(row, header=header),
                         reply_markup=keyboards.tender_keyboard(services.settings, row))


async def send_buyer(message: Message, services: Services, edrpou: str, chat_id: int) -> None:
    snapshot = await services.snapshots.get()
    profile = queries.buyer_profile(snapshot, edrpou)
    if profile is None:
        await message.answer(NOT_FOUND_BUYER)
        return
    followed = edrpou in services.storage.followed_buyers(chat_id)
    await message.answer(fmt.buyer_card(profile),
                         reply_markup=keyboards.buyer_keyboard(services.settings, edrpou, followed))


async def search_buyer(message: Message, services: Services, query: str, chat_id: int) -> None:
    query = query.strip()
    if not query:
        await message.answer("Напишіть ЄДРПОУ або частину назви замовника: /buyer 43927048")
        return
    snapshot = await services.snapshots.get()
    if queries.EDRPOU_RE.match(query):
        await send_buyer(message, services, query, chat_id)
        return
    found = queries.search_buyers(snapshot, query, limit=10)
    if found.empty:
        await message.answer(NOT_FOUND_BUYER)
    elif len(found) == 1:
        await send_buyer(message, services, str(found.iloc[0]["buyer_edrpou"]), chat_id)
    else:
        await message.answer(f"Знайдено замовників: {len(found)}{'+' if len(found) >= 10 else ''}. "
                             "Оберіть:", reply_markup=keyboards.buyers_list_keyboard(found))
