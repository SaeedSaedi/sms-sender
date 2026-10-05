"""Parse `.txt` / `.csv` input into normalized recipient records.

Both formats expect one phone number per row. CSV rows may carry extra
columns; the first non-empty cell is taken as the phone. Lines starting
with `#` are treated as comments. A first row with no digits at all is a
column header (Excel exports start with e.g. `Phone Number`) and is
skipped. Invalid numbers are returned in a separate list so the caller
can record them as permanent failures.

With a `TokenColumns` spec or a user-ID column the file is read as a CSV
with a header row instead: the first column is the phone, and the mapped
columns become per-recipient token values and the recipient's user ID. A
row whose values can't be sent as-is (empty, too many spaces, or missing
from a value map) is invalid too.

User IDs (decided 2026-10-04): a blank one is fine — the SMS still goes out
and the recipient is reported as "missing user ID". A phone that appears
with two different user IDs can't be attributed to anyone, so every one of
its rows is invalid and the phone is not sent (`LoadResult.conflicts`).
"""
from __future__ import annotations

import csv
import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .phone import InvalidPhoneError, normalize
from .sender import token_problem

# Segment and campaign names: lowercase letters, digits and dashes. They end
# up in Shlink tags and UTM values.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify(text: str) -> str:
    """'Seg 2 (VIP).csv' → 'seg-2-vip-csv'; '' when nothing is left."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")


def segment_from_path(path: str | Path) -> str:
    """Default segment name: the input file's name without its extension."""
    return slugify(Path(path).stem) or "segment"


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
    user_id: str | None = None  # only with a user-ID column; None = missing


@dataclass(frozen=True)
class InvalidRow:
    raw: str
    reason: str      # in words, for the CLI and the state DB
    line_no: int
    # The reason as a key, for the dashboard: invalid_phone,
    # conflicting_user_ids, empty_value, unmapped_value or token_rule.
    key: str = "invalid_phone"


@dataclass(frozen=True)
class LoadResult:
    valid: list[LoadedRow]
    invalid: list[InvalidRow]
    duplicates_collapsed: int
    header: str | None = None  # first row, when it was skipped as a column header
    # Phones that came with two or more different user IDs: never sent.
    conflicts: frozenset[str] = frozenset()
    # Valid rows whose user-ID cell was blank (only with a user-ID column).
    missing_user_id: int = 0
    # With several lists (`load_parts`): each valid phone's segment.
    segments: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InputPart:
    """One list of a send that reads several, in order (the dashboard's
    segments): its file, its segment name and its user-ID column."""
    path: Path
    segment: str
    user_id_column: str | None = None


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


def load(
    path: str | Path, token_columns: TokenColumns | None = None,
    user_id_column: str | None = None,
) -> LoadResult:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if token_columns is not None or user_id_column is not None:
        return _load_header_csv(p, token_columns, user_id_column)

    suffix = p.suffix.lower()
    valid: list[LoadedRow] = []
    invalid: list[InvalidRow] = []
    seen: set[str] = set()
    duplicates = 0
    header: str | None = None
    first_row = True

    for line_no, line in _iter_text_lines(p):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        raw = _extract_first_cell(stripped) if suffix == ".csv" else stripped
        if not raw:
            continue
        if first_row:
            first_row = False
            # str.isdigit() also matches Persian digits, so "۰۹۱۲…" is data.
            if not any(ch.isdigit() for ch in raw):
                header = raw
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

    return LoadResult(
        valid=valid, invalid=invalid, duplicates_collapsed=duplicates, header=header,
    )


@dataclass(frozen=True)
class _CsvRow:
    line_no: int
    raw: str
    phone: str | None             # None: not a valid phone
    problem: str | None           # why the row can't be sent as-is
    tokens: dict[str, str]
    user_id: str                  # "" when blank or unmapped
    problem_key: str = "invalid_phone"


