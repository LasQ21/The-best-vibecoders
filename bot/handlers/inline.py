"""Режим inline: @бот UA-2026-... у будь-якому чаті, щоб поділитися карткою."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent

from .. import formatting as fmt
from .. import keyboards, queries
from ..services import Services

router = Router(name="inline")


@router.inline_query()
async def inline_tender(query: InlineQuery, services: Services) -> None:
    snapshot = await services.snapshots.get()
    text = (query.query or "").strip()
    rows = []
    tender_id = queries.find_tender_id(snapshot, text) if text else None
    if tender_id:
        rows = [queries.get_tender(snapshot, tender_id)]
    elif not text:
        rows, _ = queries.top(snapshot, None, 0, 5)
    results = []
    for row in rows:
        # web_app-кнопки в inline-повідомленнях не працюють — лише посилання.
        markup = keyboards._rows([keyboards.InlineKeyboardButton(text="Prozorro ↗", url=fmt.tender_url(row))]) \
            if fmt.tender_url(row) else None
        results.append(InlineQueryResultArticle(
            id=str(row["tender_id"]),
            title=f"{fmt.score(row['total_score'])} / 100 · {fmt.truncate(row['title'], 60)}",
            description=fmt.truncate(fmt.buyer_label(row["buyer_name"], row["buyer_edrpou"], short=True), 100),
            input_message_content=InputTextMessageContent(message_text=fmt.tender_card(row)),
            reply_markup=markup,
        ))
    await query.answer(results, cache_time=60, is_personal=False)
