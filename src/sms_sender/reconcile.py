"""Settle `unknown` rows by asking Kavenegar what it actually sent.

A row is `unknown` when its request may have reached Kavenegar without a
clear answer (read timeout, dropped connection, crash mid-send). Resending
it blindly could deliver a second SMS, so instead we look the phone up with
`sms/statusbyreceptor` around the time of the attempt:

- exactly one message we don't already know → it was ours: `sent`
- none → it never went out: `failed_retriable`, the next run sends it
- several → can't tell which is ours: `needs_review`, an operator decides

Message IDs this DB already accounts for (sent rows, the approval test)
never count. The campaign account carries no OTP traffic, so "several" only
happens if two campaigns reach the same phone within minutes.

Rows younger than `min_age_sec` wait: Kavenegar records a send within a
second, and the margin covers clock skew and a request still settling.
Nothing here sends an SMS.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .sender import HaltError, SendError, Sender
from .state import StateStore

logger = logging.getLogger(__name__)

DEFAULT_MIN_AGE_SEC = 300.0
# Lookup window around a row's last claim: our clock vs Kavenegar's before
# it, the claim's whole retry sequence after it (Kavenegar allows ≤ 1 day).
WINDOW_BEFORE_SEC = 120
WINDOW_AFTER_SEC = 900


@dataclass(frozen=True)
class ReconcileSummary:
    sent: int = 0          # found at Kavenegar → `sent`
    requeued: int = 0      # not found → `failed_retriable`, safe to send again
    needs_review: int = 0  # several candidates → `needs_review`
    deferred: int = 0      # too recent, or Kavenegar couldn't be asked: still `unknown`

    @property
    def checked(self) -> int:
        return self.sent + self.requeued + self.needs_review


def reconcile_unknown(
    state: StateStore, sender: Sender, *,
    min_age_sec: float = DEFAULT_MIN_AGE_SEC,
    now: Callable[[], float] = time.time,
) -> ReconcileSummary:
    """Settle every `unknown` row old enough to check.

    Raises HaltError (bad key, account problem) and leaves the remaining rows
    `unknown`; a failed lookup for one phone just defers that row.
    """
    sent = requeued = needs_review = deferred = 0
    known = state.known_message_ids()
    for phone, attempted_at in state.list_unknown():
        started = now()
        if attempted_at is None or started - attempted_at < min_age_sec:
            deferred += 1
            continue
        try:
            messages = sender.find_messages(
                phone,
                attempted_at - WINDOW_BEFORE_SEC,
                min(attempted_at + WINDOW_AFTER_SEC, started),
            )
        except HaltError:
            raise
        except SendError as e:
            deferred += 1
            logger.warning("reconcile_lookup_failed", extra={"phone": phone, "detail": e.message})
            continue

        candidates = [m for m in messages if m.message_id not in known]
        message_id = None
        if len(candidates) == 1:
            message_id = candidates[0].message_id
            changed = state.settle_unknown_sent(phone, message_id)
            outcome, detail = "reconciled_sent", (
                f"kavenegar has message {message_id} (delivery status {candidates[0].status})"
            )
            if changed:
                known.add(message_id)
                sent += 1
        elif not candidates:
            outcome, detail = "reconciled_not_sent", "no message at kavenegar around the attempt"
            changed = state.settle_unknown_not_sent(phone, detail)
            if changed:
                requeued += 1
        else:
            outcome, detail = "needs_review", (
                f"{len(candidates)} messages to this phone around the attempt: "
                + ", ".join(str(m.message_id) for m in candidates)
            )
            changed = state.settle_unknown_for_review(phone, detail)
            if changed:
                needs_review += 1
        if not changed:
            continue  # someone else settled or reset it meanwhile
        state.record_attempt(
            phone=phone, kind="reconcile", outcome=outcome, started_at=started,
            finished_at=now(), message_id=message_id, detail=detail,
        )
        logger.info("reconciled", extra={"phone": phone, "outcome": outcome})
    return ReconcileSummary(sent, requeued, needs_review, deferred)
