"""Живе оновлення даних з публічного API Prozorro.

Читаємо зростаючий фід змін тендерів від збереженого курсора
(data/feed_state.json), відкидаємо те, що не аналізуємо, і завантажуємо
повні документи тендерів.

Будь-яка помилка мережі стає ApiError, щоб fetch_updates() міг спіймати
її в одному місці й не впасти.

Повні документи перетворюються на рядки з тими самими колонками, що в
data/tenders.csv, items.csv і awards.csv (tenders_to_tables).
"""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import app

logger = logging.getLogger(__name__)

# --- Налаштування -----------------------------------------------------------

API_URL = "https://public-api.prozorro.gov.ua/api/2.5"
DATA_DIR = Path(__file__).parent / "data"
STATE_FILE = "feed_state.json"
CSV_FILES = {"tenders": "tenders.csv", "items": "items.csv", "awards": "awards.csv"}

FEED_PAGE_SIZE = 1000   # максимум, який віддає API за одну сторінку фіду
FEED_FIELDS = "procuringEntity,status,procurementMethodType,dateModified,tenderID"
MAX_FEED_PAGES = 30     # запобіжник: не гортати фід безкінечно за один запуск
# Скільки тендерів обробляти за одне натискання кнопки. 30 — це 2–3,5 с
# (з них ~1,5 с перезапис CSV), а 200 — понад 12 с, задовго для демо.
UPDATE_LIMIT = 30

# Що аналізуємо: ті самі типи процедур і статуси, що й у data/tenders.csv.
TRACKED_METHODS = ("aboveThreshold", "priceQuotation", "belowThreshold", "reporting")
TRACKED_STATUSES = ("complete", "active.awarded")
EXCLUDED_KINDS = ("defense", "special")   # оборонні замовники закриті, не чіпаємо
COUNTED_BID_STATUSES = ("active",)        # які пропозиції рахуємо в number_of_bids

# Цілі колонки з пропусками: без Int64 pandas запише 3 як «3.0», не так, як у CSV.
INTEGER_COLUMNS = {
    "tenders": ("number_of_bids", "n_items", "n_lots", "n_winners"),
    "items": ("unit_price_estimated",),
}

REQUEST_TIMEOUT = 10    # секунд на один запит
MAX_RETRIES = 4         # спроб на запит при 429, 5xx чи обриві мережі
BACKOFF_SECONDS = 0.5   # затримка росте: 0,5, 1, 2 с (без мережі кнопка чекає ~3,5 с)
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

def read_state(data_dir=DATA_DIR):
    """Стан фіду з feed_state.json (курсор forward_offset та службові поля)."""
    with open(Path(data_dir) / STATE_FILE, encoding="utf-8") as file:
        return json.load(file)


def save_state(state, data_dir=DATA_DIR):
    """Записує стан фіду. Через тимчасовий файл, щоб збій не зіпсував курсор."""
    path = Path(data_dir) / STATE_FILE
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


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


# --- Перетворення JSON у рядки CSV -------------------------------------------------
# Колонки ті самі, що в data/*.csv (див. app.TENDERS_COLUMNS, ITEMS_COLUMNS, AWARDS_COLUMNS).

def _value(obj, key):
    """Поле з об'єкта value ({amount, currency, valueAddedTaxIncluded}) або None."""
    return (obj or {}).get(key)


def _days_between(start, end):
    """Кількість днів між двома ISO-датами, округлена до сотих."""
    if not start or not end:
        return None
    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return round(seconds / 86400, 2)


def _active_awards(tender):
    return [award for award in tender.get("awards", []) if award.get("status") == "active"]


def _price_drop_pct(tender, awards, final_value):
    """Зниження ціни у % від очікуваної вартості.

    У багатолотових тендерах порівнюємо лише з лотами, де є переможець:
    нерозіграний лот не означає, що ціну знизили.
    """
    if final_value is None:
        return None
    lots = tender.get("lots", [])
    if lots:
        awarded_lots = {award.get("lotID") for award in awards}
        expected = sum(_value(lot.get("value"), "amount") or 0
                       for lot in lots if lot["id"] in awarded_lots)
    else:
        expected = _value(tender.get("value"), "amount")
    if not expected:
        return None
    return round((expected - final_value) / expected * 100, 2)


