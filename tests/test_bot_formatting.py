"""Тексти бота: екранування, обрізання, ліміт 4096, числа й відмінки."""

import unittest

import numpy as np
import pandas as pd

from bot import formatting as fmt
from bot_fixtures import make_tenders, tender


class FormattingTest(unittest.TestCase):
    def row(self, **overrides):
        return make_tenders([tender(1, **overrides)]).iloc[0]

    def test_html_in_title_is_escaped(self):
        card = fmt.tender_card(self.row(title="<script>x</script> & «Морква»"))
        self.assertIn("&lt;script&gt;", card)
        self.assertNotIn("<script>", card)
        self.assertIn("&amp;", card)

    def test_long_title_and_reasons_are_truncated(self):
        long_title = "Морква " * 200
        long_reason = "дуже довге пояснення " * 50
        card = fmt.tender_card(self.row(title=long_title, reasons=f"{long_reason}; друга причина"))
        title_line = card.split("\n")[2]
        self.assertLessEqual(len(title_line), fmt.TITLE_MAX + len("<b></b>"))
        reason_lines = [line for line in card.split("\n") if line.startswith("• ")]
        self.assertTrue(all(len(line) <= fmt.REASON_MAX + 2 for line in reason_lines))

    def test_card_fits_telegram_limit(self):
        reasons = "; ".join(f"причина {n} " + "х" * 290 for n in range(40))
        card = fmt.tender_card(self.row(reasons=reasons), max_reasons=40)
        self.assertLessEqual(len(card), fmt.MESSAGE_LIMIT)

    def test_missing_values_become_dash(self):
        row = self.row(number_of_bids=None, final_value=np.nan, expected_value=np.nan,
                       buyer_region=None, winner_name=None)
        card = fmt.tender_card(row)
        self.assertIn("Сума: — · учасників: —", card)
        self.assertNotIn("nan", card.lower().replace("nanos", ""))

    def test_numbers_and_plurals(self):
        self.assertEqual(fmt.amount(248300), "248,3 тис. грн")
        self.assertEqual(fmt.score(57.94), "57,9")
        self.assertEqual(fmt.percent(0.541, 1), "54,1%")
        self.assertEqual(fmt.number(17981), "17\u00a0981")
        self.assertEqual(fmt.plural(1, fmt.TENDER_FORMS), "1 тендер")
        self.assertEqual(fmt.plural(3, fmt.TENDER_FORMS), "3 тендери")
        self.assertEqual(fmt.plural(11, fmt.TENDER_FORMS), "11 тендерів")
        self.assertEqual(fmt.plural(22, fmt.TENDER_FORMS), "22 тендери")
        self.assertEqual(fmt.number(pd.NA), "—")

    def test_buyer_label_keeps_leading_zeros(self):
        self.assertEqual(fmt.buyer_label("Лікарня", "01999265"), "Лікарня (01999265)")

    def test_alert_card_has_disclaimer_and_reasons(self):
        card = fmt.tender_card(self.row(), header=fmt.alert_header())
        self.assertTrue(card.startswith("Новий тендер до перевірки · <b>45,0</b> / 100"))
        self.assertIn("Чому в списку:", card)
        self.assertIn(fmt.DISCLAIMER, card)
        for word in ("корупц", "порушення,", "шахрай"):
            self.assertNotIn(word, card.lower())

    def test_demo_card_is_marked(self):
        self.assertIn("(демо)", fmt.tender_card(self.row(), header=fmt.alert_header(), demo=True))


if __name__ == "__main__":
    unittest.main()
