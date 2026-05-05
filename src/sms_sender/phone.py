"""Iranian mobile-number normalization and validation.

Canonical form is 11 digits starting with `09`. Anything that can be coerced
into that form is accepted; everything else raises `InvalidPhoneError`.
"""
from __future__ import annotations

import re

CANONICAL_RE = re.compile(r"^09\d{9}$")
_DIGITS_RE = re.compile(r"\D+")

# Persian/Arabic-Indic digits → ASCII (CSV exports from Excel often carry these)
_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)


class InvalidPhoneError(ValueError):
    """Raised when a phone string cannot be normalized to canonical form."""


def normalize(raw: str) -> str:
    """Return the canonical `09XXXXXXXXX` form of an Iranian mobile number.

    Accepts: `09xxxxxxxxx`, `9xxxxxxxxx`, `+98xxxxxxxxxx`, `0098xxxxxxxxxx`,
    `98xxxxxxxxxx`, with arbitrary spaces, dashes, parens, and Persian digits.
    """
    if raw is None:
        raise InvalidPhoneError("empty")
    s = raw.strip().translate(_DIGIT_TRANSLATION)
    if not s:
        raise InvalidPhoneError("empty")
    digits = _DIGITS_RE.sub("", s)
    if not digits:
        raise InvalidPhoneError(f"no digits in {raw!r}")

    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98") and len(digits) == 12:
        digits = digits[2:]

    if len(digits) == 10 and digits.startswith("9"):
        digits = "0" + digits

    if not CANONICAL_RE.match(digits):
        raise InvalidPhoneError(f"not a valid Iranian mobile: {raw!r}")
    return digits
