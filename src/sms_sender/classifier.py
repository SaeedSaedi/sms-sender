"""Map Kavenegar status codes to actions.

Reference: https://kavenegar.com/rest.html — General Status Codes table
(checked 2026-10-03).
- HALT: account/auth/quota issues that won't resolve mid-run; abort.
- RETRY: Kavenegar refused the call for now ("try later"); retry with backoff.
  Nothing was sent, so a retry can't double-send.
- PERMANENT: per-recipient or request issue that won't be fixed by retrying.
- SUCCESS: 200.
"""
from __future__ import annotations

import enum


class Action(enum.Enum):
    SUCCESS = "success"
    RETRY = "retry"
    PERMANENT = "permanent"
    HALT = "halt"


# Account-/auth-/quota-level — abort the whole run.
HALT_CODES: frozenset[int] = frozenset({
    401,  # account disabled
    403,  # invalid api key
    407,  # no access to this method (e.g. IP not on the allowlist)
    410,  # ip not allowed (not in the current table; halt to be safe)
    416,  # source ip doesn't match the key's settings
    418,  # insufficient credit
    420,  # links in the message text are restricted for this account
    426,  # method needs the advanced service
    427,  # sender line needs an access level
    429,  # ip restricted
    501,  # account may only send test SMS to the owner's number
})

# Transient — retry with backoff.
RETRY_CODES: frozenset[int] = frozenset({
    409,  # server can't respond right now, retry later
    451,  # too many calls in a time window (ip rate limit)
})

# Per-recipient or request — don't retry, mark permanent.
PERMANENT_CODES: frozenset[int] = frozenset({
    400,  # parameters incomplete
    402,  # operation failed
    404,  # unknown method
    405,  # http method mismatch
    406,  # required params empty
    411,  # invalid receptor
    412,  # invalid sender
    413,  # message empty / over limit
    414,  # too many records in one request
    415,  # start index too large
    417,  # invalid date
    419,  # sendarray: array lengths don't match
    422,  # invalid characters
    424,  # template not found / not approved
    428,  # voice impossible (non-numeric token)
    431,  # invalid token structure (space, underscore, newline)
    432,  # code missing from template
    607,  # invalid tag
})


def classify(code: int | None) -> Action:
    """Pick the action for a Kavenegar status code.

    Unknown codes default to PERMANENT — don't burn credit looping on something
    we don't understand. If a real outage uses a new code, the user will see
    it in the failure log and we can extend the table.
    """
    if code == 200:
        return Action.SUCCESS
    if code is None:
        return Action.RETRY  # Kavenegar error with no parseable code
    if code in HALT_CODES:
        return Action.HALT
    if code in RETRY_CODES:
        return Action.RETRY
    if code in PERMANENT_CODES:
        return Action.PERMANENT
    return Action.PERMANENT