def _load_header_csv(
    p: Path, spec: TokenColumns | None, user_id_column: str | None,
) -> LoadResult:
    rows: list[_CsvRow] = []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        # A plain reader and the few columns needed: a dict per row (DictReader)
        # costs more than everything else here, on a 100,000-row list.
        reader = csv.reader(f)
        # Excel exports sometimes pad header cells; match on the trimmed names.
        header = [h.strip() for h in next(reader, [])]
        needed = list(spec.columns.values()) if spec else []
        if user_id_column is not None:
            needed.append(user_id_column)
        missing = [c for c in needed if c not in header]
        if missing:
            raise InputError(
                f"{p.name}: column(s) {', '.join(missing)} not in the header "
                f"({', '.join(header) or 'empty file'})"
            )
        phone_column = header[0]
        if user_id_column == phone_column:
            raise InputError(
                f"{p.name}: {user_id_column!r} is the phone column (the first one), "
                "not a user-ID column"
            )
        # Each name's column (its last one, as a dict of the row would have it);
        # a short row has nothing in the columns it lacks.
        where = {name: i for i, name in enumerate(header)}
        phone_at = where[phone_column]
        id_at = where[user_id_column] if user_id_column else None
        token_at = {column: where[column] for column in (spec.columns.values() if spec else ())}

        def cell(cells: list[str], i: int) -> str | None:
            return cells[i] if i < len(cells) else None

        for cells in reader:
            if not cells:
                continue  # a blank line
            raw = (cell(cells, phone_at) or "").strip()
            if not raw or raw.startswith("#"):
                continue
            user_id = (cell(cells, id_at) or "").strip() if id_at is not None else ""
            try:
                canonical = normalize(raw)
            except InvalidPhoneError as e:
                rows.append(_CsvRow(reader.line_num, raw, None, str(e), {}, user_id))
                continue
            values = {column: cell(cells, i) for column, i in token_at.items()}
            tokens, reason, key = _row_tokens(values, spec) if spec else ({}, None, "")
            rows.append(_CsvRow(reader.line_num, raw, canonical, reason, tokens, user_id, key))

    # A phone's user ID is whichever non-blank one its rows carry; two
    # different ones are a conflict.
    user_ids: dict[str, set[str]] = {}
    for r in rows:
        if r.phone and r.user_id:
            user_ids.setdefault(r.phone, set()).add(r.user_id)
    conflicts = frozenset(phone for phone, ids in user_ids.items() if len(ids) > 1)

    valid: list[LoadedRow] = []
    invalid: list[InvalidRow] = []
    seen: set[str] = set()
    duplicates = 0
    for r in rows:  # file order, so invalid rows keep their order too
        if r.phone is None:
            invalid.append(InvalidRow(raw=r.raw, reason=r.problem or "invalid", line_no=r.line_no))
            continue
        if r.phone in conflicts:
            ids = ", ".join(sorted(user_ids[r.phone]))
            invalid.append(InvalidRow(
                raw=r.raw, reason=f"conflicting user IDs ({ids}); not sent", line_no=r.line_no,
                key="conflicting_user_ids",
            ))
            continue
        if r.phone in seen:
            duplicates += 1
            continue
        if r.problem is not None:
            invalid.append(InvalidRow(raw=r.raw, reason=r.problem, line_no=r.line_no, key=r.problem_key))
            continue
        seen.add(r.phone)
        (user_id,) = user_ids.get(r.phone) or {None}
        valid.append(LoadedRow(phone=r.phone, raw=r.raw, tokens=r.tokens, user_id=user_id))

    missing_ids = sum(1 for r in valid if r.user_id is None) if user_id_column else 0
    return LoadResult(
        valid=valid, invalid=invalid, duplicates_collapsed=duplicates,
        conflicts=conflicts, missing_user_id=missing_ids,
    )


def load_parts(parts: list[InputPart], token_columns: TokenColumns | None = None) -> LoadResult:
    """Several lists as one send, each read as `load` reads it. A phone in
    more than one keeps its first list's row (its tokens, and its segment
    in `segments`); the later rows count as duplicates. As within one file,
    a phone with two different user IDs across the lists isn't sent: every
    one of its rows is invalid. One part gives what `load` gives."""
    results = [load(part.path, token_columns, part.user_id_column) for part in parts]
    if len(parts) == 1:
        (result,) = results
        return dataclasses.replace(result, segments={r.phone: parts[0].segment for r in result.valid})

    user_ids: dict[str, set[str]] = {}
    expects_id: set[str] = set()  # in a list that has a user-ID column
    for part, result in zip(parts, results):
        for r in result.valid:
            if r.user_id:
                user_ids.setdefault(r.phone, set()).add(r.user_id)
            if part.user_id_column is not None:
                expects_id.add(r.phone)
    # Two IDs within one list already made a phone a conflict there; two
    # across the lists make it one too, and it's never sent from any list.
    conflicts = set().union(*(result.conflicts for result in results))
    conflicts |= {phone for phone, ids in user_ids.items() if len(ids) > 1}

    valid: list[LoadedRow] = []
    invalid: list[InvalidRow] = []
    segments: dict[str, str] = {}
    duplicates = 0
    for part, result in zip(parts, results):
        invalid.extend(result.invalid)
        duplicates += result.duplicates_collapsed
        for r in result.valid:
            if r.phone in conflicts:
                ids = sorted(user_ids.get(r.phone, ()))
                invalid.append(InvalidRow(
                    raw=r.raw, line_no=0, key="conflicting_user_ids",
                    reason=f"conflicting user IDs ({', '.join(ids)}); not sent" if len(ids) > 1
                    else "conflicting user IDs; not sent",
                ))
                continue
            if r.phone in segments:
                duplicates += 1
                continue
            segments[r.phone] = part.segment
            # Its user ID is whichever one its lists carry (at most one here).
            (user_id,) = user_ids.get(r.phone) or {None}
            valid.append(dataclasses.replace(r, user_id=user_id))
    return LoadResult(
        valid=valid, invalid=invalid, duplicates_collapsed=duplicates, conflicts=frozenset(conflicts),
        missing_user_id=sum(1 for r in valid if r.user_id is None and r.phone in expects_id),
        segments=segments,
    )


def _row_tokens(
    row: dict[str, str | None], spec: TokenColumns,
) -> tuple[dict[str, str], str | None, str]:
    """Return (tokens, None, "") for a sendable row, or ({}, reason, key) if
    it isn't."""
    tokens: dict[str, str] = {}
    for name, column in spec.columns.items():
        value = (row.get(column) or "").strip()
        if not value:
            return {}, f"{column} is empty (needed for {name})", "empty_value"
        mapping = spec.value_maps.get(column)
        if mapping is not None:
            if value not in mapping:
                return {}, f"{column}={value!r} has no entry in its value map", "unmapped_value"
            value = mapping[value]
        problem = token_problem(name, value)
        if problem:
            return {}, f"{column}: {problem}", "token_rule"
        tokens[name] = value
    return tokens, None, ""
