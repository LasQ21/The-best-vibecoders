"""Налаштування бота з оточення і файлу .env.

Два режими роботи:
- «процес» (python -m bot): long polling, вебсервер і цикл опитування в одному процесі;
- «vercel»: той самий вебсервер як функція Vercel, оновлення від Telegram приходять
  вебхуком, а опитування Prozorro запускає зовнішній планувальник через /cron/poll.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

DEFAULT_POLL_INTERVAL_SEC = 300
DEFAULT_POLL_LIMIT = 150
DEFAULT_DB_PATH = "bot_data/bot.db"
MAX_CATCHUP_CYCLES = 5
MAX_LAG_HOURS = 6
ALERT_WINDOW_HOURS = 48
FAILURES_BEFORE_ADMIN_ALERT = 6
SLOW_RELOAD_SEC = 30
AUTH_MAX_AGE_SEC = 24 * 3600
DEV_USER_ID = 1


class ConfigError(RuntimeError):
    """Налаштування суперечливі або небезпечні."""


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """Простий розбір .env: KEY=value, коментарі й порожні рядки пропускаємо."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _int(env, key, default):
    raw = env.get(key, "")
    try:
        return int(raw) if str(raw).strip() else default
    except ValueError as error:
        raise ConfigError(f"{key} має бути цілим числом") from error


def _ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError as error:
                raise ConfigError("ADMIN_IDS — це chat_id через кому") from error
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    webapp_url: str = ""
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    bot_env: str = "prod"
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
    poll_limit: int = DEFAULT_POLL_LIMIT
    db_path: Path = ROOT / DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = 8000
    dev_auth_bypass: bool = False
    cron_secret: str = ""
    on_vercel: bool = False
    blob_enabled: bool = False
    max_catchup_cycles: int = MAX_CATCHUP_CYCLES
    max_lag_hours: int = MAX_LAG_HOURS

    @property
    def has_webapp(self) -> bool:
        return bool(self.webapp_url)

    @property
    def webhook_secret(self) -> str:
        """Секрет для заголовка X-Telegram-Bot-Api-Secret-Token, виведений з токена.

        Сам токен у URL вебхука не кладемо: він потрапив би в логи доступу.
        """
        if not self.bot_token:
            return ""
        return hashlib.sha256(f"webhook:{self.bot_token}".encode()).hexdigest()[:48]

    def is_admin(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.admin_ids

    def webapp_link(self, screen: str = "") -> str:
        """Адреса Mini App, що одразу відкриває потрібний екран (?s=tender/<id>).

        Екран передаємо в query, а не в #: Telegram дописує свої дані саме в hash.
        """
        if not self.webapp_url:
            return ""
        if not screen:
            return self.webapp_url
        joiner = "&" if "?" in self.webapp_url else "?"
        return f"{self.webapp_url}{joiner}s={quote(screen, safe='/')}"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Збирає Settings з .env і змінних оточення (оточення важливіше)."""
    if env is None:
        env = {**read_env_file(), **os.environ}
    on_vercel = bool(env.get("VERCEL"))
    webapp_url = env.get("WEBAPP_URL", "").strip().rstrip("/")
    production_host = env.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    if not webapp_url and on_vercel and production_host:
        webapp_url = f"https://{production_host}/app"

    db_raw = env.get("BOT_DB_PATH", "").strip() or DEFAULT_DB_PATH
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    settings = Settings(
        bot_token=env.get("BOT_TOKEN", "").strip(),
        webapp_url=webapp_url,
        admin_ids=_ids(env.get("ADMIN_IDS", "")),
        bot_env=(env.get("BOT_ENV", "") or "prod").strip().lower(),
        poll_interval_sec=max(30, _int(env, "POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)),
        poll_limit=max(1, _int(env, "POLL_LIMIT", DEFAULT_POLL_LIMIT)),
        db_path=db_path,
        host=env.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_int(env, "PORT", 8000),
        dev_auth_bypass=env.get("DEV_AUTH_BYPASS", "").strip() in ("1", "true", "yes"),
        cron_secret=env.get("CRON_SECRET", "").strip(),
        on_vercel=on_vercel,
        blob_enabled=bool(env.get("BLOB_READ_WRITE_TOKEN") or env.get("VERCEL_BLOB_READ_WRITE_TOKEN")),
    )
    validate(settings)
    return settings


def validate(settings: Settings) -> None:
    """Небезпечні поєднання налаштувань зупиняють запуск."""
    if settings.dev_auth_bypass:
        if settings.bot_env != "dev" or settings.host != "127.0.0.1" or settings.on_vercel:
            raise ConfigError(
                "DEV_AUTH_BYPASS=1 дозволено лише з BOT_ENV=dev і HOST=127.0.0.1 (не на Vercel)")
    if settings.webapp_url and not settings.webapp_url.startswith("https://") \
            and not settings.dev_auth_bypass:
        raise ConfigError("WEBAPP_URL має бути HTTPS-адресою: Telegram не відкриває Mini App по HTTP")
