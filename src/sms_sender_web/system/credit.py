"""Kavenegar's credit, kept current (R5 of the 2026-10-05 review). The
worker asks every 15 minutes and the status page on every view; the
campaign list warns when it's under the level an admin set, and the
notification targets hear once each time it drops under it. A send still
checks its own estimate against the credit before anything goes out."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from sms_sender.notify import notify_text
from sms_sender.sender import HaltError, SendError

from .models import ProviderCheck, SystemSettings

logger = logging.getLogger(__name__)

CHECK_EVERY = timedelta(minutes=15)
LOW_MESSAGE = ("sms-sender: Kavenegar credit is {credit:,} rials, under the warning level of {floor:,}. "
               "Top up the account before the next send.")


def record(credit: int | None, problem: str = "", *, now=None) -> ProviderCheck:
    """Keep what Kavenegar said (`problem`: no_key, refused or unreachable,
    when it said nothing), and tell the targets when the credit has just
    dropped under the warning level."""
    now = now or timezone.now()
    check = ProviderCheck.load()
    check.checked_at, check.problem = now, problem
    if problem:
        check.save(update_fields=["checked_at", "problem"])
        return check
    check.credit = credit
    floor = SystemSettings.load().credit_floor
    low = floor is not None and credit is not None and credit < floor
    dropped = low and check.below_since is None
    check.below_since = (check.below_since or now) if low else None
    check.save(update_fields=["checked_at", "problem", "credit", "below_since"])
    if dropped:
        logger.warning("credit_low", extra={"credit": credit, "floor": floor})
        for target in SystemSettings.load().notify_targets or []:
            notify_text(target, LOW_MESSAGE.format(credit=credit, floor=floor))  # never raises
    return check


def check_now(sender) -> ProviderCheck | None:
    """Ask Kavenegar (account/info) and record the answer. Never raises; a
    sender that can't ask (a test's fake) records nothing."""
    ask = getattr(sender, "account_info", None)
    if ask is None:
        return None
    try:
        info = ask()
    except HaltError:
        return record(None, "refused")
    except SendError:
        return record(None, "unreachable")
    return record(info.remaining_credit)


def due(now=None) -> bool:
    checked = ProviderCheck.load().checked_at
    return checked is None or (now or timezone.now()) - checked >= CHECK_EVERY


def warning() -> dict | None:
    """What the campaign list says about the credit, or None: under the
    warning level, or Kavenegar refusing the account."""
    check = ProviderCheck.load()
    floor = SystemSettings.load().credit_floor
    if check.problem == "refused":
        return {"refused": True, "checked_at": check.checked_at}
    if floor is not None and check.credit is not None and check.credit < floor:
        return {"credit": check.credit, "floor": floor, "checked_at": check.checked_at}
    return None
