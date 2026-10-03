"""Delivery reports: whether each accepted SMS reached the phone.

`sent` means Kavenegar accepted the SMS; whether it was delivered comes
later. `sms/status` answers for at most 500 message IDs per call, and only
for 48 hours after sending — after that every ID reads 100 — so sync inside
that window: `sms-sender delivery` a few times (e.g. after 10 minutes, an
hour, a day), or on the dashboard's schedule. Read-only: nothing is sent.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .sender import Sender
from .state import StateStore

logger = logging.getLogger(__name__)

# Kavenegar delivery status codes (kavenegar.com/rest.html, "وضعیت پیامک ها").
STATUS_NAMES: dict[int, str] = {
    1: "queued",
    2: "scheduled",
    4: "at operator",
    5: "at operator",
    6: "failed at operator",
    10: "delivered",
    11: "undelivered",
    13: "cancelled",
    14: "blocked by recipient",
    100: "expired / unknown id",
}
# Statuses that won't change any more. 11 (phone off) can still turn into
# 10 while the operator retries, so it's checked again.
FINAL: frozenset[int] = frozenset({6, 10, 13, 14, 100})
WINDOW_SEC = 48 * 3600
BATCH = 500


@dataclass(frozen=True)
class DeliverySummary:
    checked: int = 0  # SMS asked about
    updated: int = 0  # of those, how many got a (new) status


def sync_delivery(
    state: StateStore, sender: Sender, *, now: Callable[[], float] = time.time,
) -> DeliverySummary:
    """Ask Kavenegar about every sent SMS of the last 48 h without a final
    delivery status, 500 at a time, and store the answers. Raises HaltError
    on account problems; other failures keep what was stored so far."""
    started = now()
    pending = state.messages_awaiting_delivery(sent_after=started - WINDOW_SEC, final=FINAL)
    checked = updated = 0
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]
        by_id = {message_id: phone for phone, message_id in batch}
        statuses = sender.delivery_statuses(list(by_id))
        checked += len(batch)
        found = {by_id[mid]: status for mid, status in statuses.items() if mid in by_id}
        state.record_delivery(found, checked_at=now())
        updated += len(found)
    logger.info("delivery_synced", extra={"checked": checked, "updated": updated})
    return DeliverySummary(checked, updated)


def describe(counts: dict[int | None, int]) -> str:
    """'delivered 120 · undelivered 3 · not checked 5' for `status`."""
    named: dict[str, int] = {}
    for status, n in counts.items():
        name = "not checked" if status is None else STATUS_NAMES.get(status, f"status {status}")
        named[name] = named.get(name, 0) + n
    return " · ".join(f"{name} {n}" for name, n in sorted(named.items(), key=lambda kv: -kv[1]))
