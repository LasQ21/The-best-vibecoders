"""Prepare the data used by the Streamlit dashboard."""

import logging
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

import pandas as pd

import app as contract

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
METHOD_LABELS = {
    "aboveThreshold": "Відкриті торги",
    "belowThreshold": "Допорогова закупівля",
    "priceQuotation": "Запит ціни пропозицій",
    "reporting": "Звіт про договір",
}
CATEGORY_LABELS = {"goods": "Товари", "services": "Послуги", "works": "Роботи"}
TABLE_IDS = {
    "tenders": contract.TENDERS_STR_COLUMNS,
    "items": contract.ITEMS_STR_COLUMNS,
    "awards": contract.AWARDS_STR_COLUMNS,
}
PRICE_GROUP = ["unit_code", "currency", "unit_price_vat_included"]
PRICE_KEYS = ["tender_id", "item_id"]
REFERENCE_COLUMNS = ["price_basis", "group_key", "group_median", "price_ratio"]
LOGGER = logging.getLogger(__name__)


@dataclass
class DashboardData:
    tenders: pd.DataFrame
    items: pd.DataFrame
    awards: pd.DataFrame
    price_items: pd.DataFrame
    score_source: str
    high_threshold: int
    scoring_error: bool = False


def optional_module(name):
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        return None


def _load_tables():
    loader = optional_module("data_loader")
    if loader is not None:
        return loader.load_data()
    tenders, items, awards = (
        pd.read_csv(DATA_DIR / f"{name}.csv", dtype={field: "string" for field in ids})
        for name, ids in TABLE_IDS.items()
    )
    for table, dates in ((tenders, contract.TENDERS_DATE_COLUMNS), (awards, contract.AWARDS_DATE_COLUMNS)):
        for field in dates:
            table[field] = pd.to_datetime(table[field], format="mixed", errors="coerce", utc=True)
    tenders["number_of_bids"] = pd.to_numeric(tenders["number_of_bids"], errors="coerce").astype("Int64")
    return tenders, items, awards


def _normalize(table, ids, numbers=()):
    table = table.copy()
    for field in ids:
        if field in table:
            table[field] = table[field].astype("string").str.strip()
    for field in numbers:
        table[field] = pd.to_numeric(table[field], errors="coerce")
    return table


def _sample_rows():
    for sample_file in (ROOT / "sample_scores.csv", DATA_DIR / "sample_scores.csv"):
        if sample_file.is_file():
            return pd.read_csv(sample_file, dtype={"tender_id": "string"}).fillna({"reasons": ""}), "sample"
    return pd.DataFrame(columns=contract.SCORE_COLUMNS), "raw"


def _attach_scores(tenders, scores):
    scores = scores.loc[:, list(contract.SCORE_COLUMNS)].copy()
    scores["tender_id"] = scores["tender_id"].astype("string").str.strip()
    scores["total_score"] = pd.to_numeric(scores["total_score"], errors="coerce")
    scores = scores.loc[scores["tender_id"].notna() & scores["total_score"].between(0, 100)]
    return tenders.merge(scores, how="left", on="tender_id", validate="one_to_one")


def load_dashboard():
    raw_tenders, raw_items, raw_awards = _load_tables()
    tenders = _normalize(raw_tenders, TABLE_IDS["tenders"], ("number_of_bids", "final_value"))
    tenders["procedure"] = tenders["method_type"].replace(METHOD_LABELS)
    tenders["category"] = tenders["main_category"].replace(CATEGORY_LABELS)
    tenders["competitive_with_bids"] = (
        tenders["method_type"].isin(contract.COMPETITIVE_METHODS) & tenders["number_of_bids"].notna()
    )
    tenders["single_bid_observed"] = tenders["competitive_with_bids"] & tenders["number_of_bids"].eq(1)
    items = _normalize(raw_items, TABLE_IDS["items"], ("unit_price",))
    awards = _normalize(raw_awards, TABLE_IDS["awards"])

    error = False
    threshold = 50
    try:
        scoring = optional_module("scoring")
        if scoring is None:
            scores, source = _sample_rows()
        else:
            threshold = getattr(scoring, "HIGH_SCORE_THRESHOLD", threshold)
            scores = scoring.score_tenders(tenders, items, awards=awards)
            source = "model" if scores["ml_score"].gt(0).any() else "rules"
        contract.validate_scores(scores)
        tenders = _attach_scores(tenders, scores)
    except Exception:
        LOGGER.exception("Could not load tender scores")
        error = True
        source = "raw"
        tenders = _attach_scores(tenders, pd.DataFrame(columns=contract.SCORE_COLUMNS))
    if tenders["total_score"].notna().sum() == 0:
        source = "raw"
    return DashboardData(tenders, items, awards, comparable_items(items), source, threshold, error)


