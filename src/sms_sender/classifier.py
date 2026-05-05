"""Map Kavenegar status codes to actions.

Reference: https://kavenegar.com/rest.html — General Status Codes table.
- HALT: account/auth/quota issues that won't resolve mid-run; abort.
- RETRY: transient server-side issues; retry with backoff.
- PERMANENT: per-recipient issue that won't be fixed by retrying.
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
    401,  # invalid api key
    403,  # api key disabled
    407,  # access denied
    410,  # ip not allowed
    416,  # source ip mismatch
    418,  # insufficient credit
    426,  # plan upgrade required
})

# Transient — retry with backoff.
RETRY_CODES: frozenset[int] = frozenset({
    409,  # server unavailable
    414,  # request volume over limit
    419,  # daily/period limit (often resolves later)
})

# Per-recipient or template — don't retry, mark permanent.
PERMANENT_CODES: frozenset[int] = frozenset({
    400,  # parameters problem
    402,  # transaction failed
    404,  # method not found
    405,  # http method mismatch
    406,  # required params missing
    411,  # invalid receptor
    412,  # invalid sender
    413,  # message empty / over limit
    415,  # result index out of range
    417,  # invalid date
    422,  # invalid characters
    424,  # template not found / not approved
    428,  # voice impossible (non-numeric token)
    431,  # invalid code structure
    432,  # code missing from template
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
        return Action.RETRY  # network-level, no code parsed
    if code in HALT_CODES:
        return Action.HALT
    if code in RETRY_CODES:
        return Action.RETRY
    if code in PERMANENT_CODES:
        return Action.PERMANENT
    return Action.PERMANENT
