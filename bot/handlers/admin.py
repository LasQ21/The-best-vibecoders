"""Команди лише для ADMIN_IDS: /update, /status, /demo_alert."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from .. import formatting as fmt
from .. import keyboards, queries
from ..services import Services

router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, message: Message, services: Services) -> bool:
        return services.settings.is_admin(message.chat.id)


@router.message(Command("update"), IsAdmin())
async def cmd_update(message: Message, services: Services) -> None:
    if services.poller.lock.locked():
        await message.answer("Цикл опитування вже йде.")
        return
    await message.answer("Запускаю позачерговий цикл…")
    results = await services.poller.run()
    lines = [f"• {r.message} — взято {r.taken}, {r.seconds} с" for r in results]
    await message.answer("Готово:\n" + "\n".join(lines))


@router.message(Command("status"), IsAdmin())
async def cmd_status(message: Message, services: Services) -> None:
    status = services.poller.status()
    snapshot = services.snapshots.current
    last = status["last_cycle"] or {}
    lines = ["<b>Стан бота</b>", ""]
    if snapshot:
        lines.append(f"Знімок v{snapshot.version}: {fmt.number(len(snapshot.tenders))} тендерів, "
                     f"завантажено {fmt.ago(snapshot.loaded_at)} за {snapshot.load_seconds:.1f} с")
        lines.append(f"Джерело балу: {fmt.esc(snapshot.data.score_source)}"
                     + (" ⚠️ помилка балу" if snapshot.data.scoring_error else ""))
    else:
        lines.append("Знімок ще не завантажено")
    lines.append(f"Останній успішний цикл: {fmt.datetime_short(status['last_success'])}")
    if last:
        lines.append(f"Останній цикл: {fmt.esc(last.get('message'))} ({last.get('seconds')} с)")
    lines.append(f"Курсор: <code>{fmt.esc(status['cursor'])}</code>")
    lines.append(f"Невдач поспіль: {status['failures']}")
    lines.append(f"Підписників: {services.storage.count_subscribers()}, "
                 f"сповіщень за добу: {services.storage.alerts_last_day()}")
    mode = "Vercel (вебхук)" if services.settings.on_vercel else "процес (long polling)"
    lines.append(f"Режим: {mode}; дашборд: {'так' if services.settings.has_webapp else 'ні'}")
    await message.answer("\n".join(lines))


@router.message(Command("demo_alert"), IsAdmin())
async def cmd_demo_alert(message: Message, services: Services) -> None:
    snapshot = await services.snapshots.get()
    rows, _ = queries.top(snapshot, None, 0, 1)
    if not rows:
        await message.answer("У знімку немає тендерів з балом.")
        return
    row = rows[0]
    await message.answer(fmt.tender_card(row, header=fmt.alert_header(), demo=True),
                         reply_markup=keyboards.tender_keyboard(services.settings, row, alert=True))
