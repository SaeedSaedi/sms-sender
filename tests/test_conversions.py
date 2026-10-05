"""Conversions from the business's own records, matched to the recipients
who were sent the SMS: by the link's r first, then by user ID. A file
imported twice adds nothing twice; nothing in it needs a phone number."""
from __future__ import annotations

import pytest

from sms_sender.conversions import ConversionFileError, import_conversions, read_rows

from .test_clicks import A, B, C, D, campaign_db


def refs(state) -> dict[str, str]:
    """phone → its link's r."""
    return {r["phone"]: r["ref"] for r in state._conn().execute(
        "SELECT r.phone, l.ref FROM recipients r JOIN links l ON l.key = r.link_key")}


def test_conversions_match_by_r_then_by_user_id(tmp_path):
    state = campaign_db(tmp_path / "s.db")  # A, B, C sent with their own links; D pending
    r = refs(state)
    header = ["R", "User ID", "Amount", "Date"]
    rows = [
        [r[A], "", "۱۲۰,۰۰۰", "2026-10-05T10:00:00Z"],   # by r, Persian digits
        ["", "u-3", "5000", ""],                           # C, by user ID
        ["nope", "u-4", "", ""],                           # D wasn't sent: no match
        ["", "", "1", ""],                                 # nothing to match on
        [r[B], "", "abc", ""],                             # not a value
    ]
    conversions, invalid = read_rows(header, rows)
    result = import_conversions(state, conversions, batch="sales.csv", invalid=invalid)
    assert (result.rows, result.by_ref, result.by_user_id, result.unmatched, result.invalid) == (5, 1, 1, 1, 2)
    assert state.conversion_totals() == (2, 2, 125000.0)
    # The same file again: nothing is counted twice.
    again = import_conversions(state, conversions, batch="sales.csv")
    assert again.duplicates == 2 and again.added == 0
    assert state.conversion_totals() == (2, 2, 125000.0)


def test_a_file_needs_an_r_or_a_user_id_column():
    with pytest.raises(ConversionFileError):
        read_rows(["phone", "amount"], [["09120000001", "10"]])
    assert read_rows(["ref"], [[" abc "]])[0][0].ref == "abc"


def test_nothing_matches_someone_who_wasnt_sent(tmp_path):
    state = campaign_db(tmp_path / "s.db")
    assert state.phones_for_user_ids({"u-4"}) == {}  # D is pending
    assert set(state.phones_for_refs(refs(state).values())) == {refs(state)[p] for p in (A, B, C)}
