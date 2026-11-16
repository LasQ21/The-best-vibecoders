# Спільний контракт

Домовленість про назви функцій, таблиць і колонок між чотирма модулями.
Ті самі назви в коді лежать у [`app.py`](app.py): імпортуйте константи звідти, а не пишіть рядки вручну.

**Змінювати лише після попередження в чаті команди.** Додати нову необов'язкову колонку у свою проміжну таблицю можна без узгодження. Перейменувати чи видалити колонку з контракту — тільки після узгодження.

## Потік даних

```
data/*.csv
  └─ data_loader.load_data()           → tenders, items, awards        (Особа 4)
       ├─ flags.compute_flags()        → прапорці + price_ratio + reasons (Особа 1)
       └─ ml.anomaly_scores()          → ml_score + ml_reason             (Особа 2)
            └─ scoring.score_tenders() → одна таблиця для сайту           (Особи 1+2)
                 └─ app.py             → лише показує                     (Особа 3)

updater.fetch_updates() → дописує data/*.csv → сайт очищає кеш і перечитує  (Особа 2)
```

## Файли й власники

| Файл | Власник | Що всередині |
| --- | --- | --- |
| `data_loader.py` | Особа 4 | `load_data()`, `DATA_DIR` |
| `flags.py` | Особа 1 | `compute_flags()`, `item_price_ratios()` |
| `scoring.py` | Особи 1+2 | `score_tenders()`, ваги |
| `ml.py` | Особа 2 | `anomaly_scores()` |
| `updater.py` | Особа 2 | `fetch_updates()` |
| `app.py` | Особа 3 (контрактна частина — усі) | константи й перевірки контракту вгорі файлу, нижче інтерфейс |
| `CONTRACT.md` | усі | цей контракт |
| `charts.py` | Особа 4 | функції, що повертають фігури plotly |
| `sample_scores.csv` | Особа 4 | заглушка для сайту, поки ядра немає |
| `README.md`, `requirements.txt`, `.gitignore` | Особа 4 | |

Чужі файли не редагуємо: знайшли помилку — пишемо власнику.

## Функції

### `data_loader.load_data(data_dir: Path = DATA_DIR) -> tuple[tenders, items, awards]`

```python
tenders, items, awards = load_data()
```

- `DATA_DIR = Path(__file__).parent / "data"`. Це єдине місце з шляхом до даних, шлях лише відносний.
- Як рядки (`dtype=str`): `app.TENDERS_STR_COLUMNS`, `ITEMS_STR_COLUMNS`, `AWARDS_STR_COLUMNS` (ЄДРПОУ, `cpv_*`, `profile_id`, `category_id`, усі `*_id`). Інакше ЄДРПОУ `02007472` перетвориться на `2007472`.
- Дати: `pd.to_datetime(..., utc=True, format="ISO8601")`. У сирих даних змішано `+02:00` і `+03:00` та різну кількість знаків після секунд, тому все в UTC.
- `number_of_bids` має тип `Int64` (nullable): у `reporting` він порожній.
- Повертає три таблиці; кожна містить усі колонки CSV (`app.*_COLUMNS`).
- `tenders.tender_id` унікальний (перевірено: 17 981 тендер). `items` (70 125 рядків) і `awards` (21 564 рядки) посилаються на `tenders.tender_id`.

### `flags.compute_flags(tenders, items) -> DataFrame`

Один рядок на **кожен** тендер з `tenders`, зокрема на `reporting` (там прапорці 0).

| Колонка | Тип | Значення |
| --- | --- | --- |
| `tender_id` | str | ключ |
| `flag_price` | int 0/1 | переплата за одиницю |
| `flag_single_bid` | int 0/1 | один учасник у конкурентній процедурі |
| `flag_repeat_winner` | int 0/1 | постійний переможець замовника |
| `flag_short_period` | int 0/1 | короткий строк подачі для свого `method_type` |
| `price_ratio` | float | максимум ціна/медіана по позиціях тендера; `NaN`, якщо порівнянних цін немає |
| `reasons` | str | пояснення українською через `; `, а `""`, якщо нічого не спрацювало. Ніколи `NaN` |

Пороги задаються константами вгорі `flags.py`, кожне правило в окремій функції.

### `flags.item_price_ratios(items) -> DataFrame`

Порівняння цін на рівні позицій. Використовується всередині `compute_flags()` і на сторінці «Ціни на товар».
Лише `price_source` з `app.REAL_PRICE_SOURCES` і `unit_price > 0`.

Колонки (`app.ITEM_PRICE_COLUMNS`): `tender_id`, `item_id`, `profile_id`, `cpv_id`, `unit_code`, `unit_price_vat_included`, `unit_price`, `group_key` (рядок, за яким групували), `group_size`, `group_median`, `price_ratio`, `price_basis` (`"profile"`, або `"cpv"` для грубого порівняння).

### `ml.anomaly_scores(tenders, flags) -> DataFrame`

| Колонка | Тип | Значення |
| --- | --- | --- |
| `tender_id` | str | ключ, по рядку на кожен тендер з `tenders` |
| `ml_score` | float 0–1 | 1 означає найаномальніший; для неконкурентних процедур `0.0` |
| `ml_reason` | str | 1–2 ознаки з найбільшим z-score для топових тендерів, інакше `""` |

