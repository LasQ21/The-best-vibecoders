"""SQLite бота: підписки, відстежувані й приховані замовники, надіслані сповіщення, kv.

Про користувача зберігаємо лише chat_id і дати. Запити короткі й локальні, тому
виконуються синхронно; кожен метод — окрема транзакція.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

DEFAULT_MIN_SCORE = 40
DEFAULT_FOLLOW_MIN_SCORE = 30
DEFAULT_MAX_PER_HOUR = 10
MIN_SCORE_RANGE = (30, 100)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id          INTEGER PRIMARY KEY,
    active           INTEGER NOT NULL DEFAULT 0,
    min_score        REAL    NOT NULL DEFAULT 40,
    regions          TEXT    NOT NULL DEFAULT '[]',
    flags            TEXT    NOT NULL DEFAULT '[]',
    follow_min_score REAL    NOT NULL DEFAULT 30,
    max_per_hour     INTEGER NOT NULL DEFAULT 10,
    paused_until     TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS followed_buyers (
    chat_id      INTEGER NOT NULL,
    buyer_edrpou TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, buyer_edrpou)
);
CREATE TABLE IF NOT EXISTS muted_buyers (
    chat_id      INTEGER NOT NULL,
    buyer_edrpou TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, buyer_edrpou)
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    chat_id   INTEGER NOT NULL,
    tender_id TEXT    NOT NULL,
    score     REAL    NOT NULL,
    sent_at   TEXT    NOT NULL,
    collapsed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, tender_id)
);
CREATE INDEX IF NOT EXISTS alerts_sent_time ON alerts_sent (chat_id, sent_at);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Subscription:
    chat_id: int
    active: bool = False
    min_score: float = DEFAULT_MIN_SCORE
    regions: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    follow_min_score: float = DEFAULT_FOLLOW_MIN_SCORE
    max_per_hour: int = DEFAULT_MAX_PER_HOUR
    paused_until: datetime | None = None
    buyers: frozenset[str] = field(default_factory=frozenset)
    muted: frozenset[str] = field(default_factory=frozenset)

    def is_paused(self, now: datetime | None = None) -> bool:
        return self.paused_until is not None and self.paused_until > (now or utcnow())

    def with_changes(self, **changes) -> "Subscription":
        return replace(self, **changes)


def clamp_score(value: float) -> float:
    low, high = MIN_SCORE_RANGE
    return float(min(high, max(low, round(float(value)))))


class Storage:
    """Обгортка над sqlite3. on_change викликається після кожного запису."""

    def __init__(self, path: Path | str, on_change: Callable[[], None] | None = None):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.on_change = on_change

    # --- службове ------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def reopen(self) -> None:
        """Перечитати файл БД (після того, як його замінили ззовні)."""
        with self._lock:
            self._conn.close()
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)

    def _query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _write(self, statements: list[tuple[str, tuple]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    self._conn.execute(sql, params)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        if self.on_change is not None:
            self.on_change()

    # --- kv --------------------------------------------------------------------

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_kv(self, key: str, value: str) -> None:
        self._write([("INSERT INTO kv(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))])

    def get_json(self, key: str, default=None):
        raw = self.get_kv(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value) -> None:
        self.set_kv(key, json.dumps(value, ensure_ascii=False))

    # --- підписки ----------------------------------------------------------------

    def _subscription_from_row(self, row: sqlite3.Row) -> Subscription:
        chat_id = row["chat_id"]
        return Subscription(
            chat_id=chat_id,
            active=bool(row["active"]),
            min_score=float(row["min_score"]),
            regions=tuple(json.loads(row["regions"] or "[]")),
            flags=tuple(json.loads(row["flags"] or "[]")),
            follow_min_score=float(row["follow_min_score"]),
            max_per_hour=int(row["max_per_hour"]),
            paused_until=parse_iso(row["paused_until"]),
            buyers=frozenset(self.followed_buyers(chat_id)),
            muted=frozenset(self.muted_buyers(chat_id)),
        )

    def get_subscription(self, chat_id: int) -> Subscription:
        """Підписка чату; якщо її ще немає — типова, вимкнена (у БД не пишемо)."""
        rows = self._query("SELECT * FROM subscriptions WHERE chat_id = ?", (chat_id,))
        if not rows:
            return Subscription(chat_id=chat_id,
                                buyers=frozenset(self.followed_buyers(chat_id)),
                                muted=frozenset(self.muted_buyers(chat_id)))
        return self._subscription_from_row(rows[0])

    def has_subscription(self, chat_id: int) -> bool:
        return bool(self._query("SELECT 1 FROM subscriptions WHERE chat_id = ?", (chat_id,)))

    def save_subscription(self, sub: Subscription) -> Subscription:
        now = iso(utcnow())
        sub = sub.with_changes(min_score=clamp_score(sub.min_score),
                               follow_min_score=clamp_score(sub.follow_min_score),
                               max_per_hour=max(1, min(60, int(sub.max_per_hour))))
        self._write([(
            "INSERT INTO subscriptions(chat_id, active, min_score, regions, flags, follow_min_score,"
            " max_per_hour, paused_until, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET active=excluded.active, min_score=excluded.min_score,"
            " regions=excluded.regions, flags=excluded.flags,"
            " follow_min_score=excluded.follow_min_score, max_per_hour=excluded.max_per_hour,"
            " paused_until=excluded.paused_until, updated_at=excluded.updated_at",
            (sub.chat_id, int(sub.active), sub.min_score,
             json.dumps(list(sub.regions), ensure_ascii=False),
             json.dumps(list(sub.flags)), sub.follow_min_score, sub.max_per_hour,
             iso(sub.paused_until) if sub.paused_until else None, now, now),
        )])
        return self.get_subscription(sub.chat_id)

    def set_active(self, chat_id: int, active: bool) -> Subscription:
        sub = self.get_subscription(chat_id)
        return self.save_subscription(sub.with_changes(active=active, paused_until=None))

    def active_subscriptions(self) -> list[Subscription]:
        rows = self._query("SELECT * FROM subscriptions WHERE active = 1")
        now = utcnow()
        subs = [self._subscription_from_row(row) for row in rows]
        return [sub for sub in subs if not sub.is_paused(now)]

    def count_subscribers(self) -> int:
        return int(self._query("SELECT COUNT(*) AS n FROM subscriptions WHERE active = 1")[0]["n"])

    # --- замовники -----------------------------------------------------------------

    def followed_buyers(self, chat_id: int) -> list[str]:
        rows = self._query("SELECT buyer_edrpou FROM followed_buyers WHERE chat_id = ? "
                           "ORDER BY created_at", (chat_id,))
        return [row["buyer_edrpou"] for row in rows]

    def muted_buyers(self, chat_id: int) -> list[str]:
        rows = self._query("SELECT buyer_edrpou FROM muted_buyers WHERE chat_id = ? "
                           "ORDER BY created_at", (chat_id,))
        return [row["buyer_edrpou"] for row in rows]

    def follow_buyer(self, chat_id: int, edrpou: str, follow: bool = True) -> None:
        if follow:
            self._write([
                ("INSERT OR IGNORE INTO followed_buyers VALUES(?,?,?)", (chat_id, edrpou, iso(utcnow()))),
                ("DELETE FROM muted_buyers WHERE chat_id = ? AND buyer_edrpou = ?", (chat_id, edrpou)),
            ])
        else:
            self._write([("DELETE FROM followed_buyers WHERE chat_id = ? AND buyer_edrpou = ?",
                          (chat_id, edrpou))])

    def mute_buyer(self, chat_id: int, edrpou: str, mute: bool = True) -> None:
        if mute:
            self._write([
                ("INSERT OR IGNORE INTO muted_buyers VALUES(?,?,?)", (chat_id, edrpou, iso(utcnow()))),
                ("DELETE FROM followed_buyers WHERE chat_id = ? AND buyer_edrpou = ?", (chat_id, edrpou)),
            ])
        else:
            self._write([("DELETE FROM muted_buyers WHERE chat_id = ? AND buyer_edrpou = ?",
                          (chat_id, edrpou))])

    # --- надіслані сповіщення --------------------------------------------------------

    def was_sent(self, chat_id: int, tender_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM alerts_sent WHERE chat_id = ? AND tender_id = ?",
                                (chat_id, tender_id)))

    def sent_tender_ids(self, chat_id: int, tender_ids: Iterable[str]) -> set[str]:
        ids = list(tender_ids)
        found: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._query(f"SELECT tender_id FROM alerts_sent WHERE chat_id = ? "
                               f"AND tender_id IN ({marks})", (chat_id, *chunk))
            found.update(row["tender_id"] for row in rows)
        return found

    def record_sent(self, chat_id: int, items: Iterable[tuple[str, float]], collapsed: bool = False,
                    when: datetime | None = None) -> None:
        moment = iso(when or utcnow())
        statements = [("INSERT OR IGNORE INTO alerts_sent VALUES(?,?,?,?,?)",
                       (chat_id, tender_id, float(score), moment, int(collapsed)))
                      for tender_id, score in items]
        if statements:
            self._write(statements)

    def sent_since(self, chat_id: int, since: datetime) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM alerts_sent WHERE chat_id = ? AND sent_at >= ? "
                           "AND collapsed = 0", (chat_id, iso(since)))
        return int(rows[0]["n"])

    def recent_alerts(self, chat_id: int, limit: int = 30) -> list[dict]:
        rows = self._query("SELECT tender_id, score, sent_at, collapsed FROM alerts_sent "
                           "WHERE chat_id = ? ORDER BY sent_at DESC LIMIT ?", (chat_id, limit))
        return [{"tender_id": row["tender_id"], "score": row["score"],
                 "sent_at": parse_iso(row["sent_at"]), "collapsed": bool(row["collapsed"])}
                for row in rows]

    def alerts_last_day(self) -> int:
        since = iso(utcnow() - timedelta(days=1))
        return int(self._query("SELECT COUNT(*) AS n FROM alerts_sent WHERE sent_at >= ?",
                               (since,))[0]["n"])
