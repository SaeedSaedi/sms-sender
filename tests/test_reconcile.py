from datetime import datetime, timezone

import pytest

from sms_sender.reconcile import (
    TRUST_NOT_FOUND_SEC,
    WINDOW_AFTER_SEC,
    WINDOW_BEFORE_SEC,
    reconcile_unknown,
)
from sms_sender.sender import HaltError, ProviderMessage, SendError
from sms_sender.state import FAILED_RETRIABLE, NEEDS_REVIEW, SENT, UNKNOWN, StateStore
from sms_sender.window import TEHRAN

PHONE = "09120000001"
# 15:30 in Tehran, 12:00 UTC: far from midnight in both.
NOON = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc).timestamp()


class FakeProvider:
    """Stands in for Sender.find_messages (Kavenegar sms/statusbyreceptor)."""

    def __init__(self, messages=None, error=None):
        self.messages = messages or []
        self.error = error
        self.lookups = []

    def find_messages(self, phone, start, end):
        self.lookups.append((phone, start, end))
        if self.error:
            raise self.error
        return list(self.messages)


class PerDayProvider(FakeProvider):
    """Answers like the live API (checked 2026-10-04): the phone's messages
    on the Tehran date of the window's start, whatever its time of day."""

    def __init__(self, by_day):
        super().__init__()
        self.by_day = by_day  # date -> [ProviderMessage]

    def find_messages(self, phone, start, end):
        self.lookups.append((phone, start, end))
        return list(self.by_day.get(datetime.fromtimestamp(start, TEHRAN).date(), []))


def unknown_row(tmp_path, at: float = NOON, name: str = "s.db") -> StateStore:
    state = StateStore(tmp_path / name)
    state.upsert_pending([(PHONE, PHONE)])
    state.claim(PHONE)
    state.mark_unknown(PHONE, "outcome unknown: read timed out")
    with state._tx() as conn:
        conn.execute("UPDATE recipients SET last_attempt_at=? WHERE phone=?", (at, PHONE))
    return state


def later(seconds: float = 3600, at: float = NOON):
    return lambda: at + seconds


def test_one_unknown_message_at_kavenegar_means_it_was_sent(tmp_path):
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(555, 10)]), now=later())
    assert (result.sent, result.requeued, result.needs_review, result.deferred) == (1, 0, 0, 0)
    assert state.counts() == {SENT: 1}
    assert state.known_message_ids() == {555}
    [check] = [a for a in state.attempts_for(PHONE) if a["kind"] == "reconcile"]
    assert (check["outcome"], check["message_id"]) == ("reconciled_sent", 555)


def test_nothing_that_day_means_it_was_never_sent(tmp_path):
    """Kavenegar lists a phone's messages for the whole day, a fresh one
    within a minute (checked live on 2026-10-04): not found on the day of a
    recent attempt means it never went out — safe to send again."""
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([]), now=later())
    assert (result.requeued, result.needs_review) == (1, 0)
    assert state.counts() == {FAILED_RETRIABLE: 1}
    assert state.list_claimable_phones() == [PHONE]


def test_not_found_is_not_trusted_after_a_day(tmp_path):
    """A 5-day-old message wasn't listed: for an old attempt, "not found"
    proves nothing, so an operator decides."""
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([]), now=later(TRUST_NOT_FOUND_SEC + 60))
    assert (result.requeued, result.needs_review) == (0, 1)
    assert state.counts() == {NEEDS_REVIEW: 1}
    assert state.list_claimable_phones() == []


def test_review_instead_of_requeue_on_request(tmp_path):
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([]), requeue_not_found=False, now=later())
    assert result.needs_review == 1
    assert state.counts() == {NEEDS_REVIEW: 1}


def test_several_candidates_need_review(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider([ProviderMessage(1, 10), ProviderMessage(2, 10)])
    result = reconcile_unknown(state, provider, now=later())
    assert result.needs_review == 1
    assert state.counts() == {NEEDS_REVIEW: 1}
    assert state.list_claimable_phones() == []


def test_messages_already_accounted_for_are_not_ours(tmp_path):
    """The approval test went to this same phone (a recorded call with its
    message ID): that message must not settle the unknown row as sent."""
    state = unknown_row(tmp_path)
    state.record_attempt(
        phone=PHONE, kind="send", outcome="accepted", started_at=1.0, message_id=777,
    )
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(777, 10)]), now=later())
    assert result.requeued == 1
    assert state.counts() == {FAILED_RETRIABLE: 1}


def test_another_campaigns_message_that_day_is_not_ours(tmp_path):
    """The lookup returns the whole day: another campaign's SMS to the same
    person, recorded in its own DB next to this one, must not count."""
    other = StateStore(tmp_path / "other-campaign.db")
    other.upsert_pending([(PHONE, PHONE)])
    other.claim(PHONE)
    other.mark_sent(PHONE, message_id=888, status_code=200)
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(888, 10)]), now=later())
    assert (result.sent, result.requeued) == (0, 1)
    assert state.counts() == {FAILED_RETRIABLE: 1}


def test_an_unreadable_neighbour_db_is_skipped(tmp_path):
    (tmp_path / "broken.db").write_bytes(b"not a database")
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(9, 10)]), now=later())
    assert result.sent == 1


def test_recent_rows_wait_without_a_lookup(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider([ProviderMessage(555, 10)])
    result = reconcile_unknown(state, provider, min_age_sec=300, now=later(10))
    assert result.deferred == 1
    assert provider.lookups == []
    assert state.counts() == {UNKNOWN: 1}


def test_one_lookup_when_the_window_stays_inside_a_day(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider([])
    reconcile_unknown(state, provider, now=later())
    assert provider.lookups == [(PHONE, NOON - WINDOW_BEFORE_SEC, NOON + WINDOW_AFTER_SEC)]


def test_a_window_across_midnight_asks_about_both_days(tmp_path):
    """23:58 in Tehran: the message may be filed under the next day."""
    attempt = datetime(2026, 10, 4, 23, 58, tzinfo=TEHRAN).timestamp()
    state = unknown_row(tmp_path, at=attempt)
    next_day = datetime(2026, 10, 5, tzinfo=TEHRAN).date()
    provider = PerDayProvider({next_day: [ProviderMessage(4242, 10)]})
    result = reconcile_unknown(state, provider, now=later(at=attempt))
    assert result.sent == 1
    assert state.counts() == {SENT: 1}
    days = {datetime.fromtimestamp(start, TEHRAN).date() for _, start, _ in provider.lookups}
    assert days == {datetime(2026, 10, 4).date(), next_day}


def test_failed_lookup_leaves_the_row_unknown(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider(error=SendError(None, "http: timeout"))
    result = reconcile_unknown(state, provider, now=later())
    assert result.deferred == 1
    assert state.counts() == {UNKNOWN: 1}


def test_account_problem_propagates_and_leaves_the_row_unknown(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider(error=HaltError(403, "invalid api key"))
    with pytest.raises(HaltError):
        reconcile_unknown(state, provider, now=later())
    assert state.counts() == {UNKNOWN: 1}
