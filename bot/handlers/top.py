from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import formatting as fmt
from .. import keyboards, queries
from ..services import Services

router = Router(name="top")
PAGE = 5


async def _render(services: Services, offset: int, region_index: int | None):
    snapshot = await services.snapshots.get()
    regions = queries.filter_options(snapshot, "buyer_region")
    region = regions[region_index] if region_index is not None and region_index < len(regions) else None
    if region is None:
        region_index = None
    rows, total = queries.top(snapshot, region, offset, PAGE)
    where = f" · {fmt.esc(region)}" if region else " · уся країна"
    lines = [f"<b>Найвищий бал</b>{where}", f"{offset + 1}–{offset + len(rows)} з {fmt.number(total)}", ""]
    for index, row in enumerate(rows, start=offset + 1):
        lines.append(fmt.short_tender_line(row, index))
    if not rows:
        lines.append("Тут поки порожньо.")
    lines += ["", "Натисніть на тендер, щоб побачити пояснення."]
    markup = keyboards.top_keyboard(services.settings, rows, offset, total, region_index, PAGE)
    return fmt.fit("\n".join(lines)), markup


@router.message(Command("top"))
async def cmd_top(message: Message, services: Services) -> None:
    text, markup = await _render(services, 0, None)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith(keyboards.CB_TOP))
async def cb_top(callback: CallbackQuery, services: Services) -> None:
    offset_raw, _, region_raw = callback.data[len(keyboards.CB_TOP):].partition(":")
    offset = int(offset_raw) if offset_raw.isdigit() else 0
    region_index = int(region_raw) if region_raw.isdigit() else None
    text, markup = await _render(services, offset, region_index)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CB_REGIONS))
async def cb_regions(callback: CallbackQuery, services: Services) -> None:
    page_raw = callback.data[len(keyboards.CB_REGIONS):]
    page = int(page_raw) if page_raw.isdigit() else 0
    snapshot = await services.snapshots.get()
    regions = queries.filter_options(snapshot, "buyer_region")
    await callback.message.edit_reply_markup(reply_markup=keyboards.regions_keyboard(regions, page))
    await callback.answer("Оберіть регіон")
