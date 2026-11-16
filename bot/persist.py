"""Де лежать дані бота між перезапусками.

Локально (python -m bot) усе просто: data/*.csv і bot_data/bot.db на постійному диску.

На Vercel диск функції лише для читання, а /tmp зникає разом з екземпляром. Тому:
- data/*.csv з репозиторію копіюємо в /tmp/bot/data і дописуємо туди тендери зі стрічки;
- ті самі дозавантажені рядки (лише їх) зберігаємо у Vercel Blob як «дельту» й при
  холодному старті накладаємо на копію знімка через updater.upsert_tables;
- файл SQLite теж лежить у Blob: завантажуємо при старті, вивантажуємо після змін.

Це розраховано на невелике навантаження (один-два екземпляри функції одночасно).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Protocol

import pandas as pd

import app as contract
import data_loader
import updater

logger = logging.getLogger(__name__)

WORK_DIR = Path("/tmp/bot")
BLOB_PREFIX = "bot-state/"
DB_BLOB = BLOB_PREFIX + "bot.db"
DELTA_FILES = dict(updater.CSV_FILES)          # tenders/items/awards -> *.csv
DELTA_COLUMNS = {"tenders": contract.TENDERS_COLUMNS, "items": contract.ITEMS_COLUMNS,
                 "awards": contract.AWARDS_COLUMNS}
DB_REFRESH_MIN_SEC = 5


class BlobClient(Protocol):
    def get(self, pathname: str) -> bytes | None: ...
    def put(self, pathname: str, body: bytes) -> None: ...
    def uploaded_at(self, pathname: str) -> float | None: ...


class VercelBlob:
    """Мінімальна обгортка над vercel-blob (приватне сховище)."""

    def __init__(self):
        from vercel.blob import sync as blob  # імпорт лише на Vercel
        from vercel.blob import errors
        self._blob = blob
        self._not_found = getattr(errors, "BlobNotFoundError", Exception)

    def get(self, pathname: str) -> bytes | None:
        try:
            result = self._blob.get_blob(pathname, access="private")
        except self._not_found:
            return None
        body = getattr(result, "body", None)
        if body is None and hasattr(result, "content"):
            body = result.content
        return bytes(body) if body is not None else None

    def put(self, pathname: str, body: bytes) -> None:
        self._blob.put_blob(pathname, body, access="private", add_random_suffix=False,
                            allow_overwrite=True, content_type="application/octet-stream")

    def uploaded_at(self, pathname: str) -> float | None:
        try:
            head = self._blob.head_blob(pathname)
        except self._not_found:
            return None
        moment = getattr(head, "uploaded_at", None)
        return moment.timestamp() if hasattr(moment, "timestamp") else None


class LocalPersistence:
    """Постійний диск: нічого не синхронізуємо."""

    remote = False

    def __init__(self, data_dir: Path, db_path: Path):
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path)

    def prepare(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def db_changed(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def refresh_db(self) -> bool:
        return False

    def tables_ingested(self, tables: dict[str, pd.DataFrame]) -> None:
        pass


class BlobPersistence:
    """Знімок у /tmp + дельта й SQLite у Vercel Blob."""

    remote = True

    def __init__(self, blob: BlobClient, work_dir: Path = WORK_DIR,
                 source_dir: Path = data_loader.DATA_DIR):
        self.blob = blob
        self.work_dir = Path(work_dir)
        self.source_dir = Path(source_dir)
        self.data_dir = self.work_dir / "data"
        self.delta_dir = self.work_dir / "delta"
        self.db_path = self.work_dir / "bot.db"
        self._dirty = False
        self._lock = threading.Lock()
        self._db_seen_at: float | None = None
        self._last_refresh = 0.0

    # --- старт -----------------------------------------------------------------

    def prepare(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if not (self.data_dir / "tenders.csv").exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for name in (*DELTA_FILES.values(), updater.STATE_FILE, "cpv.csv"):
                source = self.source_dir / name
                if source.exists():
                    shutil.copyfile(source, self.data_dir / name)
            self._apply_delta()
        self._download_db()

    def _empty_delta(self) -> None:
        self.delta_dir.mkdir(parents=True, exist_ok=True)
        for name, file in DELTA_FILES.items():
            path = self.delta_dir / file
            if not path.exists():
                pd.DataFrame(columns=DELTA_COLUMNS[name]).to_csv(path, index=False)

    def _apply_delta(self) -> None:
        self._empty_delta()
        tables = {}
        for name, file in DELTA_FILES.items():
            body = self.blob.get(BLOB_PREFIX + file)
            if body:
                (self.delta_dir / file).write_bytes(body)
            tables[name] = pd.read_csv(self.delta_dir / file, dtype=str, keep_default_na=False)
        if not tables["tenders"].empty:
            added, updated = updater.upsert_tables(tables, self.data_dir)
            logger.info("Дельта з Blob: %s тендерів (+%s, оновлено %s)",
                        len(tables["tenders"]), added, updated)

    def _download_db(self) -> None:
        body = self.blob.get(DB_BLOB)
        if body:
            self.db_path.write_bytes(body)
        self._db_seen_at = self.blob.uploaded_at(DB_BLOB) if body else None
        self._last_refresh = time.monotonic()

    # --- SQLite ------------------------------------------------------------------

    def db_changed(self) -> None:
        self._dirty = True

    def flush(self) -> None:
        """Вивантажити SQLite в Blob, якщо були зміни."""
        with self._lock:
            if not self._dirty or not self.db_path.exists():
                return
            self._dirty = False
            body = self.db_path.read_bytes()
        try:
            self.blob.put(DB_BLOB, body)
            self._db_seen_at = time.time()
        except Exception:
            self._dirty = True
            logger.exception("Не вдалося зберегти bot.db у Blob")

    def refresh_db(self) -> bool:
        """Підтягнути свіжу SQLite, якщо інший екземпляр її оновив. True — файл замінено."""
        if self._dirty or time.monotonic() - self._last_refresh < DB_REFRESH_MIN_SEC:
            return False
        self._last_refresh = time.monotonic()
        try:
            remote_at = self.blob.uploaded_at(DB_BLOB)
        except Exception:
            logger.exception("Blob недоступний")
            return False
        if remote_at is None or (self._db_seen_at and remote_at <= self._db_seen_at + 0.5):
            return False
        body = self.blob.get(DB_BLOB)
        if not body:
            return False
        self.db_path.write_bytes(body)
        self._db_seen_at = remote_at
        return True

    # --- дельта тендерів -------------------------------------------------------------

    def tables_ingested(self, tables: dict[str, pd.DataFrame]) -> None:
        """Дописати нові рядки в дельту й вивантажити її в Blob."""
        self._empty_delta()
        updater.upsert_tables(tables, self.delta_dir)
        for file in DELTA_FILES.values():
            try:
                self.blob.put(BLOB_PREFIX + file, (self.delta_dir / file).read_bytes())
            except Exception:
                logger.exception("Не вдалося зберегти дельту %s у Blob", file)


def make_persistence(settings) -> LocalPersistence | BlobPersistence:
    if settings.on_vercel:
        if not settings.blob_enabled:
            logger.warning("На Vercel без Blob: підписки й дозавантажені тендери зникнуть "
                           "після перезапуску екземпляра")
            return LocalPersistence(_vercel_tmp_data(), WORK_DIR / "bot.db")
        return BlobPersistence(VercelBlob())
    return LocalPersistence(data_loader.DATA_DIR, settings.db_path)


def _vercel_tmp_data() -> Path:
    """Без Blob на Vercel усе одно потрібна копія даних у /tmp (диск лише для читання)."""
    target = WORK_DIR / "data"
    if not (target / "tenders.csv").exists():
        target.mkdir(parents=True, exist_ok=True)
        for name in (*DELTA_FILES.values(), updater.STATE_FILE, "cpv.csv"):
            source = data_loader.DATA_DIR / name
            if source.exists():
                shutil.copyfile(source, target / name)
    return target

