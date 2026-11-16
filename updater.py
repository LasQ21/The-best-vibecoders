"""Живе оновлення даних з публічного API Prozorro.

Читаємо зростаючий фід змін тендерів від збереженого курсора
(data/feed_state.json), відкидаємо те, що не аналізуємо, і завантажуємо
повні документи тендерів.

Тут лише мережева частина. Будь-яка помилка мережі стає ApiError,
щоб fetch_updates() міг спіймати її в одному місці й не впасти.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- Налаштування -----------------------------------------------------------

API_URL = "https://public-api.prozorro.gov.ua/api/2.5"
STATE_PATH = Path(__file__).parent / "data" / "feed_state.json"

FEED_PAGE_SIZE = 1000   # максимум, який віддає API за одну сторінку фіду
FEED_FIELDS = "procuringEntity,status,procurementMethodType,dateModified,tenderID"
MAX_FEED_PAGES = 30     # запобіжник: не гортати фід безкінечно за один запуск

# Що аналізуємо: ті самі типи процедур і статуси, що й у data/tenders.csv.
TRACKED_METHODS = ("aboveThreshold", "priceQuotation", "belowThreshold", "reporting")
TRACKED_STATUSES = ("complete", "active.awarded")
EXCLUDED_KINDS = ("defense", "special")   # оборонні замовники закриті, не чіпаємо

REQUEST_TIMEOUT = 10    # секунд на один запит
MAX_RETRIES = 4         # спроб на запит при 429, 5xx чи обриві мережі
BACKOFF_SECONDS = 1.0   # затримка росте: 1, 2, 4, 8 с
MAX_WORKERS = 4         # паралельних запитів, щоб API не відповів 429


def _ssl_context():
    """SSL-контекст із сертифікатами certifi, якщо пакет є.

    Python з python.org на macOS не бачить системних сертифікатів, і без цього
    кожен HTTPS-запит падає з CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = _ssl_context()


class ApiError(Exception):
    """API Prozorro недоступний або відповів помилкою після всіх повторів."""


# --- HTTP ---------------------------------------------------------------------

def get_json(url):
    """GET запит з повторами й зростаючою затримкою. Повертає розібраний JSON.

    Повторюємо при 429 (забагато запитів), 5xx і мережевих помилках.
    Інші коди (наприклад, 404) повторювати немає сенсу.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT,
                                        context=SSL_CONTEXT) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise ApiError(f"Prozorro відповів {error.code} на {url}") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
        except json.JSONDecodeError as error:
            raise ApiError(f"Prozorro повернув не JSON на {url}") from error
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_SECONDS * 2 ** attempt)
    raise ApiError(f"Prozorro недоступний: {last_error}")


# --- Курсор ---------------------------------------------------------------------

def read_state(path=STATE_PATH):
    """Стан фіду з feed_state.json (курсор forward_offset та службові поля)."""
    with open(path, encoding="utf-8") as file:
        return json.load(file)


# --- Фід ----------------------------------------------------------------------------

def feed_url(offset):
    """URL сторінки зростаючого фіду від offset.

    offset — або непрозорий курсор API (з next_page.offset), або ISO-дата
    dateModified: API приймає обидва.
    """
    query = urllib.parse.urlencode({
        "offset": offset,
        "limit": FEED_PAGE_SIZE,
        "opt_fields": FEED_FIELDS,
    })
    return f"{API_URL}/tenders?{query}"


def is_tracked(entry):
    """Чи цей запис фіду з тих, що ми аналізуємо (цивільні, завершені, наших типів)."""
    kind = entry.get("procuringEntity", {}).get("kind")
    return (entry.get("procurementMethodType") in TRACKED_METHODS
            and entry.get("status") in TRACKED_STATUSES
            and kind not in EXCLUDED_KINDS)


def read_feed(offset, limit):
    """Гортає фід від offset і збирає до limit записів, які ми відстежуємо.

    Повертає (entries, next_offset). next_offset — звідки читати наступного разу:
    - якщо дійшли до кінця фіду, це next_page.offset останньої сторінки;
    - якщо зупинилися на limit посеред сторінки, це dateModified останнього
      взятого запису, тож решта сторінки не загубиться (повтори прибере upsert).
    """
    entries = []
    for _ in range(MAX_FEED_PAGES):
        page = get_json(feed_url(offset))
        data = page.get("data", [])
        page_offset = page.get("next_page", {}).get("offset", offset)
        for entry in data:
            if not is_tracked(entry):
                continue
            entries.append(entry)
            if len(entries) >= limit:
                return entries, entry["dateModified"]
        offset = page_offset
        if not data:   # дійшли до кінця фіду
            break
    return entries, offset


# --- Повні документи ------------------------------------------------------------

def fetch_tender(tender_id):
    """Повний документ тендера.

    У priceQuotation ціни позицій договору є лише в /contracts/{id},
    тому для активних договорів таких тендерів довантажуємо їх окремо
    і кладемо в tender["contracts_full"].
    """
    tender = get_json(f"{API_URL}/tenders/{tender_id}")["data"]
    if tender.get("procurementMethodType") == "priceQuotation":
        tender["contracts_full"] = [
            get_json(f"{API_URL}/contracts/{contract['id']}")["data"]
            for contract in tender.get("contracts", [])
            if contract.get("status") == "active"
        ]
    return tender


def _fetch_or_none(tender_id):
    """fetch_tender, але помилка одного тендера не зупиняє решту."""
    try:
        return fetch_tender(tender_id)
    except ApiError:
        return None


def fetch_tenders(tender_ids):
    """Завантажує тендери паралельно (MAX_WORKERS потоків).

    Повертає (tenders, failed): список документів і кількість тих,
    які не вдалося завантажити.
    """
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_fetch_or_none, tender_ids))
    tenders = [tender for tender in results if tender is not None]
    return tenders, len(results) - len(tenders)
