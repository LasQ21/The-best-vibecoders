from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .. import formatting as fmt
from .. import keyboards, queries
from ..alerts import preview_count_fast
from ..services import Services
from ..storage import Subscription, utcnow

router = Router(name="alerts")


async def _status_text(services: Services, sub: Subscription) -> str:
    if not sub.active:
        state = "🔕 вимкнені"
    elif sub.is_paused():
        state = f"⏸ пауза до {fmt.datetime_short(sub.paused_until)}"
    else:
        state = "🔔 увімкнені"
    lines = [f"<b>Сповіщення:</b> {state}", "",
             f"Поріг балу: від <b>{fmt.score(sub.min_score)}</b>",
             f"Регіони: {fmt.esc(', '.join(sub.regions)) if sub.regions else 'уся країна'}",
             "Прапорці: " + (", ".join(fmt.FLAG_LABELS.get(f, f) for f in sub.flags) if sub.flags else "будь-які"),
             f"Відстежувані замовники: {len(sub.buyers)} (поріг {fmt.score(sub.follow_min_score)})",
             f"Не частіше за {sub.max_per_hour} на годину — решта згортається в одне повідомлення."]
    try:
        snapshot = await services.snapshots.get()
        count = preview_count_fast(queries.recent_candidates(snapshot, 7), sub)
        lines += ["", f"З такими налаштуваннями за останні 7 днів було б "
                      f"<b>{fmt.plural(count, fmt.ALERT_FORMS)}</b>."]
    except Exception:
        pass
    lines += ["", "Сповіщаємо про тендери, у яких щойно визначили переможця і бал не нижчий за поріг. "
                  "Тендери, що вже були в даних, не розсилаються."]
    return "\n".join(lines)


async def show_alerts(message: Message, services: Services, chat_id: int, edit: bool = False) -> None:
    sub = services.storage.get_subscription(chat_id)
    text = await _status_text(services, sub)
    markup = keyboards.alerts_keyboard(services.settings, sub.active, sub.min_score, sub.is_paused())
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=markup)


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, services: Services) -> None:
    await show_alerts(message, services, message.chat.id)


@router.callback_query(F.data.startswith(keyboards.CB_ALERTS))
async def cb_alerts(callback: CallbackQuery, services: Services) -> None:
    chat_id = callback.message.chat.id
    action = callback.data[len(keyboards.CB_ALERTS):]
    storage = services.storage
    sub = storage.get_subscription(chat_id)
    note = None
    if action == "on":
        storage.set_active(chat_id, True)
        note = "Сповіщення увімкнено"
    elif action == "off":
        storage.set_active(chat_id, False)
        note = "Сповіщення вимкнено"
    elif action == "pause":
        storage.save_subscription(sub.with_changes(paused_until=utcnow() + timedelta(days=1)))
        note = "Пауза на добу"
    elif action == "resume":
        storage.save_subscription(sub.with_changes(paused_until=None))
        note = "Паузу знято"
    elif action.startswith("score:"):
        value = action.split(":", 1)[1]
        if value.isdigit():
            storage.save_subscription(sub.with_changes(min_score=float(value)))
            note = f"Поріг: {value}"
    await callback.answer(note)
    await show_alerts(callback.message, services, chat_id, edit=True)
