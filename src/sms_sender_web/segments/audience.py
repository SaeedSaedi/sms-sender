"""A segment made from a report (plan 05, P3): the recipients a filter
matches (clicked, didn't click, delivered but didn't click, not delivered,
rejected, or any other combination), ready for another campaign.

Numbers and user IDs come from the campaign DB. Token columns come back
from the segment file each recipient came from, while it still exists:
the columns every source has in common, so no row is left with an empty
token cell (the engine would refuse that row). The file is written in the
CLI's input format, like an upload's prepared copy."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sms_sender.phone import InvalidPhoneError, normalize
from sms_sender.state import REMOVED, RecipientFilter, StateStore

from ..suppression.service import phones_for
from . import files
from .models import Segment

USER_ID = "user_id"


@dataclass(frozen=True)
class Audience:
    segment: Segment
    rows: int
    columns: list[str]   # the token columns that came back
    missing_sources: list[str]  # source segments whose file is gone


def _source_rows(segment: Segment) -> tuple[dict[str, dict[str, str]], list[str]] | None:
    """phone → the row's token columns, and their names; None when the file is gone."""
    if segment.status != Segment.Status.READY or not segment.path.exists():
        return None
    with segment.path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns = [c for c in (segment.token_columns or []) if c in (reader.fieldnames or [])]
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            try:
                phone = normalize(row.get("phone") or "")
            except InvalidPhoneError:
                continue
            rows.setdefault(phone, {c: row.get(c) or "" for c in columns})
    return rows, columns


def make_audience(store: StateStore, where: RecipientFilter, *, slug: str, name: str, user) -> Audience:
    # Invalid input rows have no number; a campaign whose numbers were
    # removed (plan 06, D2) has placeholders, never a list to send to.
    picked = [r for r in store.iter_recipients(where) if not r["phone"].startswith(("INVALID:", REMOVED))]
    sources: dict[str, tuple[dict, list[str]] | None] = {}
    for slug_ in sorted({r["segment"] for r in picked if r["segment"]}):
        found = Segment.objects.filter(slug=slug_).first()
        sources[slug_] = _source_rows(found) if found else None
    missing = sorted(s for s, rows in sources.items() if rows is None)
    have = [rows for rows in sources.values() if rows is not None]
    columns = [] if missing or not have else [c for c in have[0][1] if all(c in cols for _, cols in have)]
    with_ids = any(r["user_id"] for r in picked)
    header = ["phone"] + ([USER_ID] if with_ids else []) + columns

    segment = Segment(slug=slug, name=name, original_name=f"{store.get_meta('campaign') or ''} report",
                      status=Segment.Status.READY, has_header=True, uploaded_by=user)
    path = segment.path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for r in picked:
            values = (sources.get(r["segment"]) or ({}, []))[0].get(r["phone"], {})
            writer.writerow([r["phone"]] + ([r["user_id"] or ""] if with_ids else []) + [values.get(c, "") for c in columns])
    tmp.replace(path)
    segment.columns = header
    segment.user_id_column = USER_ID if with_ids else ""
    segment.token_columns = columns
    segment.summary = files.summarize(path, segment.user_id_column, phones_for())
    segment.save()
    return Audience(segment, len(picked), columns, missing)


def suggest_slug(base: str) -> str:
    """The first free `<base>-audience`, `-audience-2`, … (64 characters at most)."""
    stem = f"{base[:52]}-audience"
    candidate, n = stem, 1
    while Segment.objects.filter(slug=candidate).exists() or Path(Segment.folder() / f"{candidate}.csv").exists():
        n += 1
        candidate = f"{stem}-{n}"
    return candidate
