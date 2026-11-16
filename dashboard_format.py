"""Форматування чисел, сум і посилань для сайту (без Streamlit, тож легко тестується)."""

import pandas as pd

from app import PROZORRO_TENDER_URL

DASH = "—"


def text_value(value, fallback=DASH):
    return str(value) if pd.notna(value) and str(value).strip() else fallback


def number(value, decimals=0):
    """Число українською: вузький пробіл між тисячами і кома в дробовій частині."""
    if pd.isna(value):
        return DASH
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def percent(value):
    """Частка 0–1 у відсотках з одним знаком після коми."""
    return DASH if pd.isna(value) else f"{number(value * 100, 1)}%"


def money(value, currency="UAH"):
    if pd.isna(value):
        return DASH
    unit = {"UAH": "грн", "EUR": "EUR", "USD": "USD"}.get(currency, currency)
    decimals = 2 if abs(value) < 1000 else 0
    return f"{number(value, decimals)} {unit}"


def compact_money(value):
    """Великі суми в мільйонах і мільярдах гривень."""
    if pd.isna(value):
        return DASH
    if abs(value) >= 1_000_000_000:
        return f"{number(value / 1_000_000_000, 2)} млрд ₴"
    if abs(value) >= 1_000_000:
        return f"{number(value / 1_000_000, 2)} млн ₴"
    return money(value)


def tender_link(code):
    """Посилання на тендер на prozorro.gov.ua або None, якщо код не схожий на UA-…."""
    if pd.notna(code) and str(code).startswith("UA-"):
        return PROZORRO_TENDER_URL.format(tender_code=code)
    return None
