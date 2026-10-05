"""Restricted sending (plan 06, D7): while SMS_SENDER_ALLOWED_NUMBERS is set,
an SMS may go only to the numbers it lists, e.g. your own phone while you
try the panel out on real data.

- The real `Sender` refuses any other number before calling Kavenegar.
- A run refuses before anything is sent (and before any short link is made)
  when its queue or its test number holds one.
- The sandbox never sends, so it isn't bound by it.
- A value that isn't a phone number allows nothing: a typo must never lift
  the rule.

The CLI and the dashboard read the same variable (both load `.env`), so
removing it from `.env` and restarting is what lifts it. (Not Kavenegar's
own "debug mode", where the account accepts SMS and delivers none.)"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .phone import InvalidPhoneError, normalize

ENV_ALLOWED_NUMBERS = "SMS_SENDER_ALLOWED_NUMBERS"


@dataclass(frozen=True)
class Allowlist:
    numbers: frozenset[str]
    invalid: int = 0  # entries that aren't phone numbers; while any, nothing is allowed

    def allows(self, phone: str) -> bool:
        if self.invalid:
            return False
        try:
            return normalize(phone) in self.numbers
        except InvalidPhoneError:
            return False


def allowlist(raw: str | None = None) -> Allowlist | None:
    """The numbers SMS_SENDER_ALLOWED_NUMBERS allows (comma-separated, in
    any form `phone.normalize` takes); None when sending isn't restricted."""
    raw = os.environ.get(ENV_ALLOWED_NUMBERS, "") if raw is None else raw
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts:
        return None
    numbers, invalid = set(), 0
    for part in parts:
        try:
            numbers.add(normalize(part))
        except InvalidPhoneError:
            invalid += 1
    return Allowlist(frozenset(numbers), invalid)
