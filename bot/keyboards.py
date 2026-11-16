"""Inline-клавіатури. callback_data обмежена 64 байтами: префікс + tender_id (32) влазить."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from . import formatting as fmt
from .config import Settings

# Префікси callback_data
CB_TENDER = "t:"        # t:<tender_id> — картка тендера
CB_BUYER = "b:"         # b:<edrpou> — профіль замовника
CB_FOLLOW = "f:"        # f:<edrpou> — стежити
CB_UNFOLLOW = "uf:"     # uf:<edrpou>
CB_MUTE = "m:"          # m:<edrpou> — не показувати замовника
CB_UNMUTE = "um:"
CB_TOP = "top:"         # top:<offset>:<region_index|->
CB_REGIONS = "reg:"     # reg:<page>
CB_ALERTS = "al:"       # al:on | al:off | al:score:<n> | al:pause | al:menu


def _webapp_button(settings: Settings, text: str, screen: str = "") -> InlineKeyboardButton | None:
    if not settings.has_webapp:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=settings.webapp_link(screen)))


def _rows(*rows) -> InlineKeyboardMarkup:
    clean = [[button for button in row if button is not None] for row in rows]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in clean if row])


def start_keyboard(settings: Settings, active: bool) -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton(text="🔕 Вимкнути сповіщення", callback_data=CB_ALERTS + "off")
              if active else
              InlineKeyboardButton(text="🔔 Увімкнути сповіщення", callback_data=CB_ALERTS + "on"))
    return _rows([_webapp_button(settings, "📊 Відкрити дашборд")], [toggle],
                 [InlineKeyboardButton(text="🏆 Топ тендерів", callback_data=CB_TOP + "0:-"),
                  InlineKeyboardButton(text="ℹ️ Як рахується бал", callback_data="about")])


def tender_keyboard(settings: Settings, row, alert: bool = False) -> InlineKeyboardMarkup:
    tender_id = str(row["tender_id"])
    edrpou = "" if fmt.is_missing(row.get("buyer_edrpou")) else str(row.get("buyer_edrpou"))
    url = fmt.tender_url(row)
    first = [_webapp_button(settings, "📊 Відкрити в дашборді", f"tender/{tender_id}"),
             InlineKeyboardButton(text="Prozorro ↗", url=url) if url else None]
    rows = [first]
    if edrpou:
        if alert:
            rows.append([InlineKeyboardButton(text="👁 Стежити за замовником", callback_data=CB_FOLLOW + edrpou),
                         InlineKeyboardButton(text="🙈 Не показувати замовника", callback_data=CB_MUTE + edrpou)])
        else:
            rows.append([InlineKeyboardButton(text="🏢 Замовник", callback_data=CB_BUYER + edrpou),
                         InlineKeyboardButton(text="👁 Стежити", callback_data=CB_FOLLOW + edrpou)])
    return _rows(*rows)


def buyer_keyboard(settings: Settings, edrpou: str, followed: bool) -> InlineKeyboardMarkup:
    follow = (InlineKeyboardButton(text="✓ Ви стежите — скасувати", callback_data=CB_UNFOLLOW + edrpou)
              if followed else
              InlineKeyboardButton(text="👁 Стежити за замовником", callback_data=CB_FOLLOW + edrpou))
    return _rows([_webapp_button(settings, "📊 Профіль у дашборді", f"buyer/{edrpou}")], [follow])


def buyers_list_keyboard(buyers) -> InlineKeyboardMarkup:
    rows = []
    for _, row in buyers.iterrows():
        label = fmt.truncate(fmt.buyer_label(row["buyer_name"], row["buyer_edrpou"], short=True), 60)
        rows.append([InlineKeyboardButton(text=label, callback_data=CB_BUYER + str(row["buyer_edrpou"]))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def top_keyboard(settings: Settings, rows, offset: int, total: int, region_index: int | None,
                 size: int) -> InlineKeyboardMarkup:
    region_part = "-" if region_index is None else str(region_index)
    buttons = [[InlineKeyboardButton(text=f"{fmt.score(row['total_score'])} · {fmt.truncate(row['title'], 48)}",
                                     callback_data=CB_TENDER + str(row["tender_id"]))] for row in rows]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="← Назад",
                                        callback_data=f"{CB_TOP}{max(0, offset - size)}:{region_part}"))
    if offset + size < total:
        nav.append(InlineKeyboardButton(text="Далі →", callback_data=f"{CB_TOP}{offset + size}:{region_part}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="📍 Регіон", callback_data=CB_REGIONS + "0")]
                   + ([InlineKeyboardButton(text="Уся країна", callback_data=CB_TOP + "0:-")]
                      if region_index is not None else []))
    web = _webapp_button(settings, "📊 Усі в дашборді", "tenders")
    if web:
        buttons.append([web])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def regions_keyboard(regions: list[str], page: int, per_page: int = 12) -> InlineKeyboardMarkup:
    chunk = regions[page * per_page:(page + 1) * per_page]
    buttons, row = [], []
    for index, region in enumerate(chunk, start=page * per_page):
        row.append(InlineKeyboardButton(text=fmt.truncate(region.replace(" область", " обл."), 28),
                                        callback_data=f"{CB_TOP}0:{index}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"{CB_REGIONS}{page - 1}"))
    if (page + 1) * per_page < len(regions):
        nav.append(InlineKeyboardButton(text="→", callback_data=f"{CB_REGIONS}{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def alerts_keyboard(settings: Settings, active: bool, min_score: float, paused: bool) -> InlineKeyboardMarkup:
    toggle = (InlineKeyboardButton(text="🔕 Вимкнути", callback_data=CB_ALERTS + "off") if active
              else InlineKeyboardButton(text="🔔 Увімкнути", callback_data=CB_ALERTS + "on"))
    pause = InlineKeyboardButton(text="▶️ Зняти паузу" if paused else "⏸ Пауза на добу",
                                 callback_data=CB_ALERTS + ("resume" if paused else "pause"))
    scores = [InlineKeyboardButton(text=("• " if int(min_score) == value else "") + str(value),
                                   callback_data=f"{CB_ALERTS}score:{value}")
              for value in (30, 40, 50, 60)]
    return _rows([toggle, pause] if active else [toggle], scores,
                 [_webapp_button(settings, "⚙️ Регіони, прапорці, замовники", "alerts")])


def collapsed_keyboard(settings: Settings, min_score: float) -> InlineKeyboardMarkup | None:
    button = _webapp_button(settings, "📊 Переглянути в дашборді", f"tenders?min_score={int(min_score)}")
    return _rows([button]) if button else None