`random_state=42`: результат не змінюється між запусками.

### `scoring.score_tenders(tenders, items) -> DataFrame`

Єдина функція ядра, яку викликає сайт. Усередині викликає `compute_flags()` і `anomaly_scores()`.

Колонки рівно `app.SCORE_COLUMNS`:

| Колонка | Тип | Значення |
| --- | --- | --- |
| `tender_id` | str | ключ, по рядку на тендер |
| `flag_price`, `flag_single_bid`, `flag_repeat_winner`, `flag_short_period` | int 0/1 | з `compute_flags()` |
| `price_ratio` | float | з `compute_flags()`, може бути `NaN` |
| `ml_score` | float 0–1 | з `anomaly_scores()`, без `NaN` |
| `total_score` | float 0–100 | зважена сума, округлена до 0,1 |
| `reasons` | str | `reasons` з прапорців + `ml_reason` через `; ` |

- Рядки відсортовані за `total_score` спадно.
- Ваги задаються константами вгорі `scoring.py`, сума ваг 100. Стартова пропозиція: ціна 30, один учасник 20, постійний переможець 20, короткий строк 10, ML 20.
- Назву замовника, регіон, суму тощо сайт бере з `tenders` через `merge` по `tender_id`. У `scores` ці колонки не дублюємо.

### `updater.fetch_updates(limit: int = 200) -> app.UpdateResult`

```python
UpdateResult(added: int, updated: int, ok: bool, message: str)
```

- **Ніколи не кидає виняток.** Немає інтернету, 429 або таймаут: `UpdateResult(0, 0, ok=False, message="Prozorro недоступний, працюємо на збережених даних")`.
- Upsert у `data/tenders.csv`, `items.csv`, `awards.csv` через `drop_duplicates("tender_id", keep="last")`; курсор зберігається в `data/feed_state.json`.
- Два запуски поспіль не створюють дублів.

### `charts.py`

Кожна функція повертає `plotly.graph_objects.Figure` і нічого не показує сама.

```python
price_histogram(prices: pd.Series, median: float, highlight: float | None = None, title: str = "") -> Figure
top_winners(buyer_tenders: pd.DataFrame, n: int = 10) -> Figure        # buyer_tenders: рядки tenders одного замовника
purchases_over_time(buyer_tenders: pd.DataFrame, freq: str = "W") -> Figure
```

## Правила для сайту (`app.py`)

- Читає лише `load_data()` і `score_tenders()`, обидва під `@st.cache_data`.
- Інші модулі імпортують константи з `app.py`, тому інтерфейс Streamlit має бути у функції `main()`, яку викликає лише `if __name__ == "__main__": main()` (Streamlit запускає файл саме як `__main__`). Тоді `import app` у `flags.py` чи `ml.py` не запускає сайт. Модулі ядра імпортують `app` лише як `import app` і не імпортують з нього нічого, крім контракту.
- Агрегати сторінок (метрики огляду, профіль замовника) рахуються функціями в модулях, а не в `app.py`. Фільтрувати й сортувати таблицю в `app.py` можна.
- Поки ядра немає: `scores = pd.read_csv("sample_scores.csv", dtype={"tender_id": str}).fillna({"reasons": ""})`. Без `fillna` порожні `reasons` стають `NaN`. Формат той самий, тож перехід на справжні дані змінює один рядок. Бали в заглушці вигадані, `tender_id` справжні (серед них тендери обох демо-замовників).
- Посилання на тендер: `app.PROZORRO_TENDER_URL.format(tender_code=...)`.
- Замовник у списках показується як «назва (ЄДРПОУ)».

## Константи предметної області (`app.py`)

- `COMPETITIVE_METHODS = ("aboveThreshold", "priceQuotation", "belowThreshold")`. `reporting` не конкурентна.
- `REAL_PRICE_SOURCES = ("contract_item", "tender_item")`. `estimated_from_award` і порожні значення в порівняння цін не беремо.

## Перевірка контракту

```python
import app
app.validate_tables(tenders, items, awards)
app.validate_flags(flags)
app.validate_ml(ml)
app.validate_scores(scores)   # також для sample_scores.csv
```

Кожна функція кидає `app.ContractError` з поясненням, що не так. Запускайте перед кожним пушем, який змінює свою таблицю.

## Орієнтири з даних (для самоперевірки)

| Що | Значення |
| --- | --- |
| Тендерів | 17 981 (`priceQuotation` 7 306, `aboveThreshold` 6 372, `reporting` 3 500, `belowThreshold` 803) |
| Частка з одним учасником серед конкурентних | 54,1% (7 829 з 14 481) |
| Позицій з реальною ціною | 20 511 з 70 125 (`contract_item` 19 529, `tender_item` 982) |
| Демо: ДЖКГ Харківської міськради | ЄДРПОУ `43927048`, 90 конкурентних тендерів, 98% з одним учасником |
| Демо: Диканська лікарня планового лікування | ЄДРПОУ `01999265`, 94 тендери |
