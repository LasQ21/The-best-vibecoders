"""Відбір і надсилання сповіщень: поріг, регіон, замовники, без повторів, ліміт, блокування."""

import asyncio
import unittest
from datetime import timedelta

from bot import alerts
from bot.storage import Storage, Subscription
from bot_fixtures import NOW, make_tenders, temp_db_path, tender


def candidates(rows):
    return alerts.candidates_from(make_tenders(rows), NOW, 48)


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.row = make_tenders([tender(1, total_score=45.0)]).iloc[0]

    def test_threshold(self):
        self.assertTrue(alerts.matches(Subscription(1, active=True, min_score=40), self.row))
        self.assertFalse(alerts.matches(Subscription(1, active=True, min_score=50), self.row))

    def test_region(self):
        sub = Subscription(1, active=True, min_score=40, regions=("м. Київ",))
        self.assertFalse(alerts.matches(sub, self.row))
        sub = sub.with_changes(regions=("Полтавська область",))
        self.assertTrue(alerts.matches(sub, self.row))

    def test_followed_buyer_has_lower_threshold_and_ignores_region(self):
        row = make_tenders([tender(1, total_score=32.0)]).iloc[0]
        sub = Subscription(1, active=True, min_score=40, regions=("м. Київ",), buyers=frozenset({"01999265"}))
        self.assertTrue(alerts.matches(sub, row))
        self.assertFalse(alerts.matches(sub.with_changes(buyers=frozenset()), row))

    def test_muted_buyer_never_matches(self):
        sub = Subscription(1, active=True, min_score=30, muted=frozenset({"01999265"}))
        self.assertFalse(alerts.matches(sub, self.row))

    def test_flags_filter(self):
        sub = Subscription(1, active=True, min_score=30, flags=("flag_price",))
        self.assertFalse(alerts.matches(sub, self.row))
        self.assertTrue(alerts.matches(sub.with_changes(flags=("flag_price", "flag_single_bid")), self.row))

    def test_fast_preview_equals_row_by_row(self):
        table = make_tenders([tender(n, total_score=float(n * 7 % 70),
                                     buyer_region=("м. Київ" if n % 3 else "Полтавська область"),
                                     flag_price=n % 2) for n in range(1, 30)])
        subs = [Subscription(1, min_score=30), Subscription(1, min_score=40, regions=("м. Київ",)),
                Subscription(1, min_score=35, flags=("flag_price",)),
                Subscription(1, min_score=60, buyers=frozenset({"01999265"}), muted=frozenset())]
        for sub in subs:
            self.assertEqual(alerts.preview_count_fast(table, sub), alerts.preview_count(table, sub))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(temp_db_path())
        self.sub = self.storage.save_subscription(Subscription(7, active=True, min_score=40, max_per_hour=2))

    def test_48h_window(self):
        rows = [tender(1, total_score=50.0, date_modified=NOW - timedelta(hours=47)),
                tender(2, total_score=50.0, date_modified=NOW - timedelta(hours=49))]
        found = candidates(rows)
        self.assertEqual(found["tender_id"].tolist(), [f"{1:032x}"])

    def test_no_repeats(self):
        found = candidates([tender(1, total_score=50.0)])
        self.storage.record_sent(7, [(f"{1:032x}", 50.0)])
        self.assertEqual(alerts.plan_alerts(found, [self.sub], self.storage, NOW), [])

    def test_hourly_limit_collapses_rest(self):
        found = candidates([tender(n, total_score=50.0 + n) for n in range(1, 6)])
        plans = alerts.plan_alerts(found, [self.storage.get_subscription(7)], self.storage, NOW)
        self.assertEqual(len(plans[0].send), 2)
        self.assertEqual(len(plans[0].collapsed), 3)
        # Найвищий бал іде першим.
        self.assertEqual(plans[0].send[0]["total_score"], 55.0)


class FakeError(Exception):
    def __init__(self, retry_after):
        super().__init__("flood")
        self.retry_after = retry_after


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(temp_db_path())
        self.storage.save_subscription(Subscription(7, active=True, min_score=40))
        self.storage.save_subscription(Subscription(8, active=True, min_score=40))
        self.sent = []
        self.slept = []

    def sender(self, blocked=(), flood_once=False):
        state = {"flooded": False}

        async def send(chat_id, text, markup):
            if chat_id in blocked:
                raise alerts.Blocked("blocked")
            if flood_once and not state["flooded"]:
                state["flooded"] = True
                raise FakeError(3)
            self.sent.append((chat_id, text))

        async def sleep(seconds):
            self.slept.append(seconds)

        clock = iter(range(0, 10_000))
        return alerts.AlertSender(send, sleep=sleep, clock=lambda: float(next(clock)))

    def run_dispatch(self, sender, rows):
        found = candidates(rows)
        plans = alerts.plan_alerts(found, self.storage.active_subscriptions(), self.storage, NOW)
        card = lambda row: (f"card {row['tender_id']}", None)
        collapsed = lambda plan: (f"ще {len(plan.collapsed)}", None)
        return asyncio.run(alerts.dispatch(plans, sender, self.storage, card, collapsed))

    def test_blocked_user_is_unsubscribed(self):
        result = self.run_dispatch(self.sender(blocked={8}), [tender(1, total_score=50.0)])
        self.assertEqual(result.blocked, 1)
        self.assertFalse(self.storage.get_subscription(8).active)
        self.assertTrue(self.storage.get_subscription(7).active)
        self.assertEqual([chat for chat, _ in self.sent], [7])

    def test_one_message_per_tender_per_chat(self):
        rows = [tender(1, total_score=50.0)]
        self.run_dispatch(self.sender(), rows)
        self.run_dispatch(self.sender(), rows)
        self.assertEqual(sorted(chat for chat, _ in self.sent), [7, 8])

    def test_retry_after_waits_and_retries(self):
        self.storage.set_active(8, False)
        self.run_dispatch(self.sender(flood_once=True), [tender(1, total_score=50.0)])
        self.assertIn(3.0, self.slept)
        self.assertEqual(len(self.sent), 1)


class ThrottleTest(unittest.TestCase):
    def test_one_per_second_per_chat(self):
        moments = []
        now = [0.0]

        async def send(chat_id, text, markup):
            moments.append(now[0])

        async def sleep(seconds):
            now[0] += seconds

        sender = alerts.AlertSender(send, sleep=sleep, clock=lambda: now[0])

        async def run():
            for _ in range(3):
                await sender.send(1, "x")
        asyncio.run(run())
        self.assertEqual(moments, [0.0, 1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