def tender_row(tender):
    """Рядок для tenders.csv з повного документа тендера."""
    buyer = tender.get("procuringEntity", {})
    address = buyer.get("address", {})
    value = tender.get("value", {})
    items = tender.get("items", [])
    period = tender.get("tenderPeriod") or {}
    awards = _active_awards(tender)
    is_reporting = tender.get("procurementMethodType") == "reporting"

    final_value = round(sum(_value(award.get("value"), "amount") or 0 for award in awards), 2) or None
    expected_value = _value(value, "amount")
    price_drop_pct = _price_drop_pct(tender, awards, final_value)

    winner = (awards[0].get("suppliers") or [{}])[0] if awards else {}
    winners = {supplier.get("identifier", {}).get("id")
               for award in awards for supplier in award.get("suppliers", [])}
    bids = [bid for bid in tender.get("bids", []) if bid.get("status") in COUNTED_BID_STATUSES]

    return {
        "tender_id": tender["id"],
        "tender_code": tender.get("tenderID"),
        "title": tender.get("title"),
        "buyer_name": buyer.get("name"),
        "buyer_edrpou": buyer.get("identifier", {}).get("id"),
        "buyer_kind": buyer.get("kind"),
        "buyer_region": address.get("region"),
        "buyer_locality": address.get("locality"),
        "method_type": tender.get("procurementMethodType"),
        "status": tender.get("status"),
        "main_category": tender.get("mainProcurementCategory"),
        "cpv_main": items[0].get("classification", {}).get("id") if items else None,
        "expected_value": expected_value,
        "currency": _value(value, "currency"),
        "vat_included": _value(value, "valueAddedTaxIncluded"),
        "final_value": final_value,
        "final_vat_included": _value(awards[0].get("value"), "valueAddedTaxIncluded") if awards else None,
        "price_drop_pct": price_drop_pct,
        "number_of_bids": None if is_reporting else len(bids),
        "tender_start": period.get("startDate"),
        "tender_end": period.get("endDate"),
        "submission_days": _days_between(period.get("startDate"), period.get("endDate")),
        "date_created": tender.get("dateCreated"),
        "date_modified": tender.get("dateModified"),
        "winner_name": winner.get("name"),
        "winner_edrpou": winner.get("identifier", {}).get("id"),
        "n_items": len(items),
        "n_lots": len(tender.get("lots", [])),
        "n_winners": len(winners - {None}),
    }


def _item_prices(tender):
    """Ціна за одиницю для кожної позиції: item_id -> (ціна, ПДВ, джерело, оцінена?).

    Порядок джерел (від найточнішого):
    1. contract_item — ціна з позиції договору (у priceQuotation з /contracts/{id});
    2. tender_item — ціна, вказана в позиції тендера (буває в reporting);
    3. estimated_from_award — сума рішення ÷ кількість, лише якщо в лоті одна позиція.
    """
    prices = {}
    for contract in tender.get("contracts_full", []):
        for item in contract.get("items", []):
            unit_value = item.get("unit", {}).get("value")
            if unit_value and unit_value.get("amount") is not None:
                prices[item["id"]] = (unit_value["amount"], unit_value.get("valueAddedTaxIncluded"),
                                      "contract_item", 0)
    for item in tender.get("items", []):
        unit_value = item.get("unit", {}).get("value")
        if item["id"] not in prices and unit_value and unit_value.get("amount") is not None:
            prices[item["id"]] = (unit_value["amount"], unit_value.get("valueAddedTaxIncluded"),
                                  "tender_item", 0)

    items_per_lot = {}
    for item in tender.get("items", []):
        items_per_lot.setdefault(item.get("relatedLot"), []).append(item)
    for award in _active_awards(tender):
        lot_items = items_per_lot.get(award.get("lotID"), [])
        if len(lot_items) != 1:
            continue
        item = lot_items[0]
        amount, quantity = _value(award.get("value"), "amount"), item.get("quantity")
        if item["id"] not in prices and amount is not None and quantity:
            prices[item["id"]] = (round(amount / quantity, 4),
                                  _value(award.get("value"), "valueAddedTaxIncluded"),
                                  "estimated_from_award", 1)
    return prices


def item_rows(tender):
    """Рядки для items.csv: по одному на позицію тендера."""
    prices = _item_prices(tender)
    currency = _value(tender.get("value"), "currency")
    rows = []
    for item in tender.get("items", []):
        unit = item.get("unit", {})
        classification = item.get("classification", {})
        price, vat, source, estimated = prices.get(item["id"], (None, None, None, None))
        rows.append({
            "tender_id": tender["id"],
            "item_id": item["id"],
            "description": item.get("description"),
            "cpv_id": classification.get("id"),
            "cpv_description": classification.get("description"),
            "quantity": item.get("quantity"),
            "unit_code": unit.get("code"),
            "unit_name": unit.get("name"),
            "unit_price": price,
            "currency": currency,
            "unit_price_estimated": estimated,
            "unit_price_vat_included": vat,
            "price_source": source,
            "lot_id": item.get("relatedLot"),
            "profile_id": item.get("profile"),
            "category_id": item.get("category"),
        })
    return rows


def award_rows(tender):
    """Рядки для awards.csv: усі рішення тендера, з будь-яким статусом."""
    rows = []
    for award in tender.get("awards", []):
        supplier = (award.get("suppliers") or [{}])[0]
        rows.append({
            "tender_id": tender["id"],
            "award_id": award["id"],
            "status": award.get("status"),
            "supplier_name": supplier.get("name"),
            "supplier_edrpou": supplier.get("identifier", {}).get("id"),
            "value": _value(award.get("value"), "amount"),
            "date": award.get("date"),
            "lot_id": award.get("lotID"),
            "currency": _value(award.get("value"), "currency"),
            "vat_included": _value(award.get("value"), "valueAddedTaxIncluded"),
        })
    return rows


