"""Маленькі вигадані таблиці для тестів бота (без мережі й без data/*.csv)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import app as contract
from dashboard_data import CATEGORY_LABELS, METHOD_LABELS, DashboardData

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
TOKEN = "123456:TEST-token-not-real"


def tender(n: int, **overrides) -> dict:
    row = {column: None for column in contract.TENDERS_COLUMNS}
    row.update({
        "tender_id": f"{n:032x}",
        "tender_code": f"UA-2026-11-16-{n:06d}-a",
        "title": f"Тендер номер {n}",
        "buyer_name": "КНП «Тестова лікарня»",
        "buyer_edrpou": "01999265",
        "buyer_kind": "general",
        "buyer_region": "Полтавська область",
        "method_type": "priceQuotation",
        "status": "complete",
        "main_category": "goods",
        "expected_value": 250000.0,
        "currency": "UAH",
        "final_value": 248300.0,
        "price_drop_pct": 0.7,
        "number_of_bids": 1,
        "submission_days": 2.4,
        "date_created": NOW - timedelta(days=3),
        "date_modified": NOW - timedelta(hours=2),
        "winner_name": "ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ \"СМ-КАСКАД\"",
        "winner_edrpou": "45346906",
        "n_items": 1, "n_lots": 0, "n_winners": 1,
        # бал (як після scoring.score_tenders + merge)
        "flag_price": 0, "flag_single_bid": 1, "flag_repeat_winner": 0, "flag_short_period": 0,
        "price_ratio": np.nan, "ml_score": 0.3, "total_score": 45.0,
        "reasons": "1 учасник у конкурентній процедурі",
    })
    row.update(overrides)
    return row


def make_tenders(rows: list[dict]) -> pd.DataFrame:
    tenders = pd.DataFrame(rows)
    for column in contract.TENDERS_DATE_COLUMNS:
        tenders[column] = pd.to_datetime(tenders[column], utc=True)
    tenders["number_of_bids"] = pd.to_numeric(tenders["number_of_bids"], errors="coerce").astype("Int64")
    tenders["procedure"] = tenders["method_type"].replace(METHOD_LABELS)
    tenders["category"] = tenders["main_category"].replace(CATEGORY_LABELS)
    tenders["competitive_with_bids"] = (tenders["method_type"].isin(contract.COMPETITIVE_METHODS)
                                        & tenders["number_of_bids"].notna())
    tenders["single_bid_observed"] = tenders["competitive_with_bids"] & tenders["number_of_bids"].eq(1)
    return tenders


def default_rows() -> list[dict]:
    return [
        tender(1, title="Морква <b>свіжа</b> & «Капуста»", total_score=57.9, flag_short_period=1,
               reasons="1 учасник у конкурентній процедурі; строк подачі 2,4 дн. — коротший"),
        tender(2, total_score=12.0, buyer_edrpou="43927048", buyer_name="ДЖКГ",
               buyer_region="Харківська область", number_of_bids=3, flag_single_bid=0, reasons=""),
        tender(3, method_type="reporting", number_of_bids=None, total_score=0.0, flag_single_bid=0,
               reasons="", price_ratio=np.nan, final_value=np.nan, winner_name=None, winner_edrpou=None),
        tender(4, total_score=41.0, flag_price=1, price_ratio=2.4, buyer_region="м. Київ",
               buyer_edrpou="00000001", date_modified=NOW - timedelta(days=5)),
    ]


def make_dashboard(rows: list[dict] | None = None) -> DashboardData:
    tenders = make_tenders(rows or default_rows())
    items = pd.DataFrame([{
        "tender_id": f"{1:032x}", "item_id": "i1", "description": "Морква", "cpv_id": "03220000-9",
        "cpv_description": "Овочі", "quantity": 100.0, "unit_code": "KGM", "unit_name": "кілограм",
        "unit_price": 41.94, "currency": "UAH", "unit_price_estimated": 0, "unit_price_vat_included": False,
        "price_source": "contract_item", "lot_id": None, "profile_id": "p1", "category_id": "c1",
    }] + [{
        "tender_id": f"{2:032x}", "item_id": f"x{k}", "description": "Морква", "cpv_id": "03220000-9",
        "cpv_description": "Овочі", "quantity": 100.0, "unit_code": "KGM", "unit_name": "кілограм",
        "unit_price": 18.0 + k, "currency": "UAH", "unit_price_estimated": 0, "unit_price_vat_included": False,
        "price_source": "contract_item", "lot_id": None, "profile_id": "p1", "category_id": "c1",
    } for k in range(5)])
    price_items = items.assign(price_basis="profile", group_key="p1|KGM|False",
                               group_median=19.0, price_ratio=items["unit_price"] / 19.0)
    awards = pd.DataFrame([{
        "tender_id": f"{n:032x}", "award_id": f"a{n}", "status": "active",
        "supplier_name": "ТОВ \"СМ-КАСКАД\"", "supplier_edrpou": "45346906", "value": 1000.0,
        "date": NOW, "lot_id": None, "currency": "UAH", "vat_included": True,
    } for n in (1, 2, 4)])
    return DashboardData(tenders, items, awards, price_items, "model", 40, False)


def make_snapshot(rows: list[dict] | None = None):
    from bot.snapshot import Snapshot
    return Snapshot(data=make_dashboard(rows), version=1, loaded_at=NOW, load_seconds=0.1)


def temp_db_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="botdb")) / "bot.db"
