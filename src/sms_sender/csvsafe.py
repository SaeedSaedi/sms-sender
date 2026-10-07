"""CSV cells safe to open in a spreadsheet.

Excel and LibreOffice run a cell that starts with `=`, `+`, `-` or `@` (or a
tab or a carriage return) as a formula, and a formula can reach the network
or run commands. Our downloads carry text people put in files: raw cells of
a list, user IDs, names. Such a cell gets a leading apostrophe. A plain
number (a +98 phone, a negative amount) stays as it is, so the files still
read as data. Not for the files the engine itself reads (a segment's
prepared copy): those stay exactly as given.
"""
from __future__ import annotations

import re
from typing import Iterable

_STARTS = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)?")


def cell(value):
    if isinstance(value, str) and value.startswith(_STARTS) and not _NUMBER.fullmatch(value):
        return "'" + value
    return value


def row(values: Iterable) -> list:
    return [cell(v) for v in values]
