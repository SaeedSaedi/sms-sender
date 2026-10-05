"""Conversions from the business's own records (plan 05, decision 3: by
CSV first, GA4 later), matched to the recipients who were sent the SMS.

A row names the link's `r` (each recipient's own, random reference), or
the user ID from the import, or both; `r` is tried first. It may carry a
value (an amount) and a moment (ISO 8601, or unix seconds). Nothing
personal is needed: the file never has to hold a phone number."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .state import StateStore

# Header names, case and spaces aside.
REF_NAMES = {"r", "ref", "reference"}
USER_ID_NAMES = {"user_id", "userid", "user", "user id"}
VALUE_NAMES = {"value", "amount", "revenue"}
WHEN_NAMES = {"converted_at", "date", "time", "at", "timestamp"}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


@dataclass(frozen=True)
class Conversion:
    ref: str | None
    user_id: str | None
    value: float | None
    converted_at: float | None


@dataclass(frozen=True)
class ImportResult:
    rows: int
    by_ref: int
    by_user_id: int
    unmatched: int      # neither the r nor the user ID belongs to anyone sent
    duplicates: int     # already imported
    invalid: int        # no r or user ID, or a value / moment that isn't one

    @property
    def added(self) -> int:
        return self.by_ref + self.by_user_id - self.duplicates


class ConversionFileError(ValueError):
    """The file has neither an r column nor a user ID column."""


def _column(header: list[str], names: set[str]) -> int | None:
    for i, name in enumerate(header):
        if re.sub(r"\s+", " ", name.strip().lower()) in names:
            return i
    return None


def _number(text: str) -> float | None:
    text = text.strip().translate(_DIGITS).replace(",", "").replace("٬", "")
    return float(text) if text else None


def _moment(text: str) -> float | None:
    text = text.strip().translate(_DIGITS)
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def read_rows(header: list[str], rows: Iterable[list[str]]) -> tuple[list[Conversion], int]:
    """The file's conversions, and how many rows weren't usable."""
    ref_at, user_at = _column(header, REF_NAMES), _column(header, USER_ID_NAMES)
    if ref_at is None and user_at is None:
        raise ConversionFileError("no r or user ID column")
    value_at, when_at = _column(header, VALUE_NAMES), _column(header, WHEN_NAMES)

    def cell(row: list[str], at: int | None) -> str:
        return row[at].strip() if at is not None and at < len(row) else ""

    out, invalid = [], 0
    for row in rows:
        ref, user_id = cell(row, ref_at), cell(row, user_at)
        if not ref and not user_id:
            invalid += 1
            continue
        try:
            value, when = _number(cell(row, value_at)), _moment(cell(row, when_at))
        except ValueError:
            invalid += 1
            continue
        out.append(Conversion(ref or None, user_id or None, value, when))
    return out, invalid


def import_conversions(state: StateStore, conversions: list[Conversion], *, batch: str,
                       invalid: int = 0) -> ImportResult:
    by_ref = state.phones_for_refs({c.ref for c in conversions if c.ref})
    by_id = state.phones_for_user_ids({c.user_id for c in conversions if c.user_id})
    rows, refs, ids, unmatched = [], 0, 0, 0
    for c in conversions:
        if c.ref and c.ref in by_ref:
            rows.append((by_ref[c.ref], "ref", c.ref, c.user_id, c.value, c.converted_at))
            refs += 1
        elif c.user_id and c.user_id in by_id:
            rows.append((by_id[c.user_id], "user_id", c.ref, c.user_id, c.value, c.converted_at))
            ids += 1
        else:
            unmatched += 1
    _added, duplicates = state.add_conversions(rows, batch)
    return ImportResult(len(conversions) + invalid, refs, ids, unmatched, duplicates, invalid)
