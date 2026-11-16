
from __future__ import annotations

from dataclasses import dataclass

# --- Предметна область ------------------------------------------------------

COMPETITIVE_METHODS = ("aboveThreshold", "priceQuotation", "belowThreshold")

# Реальні ціни за одиницю. estimated_from_award (сума ÷ кількість) не змішуємо.
REAL_PRICE_SOURCES = ("contract_item", "tender_item")

PROZORRO_TENDER_URL = "https://prozorro.gov.ua/tender/{tender_code}"

# --- Вхідні таблиці (data_loader.load_data) ---------------------------------

# Колонки, які мають читатися як рядки (провідні нулі в ЄДРПОУ).
TENDERS_STR_COLUMNS = (
    "tender_id", "tender_code", "buyer_edrpou", "winner_edrpou", "cpv_main",
)
ITEMS_STR_COLUMNS = (
    "tender_id", "item_id", "cpv_id", "unit_code", "lot_id", "profile_id",
    "category_id",
)
AWARDS_STR_COLUMNS = ("tender_id", "award_id", "supplier_edrpou", "lot_id")

# Колонки з датами: tz-aware, приведені до UTC (у сирих даних +02:00 і +03:00).
TENDERS_DATE_COLUMNS = ("tender_start", "tender_end", "date_created", "date_modified")
AWARDS_DATE_COLUMNS = ("date",)

TENDERS_COLUMNS = (
    "tender_id", "tender_code", "title", "buyer_name", "buyer_edrpou",
    "buyer_kind", "buyer_region", "buyer_locality", "method_type", "status",
    "main_category", "cpv_main", "expected_value", "currency", "vat_included",
    "final_value", "final_vat_included", "price_drop_pct", "number_of_bids",
    "tender_start", "tender_end", "submission_days", "date_created",
    "date_modified", "winner_name", "winner_edrpou", "n_items", "n_lots",
    "n_winners",
)
ITEMS_COLUMNS = (
    "tender_id", "item_id", "description", "cpv_id", "cpv_description",
    "quantity", "unit_code", "unit_name", "unit_price", "currency",
    "unit_price_estimated", "unit_price_vat_included", "price_source",
    "lot_id", "profile_id", "category_id",
)
AWARDS_COLUMNS = (
    "tender_id", "award_id", "status", "supplier_name", "supplier_edrpou",
    "value", "date", "lot_id", "currency", "vat_included",
)

# --- Проміжні та вихідні таблиці --------------------------------------------

FLAG_COLUMNS = (
    "flag_price",
    "flag_single_bid",
    "flag_repeat_winner",
    "flag_short_period",
)

# flags.compute_flags(tenders, items)
FLAGS_COLUMNS = ("tender_id", *FLAG_COLUMNS, "price_ratio", "reasons")

# flags.item_price_ratios(items) — для сторінки «Ціни на товар»
ITEM_PRICE_COLUMNS = (
    "tender_id", "item_id", "profile_id", "cpv_id", "unit_code",
    "unit_price_vat_included", "unit_price", "group_key", "group_size",
    "group_median", "price_ratio", "price_basis",
)
PRICE_BASIS_VALUES = ("profile", "cpv")  # cpv = «грубе порівняння»

# ml.anomaly_scores(tenders, flags)
ML_COLUMNS = ("tender_id", "ml_score", "ml_reason")

# scoring.score_tenders(tenders, items) — єдине, що читає сайт
SCORE_COLUMNS = (
    "tender_id",
    *FLAG_COLUMNS,
    "price_ratio",
    "ml_score",
    "total_score",
    "reasons",
)


# --- Автооновлення (updater.fetch_updates) ----------------------------------

@dataclass(frozen=True)
class UpdateResult:
    """Результат fetch_updates(). Функція ніколи не кидає виняток."""

    added: int = 0
    updated: int = 0
    ok: bool = True
    message: str = ""


# --- Перевірки --------------------------------------------------------------

class ContractError(ValueError):
    """Таблиця не відповідає контракту."""


def _require_columns(df, columns, name):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ContractError(f"{name}: бракує колонок {missing}")


def _require_unique_ids(df, name):
    if df["tender_id"].isna().any():
        raise ContractError(f"{name}: є порожні tender_id")
    if df["tender_id"].duplicated().any():
        raise ContractError(f"{name}: tender_id мають бути унікальні")


def _require_flags(df, name):
    for col in FLAG_COLUMNS:
        if not df[col].isin([0, 1]).all():
            raise ContractError(f"{name}: {col} має містити лише 0/1")


def _require_reasons(df, name, col="reasons"):
    if df[col].isna().any():
        raise ContractError(f"{name}: {col} не може бути NaN, лише ''")


def _require_range(df, col, lo, hi, name):
    values = df[col]
    if values.isna().any() or not values.between(lo, hi).all():
        raise ContractError(f"{name}: {col} має бути в межах [{lo}, {hi}] без NaN")


def validate_tables(tenders, items, awards):
    """Перевіряє вихід load_data()."""
    for df, cols, str_cols, name in (
        (tenders, TENDERS_COLUMNS, TENDERS_STR_COLUMNS, "tenders"),
        (items, ITEMS_COLUMNS, ITEMS_STR_COLUMNS, "items"),
        (awards, AWARDS_COLUMNS, AWARDS_STR_COLUMNS, "awards"),
    ):
        _require_columns(df, cols, name)
        for col in str_cols:
            if not df[col].dropna().map(type).eq(str).all():
                raise ContractError(f"{name}: {col} має бути рядком")
    _require_unique_ids(tenders, "tenders")
    for col in TENDERS_DATE_COLUMNS:
        if str(getattr(tenders[col].dtype, "tz", None)) != "UTC":
            raise ContractError(f"tenders: {col} має бути датою з tz=UTC")


def validate_flags(df):
    """Перевіряє вихід flags.compute_flags()."""
    _require_columns(df, FLAGS_COLUMNS, "flags")
    _require_unique_ids(df, "flags")
    _require_flags(df, "flags")
    _require_reasons(df, "flags")


def validate_ml(df):
    """Перевіряє вихід ml.anomaly_scores()."""
    _require_columns(df, ML_COLUMNS, "ml")
    _require_unique_ids(df, "ml")
    _require_range(df, "ml_score", 0, 1, "ml")
    _require_reasons(df, "ml", col="ml_reason")


def validate_scores(df):
    """Перевіряє вихід scoring.score_tenders() або sample_scores.csv."""
    _require_columns(df, SCORE_COLUMNS, "scores")
    _require_unique_ids(df, "scores")
    _require_flags(df, "scores")
    _require_range(df, "ml_score", 0, 1, "scores")
    _require_range(df, "total_score", 0, 100, "scores")
    _require_reasons(df, "scores")
    if not df["total_score"].is_monotonic_decreasing:
        raise ContractError("scores: рядки мають бути відсортовані за total_score спадно")


# --- Інтерфейс Streamlit ----------------------------------------------------

def main():
    """Інтерфейс сайту (Особа 3)."""


if __name__ == "__main__":
    main()
