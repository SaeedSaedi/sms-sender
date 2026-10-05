"""Which recipients a list or an export shows (StateStore.RecipientFilter):
by the status a person reads, segment, delivery, clicks on their own link,
and a missing user ID. The dashboard's recipients tab and its exports use
it."""
from __future__ import annotations

from sms_sender.state import INVALID, PENDING, SENT, UNCHECKED, RecipientFilter

from .test_clicks import A, B, C, D, campaign_db


def phones(state, **kw) -> list[str]:
    return [r["phone"] for r in state.recipients_page(limit=100, where=RecipientFilter(**kw))]


def make(tmp_path):
    """A, B (vip-2) and C (new-users) sent their own links, D pending; A
    delivered and clicked twice, B undelivered; B has no user ID; one input
    row wasn't a number."""
    state = campaign_db(tmp_path / "s.db")
    state.record_delivery({B: 11}, checked_at=0)
    state.record_clicks({"c1": 2}, synced_at=0)
    state.record_invalid("not-a-number", "invalid_phone")
    return state


def test_each_filter_picks_its_rows(tmp_path):
    state = make(tmp_path)
    assert phones(state) == [A, B, C, D, "INVALID:not-a-number"]
    assert phones(state, status=SENT) == [A, B, C]
    assert phones(state, status=PENDING) == [D]
    assert phones(state, status=INVALID) == ["INVALID:not-a-number"]  # as the counts call it
    assert phones(state, segment="new-users") == [C, D]
    assert phones(state, delivery="delivered") == [A]
    assert phones(state, delivery="not_delivered") == [B]
    assert phones(state, delivery=UNCHECKED) == [C]  # accepted; delivery not asked yet
    assert phones(state, clicked=True) == [A]
    assert phones(state, clicked=False) == [B, C]  # D was never sent: its link didn't go out
    assert phones(state, missing_user_id=True) == [B]
    assert phones(state, segment="vip-2", clicked=False) == [B]  # filters combine


def test_counts_pages_and_exports_agree(tmp_path):
    state = make(tmp_path)
    where = RecipientFilter(status=SENT)
    assert state.recipient_total(where=where) == 3
    assert [r["phone"] for r in state.iter_recipients(where)] == [A, B, C]
    assert state.recipient_total(phone=A) == 1 and state.recipient_total() == 5
    assert state.recipients_page(limit=1, offset=1, where=where)[0]["phone"] == B
    assert state.display_counts()[INVALID] == state.recipient_total(where=RecipientFilter(status=INVALID))


def test_what_a_campaign_can_be_filtered_by(tmp_path):
    state = make(tmp_path)
    assert state.segments_in_use() == ["new-users", "vip-2"]
    assert state.has_user_ids() and state.has_personal_links()
