"""Settle `unknown` rows by asking Kavenegar what it actually sent.

A row is `unknown` when its request may have reached Kavenegar without a
clear answer (read timeout, dropped connection, crash mid-send). Resending
it blindly could deliver a second SMS, so instead we look the phone up with
`sms/statusbyreceptor`.

How that lookup behaves (checked against the live API on 2026-10-04): it
answers per calendar day, not per second — any window inside a day returns
that whole day's messages to the phone, and other days return nothing. A
fresh message is listed within a minute, and the entries carry no time.
`sms/select`, which has each message's time and text, needs our IP on an
allowlist (error 407), so it isn't used.

First, the row's own call records: if Kavenegar accepted the SMS after the
row's claim and the process stopped before marking it, the row is `sent`
with that message, without a lookup. (The lookup couldn't find it: its ID
is already known, so it would be skipped, and the row sent again.)

Otherwise the candidates are the phone's messages on the day(s) of the attempt,
minus every message ID already recorded — in this campaign's DB (sent rows,
the approval test) and in the other campaign DBs next to it, since another
campaign may have reached the same person that day. Then:

- exactly one left → it was ours: `sent`
- none, for an attempt under a day old → it never went out:
  `failed_retriable`, the next run sends it (see REQUEUE_NOT_FOUND)
- none, for an older attempt → `needs_review`: a 5-day-old message wasn't
  listed (2026-10-04), so "not found" proves nothing that late
- several → can't tell which is ours: `needs_review`, an operator decides

A message sent from the campaign account outside sms-sender (e.g. from
Kavenegar's panel) on the same day isn't recorded anywhere and could be
taken for ours: the row then counts as `sent` without an SMS of its own.
That errs on the side of never sending twice.

Rows younger than `min_age_sec` wait: Kavenegar records a send within a
second, and the margin covers clock skew and a request still settling.
Nothing here sends an SMS.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .sender import HaltError, ProviderMessage, SendError, Sender
from .state import StateStore
from .window import TEHRAN

logger = logging.getLogger(__name__)

DEFAULT_MIN_AGE_SEC = 300.0

# "Nothing at Kavenegar" means "never sent": confirmed for a fresh message on
# 2026-10-04 (found within a minute), so a not-found row is sent again — but
# only while Kavenegar still lists the attempt's day.
REQUEUE_NOT_FOUND = True
TRUST_NOT_FOUND_SEC = 24 * 3600
# Window around a row's last claim: our clock vs Kavenegar's before it, the
# claim's whole retry sequence after it. Kavenegar only looks at the days it
# touches.
WINDOW_BEFORE_SEC = 120
WINDOW_AFTER_SEC = 900


@dataclass(frozen=True)
class ReconcileSummary:
    sent: int = 0          # found at Kavenegar → `sent`
    requeued: int = 0      # not found → `failed_retriable`, safe to send again
    needs_review: int = 0  # several candidates, or too old to trust → `needs_review`
    deferred: int = 0      # too recent, or Kavenegar couldn't be asked: still `unknown`

    @property
    def checked(self) -> int:
        return self.sent + self.requeued + self.needs_review


def _spans(start: float, end: float) -> list[tuple[float, float]]:
    """The lookups to make. Kavenegar answers per day; when the window
    crosses midnight — in Tehran or in UTC, it isn't documented which it
    uses — each day is also asked on its own."""
    spans = [(start, end)]
    for tz in (TEHRAN, timezone.utc):
        if datetime.fromtimestamp(start, tz).date() != datetime.fromtimestamp(end, tz).date():
            spans += [(start, start + 1), (end - 1, end)]
            break
    return spans


def _lookup(sender: Sender, phone: str, start: float, end: float) -> list[ProviderMessage]:
    found: dict[int, ProviderMessage] = {}
    for a, b in _spans(start, end):
        for message in sender.find_messages(phone, a, b):
            found.setdefault(message.message_id, message)
    return list(found.values())


def reconcile_unknown(
    state: StateStore, sender: Sender, *,
    min_age_sec: float = DEFAULT_MIN_AGE_SEC,
    requeue_not_found: bool = REQUEUE_NOT_FOUND,
    now: Callable[[], float] = time.time,
) -> ReconcileSummary:
    """Settle every `unknown` row old enough to check.

    Raises HaltError (bad key, account problem) and leaves the remaining rows
    `unknown`; a failed lookup for one phone just defers that row.
    """
    sent = requeued = needs_review = deferred = 0
    rows = state.list_unknown()
    if not rows:
        return ReconcileSummary()
    known = state.known_message_ids() | state.neighbour_message_ids()
    for phone, attempted_at in rows:
        started = now()
        recorded = state.accepted_since(phone, attempted_at)
        if recorded is not None:
            # Kavenegar accepted it and the call was recorded, but the process
            # stopped before the row was marked. The record is the proof; a
            # lookup would skip this message as one we already know.
            message_id, cost = recorded
            if state.settle_unknown_sent(phone, message_id, cost):
                sent += 1
                state.record_attempt(
                    phone=phone, kind="reconcile", outcome="reconciled_sent", started_at=started,
                    finished_at=now(), message_id=message_id,
                    detail=f"kavenegar accepted message {message_id} before the process stopped",
                )
                logger.info("reconciled", extra={"phone": phone, "outcome": "reconciled_sent"})
            continue
        if attempted_at is None or started - attempted_at < min_age_sec:
            deferred += 1
            continue
        try:
            messages = _lookup(
                sender, phone,
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
        fresh = started - attempted_at < TRUST_NOT_FOUND_SEC
        if len(candidates) == 1:
            message_id = candidates[0].message_id
            changed = state.settle_unknown_sent(phone, message_id)
            outcome, detail = "reconciled_sent", (
                f"kavenegar has message {message_id} (delivery status {candidates[0].status})"
            )
            if changed:
                known.add(message_id)
                sent += 1
        elif not candidates and requeue_not_found and fresh:
            outcome, detail = "reconciled_not_sent", "no message at kavenegar on the attempt's day"
            changed = state.settle_unknown_not_sent(phone, detail)
            if changed:
                requeued += 1
        elif not candidates:
            outcome, detail = "needs_review", (
                "no message at kavenegar on the attempt's day, but the attempt is "
                "more than a day old, so that's no proof it wasn't sent; check first"
                if requeue_not_found else
                "no message at kavenegar on the attempt's day (requeue switched off)"
            )
            changed = state.settle_unknown_for_review(phone, detail)
            if changed:
                needs_review += 1
        else:
            outcome, detail = "needs_review", (
                f"{len(candidates)} messages to this phone on the attempt's day: "
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
