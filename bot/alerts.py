"""Сповіщення: хто що отримує, без повторів, з обмеженням частоти.

Відбір — чисті функції (matches, plan_alerts), надсилання — AlertSender із чергою:
не більше одного повідомлення на секунду в чат і ~25 на секунду загалом.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import pandas as pd

import app as contract

from . import formatting as fmt
from .storage import Storage, Subscription

logger = logging.getLogger(__name__)

GLOBAL_PER_SECOND = 25
PER_CHAT_INTERVAL = 1.0
MAX_SEND_ATTEMPTS = 3


def matches(sub: Subscription, row) -> bool:
    """Чи підходить тендер під підписку.

    Відстежуваний замовник має окремий нижчий поріг і проходить незалежно від
    регіонів і прапорців. Приховані замовники не проходять ніколи.
    """
    buyer = row.get("buyer_edrpou")
    buyer = None if fmt.is_missing(buyer) else str(buyer)
    total = fmt.safe_float(row.get("total_score"))
    if total != total:  # NaN
        return False
    if buyer and buyer in sub.muted:
        return False
    if buyer and buyer in sub.buyers and total >= sub.follow_min_score:
        return True
    if total < sub.min_score:
        return False
    if sub.regions and row.get("buyer_region") not in sub.regions:
        return False
    if sub.flags:
        flags = [flag for flag in sub.flags if flag in contract.FLAG_COLUMNS]
        if flags and not any(fmt.safe_float(row.get(flag), 0) == 1 for flag in flags):
            return False
    return True


def candidates_from(changed: pd.DataFrame, now: datetime, window_hours: int) -> pd.DataFrame:
    """Змінені тендери з date_modified не старшим за window_hours, за балом спадно."""
    if changed.empty:
        return changed
    since = pd.Timestamp(now - timedelta(hours=window_hours))
    fresh = changed.loc[changed["date_modified"].ge(since) & changed["total_score"].notna()]
    return fresh.sort_values("total_score", ascending=False)


@dataclass
class ChatPlan:
    chat_id: int
    send: list = field(default_factory=list)       # рядки тендерів окремими повідомленнями
    collapsed: list = field(default_factory=list)  # решта — одним «ще N»
    min_score: float = 40


def plan_alerts(candidates: pd.DataFrame, subs: list[Subscription], storage: Storage,
                now: datetime | None = None) -> list[ChatPlan]:
    """Для кожної підписки — що надіслати окремо, а що згорнути (ліміт на годину)."""
    now = now or datetime.now(timezone.utc)
    plans = []
    if candidates.empty:
        return plans
    rows = [row for _, row in candidates.iterrows()]
    for sub in subs:
        matched = [row for row in rows if matches(sub, row)]
        if not matched:
            continue
        already = storage.sent_tender_ids(sub.chat_id, [row["tender_id"] for row in matched])
        matched = [row for row in matched if row["tender_id"] not in already]
        if not matched:
            continue
        budget = max(0, sub.max_per_hour - storage.sent_since(sub.chat_id, now - timedelta(hours=1)))
        plans.append(ChatPlan(chat_id=sub.chat_id, send=matched[:budget],
                              collapsed=matched[budget:], min_score=sub.min_score))
    return plans


SendFunc = Callable[[int, str, object], Awaitable[None]]


class Blocked(Exception):
    """Користувач заблокував бота."""


class AlertSender:
    """Надсилання з обмеженням частоти.

    send_message(chat_id, text, markup) — корутина, що кидає Blocked, коли бот
    заблокований, і RetryAfter (атрибут retry_after) при перевищенні ліміту Telegram.
    """

    def __init__(self, send_message: SendFunc, sleep=asyncio.sleep, clock=time.monotonic):
        self._send = send_message
        self._sleep = sleep
        self._clock = clock
        self._last_by_chat: dict[int, float] = defaultdict(lambda: -1e9)
        self._window: list[float] = []
        self._lock = asyncio.Lock()

    async def _throttle(self, chat_id: int) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._window = [moment for moment in self._window if now - moment < 1.0]
                wait_chat = self._last_by_chat[chat_id] + PER_CHAT_INTERVAL - now
                wait_global = (self._window[0] + 1.0 - now) if len(self._window) >= GLOBAL_PER_SECOND else 0
                wait = max(wait_chat, wait_global, 0)
                if wait <= 0:
                    self._window.append(now)
                    self._last_by_chat[chat_id] = now
                    return
                await self._sleep(wait)

    async def send(self, chat_id: int, text: str, markup=None) -> bool:
        """True — надіслано. Blocked прокидаємо нагору, решту помилок логуємо."""
        for attempt in range(MAX_SEND_ATTEMPTS):
            await self._throttle(chat_id)
            try:
                await self._send(chat_id, text, markup)
                return True
            except Blocked:
                raise
            except Exception as error:
                retry_after = getattr(error, "retry_after", None)
                if retry_after is not None and attempt < MAX_SEND_ATTEMPTS - 1:
                    logger.warning("Telegram просить зачекати %s с", retry_after)
                    await self._sleep(float(retry_after))
                    continue
                logger.warning("Не вдалося надіслати в %s: %s", chat_id, error)
                return False
        return False


@dataclass
class DispatchResult:
    sent: int = 0
    collapsed: int = 0
    blocked: int = 0
    chats: int = 0


async def dispatch(plans: list[ChatPlan], sender: AlertSender, storage: Storage,
                   card: Callable[[object], tuple[str, object]],
                   collapsed_message: Callable[[ChatPlan], tuple[str, object]]) -> DispatchResult:
    """Надіслати за планом і записати в alerts_sent. Чати обробляються паралельно."""
    result = DispatchResult()

    async def one_chat(plan: ChatPlan):
        try:
            for row in plan.send:
                text, markup = card(row)
                if await sender.send(plan.chat_id, text, markup):
                    storage.record_sent(plan.chat_id, [(row["tender_id"], float(row["total_score"]))])
                    result.sent += 1
            if plan.collapsed:
                text, markup = collapsed_message(plan)
                if await sender.send(plan.chat_id, text, markup):
                    storage.record_sent(plan.chat_id, [(row["tender_id"], float(row["total_score"]))
                                                       for row in plan.collapsed], collapsed=True)
                    result.collapsed += len(plan.collapsed)
        except Blocked:
            storage.set_active(plan.chat_id, False)
            result.blocked += 1
            logger.info("Чат %s заблокував бота — підписку вимкнено", plan.chat_id)

    result.chats = len(plans)
    await asyncio.gather(*(one_chat(plan) for plan in plans))
    return result


def preview_count(candidates: pd.DataFrame, sub: Subscription) -> int:
    """Скільки сповіщень дала б підписка на цих тендерах (без ліміту на годину)."""
    if candidates.empty:
        return 0
    probe = sub.with_changes(muted=sub.muted)
    count = 0
    for _, row in candidates.iterrows():
        if matches(probe, row):
            count += 1
    return count


def preview_count_fast(candidates: pd.DataFrame, sub: Subscription) -> int:
    """Векторна версія preview_count для екрана «Сповіщення» (тисячі рядків)."""
    if candidates.empty:
        return 0
    total = candidates["total_score"]
    buyer = candidates["buyer_edrpou"].astype("string")
    followed = buyer.isin(list(sub.buyers)) & total.ge(sub.follow_min_score)
    regular = total.ge(sub.min_score)
    if sub.regions:
        regular &= candidates["buyer_region"].isin(list(sub.regions))
    flags = [flag for flag in sub.flags if flag in contract.FLAG_COLUMNS]
    if flags:
        regular &= candidates[flags].fillna(0).eq(1).any(axis=1)
    keep = (followed | regular) & ~buyer.isin(list(sub.muted)).fillna(False)
    return int(keep.fillna(False).sum())
