"""The message as a recipient will read it (plan 05, P2): a template's text
with its tokens filled in, how long it is, and how many SMS parts it takes.

Kavenegar holds the template itself; the dashboard keeps a copy of its text
(MessageTemplate) only to preview. Placeholders are %token, %token2,
%token3, %token10 and %token20, as in Kavenegar's panel.

Length, as the networks count it:
- text in the GSM 7-bit alphabet: 160 characters, or 153 per part when it
  takes more than one (^ { } \\ [ ] ~ | € count twice);
- anything else, Persian included (UCS-2): 70, or 67 per part. Characters
  outside the basic plane (most emoji) count twice.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from sms_sender.sender import TOKEN_MAX_SPACES

TOKENS = tuple(TOKEN_MAX_SPACES)  # token, token2, token3, token10, token20
# Longest first: "%token2" must not be read as "%token" followed by "2".
_PLACEHOLDER = re.compile(r"%(token(?:20|10|3|2)?)(?![0-9])")

_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = set("^{}\\[~]|€\f")


class Length(NamedTuple):
    chars: int     # what the network counts (7-bit units, or UTF-16 units)
    parts: int     # SMS parts it's billed and sent as
    unicode: bool  # True: UCS-2 (Persian), 70 per SMS


def length(text: str) -> Length:
    if all(c in _GSM7 or c in _GSM7_EXT for c in text):
        units = sum(2 if c in _GSM7_EXT else 1 for c in text)
        single, per_part, unicode = 160, 153, False
    else:
        units = len(text.encode("utf-16-le")) // 2
        single, per_part, unicode = 70, 67, True
    parts = 1 if units <= single else -(-units // per_part)
    return Length(units, parts, unicode)


def placeholders(text: str) -> list[str]:
    """The tokens a template's text uses, in order, each once."""
    return list(dict.fromkeys(m.group(1) for m in _PLACEHOLDER.finditer(text)))


def fill(text: str, values: dict[str, str]) -> str:
    """The text with each placeholder that has a value replaced; the rest
    stay as written (%token2), so a missing value shows."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


class Check(NamedTuple):
    missing: tuple[str, ...]  # the text uses them, nothing fills them
    unused: tuple[str, ...]   # filled, but the text doesn't use them


def check(text: str, filled: set[str]) -> Check:
    used = placeholders(text)
    return Check(
        missing=tuple(t for t in used if t not in filled),
        unused=tuple(t for t in TOKENS if t in filled and t not in used),
    )
