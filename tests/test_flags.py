"""Тести прапорців і балу на маленьких вигаданих таблицях.

Запуск з кореня репозиторію: python -m unittest discover tests
"""

import unittest

import pandas as pd

import app
from flags import (MIN_PRICE_GROUP_SIZE, REPEAT_WINNER_MIN_TENDERS, compute_flags,
                   item_price_ratios, plural_positions)
from scoring import combine_scores, overview_metrics


def make_tenders(rows):
    """Тендери з розумними значеннями за замовчуванням; rows перевизначають поля."""
    defaults = {"buyer_edrpou": "00000001", "buyer_name": "Замовник", "buyer_region": "м. Київ",
                "method_type": "priceQuotation", "number_of_bids": 3, "price_drop_pct": 10.0,
                "submission_days": 4.0, "winner_edrpou": "11111111",
                "winner_name": "Постачальник", "final_value": 1000.0}
    tenders = pd.DataFrame([{**defaults, "tender_id": f"t{n}", **row} for n, row in enumerate(rows)])
    tenders["number_of_bids"] = tenders["number_of_bids"].astype("Int64")
    return tenders


def make_items(prices, profile_id="p1", vat=False, source="contract_item"):
    """По одній позиції на тендер t0, t1, ... з вказаними цінами."""
    return pd.DataFrame({
        "tender_id": [f"t{n}" for n in range(len(prices))],
        "item_id": [f"i{n}" for n in range(len(prices))],
        "description": "Товар", "cpv_id": "33600000-6", "quantity": 10.0,
        "unit_code": "KGM", "unit_price": prices, "currency": "UAH",
        "unit_price_vat_included": vat, "price_source": source, "profile_id": profile_id,
    })


NO_ITEMS = make_items([]).iloc[:0]


class SingleBidTest(unittest.TestCase):
    def test_only_competitive_procedures_are_flagged(self):
        tenders = make_tenders([{"number_of_bids": 1},
                                {"number_of_bids": 2},
                                {"number_of_bids": pd.NA, "method_type": "reporting"}])
        flags = compute_flags(tenders, NO_ITEMS).set_index("tender_id")
        self.assertEqual(flags["flag_single_bid"].to_dict(), {"t0": 1, "t1": 0, "t2": 0})

    def test_reason_mentions_no_price_drop(self):
        tenders = make_tenders([{"number_of_bids": 1, "price_drop_pct": 0.0}])
        reasons = compute_flags(tenders, NO_ITEMS)["reasons"].iloc[0]
        self.assertIn("ціну не знизили", reasons)


class PriceTest(unittest.TestCase):
    def test_overpriced_item_is_flagged_with_overpay(self):
        prices = [10.0] * (MIN_PRICE_GROUP_SIZE - 1) + [50.0]
        tenders = make_tenders([{}] * len(prices))
        flags = compute_flags(tenders, make_items(prices)).set_index("tender_id")
        last = f"t{len(prices) - 1}"
        self.assertEqual(flags["flag_price"].sum(), 1)
        self.assertAlmostEqual(flags.loc[last, "price_ratio"], 5.0)
        self.assertAlmostEqual(flags.loc[last, "overpay_uah"], (50 - 10) * 10)
        self.assertIn("ціна ×5,0", flags.loc[last, "reasons"])

    def test_small_group_is_not_compared(self):
        prices = [10.0] * (MIN_PRICE_GROUP_SIZE - 2) + [50.0]
        flags = compute_flags(make_tenders([{}] * len(prices)), make_items(prices))
        self.assertEqual(flags["flag_price"].sum(), 0)
        self.assertTrue(flags["price_ratio"].isna().all())

    def test_estimated_prices_and_other_vat_are_not_mixed(self):
        base = make_items([10.0] * MIN_PRICE_GROUP_SIZE)
        estimated = make_items([100.0], source="estimated_from_award").assign(item_id="e")
        with_vat = make_items([100.0], vat=True).assign(item_id="v")
        ratios = item_price_ratios(pd.concat([base, estimated, with_vat]))
        self.assertNotIn("e", set(ratios["item_id"]))
        self.assertNotIn("v", set(ratios.loc[ratios["price_basis"] == "profile", "item_id"]))
        self.assertEqual(list(ratios.columns[:len(app.ITEM_PRICE_COLUMNS)]),
                         list(app.ITEM_PRICE_COLUMNS))


class RepeatWinnerTest(unittest.TestCase):
    def test_frequent_winner_is_flagged(self):
        n = REPEAT_WINNER_MIN_TENDERS
        rows = [{"winner_edrpou": "22222222"}] * (n // 2) + [{"winner_edrpou": None}] * (n - n // 2)
        rows = [{**row, "winner_edrpou": row["winner_edrpou"] or f"9{k:07d}"}
                for k, row in enumerate(rows)]
        flags = compute_flags(make_tenders(rows), NO_ITEMS)
        self.assertEqual(flags["flag_repeat_winner"].sum(), n // 2)
        self.assertAlmostEqual(flags["winner_share"].iloc[0], 0.5)


class ShortPeriodTest(unittest.TestCase):
    def test_threshold_is_per_method_type(self):
        rows = ([{"submission_days": d} for d in range(2, 12)]
                + [{"method_type": "aboveThreshold", "submission_days": d} for d in range(7, 17)])
        flags = compute_flags(make_tenders(rows), NO_ITEMS).set_index("tender_id")
        flagged = set(flags.index[flags["flag_short_period"] == 1])
        self.assertEqual(flagged, {"t0", "t10"})   # найкоротший у кожному типі


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.tenders = make_tenders([{"number_of_bids": 1, "submission_days": 1.0},
                                     {}, {"method_type": "reporting", "number_of_bids": pd.NA}])
        self.flags = compute_flags(self.tenders, NO_ITEMS)

    def test_contract_holds_without_ml(self):
        scores = combine_scores(self.flags, None)
        app.validate_flags(self.flags)
        app.validate_scores(scores)
        self.assertEqual(scores["tender_id"].iloc[0], "t0")

    def test_ml_reason_is_appended(self):
        ml = pd.DataFrame({"tender_id": ["t0", "t1", "t2"], "ml_score": [1.0, 0.0, 0.0],
                           "ml_reason": ["ML: тест", "", ""]})
        scores = combine_scores(self.flags, ml).set_index("tender_id")
        self.assertTrue(scores.loc["t0", "reasons"].endswith("; ML: тест"))
        self.assertEqual(scores.loc["t1", "reasons"], "")

    def test_overview_metrics(self):
        metrics = overview_metrics(self.tenders, combine_scores(self.flags, None))
        self.assertEqual(metrics["tenders"], 3)
        self.assertAlmostEqual(metrics["single_bid_share"], 0.5)


class FormattingTest(unittest.TestCase):
    def test_plural_positions(self):
        self.assertEqual([plural_positions(n) for n in (1, 3, 5, 11, 22)],
                         ["1 позиція", "3 позиції", "5 позицій", "11 позицій", "22 позиції"])


if __name__ == "__main__":
    unittest.main()
