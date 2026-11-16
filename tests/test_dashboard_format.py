"""Форматування чисел, сум і посилань на сайті."""

import math
import unittest

import dashboard_format as fmt

NBSP = " "


class DashboardFormatTest(unittest.TestCase):
    def test_number_uses_ukrainian_separators(self):
        self.assertEqual(fmt.number(1234567.891, 2), f"1{NBSP}234{NBSP}567,89")
        self.assertEqual(fmt.number(17981), f"17{NBSP}981")

    def test_missing_values_become_dash(self):
        for func in (fmt.number, fmt.percent, fmt.money, fmt.compact_money):
            self.assertEqual(func(math.nan), "—", func.__name__)
            self.assertEqual(func(None), "—", func.__name__)
        self.assertEqual(fmt.text_value(None), "—")
        self.assertEqual(fmt.text_value("   "), "—")
        self.assertEqual(fmt.text_value(None, "немає"), "немає")

    def test_percent(self):
        self.assertEqual(fmt.percent(0.541), "54,1%")

    def test_money(self):
        self.assertEqual(fmt.money(500), "500,00 грн")
        self.assertEqual(fmt.money(1500), f"1{NBSP}500 грн")
        self.assertEqual(fmt.money(10, "EUR"), "10,00 EUR")

    def test_compact_money(self):
        self.assertEqual(fmt.compact_money(122_030_000), "122,03 млн ₴")
        self.assertEqual(fmt.compact_money(2_500_000_000), "2,50 млрд ₴")
        self.assertEqual(fmt.compact_money(999), "999,00 грн")

    def test_tender_link(self):
        self.assertEqual(fmt.tender_link("UA-2026-06-26-011657-a"),
                         "https://prozorro.gov.ua/tender/UA-2026-06-26-011657-a")
        self.assertIsNone(fmt.tender_link("abc"))
        self.assertIsNone(fmt.tender_link(None))
        self.assertIsNone(fmt.tender_link(math.nan))


if __name__ == "__main__":
    unittest.main()
