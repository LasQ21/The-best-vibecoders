"""Тексти бота: числа, дати, картки тендера й замовника (HTML для Telegram).

Кожне значення з даних проходить через html.escape. Порожнє значення показуємо як «—».
Формулювання нейтральні: «до перевірки», «високий бал», «варто подивитись».
"""

from __future__ import annotations

import html
import math
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

import app as contract
import charts

DISCLAIMER = "Бал означає «подивіться уважніше», а не «тут порушення»."
DASH = "—"
MESSAGE_LIMIT = 4096
TITLE_MAX = 200
REASON_MAX = 300
REASON_SEPARATOR = "; "

FLAG_LABELS = {
    "flag_price": "Переплата",
    "flag_single_bid": "Один учасник",
    "flag_repeat_winner": "Постійний переможець",
    "flag_short_period": "Короткий строк",
}
FLAG_HINTS = {
    "flag_price": "ціна за одиницю щонайменше вдвічі вища за медіану для того самого товару",
    "flag_single_bid": "у конкурентній процедурі подано одну пропозицію",
    "flag_repeat_winner": "переміг найчастіший постачальник замовника (≥40% його тендерів)",
    "flag_short_period": "строк подачі коротший за 10-й перцентиль для свого типу процедури",
}
SHORT_FLAG_KEYS = {"p": "flag_price", "s": "flag_single_bid", "r": "flag_repeat_winner",
                   "t": "flag_short_period"}

TENDER_FORMS = ("тендер", "тендери", "тендерів")
ALERT_FORMS = ("сповіщення", "сповіщення", "сповіщень")
BID_FORMS = ("учасник", "учасники", "учасників")

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # немає бази часових поясів
    KYIV = timezone(timedelta(hours=2))


# --- числа ------------------------------------------------------------------

def is_missing(value) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def number(value, digits: int = 0) -> str:
    """1234567.8 -> '1 234 568'; з digits=1 -> '1 234 567,8'."""
    if is_missing(value):
        return DASH
    return charts.format_number(float(value), digits)


def score(value) -> str:
    """57.94 -> '57,9'."""
    if is_missing(value):
        return DASH
    return charts.format_number(float(value), 1)


def percent(share, digits: int = 0) -> str:
    """0.541 -> '54%' (digits=1 -> '54,1%')."""
    if is_missing(share):
        return DASH
    return f"{charts.format_number(float(share) * 100, digits)}%"


def amount(value, currency: str | None = "UAH") -> str:
    """248300 -> '248,3 тис. грн'. Інша валюта: '1 200 EUR'."""
    if is_missing(value):
        return DASH
    value = float(value)
    if currency and currency != "UAH" and not is_missing(currency):
        return f"{charts.format_number(value)} {html.escape(str(currency))}"
    return charts.format_amount(value)


def plural(count, forms) -> str:
    return charts.plural(int(count), forms)


def days(value) -> str:
    if is_missing(value):
        return DASH
    return f"{charts.format_number(float(value), 1)} дн."


def to_kyiv(moment) -> datetime | None:
    if is_missing(moment):
        return None
    if isinstance(moment, str):
        moment = pd.Timestamp(moment)
    if isinstance(moment, pd.Timestamp):
        moment = moment.to_pydatetime()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KYIV)


def date(moment) -> str:
    local = to_kyiv(moment)
    return local.strftime("%d.%m.%Y") if local else DASH


def datetime_short(moment) -> str:
    local = to_kyiv(moment)
    return local.strftime("%d.%m.%Y %H:%M") if local else DASH


def ago(moment, now: datetime | None = None) -> str:
    """'5 хв тому', '3 год тому', '2 дн. тому'."""
    local = to_kyiv(moment)
    if local is None:
        return DASH
    delta = (now or datetime.now(timezone.utc)) - local
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 1:
        return "щойно"
    if minutes < 60:
        return f"{minutes} хв тому"
    if minutes < 48 * 60:
        return f"{minutes // 60} год тому"
    return f"{minutes // 1440} дн. тому"


# --- текст --------------------------------------------------------------------

def esc(value) -> str:
    """html.escape для будь-якого значення; порожнє -> «—»."""
    if is_missing(value) or str(value).strip() == "":
        return DASH
    return html.escape(str(value), quote=False)


def clean(text) -> str:
    """Схлопнути пробіли й переноси: назви в даних бувають на кілька абзаців."""
    if is_missing(text):
        return ""
    return re.sub(r"\s+", " ", str(text).replace("­", "")).strip()


