"""Parse `.txt` / `.csv` input into normalized recipient records.

Both formats expect one phone number per row. CSV rows may carry extra
columns; the first non-empty cell is taken as the phone. Lines starting
with `#` are treated as comments. Invalid numbers are returned in a
separate list so the caller can record them as permanent failures.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .phone import InvalidPhoneError, normalize


@dataclass(frozen=True)
class LoadedRow:
    phone: str       # canonical
    raw: str         # as it appeared in the input


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


def load(path: str | Path) -> LoadResult:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)

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