def overview_metrics(tenders, high_threshold):
    scores = tenders.dropna(subset=["total_score"])
    competitive_count = tenders["method_type"].isin(contract.COMPETITIVE_METHODS).sum()
    share = tenders["single_bid_observed"].sum() / competitive_count if competitive_count else None
    high_count, high_value = None, None
    if not scores.empty:
        scoring = optional_module("scoring")
        metrics_for_scores = getattr(scoring, "overview_metrics", None)
        if metrics_for_scores:
            amounts = tenders["final_value"].where(tenders["currency"].eq("UAH"))
            metrics = metrics_for_scores(tenders.assign(final_value=amounts), scores, high_threshold)
            share = metrics["single_bid_share"] if competitive_count else None
            high_count = metrics["high_score_tenders"]
            high_value = metrics["high_score_value_uah"]
        else:
            high = scores.loc[scores["total_score"].ge(high_threshold)]
            high_count = len(high)
            high_value = high.loc[high["currency"].eq("UAH"), "final_value"].sum()
    return {
        "count": len(tenders),
        "single_bid_share": share,
        "high_risk": high_count,
        "high_value": high_value,
        "first_date": tenders["date_created"].min(),
        "last_date": tenders["date_created"].max(),
    }


def filter_options(tenders, column):
    return sorted(tenders[column].dropna().unique().tolist(), key=str.casefold)


def filter_tenders(tenders, filters, minimum_score=None):
    for field, selected in filters.items():
        if selected:
            tenders = tenders.loc[tenders[field].isin(selected)]
    if minimum_score is not None:
        tenders = tenders.loc[tenders["total_score"].ge(minimum_score)]
    return tenders


def matching_tenders(tenders, query="", signal="all"):
    if signal != "all":
        tenders = tenders.loc[tenders[signal].eq(1)]
    if not query:
        return tenders

    query = query.casefold()
    matches = (
        tenders["title"].str.casefold().str.contains(query, regex=False, na=False)
        | tenders["buyer_name"].str.casefold().str.contains(query, regex=False, na=False)
        | tenders["tender_code"].str.casefold().str.contains(query, regex=False, na=False)
    )
    return tenders.loc[matches]


def ranked_tenders(tenders, score_source):
    if score_source == "raw":
        return tenders.loc[tenders["single_bid_observed"]].sort_values("date_created", ascending=False)
    return tenders.dropna(subset=["total_score"]).sort_values(
        ["total_score", "price_ratio", "date_created", "tender_id"], ascending=[False, False, False, True],
    )


def matching_buyers(tenders, query):
    buyers = tenders.loc[tenders["buyer_edrpou"].notna(), ["buyer_edrpou", "buyer_name"]]
    match = (
        buyers["buyer_name"].str.casefold().str.contains(query.casefold(), regex=False, na=False)
        | buyers["buyer_edrpou"].str.contains(query, regex=False, na=False)
    )
    return buyers.loc[match].drop_duplicates("buyer_edrpou").sort_values("buyer_name")


def supplier_counts(tenders, awards):
    active = awards.loc[awards["tender_id"].isin(tenders["tender_id"]) & awards["status"].eq("active")]
    active = active.dropna(subset=["supplier_name"])
    active = active.assign(supplier=active["supplier_edrpou"].fillna(active["supplier_name"]))
    return (
        active.groupby("supplier", as_index=False)
        .agg(name=("supplier_name", "first"), count=("tender_id", "nunique"))
        .nlargest(8, "count")
    )


def procurement_months(tenders):
    return (
        tenders.dropna(subset=["date_created"])
        .groupby(tenders["date_created"].dt.strftime("%Y-%m"))
        .size().rename("count").rename_axis("month").reset_index()
    )


def procedure_counts(tenders):
    return tenders["procedure"].value_counts().rename("count").rename_axis("name").reset_index()


def comparable_items(items):
    accepted = items["price_source"].isin(contract.REAL_PRICE_SOURCES)
    valid = items["unit_price"].gt(0) & items["unit_price"].lt(float("inf"))
    comparable = items.loc[accepted & valid].dropna(subset=PRICE_GROUP + PRICE_KEYS)
    try:
        compare = getattr(optional_module("flags"), "item_price_ratios", None)
        if compare is not None:
            references = compare(comparable)[PRICE_KEYS + REFERENCE_COLUMNS]
            return comparable.merge(references, how="left", on=PRICE_KEYS, validate="one_to_one")
    except Exception:
        LOGGER.exception("Could not load reference price comparisons")
    return comparable.assign(**dict.fromkeys(REFERENCE_COLUMNS))


def matching_items(items, query="", tender_id=None):
    if tender_id is not None:
        return items.loc[items["tender_id"].eq(tender_id)]
    text = query.casefold()
    match = (
        items["description"].str.casefold().str.contains(text, regex=False, na=False)
        | items["cpv_id"].str.contains(text, regex=False, na=False)
        | items["profile_id"].str.casefold().str.contains(text, regex=False, na=False)
    )
    return items.loc[match]


def price_comparison(items, selected, method):
    if pd.notna(selected["price_basis"]) and method == selected["price_basis"]:
        match = items["price_basis"].eq(method) & items["group_key"].eq(selected["group_key"])
        return items.loc[match, "unit_price"], selected["group_median"], selected["price_ratio"]

    key = "profile_id" if method == "profile" else "cpv_id"
    match = items[key].eq(selected[key])
    for field in PRICE_GROUP:
        match &= items[field].eq(selected[field])
    prices = items.loc[match, "unit_price"].dropna()
    median = prices.median()
    ratio = selected["unit_price"] / median if pd.notna(median) and median > 0 else None
    return prices, median, ratio
