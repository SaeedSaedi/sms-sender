"""Parse `.txt` / `.csv` input into normalized recipient records.

Both formats expect one phone number per row. CSV rows may carry extra
columns; the first non-empty cell is taken as the phone. Lines starting
with `#` are treated as comments. Invalid numbers are returned in a
separate list so the caller can record them as permanent failures.

With a `TokenColumns` spec the file is read as a CSV with a header row
instead: the first column is the phone, and the mapped columns become
per-recipient token values. A row whose values can't be sent as-is
(empty, too many spaces, or missing from a value map) is invalid too.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .phone import InvalidPhoneError, normalize
from .sender import TOKEN_MAX_SPACES


class InputError(ValueError):
    """The input file can't be used as given (e.g. a mapped column is missing)."""


@dataclass(frozen=True)
class TokenColumns:
    """Which CSV column feeds which Kavenegar token, per recipient.

    `columns` maps token name → header column, e.g. {"token10": "first_name"}.
    `value_maps` translates a column's values before sending, e.g.
    {"trade_side": {"Buy": "خرید", "Sell": "فروش"}}. A value missing from its
    column's map makes the row invalid — the untranslated text is never sent.
    """
    columns: dict[str, str]
    value_maps: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedRow:
    phone: str       # canonical
    raw: str         # as it appeared in the input
    tokens: dict[str, str] = field(default_factory=dict)  # per-recipient; only with TokenColumns


@dataclass(frozen=True)
class InvalidRow:
    raw: str
    reason: str
    line_no: int


@dataclass(frozen=True)
class LoadResult:
    valid: list[LoadedRow]
    invalid: list[InvalidRow]
    duplicates_collapsed: int


def _iter_text_lines(path: Path) -> Iterable[tuple[int, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for i, line in enumerate(f, start=1):
            yield i, line


def _extract_first_cell(line: str) -> str:
    # csv.reader handles quoted cells, embedded commas, etc.
    reader = csv.reader([line])
    try:
        row = next(reader)
    except StopIteration:
        return ""
    for cell in row:
        cell = cell.strip()
        if cell:
            return cell
    return ""


def load(path: str | Path, token_columns: TokenColumns | None = None) -> LoadResult:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if token_columns is not None:
        return _load_with_token_columns(p, token_columns)

    suffix = p.suffix.lower()
    valid: list[LoadedRow] = []
    invalid: list[InvalidRow] = []
    seen: set[str] = set()
    duplicates = 0

    for line_no, line in _iter_text_lines(p):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw = _extract_first_cell(stripped) if suffix == ".csv" else stripped
        if not raw:
            continue
        try:
            canonical = normalize(raw)
        except InvalidPhoneError as e:
            invalid.append(InvalidRow(raw=raw, reason=str(e), line_no=line_no))
            continue
        if canonical in seen:
            duplicates += 1
            continue
        seen.add(canonical)
        valid.append(LoadedRow(phone=canonical, raw=raw))

    return LoadResult(valid=valid, invalid=invalid, duplicates_collapsed=duplicates)


def _load_with_token_columns(p: Path, spec: TokenColumns) -> LoadResult:
    valid: list[LoadedRow] = []
    invalid: list[InvalidRow] = []
    seen: set[str] = set()
    duplicates = 0

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # Excel exports sometimes pad header cells; match on the trimmed names.
        header = [h.strip() for h in reader.fieldnames or []]
        reader.fieldnames = header
        missing = [c for c in spec.columns.values() if c not in header]
        if missing:
            raise InputError(
                f"{p.name}: column(s) {', '.join(missing)} not in the header "
                f"({', '.join(header) or 'empty file'})"
            )
        phone_column = header[0]
        for row in reader:
            raw = (row.get(phone_column) or "").strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                canonical = normalize(raw)
            except InvalidPhoneError as e:
                invalid.append(InvalidRow(raw=raw, reason=str(e), line_no=reader.line_num))
                continue
            if canonical in seen:
                duplicates += 1
                continue
            tokens, reason = _row_tokens(row, spec)
            if reason is not None:
                invalid.append(InvalidRow(raw=raw, reason=reason, line_no=reader.line_num))
                continue
            seen.add(canonical)
            valid.append(LoadedRow(phone=canonical, raw=raw, tokens=tokens))

    return LoadResult(valid=valid, invalid=invalid, duplicates_collapsed=duplicates)


def _row_tokens(row: dict[str, str | None], spec: TokenColumns) -> tuple[dict[str, str], str | None]:
    """Return (tokens, None) for a sendable row, or ({}, reason) if it isn't."""
    tokens: dict[str, str] = {}
    for name, column in spec.columns.items():
        value = (row.get(column) or "").strip()
        if not value:
            return {}, f"{column} is empty (needed for {name})"
        mapping = spec.value_maps.get(column)
        if mapping is not None:
            if value not in mapping:
                return {}, f"{column}={value!r} has no entry in its value map"
            value = mapping[value]
        spaces = value.count(" ")
        if spaces > TOKEN_MAX_SPACES[name]:
            return {}, (
                f"{column} has {spaces} space(s); {name} allows at most "
                f"{TOKEN_MAX_SPACES[name]}"
            )
        tokens[name] = value
    return tokens, None