def truncate(text, limit: int) -> str:
    text = clean(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip(" ,;:.-") + "…"


def buyer_label(name, edrpou, short: bool = False) -> str:
    """«назва (ЄДРПОУ)», ЄДРПОУ — рядок з провідними нулями."""
    shown = charts.short_name(clean(name), 10_000) if short else clean(name)
    if not shown:
        shown = DASH
    edrpou = "" if is_missing(edrpou) else str(edrpou).strip()
    return f"{shown} ({edrpou})" if edrpou else shown


def reasons_list(reasons, limit: int = REASON_MAX) -> list[str]:
    if is_missing(reasons):
        return []
    return [truncate(part, limit) for part in str(reasons).split(REASON_SEPARATOR) if part.strip()]


def procedure(row) -> str:
    value = row.get("procedure") if hasattr(row, "get") else None
    if is_missing(value) or not value:
        value = row.get("method_type")
    return clean(value) or DASH


def tender_amount(row) -> str:
    value = row.get("final_value")
    if is_missing(value):
        value = row.get("expected_value")
    return amount(value, row.get("currency"))


def bids(row) -> str:
    value = row.get("number_of_bids")
    return DASH if is_missing(value) else str(int(value))


def active_flags(row) -> list[str]:
    return [column for column in contract.FLAG_COLUMNS
            if not is_missing(row.get(column)) and int(row.get(column)) == 1]


def tender_url(row) -> str:
    code = row.get("tender_code")
    if is_missing(code) or not str(code).strip():
        return ""
    return contract.PROZORRO_TENDER_URL.format(tender_code=str(code).strip())


# --- картки ------------------------------------------------------------------------

def tender_card(row, header: str | None = None, demo: bool = False,
                max_reasons: int = 6) -> str:
    """Картка тендера для чату (як у сповіщенні).

    header: перший рядок; без нього — «Тендер до перевірки» чи «Тендер» за балом.
    """
    total = row.get("total_score")
    if header is None:
        header = "Тендер"
    head = f"{header} · <b>{score(total)}</b> / 100"
    if demo:
        head += " (демо)"
    lines = [head, ""]
    lines.append(f"<b>{esc(truncate(row.get('title'), TITLE_MAX))}</b>")
    lines.append(f"Замовник: {esc(buyer_label(row.get('buyer_name'), row.get('buyer_edrpou')))}")
    lines.append(f"{esc(row.get('buyer_region'))} · {esc(procedure(row))}")
    lines.append(f"Сума: {tender_amount(row)} · учасників: {bids(row)}")
    winner = row.get("winner_name")
    if not is_missing(winner) and clean(winner):
        lines.append(f"Переможець: {esc(buyer_label(winner, row.get('winner_edrpou'), short=True))}")
    code = row.get("tender_code")
    if not is_missing(code):
        lines.append(f"<code>{esc(code)}</code>")

    reasons = reasons_list(row.get("reasons"))
    if reasons:
        lines += ["", "Чому в списку:"]
        lines += [f"• {esc(reason)}" for reason in reasons[:max_reasons]]
        if len(reasons) > max_reasons:
            lines.append(f"• і ще {len(reasons) - max_reasons}")
    elif not is_missing(total) and float(total) == 0:
        lines += ["", "Прапорці не спрацювали."]
    lines += ["", f"<i>{DISCLAIMER}</i>"]
    return fit("\n".join(lines))


def alert_header() -> str:
    return "Новий тендер до перевірки"


def collapsed_text(count: int, min_score: float) -> str:
    return (f"Ще {plural(count, TENDER_FORMS)} з балом від {score(min_score)} "
            "за цю годину. Щоб не засипати чат, вони зібрані в дашборді.")


def buyer_card(profile: dict) -> str:
    """Профіль замовника (див. queries.buyer_profile)."""
    lines = [f"<b>{esc(buyer_label(profile['name'], profile['edrpou']))}</b>",
             esc(profile.get("region")), ""]
    lines.append(f"Тендерів у даних: {number(profile['tenders_total'])} "
                 f"(конкурентних: {number(profile['competitive'])})")
    lines.append(f"З одним учасником: {percent(profile['single_bid_share'])}")
    top = profile.get("top_supplier")
    if top:
        lines.append(f"Головний постачальник: {esc(charts.short_name(top['name'], 80))} — "
                     f"{plural(top['wins'], TENDER_FORMS)} ({percent(top['share'])})")
    lines.append(f"Середній бал: {score(profile['mean_score'])} · "
                 f"з високим балом: {number(profile['high_count'])}")
    top_tenders = profile.get("top_tenders") or []
    if top_tenders:
        lines += ["", "Найвищий бал:"]
        for tender in top_tenders[:3]:
            lines.append(f"• {score(tender['total_score'])} — {esc(truncate(tender['title'], 80))}")
    lines += ["", f"<i>{DISCLAIMER}</i>"]
    return fit("\n".join(lines))


def fit(text: str, limit: int = MESSAGE_LIMIT) -> str:
    """Telegram приймає до 4096 знаків; ріжемо за рядками, не посеред тегу."""
    if len(text) <= limit:
        return text
    kept, size = [], 0
    for line in text.split("\n"):
        if size + len(line) + 1 > limit - 2:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept).rstrip() + "\n…"


def short_tender_line(row, index: int | None = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    return (f"{prefix}<b>{score(row.get('total_score'))}</b> · "
            f"{esc(truncate(row.get('title'), 90))}\n"
            f"    {esc(truncate(buyer_label(row.get('buyer_name'), row.get('buyer_edrpou'), short=True), 90))}"
            f" · {esc(row.get('buyer_region'))}")


def about_text(weights: dict, threshold: float) -> str:
    names = {**FLAG_LABELS, "ml_score": "Оцінка моделі (IsolationForest)"}
    lines = ["<b>Як рахується бал</b>", "",
             "Кожен тендер отримує бал від 0 до 100: чотири прості правила («прапорці») "
             "і оцінка нетиповості від моделі машинного навчання. Поруч завжди пояснення.", ""]
    for key, weight in weights.items():
        lines.append(f"• {names.get(key, key)} — до {weight} балів")
    lines += ["", f"Високий бал — від {score(threshold)}: на знімку це близько 1% тендерів.",
              "Прямі договори (<i>reporting</i>) мають бал 0: там немає учасників і строків.",
              "", "Джерело: публічний API Prozorro, цивільні тендери зі статусом "
              "«завершено» або «визначено переможця».", "", f"<i>{DISCLAIMER}</i>"]
    return "\n".join(lines)


def safe_float(value, default=math.nan) -> float:
    try:
        return default if is_missing(value) else float(value)
    except (TypeError, ValueError):
        return default
