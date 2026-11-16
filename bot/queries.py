"""Запити до знімка, спільні для чату й дашборда.

Лише читання: функції отримують Snapshot і нічого в ньому не змінюють.
Уся логіка відбору — функції з dashboard_data і scoring; тут їх лише поєднано.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

import app as contract
import dashboard_data
import scoring

from .formatting import is_missing
from .snapshot import Snapshot

TENDER_CODE_RE = re.compile(r"UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]", re.IGNORECASE)
TENDER_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")
EDRPOU_RE = re.compile(r"^\d{8}$|^\d{10}$")
PAGE_SIZE = 20


# --- тендери ----------------------------------------------------------------

def ranked(snapshot: Snapshot) -> pd.DataFrame:
    """Тендери з балом за спаданням (кеш на версію знімка)."""
    return snapshot.derived("ranked", lambda: dashboard_data.ranked_tenders(
        snapshot.tenders, snapshot.data.score_source))


def by_id(snapshot: Snapshot) -> pd.DataFrame:
    return snapshot.derived("by_id", lambda: snapshot.tenders.set_index("tender_id", drop=False))


def by_code(snapshot: Snapshot) -> dict[str, str]:
    def build():
        tenders = snapshot.tenders.dropna(subset=["tender_code"])
        return dict(zip(tenders["tender_code"].str.upper(), tenders["tender_id"]))
    return snapshot.derived("by_code", build)


def get_tender(snapshot: Snapshot, tender_id: str):
    """Рядок тендера (Series) або None."""
    table = by_id(snapshot)
    if tender_id not in table.index:
        return None
    return table.loc[tender_id]


def find_tender_id(snapshot: Snapshot, text: str) -> str | None:
    """tender_id за кодом UA-..., 32-символьним id або посиланням prozorro.gov.ua."""
    text = (text or "").strip()
    match = TENDER_CODE_RE.search(text)
    if match:
        return by_code(snapshot).get(match.group(0).upper())
    match = TENDER_ID_RE.search(text.lower())
    if match and match.group(0) in by_id(snapshot).index:
        return match.group(0)
    return None


def looks_like_tender(text: str) -> bool:
    text = text or ""
    return bool(TENDER_CODE_RE.search(text) or TENDER_ID_RE.search(text.lower())
                or "prozorro.gov.ua/tender" in text.lower())


def filter_options(snapshot: Snapshot, column: str) -> list[str]:
    return snapshot.derived(f"options:{column}",
                            lambda: dashboard_data.filter_options(snapshot.tenders, column))


@dataclass
class TenderFilters:
    regions: tuple[str, ...] = ()
    procedures: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    min_score: float | None = None
    buyer: str | None = None
    since_hours: int | None = None

    @property
    def active_count(self) -> int:
        return sum(bool(x) for x in (self.regions, self.procedures, self.categories, self.flags))


def filtered(snapshot: Snapshot, filters: TenderFilters) -> pd.DataFrame:
    tenders = ranked(snapshot)
    tenders = dashboard_data.filter_tenders(tenders, {
        "buyer_region": list(filters.regions),
        "procedure": list(filters.procedures),
        "category": list(filters.categories),
    }, filters.min_score)
    for flag in filters.flags:
        if flag in contract.FLAG_COLUMNS:
            tenders = tenders.loc[tenders[flag].eq(1)]
    if filters.buyer:
        tenders = tenders.loc[tenders["buyer_edrpou"].eq(filters.buyer)]
    if filters.since_hours:
        since = pd.Timestamp(reference_time(snapshot) - timedelta(hours=filters.since_hours))
        tenders = tenders.loc[tenders["date_modified"].ge(since)]
    return tenders


def page(table: pd.DataFrame, offset: int = 0, size: int = PAGE_SIZE) -> list:
    offset = max(0, int(offset))
    return [row for _, row in table.iloc[offset: offset + size].iterrows()]


def top(snapshot: Snapshot, region: str | None = None, offset: int = 0, size: int = 5):
    filters = TenderFilters(regions=(region,) if region else ())
    table = filtered(snapshot, filters)
    return page(table, offset, size), len(table)


# --- огляд ---------------------------------------------------------------------

def reference_time(snapshot: Snapshot) -> datetime:
    """Найсвіжіша зміна в даних: від неї рахуємо «за 24 години» і «за 7 днів»."""
    def build():
        latest = snapshot.tenders["date_modified"].max()
        return latest.to_pydatetime() if not is_missing(latest) else datetime.now(timezone.utc)
    return snapshot.derived("reference_time", build)


def overview(snapshot: Snapshot) -> dict:
    def build():
        tenders = snapshot.tenders
        threshold = snapshot.threshold
        metrics = dashboard_data.overview_metrics(tenders, threshold)
        competitive = tenders.loc[tenders["method_type"].isin(contract.COMPETITIVE_METHODS)]
        flag_counts = {column: int(tenders[column].fillna(0).sum()) for column in contract.FLAG_COLUMNS}
        scores = competitive["total_score"].dropna()
        bins = list(range(0, 105, 5))
        counts = pd.cut(scores, bins=bins, right=False, include_lowest=True).value_counts(sort=False)
        distribution = [{"start": int(interval.left), "count": int(count)}
                        for interval, count in counts.items()]
        if len(scores):
            distribution[-1]["count"] += int((scores >= 100).sum())
        high = tenders.loc[tenders["total_score"].ge(threshold)]
        regions = (high.groupby("buyer_region").size().sort_values(ascending=False)
                   .rename("count").reset_index().head(8).to_dict("records"))
        reference = reference_time(snapshot)
        day_ago = pd.Timestamp(reference - timedelta(hours=24))
        fresh = tenders.loc[tenders["date_modified"].ge(day_ago)]
        return {
            "metrics": metrics,
            "competitive": len(competitive),
            "flag_counts": flag_counts,
            "distribution": distribution,
            "regions": regions,
            "fresh_count": len(fresh),
            "fresh_high": int(fresh["total_score"].ge(threshold).sum()),
            "reference": reference,
            "max_score": float(scores.max()) if len(scores) else None,
        }
    return snapshot.derived("overview", build)


# --- замовники -------------------------------------------------------------------

def buyer_summary(snapshot: Snapshot) -> pd.DataFrame:
    def build():
        tenders = snapshot.tenders
        scores = tenders.dropna(subset=["total_score"])[list(contract.SCORE_COLUMNS)]
        return scoring.buyer_summary(tenders, scores, snapshot.data.awards)
    return snapshot.derived("buyer_summary", build)


def buyer_rating(snapshot: Snapshot) -> pd.DataFrame:
    """Рейтинг замовників лише там, де достатньо даних."""
    summary = buyer_summary(snapshot)
    return summary.loc[summary["competitive_tenders"] >= scoring.MIN_BUYER_TENDERS]


def search_buyers(snapshot: Snapshot, query: str, limit: int = 30) -> pd.DataFrame:
    query = (query or "").strip()
    if not query:
        return buyer_rating(snapshot).head(limit)
    found = dashboard_data.matching_buyers(snapshot.tenders, query)
    summary = buyer_summary(snapshot).set_index("buyer_edrpou")
    found = found.join(summary.drop(columns=["buyer_name"]), on="buyer_edrpou")
    # Точний збіг ЄДРПОУ — першим, далі ті, де більше тендерів.
    found = found.assign(_exact=found["buyer_edrpou"].eq(query))
    return found.sort_values(["_exact", "tenders_total"], ascending=[False, False]).head(limit)


def buyer_tenders(snapshot: Snapshot, edrpou: str) -> pd.DataFrame:
    tenders = snapshot.tenders
    return tenders.loc[tenders["buyer_edrpou"].eq(edrpou)]


def buyer_profile(snapshot: Snapshot, edrpou: str) -> dict | None:
    edrpou = (edrpou or "").strip()
    own = buyer_tenders(snapshot, edrpou)
    if own.empty:
        return None
    summary = buyer_summary(snapshot)
    row = summary.loc[summary["buyer_edrpou"].eq(edrpou)]
    info = row.iloc[0] if len(row) else None
    competitive = own.loc[own["method_type"].isin(contract.COMPETITIVE_METHODS)]
    with_bids = competitive.loc[competitive["number_of_bids"].notna()]
    single_share = (with_bids["number_of_bids"].eq(1).mean() if len(with_bids) else None)
    suppliers = dashboard_data.supplier_counts(own, snapshot.data.awards)
    top_supplier = None
    if info is not None and not is_missing(info.get("top_supplier_name")):
        top_supplier = {"name": info["top_supplier_name"], "wins": int(info["top_supplier_wins"]),
                        "share": info["top_supplier_share"]}
    elif len(suppliers):
        first = suppliers.iloc[0]
        top_supplier = {"name": first["name"], "wins": int(first["count"]),
                        "share": first["count"] / max(1, len(own))}
    ranked_own = dashboard_data.ranked_tenders(own, snapshot.data.score_source)
    scores = own["total_score"].dropna()
    final_values = own["final_value"].where(own["currency"].eq("UAH"))
    return {
        "edrpou": edrpou,
        "name": own["buyer_name"].mode().iat[0] if own["buyer_name"].notna().any() else "",
        "region": own["buyer_region"].dropna().iat[0] if own["buyer_region"].notna().any() else "",
        "tenders_total": len(own),
        "competitive": len(competitive),
        "single_bid_share": single_share,
        "top_supplier": top_supplier,
        "suppliers": suppliers.to_dict("records"),
        "mean_score": float(scores.mean()) if len(scores) else None,
        "high_count": int(scores.ge(snapshot.threshold).sum()),
        "total_value": float(final_values.sum()) if final_values.notna().any() else None,
        "enough_data": len(competitive) >= scoring.MIN_BUYER_TENDERS,
        "top_tenders": [row for _, row in ranked_own.head(10).iterrows()],
    }


# --- позиції й ціни --------------------------------------------------------------

def tender_items(snapshot: Snapshot, tender_id: str) -> pd.DataFrame:
    """Позиції тендера з реальною ціною і порівнянням із медіаною."""
    return dashboard_data.matching_items(snapshot.data.price_items, tender_id=tender_id)


def all_items_count(snapshot: Snapshot, tender_id: str) -> int:
    items = snapshot.data.items
    return int(items["tender_id"].eq(tender_id).sum())


def price_comparison(snapshot: Snapshot, tender_id: str, item_id: str, basis: str | None = None):
    """(ціни групи, медіана, відношення, рядок позиції) або None."""
    items = tender_items(snapshot, tender_id)
    match = items.loc[items["item_id"].eq(item_id)]
    if match.empty:
        return None
    selected = match.iloc[0]
    method = basis or (selected["price_basis"] if not is_missing(selected["price_basis"]) else "cpv")
    if method not in contract.PRICE_BASIS_VALUES:
        method = "cpv"
    if method == "profile" and is_missing(selected.get("profile_id")):
        method = "cpv"
    prices, median, ratio = dashboard_data.price_comparison(snapshot.data.price_items, selected, method)
    return prices, median, ratio, selected, method


# --- сповіщення ---------------------------------------------------------------------

def recent_candidates(snapshot: Snapshot, days: int = 7) -> pd.DataFrame:
    reference = reference_time(snapshot)
    since = pd.Timestamp(reference - timedelta(days=days))
    tenders = snapshot.tenders
    return tenders.loc[tenders["date_modified"].ge(since) & tenders["total_score"].notna()]
