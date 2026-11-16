"""Перевірка initData з Telegram Mini App.

Кожен запит до /app/v/* і /app/fig/* несе заголовок «Authorization: tma <initData>».
Підпис перевіряє aiogram.utils.web_app.safe_parse_webapp_init_data токеном бота;
auth_date старший за 24 години відхиляємо. chat_id для підписки беремо лише звідси.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.utils.web_app import safe_parse_webapp_init_data

from ..config import AUTH_MAX_AGE_SEC, DEV_USER_ID, Settings

SCHEME = "tma"


class AuthError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WebUser:
    user_id: int
    first_name: str = ""
    language: str | None = None
    dev: bool = False

    @property
    def chat_id(self) -> int:
        # Mini App з кнопки бота відкривається в приватному чаті, де chat_id == user_id.
        return self.user_id


def authenticate(settings: Settings, header: str | None, now: datetime | None = None) -> WebUser:
    if settings.dev_auth_bypass:
        return WebUser(user_id=DEV_USER_ID, first_name="Розробник", dev=True)
    if not header:
        raise AuthError("немає заголовка Authorization")
    scheme, _, init_data = header.partition(" ")
    if scheme.lower() != SCHEME or not init_data.strip():
        raise AuthError("очікується Authorization: tma <initData>")
    if not settings.bot_token:
        raise AuthError("бот не налаштований")
    try:
        data = safe_parse_webapp_init_data(settings.bot_token, init_data.strip())
    except ValueError as error:
        raise AuthError("підпис initData не збігається") from error
    now = now or datetime.now(timezone.utc)
    auth_date = data.auth_date
    if auth_date.tzinfo is None:
        auth_date = auth_date.replace(tzinfo=timezone.utc)
    if (now - auth_date).total_seconds() > AUTH_MAX_AGE_SEC:
        raise AuthError("initData застаріли, відкрийте дашборд знову")
    if data.user is None:
        raise AuthError("в initData немає користувача")
    return WebUser(user_id=data.user.id, first_name=data.user.first_name or "",
                   language=data.user.language_code)
