"""Опитування стрічки: відбір конкурентних, курсор, часткова невдача, відставання, повний цикл."""

import asyncio
import unittest
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pandas as pd

import updater
from bot import ingest
from bot.alerts import AlertSender
from bot.config import Settings
from bot.persist import LocalPersistence
from bot.services import AlertService
from bot.snapshot import SnapshotStore
from bot.storage import Storage, Subscription
from bot_fixtures import NOW, make_dashboard, temp_db_path, tender


def entry(n, method="priceQuotation", status="complete", minute=0):
    return {"id": f"{n:032x}", "procurementMethodType": method, "status": status,
            "procuringEntity": {"kind": "general"},
            "dateModified": f"2026-11-16T10:{minute:02d}:00+02:00"}


def fake_feed(pages):
    """pages: {offset: (entries, next_offset)} -> підставний get_json."""
    def get_json(url):
        offset = parse_qs(urlparse(url).query)["offset"][0]
        data, next_offset = pages.get(offset, ([], offset))
        return {"data": data, "next_page": {"offset": next_offset}}
    return get_json


class FeedTest(unittest.TestCase):
    def test_takes_only_competitive(self):
        pages = {"c0": ([entry(1), entry(2, method="reporting"), entry(3, method="aboveThreshold"),
                         entry(4, status="active.tendering"), entry(5, method="belowThreshold")], "c1")}
        batch = ingest.read_competitive_feed("c0", 150, fake_feed(pages))
        self.assertEqual([e["id"][-1] for e in batch.entries], ["1", "3", "5"])
        self.assertEqual(batch.skipped_reporting, 1)
        self.assertEqual(batch.next_offset, "c1")
        self.assertFalse(batch.hit_limit)

    def test_cursor_when_limit_hit_mid_page(self):
        pages = {"c0": ([entry(1, minute=1), entry(2, minute=2), entry(3, minute=3)], "c1")}
        batch = ingest.read_competitive_feed("c0", 2, fake_feed(pages))
        self.assertTrue(batch.hit_limit)
        self.assertEqual(batch.next_offset, "2026-11-16T10:02:00+02:00")

    def test_cursor_on_partial_failure(self):
        entries = [entry(1, minute=1), entry(2, minute=2), entry(3, minute=3)]
        fetched = {entries[0]["id"], entries[2]["id"]}
        self.assertEqual(ingest.next_cursor(entries, fetched, "c9"), "2026-11-16T10:02:00+02:00")
        self.assertEqual(ingest.next_cursor(entries, {e["id"] for e in entries}, "c9"), "c9")

    def test_lag_jump(self):
        offset, note = ingest.start_offset("cursor", NOW - timedelta(hours=7), NOW, 6)
        self.assertEqual(offset, (NOW - timedelta(hours=1)).isoformat())
        self.assertIn("пропущено", note)
        offset, note = ingest.start_offset(None, None, NOW, 6)
        self.assertEqual(offset, (NOW - timedelta(hours=1)).isoformat())
        offset, note = ingest.start_offset("cursor", NOW - timedelta(hours=1), NOW, 6)
        self.assertEqual((offset, note), ("cursor", None))


class FakeServices:
    """Мінімальні сервіси для повного циклу: знімок змінюється після «upsert»."""

    def __init__(self, before_rows, after_rows):
        self.settings = Settings(poll_limit=150)
        self.storage = Storage(temp_db_path())
        self.persistence = LocalPersistence("/nonexistent", self.storage.path)
        self.persistence.tables_ingested = lambda tables: None
        self.states = [make_dashboard(before_rows), make_dashboard(after_rows)]
        self.snapshots = SnapshotStore(loader=lambda _dir: self.states.pop(0) if len(self.states) > 1
                                       else self.states[0])
        self.sent = []

        async def send(chat_id, text, markup):
            self.sent.append((chat_id, text))

        async def no_sleep(_):
            pass

        self.sender = AlertSender(send, sleep=no_sleep, clock=lambda: 0.0)
        self.alerts = AlertService(self)
        self.admin_messages = []

    async def notify_admins(self, text):
        self.admin_messages.append(text)


