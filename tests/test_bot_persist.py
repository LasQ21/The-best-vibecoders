"""Збереження стану на Vercel: дельта тендерів і SQLite у (підставному) Blob."""

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import app as contract
from bot.persist import BLOB_PREFIX, DB_BLOB, BlobPersistence
from bot.storage import Storage, Subscription


class MemoryBlob:
    def __init__(self):
        self.files = {}
        self.times = {}
        self.clock = 1000.0

    def get(self, pathname):
        return self.files.get(pathname)

    def put(self, pathname, body):
        self.clock += 10
        self.files[pathname] = bytes(body)
        self.times[pathname] = self.clock

    def uploaded_at(self, pathname):
        return self.times.get(pathname)


def write_source(directory: Path):
    columns = {"tenders": contract.TENDERS_COLUMNS, "items": contract.ITEMS_COLUMNS,
               "awards": contract.AWARDS_COLUMNS}
    for name, cols in columns.items():
        row = {column: "" for column in cols}
        row["tender_id"] = "old"
        if name == "tenders":
            row["date_modified"] = "2026-11-01T00:00:00+00:00"
        pd.DataFrame([row], columns=cols).to_csv(directory / f"{name}.csv", index=False)
    (directory / "feed_state.json").write_text("{}")


def new_tables():
    tenders = pd.DataFrame([{column: None for column in contract.TENDERS_COLUMNS}])
    tenders["tender_id"] = "new"
    tenders["date_modified"] = "2026-11-16T10:00:00+00:00"
    return {"tenders": tenders, "items": pd.DataFrame(columns=contract.ITEMS_COLUMNS),
            "awards": pd.DataFrame(columns=contract.AWARDS_COLUMNS)}


class BlobPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.source = Path(tempfile.mkdtemp())
        write_source(self.source)
        self.blob = MemoryBlob()

    def instance(self):
        persistence = BlobPersistence(self.blob, work_dir=Path(tempfile.mkdtemp()), source_dir=self.source)
        persistence.prepare()
        return persistence

    def test_ingested_rows_survive_cold_start(self):
        first = self.instance()
        first.tables_ingested(new_tables())
        self.assertIn(BLOB_PREFIX + "tenders.csv", self.blob.files)

        second = self.instance()   # новий екземпляр функції: /tmp порожній
        ids = pd.read_csv(second.data_dir / "tenders.csv", dtype=str)["tender_id"].tolist()
        self.assertEqual(sorted(ids), ["new", "old"])

    def test_sqlite_round_trip_and_refresh(self):
        first = self.instance()
        storage = Storage(first.db_path, on_change=first.db_changed)
        storage.save_subscription(Subscription(5, active=True, min_score=50))
        first.flush()
        self.assertIn(DB_BLOB, self.blob.files)

        second = self.instance()
        self.assertTrue(Storage(second.db_path).get_subscription(5).active)

        # Перший екземпляр змінює підписку — другий підтягує її.
        storage.set_active(5, False)
        first.flush()
        second._last_refresh = 0
        self.assertTrue(second.refresh_db())
        self.assertFalse(Storage(second.db_path).get_subscription(5).active)


if __name__ == "__main__":
    unittest.main()
