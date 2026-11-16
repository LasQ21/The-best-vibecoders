"""Завантаження даних Prozorro з data/*.csv.

Головна функція: load_data(data_dir=DATA_DIR) -> (tenders, items, awards).
Назви колонок і їхні типи задані контрактом у app.py.
"""

from pathlib import Path

import pandas as pd

# Єдине місце зі шляхом до даних. Шлях лише відносний до цього файлу.
DATA_DIR = Path(__file__).parent / "data"

TENDERS_FILE = "tenders.csv"
ITEMS_FILE = "items.csv"
AWARDS_FILE = "awards.csv"

# Кількість учасників: ціле число з пропусками (у reporting учасників немає).
NULLABLE_INT_COLUMNS = ("number_of_bids",)


def read_table(path, str_columns, date_columns=()):
    """Читає один CSV.

    str_columns читаються як рядки, щоб ЄДРПОУ 02007472 не став числом 2007472.
    date_columns приводяться до UTC: у сирих даних змішані +02:00 і +03:00,
    а після оновлення pandas записує їх уже як 2026-06-17 11:37:14+00:00.
    Порожня дата стає NaT. Решта колонок лишається такою, як її прочитав pandas.
    """
    table = pd.read_csv(path, dtype={column: str for column in str_columns},
                        low_memory=False)
    for column in date_columns:
        table[column] = pd.to_datetime(table[column], utc=True, format="ISO8601")
    return table


def to_nullable_int(values):
    """'2', 2.0 і порожнє значення -> Int64 (2, 2, <NA>)."""
    return pd.to_numeric(values, errors="coerce").round().astype("Int64")


def load_data(data_dir=DATA_DIR):
    """Три таблиці з data_dir: tenders, items, awards.

    tenders: по рядку на тендер, tender_id унікальний.
    items і awards посилаються на tenders через tender_id.
    """
    # Імпорт тут, а не вгорі файлу: Streamlit запускає app.py як __main__,
    # і той імпортує цей модуль. Імпорт app на рівні модуля дав би цикл.
    import app

    data_dir = Path(data_dir)
    tenders = read_table(data_dir / TENDERS_FILE, app.TENDERS_STR_COLUMNS,
                         app.TENDERS_DATE_COLUMNS)
    items = read_table(data_dir / ITEMS_FILE, app.ITEMS_STR_COLUMNS)
    awards = read_table(data_dir / AWARDS_FILE, app.AWARDS_STR_COLUMNS,
                        app.AWARDS_DATE_COLUMNS)

    for column in NULLABLE_INT_COLUMNS:
        tenders[column] = to_nullable_int(tenders[column])
    return tenders, items, awards