class CycleTest(unittest.TestCase):
    def make_poller(self, services, feed_entries, upsert_result=(1, 0), failed_ids=()):
        pages = {"cur": (feed_entries, "next")}

        def fetch(ids):
            docs = [{"id": i} for i in ids if i not in failed_ids]
            return docs, len(ids) - len(docs)

        services.storage.set_kv(ingest.CURSOR_KEY, "cur")
        services.storage.set_kv(ingest.CURSOR_AT_KEY, NOW.isoformat())
        poller = ingest.Poller(services, get_json=fake_feed(pages), fetch_tenders=fetch,
                               upsert=lambda tables, data_dir: upsert_result, now=lambda: NOW)
        services.poller = poller
        return poller

    def setUp(self):
        self._orig = updater.tenders_to_tables
        updater.tenders_to_tables = lambda docs: (pd.DataFrame({"tender_id": [d["id"] for d in docs]}),
                                                  pd.DataFrame(), pd.DataFrame())

    def tearDown(self):
        updater.tenders_to_tables = self._orig

    def test_new_high_score_tender_is_sent_once_and_old_ones_never(self):
        old_high = tender(1, total_score=60.0)                         # уже був на знімку
        new_high = tender(2, total_score=55.0)                         # прийшов зі стрічки
        services = FakeServices([old_high], [old_high, new_high])
        services.storage.save_subscription(Subscription(42, active=True, min_score=40))
        poller = self.make_poller(services, [entry(2)])
        result = asyncio.run(poller.run_cycle())
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(services.sent), 1)
        self.assertIn("Тендер номер 2", services.sent[0][1])
        self.assertEqual(services.storage.get_kv(ingest.CURSOR_KEY), "next")

        # Той самий тендер знову у стрічці (complete після active.awarded) — без повтору.
        services.states = [make_dashboard([old_high, new_high]),
                           make_dashboard([old_high, {**new_high, "date_modified": NOW}])]
        asyncio.run(services.snapshots.reload())
        services.storage.set_kv(ingest.CURSOR_KEY, "cur")
        asyncio.run(poller.run_cycle())
        self.assertEqual(len(services.sent), 1)

    def test_score_drift_of_old_tenders_does_not_alert(self):
        old = tender(1, total_score=38.0)
        services = FakeServices([old], [{**old, "total_score": 41.0}, tender(2, total_score=10.0)])
        services.storage.save_subscription(Subscription(42, active=True, min_score=40))
        poller = self.make_poller(services, [entry(2)])
        asyncio.run(poller.run_cycle())
        self.assertEqual(services.sent, [])

    def test_nothing_new_ends_cycle_without_reload(self):
        services = FakeServices([tender(1)], [tender(1)])
        poller = self.make_poller(services, [], upsert_result=(0, 0))
        result = asyncio.run(poller.run_cycle())
        self.assertEqual(result.message, "Нових тендерів немає")
        self.assertEqual(services.snapshots.current.version, 1)

    def test_network_failures_notify_admins_once(self):
        services = FakeServices([tender(1)], [tender(1)])
        poller = self.make_poller(services, [])

        def broken(url):
            raise updater.ApiError("offline")
        poller.get_json = broken
        for _ in range(8):
            result = asyncio.run(poller.run_cycle())
            self.assertFalse(result.ok)
        self.assertEqual(len(services.admin_messages), 1)
        poller.get_json = fake_feed({"cur": ([], "cur")})
        asyncio.run(poller.run_cycle())
        self.assertEqual(len(services.admin_messages), 2)
        self.assertIn("відновився", services.admin_messages[-1])


if __name__ == "__main__":
    unittest.main()