def tenders_to_tables(tenders):
    """Список повних документів -> три DataFrame з колонками як у data/*.csv."""
    tenders_df = pd.DataFrame([tender_row(t) for t in tenders], columns=app.TENDERS_COLUMNS)
    items_df = pd.DataFrame([row for t in tenders for row in item_rows(t)],
                            columns=app.ITEMS_COLUMNS)
    awards_df = pd.DataFrame([row for t in tenders for row in award_rows(t)],
                             columns=app.AWARDS_COLUMNS)
    for df, name in ((tenders_df, "tenders"), (items_df, "items")):
        for column in INTEGER_COLUMNS[name]:
            df[column] = df[column].astype("Int64")
    return tenders_df, items_df, awards_df


# --- Upsert у CSV -----------------------------------------------------------------

def _read_csv_as_text(path):
    """CSV як текст: збережені рядки запишуться назад байт у байт, без зміни форматів."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _write_csv(df, path):
    """Записує CSV через тимчасовий файл: або новий файл повністю, або старий без змін."""
    temporary = Path(path).with_suffix(".tmp")
    df.to_csv(temporary, index=False)
    os.replace(temporary, path)


def upsert_tables(new_tables, data_dir=DATA_DIR):
    """Зливає нові тендери з data/*.csv. Повертає (added, updated).

    updated рахує лише тендери, які справді змінилися (інша date_modified):
    тендер, на якому стоїть курсор, приходить повторно, але це не оновлення.

    Upsert, а не append: тендер, який змінився, замінює свій старий рядок.
    Для tenders — concat + drop_duplicates("tender_id", keep="last").
    Для items і awards старі рядки оновлених тендерів видаляємо цілком,
    бо позиції й рішення могли змінитися, а не лише додатися.
    """
    new_tenders = new_tables["tenders"]
    if new_tenders.empty:
        return 0, 0
    new_ids = set(new_tenders["tender_id"])

    old = {name: _read_csv_as_text(Path(data_dir) / file) for name, file in CSV_FILES.items()}
    old_modified = old["tenders"].set_index("tender_id")["date_modified"]
    known = new_tenders[new_tenders["tender_id"].isin(old_modified.index)]
    added = len(new_ids) - len(known)
    updated = int((known["date_modified"] != known["tender_id"].map(old_modified)).sum())

    merged = {
        "tenders": pd.concat([old["tenders"], new_tenders])
                     .drop_duplicates("tender_id", keep="last"),
    }
    for name in ("items", "awards"):
        kept = old[name][~old[name]["tender_id"].isin(new_ids)]
        merged[name] = pd.concat([kept, new_tables[name]])

    for name, file in CSV_FILES.items():
        _write_csv(merged[name], Path(data_dir) / file)
    return added, updated


# --- Головна функція ----------------------------------------------------------------

def _next_cursor(entries, documents, next_offset):
    """Курсор після запуску. Якщо частину тендерів не завантажили, починаємо з
    найранішого з них, щоб наступний запуск їх підхопив (повтори прибере upsert)."""
    fetched = {document["id"] for document in documents}
    missed = [entry["dateModified"] for entry in entries if entry["id"] not in fetched]
    return min(missed) if missed else next_offset


def fetch_updates(limit=UPDATE_LIMIT, since=None, data_dir=DATA_DIR):
    """Підтягує нові й змінені тендери з Prozorro і дописує їх у data/*.csv.

    limit: скільки тендерів максимум обробити за один запуск.
    since: datetime або ISO-рядок; якщо задано, читаємо фід від цього моменту,
        а не від збереженого курсора (для демо: «за останню годину»).
    Повертає app.UpdateResult(added, updated, ok, message). Ніколи не кидає
    виняток: без інтернету чи при збої CSV лишаються як були.
    """
    try:
        state = read_state(data_dir)
        offset = since.isoformat() if isinstance(since, datetime) else (since or state["forward_offset"])

        entries, next_offset = read_feed(offset, limit)
        documents, failed = fetch_tenders([entry["id"] for entry in entries])
        tables = dict(zip(("tenders", "items", "awards"), tenders_to_tables(documents)))
        added, updated = upsert_tables(tables, data_dir)

        state["forward_offset"] = _next_cursor(entries, documents, next_offset)
        state["last_update_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state, data_dir)
    except ApiError as error:
        logger.warning("Оновлення з Prozorro не вдалося: %s", error)
        return app.UpdateResult(ok=False, message="Prozorro недоступний, працюємо на збережених даних")
    except Exception as error:   # noqa: BLE001 — на демо краще повідомлення, ніж traceback
        logger.exception("Помилка під час оновлення")
        return app.UpdateResult(ok=False, message=f"Оновлення не вдалося: {error}")

    if added or updated:
        message = f"Додано {added}, оновлено {updated}"
    else:
        message = "Нових тендерів немає"
    if failed:
        message += f" (не завантажилось {failed}, спробуємо наступного разу)"
    return app.UpdateResult(added=added, updated=updated, ok=True, message=message)
