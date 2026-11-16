"""Опитування стрічки Prozorro лише для конкурентних процедур і розсилка сповіщень.

Схема одного циклу (див. TG_BOT_PLAN.md, розділ 5):
 1. запам'ятати {tender_id: date_modified} зі знімка;
 2. прочитати стрічку від курсора бота, взяти конкурентні записи (до POLL_LIMIT);
 3. завантажити документи й дописати в data/*.csv (updater, у потоці, під lock);
 4. зберегти курсор у SQLite (kv), а не в data/feed_state.json — той курсор сайту;
 5. нічого нового — кінець;
 6. перезавантажити знімок; помилка балу — без сповіщень, один лист адмінам;
 7–9. змінені за цикл тендери не старші за 48 год -> підписки -> надсилання.
10. Взяли рівно POLL_LIMIT — у стрічці є ще: наступний цикл одразу (до 5 поспіль).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import pandas as pd

import app as contract
import updater

from .config import FAILURES_BEFORE_ADMIN_ALERT, SLOW_RELOAD_SEC
from .storage import iso, parse_iso

logger = logging.getLogger(__name__)

CURSOR_KEY = "feed_cursor"
CURSOR_AT_KEY = "feed_cursor_at"
LAST_SUCCESS_KEY = "poll_last_success"
LAST_CYCLE_KEY = "poll_last_cycle"
FAILURES_KEY = "poll_failures"
LOCK_KEY = "poll_lock_until"
LOCK_SECONDS = 600


@dataclass
class FeedBatch:
    entries: list = field(default_factory=list)
    next_offset: str | None = None
    hit_limit: bool = False
    skipped_reporting: int = 0


def is_competitive(entry: dict) -> bool:
    return (updater.is_tracked(entry)
            and entry.get("procurementMethodType") in contract.COMPETITIVE_METHODS)


def read_competitive_feed(offset: str, limit: int, get_json=None, max_pages: int | None = None) -> FeedBatch:
    """Як updater.read_feed, але бере лише конкурентні процедури.

    Курсор: якщо зупинилися на ліміті посеред сторінки — dateModified останнього
    взятого запису (решта сторінки не загубиться, повтори прибере upsert);
    якщо дійшли до кінця стрічки — next_page.offset останньої сторінки.
    """
    get_json = get_json or updater.get_json
    max_pages = max_pages or updater.MAX_FEED_PAGES
    batch = FeedBatch()
    for _ in range(max_pages):
        page = get_json(updater.feed_url(offset))
        data = page.get("data", [])
        page_offset = page.get("next_page", {}).get("offset", offset)
        for entry in data:
            if not is_competitive(entry):
                if entry.get("procurementMethodType") == "reporting":
                    batch.skipped_reporting += 1
                continue
            batch.entries.append(entry)
            if len(batch.entries) >= limit:
                batch.next_offset = entry["dateModified"]
                batch.hit_limit = True
                return batch
        offset = page_offset
        if not data:
            break
    batch.next_offset = offset
    return batch


def next_cursor(entries: list, fetched_ids: set[str], next_offset: str) -> str:
    """Якщо частину документів не завантажили — найраніший dateModified з пропущених."""
    missed = [entry["dateModified"] for entry in entries if entry["id"] not in fetched_ids]
    return min(missed) if missed else next_offset


def start_offset(cursor: str | None, cursor_at: datetime | None, now: datetime,
                 max_lag_hours: int) -> tuple[str, str | None]:
    """Звідки читати стрічку. Повертає (offset, примітка про пропущений проміжок або None)."""
    if not cursor or cursor_at is None:
        return (now - timedelta(hours=1)).isoformat(), "курсора ще немає — починаємо з години тому"
    if now - cursor_at > timedelta(hours=max_lag_hours):
        gap = f"{cursor_at.isoformat(timespec='minutes')} — {(now - timedelta(hours=1)).isoformat(timespec='minutes')}"
        return (now - timedelta(hours=1)).isoformat(), f"курсор застарів, пропущено проміжок {gap}"
    return cursor, None


def changed_tenders(before: dict[str, object], tenders: pd.DataFrame, ids: set[str]) -> pd.DataFrame:
    """Тендери з ids, яких не було до циклу або в яких інший date_modified."""
    subset = tenders.loc[tenders["tender_id"].isin(ids)]
    keep = []
    for tender_id, modified in zip(subset["tender_id"], subset["date_modified"]):
        old = before.get(tender_id)
        keep.append(old is None or pd.isna(old) or old != modified)
    return subset.loc[keep]


@dataclass
class CycleResult:
    ok: bool = True
    taken: int = 0
    added: int = 0
    updated: int = 0
    failed: int = 0
    candidates: int = 0
    sent: int = 0
    collapsed: int = 0
    hit_limit: bool = False
    message: str = ""
    seconds: float = 0.0


class Poller:
    """Цикл опитування. Усі зовнішні дії — через передані залежності (зручно тестувати)."""

    def __init__(self, services, get_json=None, fetch_tenders=None, upsert=None,
                 now: Callable[[], datetime] | None = None):
        self.services = services
        self.settings = services.settings
        self.storage = services.storage
        self.get_json = get_json or updater.get_json
        self.fetch_tenders = fetch_tenders or updater.fetch_tenders
        self.upsert = upsert or updater.upsert_tables
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = asyncio.Lock()
        self.last_result: CycleResult | None = None
        self.last_cycle_at: datetime | None = None
        self._scoring_error_reported = False

    # --- службове -------------------------------------------------------------------

    def status(self) -> dict:
        storage = self.storage
        return {
            "cursor": storage.get_kv(CURSOR_KEY),
            "cursor_at": parse_iso(storage.get_kv(CURSOR_AT_KEY)),
            "last_success": parse_iso(storage.get_kv(LAST_SUCCESS_KEY)),
            "last_cycle": storage.get_json(LAST_CYCLE_KEY, {}),
            "failures": int(storage.get_kv(FAILURES_KEY, "0") or 0),
        }

    async def _notify_admins(self, text: str) -> None:
        notify = getattr(self.services, "notify_admins", None)
        if notify is not None:
            try:
                await notify(text)
            except Exception:
                logger.exception("Не вдалося написати адміністраторам")

    async def _failure(self, error: Exception) -> CycleResult:
        failures = int(self.storage.get_kv(FAILURES_KEY, "0") or 0) + 1
        self.storage.set_kv(FAILURES_KEY, str(failures))
        logger.warning("Опитування Prozorro не вдалося (%s поспіль): %s", failures, error)
        if failures == FAILURES_BEFORE_ADMIN_ALERT:
            await self._notify_admins(
                f"⚠️ Prozorro недоступний {failures} циклів поспіль. Працюємо на збережених даних.")
        return CycleResult(ok=False, message=f"Prozorro недоступний: {error}")

    async def _success(self) -> None:
        failures = int(self.storage.get_kv(FAILURES_KEY, "0") or 0)
        if failures >= FAILURES_BEFORE_ADMIN_ALERT:
            await self._notify_admins("✅ Зв'язок із Prozorro відновився.")
        if failures:
            self.storage.set_kv(FAILURES_KEY, "0")
        self.storage.set_kv(LAST_SUCCESS_KEY, iso(self.now()))

    # --- цикл ------------------------------------------------------------------------

    async def run(self, max_cycles: int | None = None) -> list[CycleResult]:
        """Один цикл і, якщо стрічка не вичерпана, ще до MAX_CATCHUP_CYCLES поспіль."""
        max_cycles = max_cycles or self.settings.max_catchup_cycles
        results = []
        for _ in range(max_cycles):
            result = await self.run_cycle()
            results.append(result)
            if not (result.ok and result.hit_limit):
                break
        return results

    async def run_cycle(self) -> CycleResult:
        async with self.lock:
            started = time.monotonic()
            result = await self._cycle()
            result.seconds = round(time.monotonic() - started, 1)
            self.last_result = result
            self.last_cycle_at = self.now()
            self.storage.set_json(LAST_CYCLE_KEY, {
                "at": iso(self.last_cycle_at), "ok": result.ok, "taken": result.taken,
                "added": result.added, "updated": result.updated, "failed": result.failed,
                "sent": result.sent, "collapsed": result.collapsed, "seconds": result.seconds,
                "message": result.message})
            persistence = getattr(self.services, "persistence", None)
            if persistence is not None:
                await asyncio.to_thread(persistence.flush)
            return result

    async def _cycle(self) -> CycleResult:
        services = self.services
        snapshot = await services.snapshots.get()
        before = dict(zip(snapshot.tenders["tender_id"], snapshot.tenders["date_modified"]))

        now = self.now()
        offset, note = start_offset(self.storage.get_kv(CURSOR_KEY),
                                    parse_iso(self.storage.get_kv(CURSOR_AT_KEY)),
                                    now, self.settings.max_lag_hours)
        if note:
            logger.warning("Стрічка Prozorro: %s", note)

        data_dir = services.persistence.data_dir
        try:
            batch = await asyncio.to_thread(read_competitive_feed, offset,
                                            self.settings.poll_limit, self.get_json)
            documents, failed = await asyncio.to_thread(
                self.fetch_tenders, [entry["id"] for entry in batch.entries])
            tables = dict(zip(("tenders", "items", "awards"), updater.tenders_to_tables(documents)))
            added, updated = await asyncio.to_thread(self.upsert, tables, data_dir)
        except updater.ApiError as error:
            return await self._failure(error)

        if added or updated:
            await asyncio.to_thread(services.persistence.tables_ingested, tables)
        fetched = {document["id"] for document in documents}
        cursor = next_cursor(batch.entries, fetched, batch.next_offset or offset)
        self.storage.set_kv(CURSOR_KEY, cursor)
        self.storage.set_kv(CURSOR_AT_KEY, iso(now))
        await self._success()

        result = CycleResult(taken=len(batch.entries), added=added, updated=updated, failed=failed,
                             hit_limit=batch.hit_limit)
        if not (added or updated):
            result.message = "Нових тендерів немає"
            return result

        snapshot = await services.snapshots.reload()
        if snapshot.load_seconds > SLOW_RELOAD_SEC:
            await self._notify_admins(
                f"⏱ Перезавантаження даних триває {snapshot.load_seconds:.0f} с — знімок росте.")
        if snapshot.data.scoring_error:
            if not self._scoring_error_reported:
                self._scoring_error_reported = True
                await self._notify_admins("⚠️ Бал не порахувався після оновлення — сповіщення "
                                          "призупинено до наступного успішного циклу.")
            result.message = "Помилка балу, сповіщення не надсилаються"
            return result
        self._scoring_error_reported = False

        changed = changed_tenders(before, snapshot.tenders, fetched)
        dispatch = await services.alerts.process(changed, now)
        result.candidates = dispatch.candidates
        result.sent = dispatch.sent
        result.collapsed = dispatch.collapsed
        result.message = f"Додано {added}, оновлено {updated}, надіслано {dispatch.sent}"
        return result


async def poll_forever(poller: Poller, interval: int, sleep: Callable[[float], Awaitable] = asyncio.sleep):
    """Нескінченний цикл для режиму «процес». Помилки не зупиняють цикл."""
    while True:
        try:
            results = await poller.run()
            for result in results:
                logger.info("Цикл опитування: %s (%.1f с)", result.message, result.seconds)
        except Exception:
            logger.exception("Цикл опитування впав")
        await sleep(interval)
