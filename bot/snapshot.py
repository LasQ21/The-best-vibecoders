"""SnapshotStore: один незмінний DashboardData у пам'яті.

Перезавантаження йде в окремому потоці (asyncio.to_thread), після чого посилання
замінюється цілком. Обробники читають лише поточний знімок і ніколи його не змінюють.
Похідні таблиці (зведення замовників тощо) кешуються на версію знімка.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import dashboard_data
import data_loader

logger = logging.getLogger(__name__)

_LOAD_LOCK = threading.Lock()


def load_dashboard_from(data_dir: Path | None = None) -> dashboard_data.DashboardData:
    """dashboard_data.load_dashboard(), але з іншої теки даних.

    load_dashboard() читає data_loader.load_data() без аргументів. Щоб не дублювати
    ядро, на час виклику підставляємо load_data з потрібною текою.
    """
    if data_dir is None or Path(data_dir).resolve() == Path(data_loader.DATA_DIR).resolve():
        return dashboard_data.load_dashboard()
    with _LOAD_LOCK, mock.patch.object(data_loader, "load_data",
                                       partial(data_loader.load_data, Path(data_dir))):
        return dashboard_data.load_dashboard()


@dataclass
class Snapshot:
    data: dashboard_data.DashboardData
    version: int
    loaded_at: datetime
    load_seconds: float
    _cache: dict[str, Any] = field(default_factory=dict)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def tenders(self):
        return self.data.tenders

    @property
    def threshold(self) -> int:
        return int(self.data.high_threshold)

    def derived(self, key: str, build: Callable[[], Any]) -> Any:
        """Обчислити раз на версію знімка (зведення замовників, опції фільтрів...)."""
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]
        value = build()
        with self._cache_lock:
            return self._cache.setdefault(key, value)


class SnapshotStore:
    def __init__(self, data_dir: Path | None = None,
                 loader: Callable[[Path | None], dashboard_data.DashboardData] = load_dashboard_from):
        self.data_dir = data_dir
        self._loader = loader
        self._current: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._version = 0
        self.last_error: str | None = None

    @property
    def current(self) -> Snapshot | None:
        return self._current

    @property
    def ready(self) -> bool:
        return self._current is not None

    async def get(self) -> Snapshot:
        """Поточний знімок; якщо його ще немає — завантажити (один раз на всіх)."""
        if self._current is None:
            await self._load(only_if_missing=True)
        assert self._current is not None
        return self._current

    async def reload(self) -> Snapshot:
        """Перечитати дані в потоці й атомарно замінити знімок."""
        return await self._load(only_if_missing=False)

    async def _load(self, only_if_missing: bool) -> Snapshot:
        async with self._lock:
            if only_if_missing and self._current is not None:
                return self._current
            started = time.monotonic()
            try:
                data = await asyncio.to_thread(self._loader, self.data_dir)
            except Exception as error:
                self.last_error = str(error)
                logger.exception("Не вдалося завантажити знімок даних")
                if self._current is None:
                    raise
                return self._current
            self._version += 1
            snapshot = Snapshot(data=data, version=self._version,
                                loaded_at=datetime.now(timezone.utc),
                                load_seconds=time.monotonic() - started)
            self._current = snapshot
            self.last_error = None
            logger.info("Знімок v%s: %s тендерів за %.1f с", snapshot.version,
                        len(data.tenders), snapshot.load_seconds)
            return snapshot
