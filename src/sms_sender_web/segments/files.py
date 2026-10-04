"""Reading an uploaded recipient list, and writing the copy the engine reads.

Uploads often come from Excel. They may be UTF-8 (with or without a BOM),
UTF-16 or Windows-1256 (Persian Windows), and separated by commas,
semicolons or tabs. Each segment gets one prepared copy in the CLI's own
format, so `sms-sender` and the worker read it as they read any input:
- UTF-8, comma-separated;
- the phone first, under the header `phone`;
- then the user-ID column and the columns kept for tokens, under their own
  names.

Every input row is kept, invalid ones included, so the campaign DB records
them like the CLI does.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from sms_sender import input_loader
from sms_sender.phone import InvalidPhoneError, normalize

from ..privacy import mask_phone

MAX_BYTES = 50 * 1024 * 1024
PHONE_HEADER = "phone"
# How many invalid rows the segment page lists (masked).
INVALID_SAMPLE = 20
_EXCEL_MAGIC = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")  # .xlsx (zip), .xls (OLE)
# Windows-1256 has no Persian «ی»: Excel writes the Arabic «ي» (or «ى») in
# its place, which would reach the SMS. Only text read as cp1256 is changed.
_CP1256_YEH = str.maketrans({"\u064a": "\u06cc", "\u0649": "\u06cc"})


class UploadError(ValueError):
    """The file can't be used. `code` picks the Persian message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Table:
    rows: list[list[str]]  # every non-blank row, cells trimmed, padded to `width`
    width: int

    def first_row_is_header(self) -> bool:
        """A guess the mapping page lets the operator correct: the first row
        is a header unless one of its cells is a mobile number."""
        return not any(is_phone(cell) for cell in self.rows[0])

    def column_names(self, has_header: bool) -> list[str]:
        """Names for the prepared file: the header's, else column-1, column-2…"""
        if has_header:
            return list(self.rows[0])
        return [f"column-{i}" for i in range(1, self.width + 1)]

    def data(self, has_header: bool) -> list[list[str]]:
        return self.rows[1:] if has_header else self.rows


def is_phone(text: str) -> bool:
    try:
        normalize(text)
    except InvalidPhoneError:
        return False
    return True


def decode(data: bytes) -> str:
    if data.startswith(_EXCEL_MAGIC):
        raise UploadError("excel")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Excel's plain "CSV" on Persian Windows.
            text = data.decode("cp1256", errors="replace").translate(_CP1256_YEH)
    if "\x00" in text:
        raise UploadError("binary")
    return text


def parse(data: bytes) -> Table:
    if len(data) > MAX_BYTES:
        raise UploadError("too_big")
    text = decode(data)
    try:
        delimiter = csv.Sniffer().sniff(text[:64 * 1024], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise UploadError("empty")
    width = max(len(row) for row in rows)
    return Table(rows=[row + [""] * (width - len(row)) for row in rows], width=width)


@dataclass(frozen=True)
class Mapping:
    has_header: bool
    phone: int                 # column indexes in the upload
    user_id: int | None
    tokens: tuple[int, ...]


class MappingError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_mapping(table: Table, mapping: Mapping) -> list[str]:
    """The prepared file's header (phone first), or MappingError."""
    names = table.column_names(mapping.has_header)
    indexes = [mapping.phone] + ([mapping.user_id] if mapping.user_id is not None else [])
    indexes += list(mapping.tokens)
    if any(not 0 <= i < table.width for i in indexes):
        raise MappingError("unknown_column")
    if len(set(indexes)) != len(indexes):
        raise MappingError("column_twice")
    others = [names[i] for i in indexes[1:]]
    if any(not name for name in others):
        raise MappingError("unnamed_column")
    if len({name.casefold() for name in others} | {PHONE_HEADER}) != len(others) + 1:
        raise MappingError("duplicate_name")  # two alike, or one called "phone"
    if mapping.has_header and len(table.rows) < 2:
        raise MappingError("no_rows")
    return [PHONE_HEADER] + others


def write_prepared(table: Table, mapping: Mapping, dest: Path) -> list[str]:
    """Write the CLI-format copy to `dest` (atomically) and return its header."""
    header = check_mapping(table, mapping)
    indexes = [mapping.phone] + ([mapping.user_id] if mapping.user_id is not None else [])
    indexes += list(mapping.tokens)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in table.data(mapping.has_header):
            if row[mapping.phone]:
                writer.writerow([row[i] for i in indexes])
    tmp.replace(dest)
    return header


def summarize(path: Path, user_id_column: str | None, suppressed: frozenset[str]) -> dict:
    """What the engine will make of the prepared file, without sending:
    counts, and a few invalid rows, masked."""
    loaded = input_loader.load(path, None, user_id_column or None)
    phones = {r.phone for r in loaded.valid}
    sample = [
        {
            "value": mask_phone(row.raw),
            "reason": row.key,
        }
        for row in loaded.invalid[:INVALID_SAMPLE]
    ]
    return {
        "rows": len(loaded.valid) + len(loaded.invalid) + loaded.duplicates_collapsed,
        "valid": len(loaded.valid),
        "invalid": len(loaded.invalid),
        "duplicates": loaded.duplicates_collapsed,
        "conflicts": len(loaded.conflicts),
        "missing_user_id": loaded.missing_user_id,
        "suppressed": len(phones & suppressed),
        "invalid_sample": sample,
    }


def preview(table: Table, has_header: bool, rows: int = 3) -> list[list[tuple[str, bool]]]:
    """The first rows for the mapping page as (text, is_phone) cells, every
    mobile number masked. Other values are shown exactly as they are."""
    return [
        [(mask_phone(cell), True) if is_phone(cell) else (cell, False) for cell in row]
        for row in table.data(has_header)[:rows]
    ]
