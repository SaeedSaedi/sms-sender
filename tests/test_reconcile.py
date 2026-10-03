import time

import pytest

from sms_sender.reconcile import WINDOW_AFTER_SEC, WINDOW_BEFORE_SEC, reconcile_unknown
from sms_sender.sender import HaltError, ProviderMessage, SendError
from sms_sender.state import FAILED_RETRIABLE, NEEDS_REVIEW, SENT, UNKNOWN, StateStore

PHONE = "09120000001"


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


def unknown_row(tmp_path) -> StateStore:
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(PHONE, PHONE)])
    state.claim(PHONE)
    state.mark_unknown(PHONE, "outcome unknown: read timed out")
    return state


def an_hour_later():
    t = time.time() + 3600
    return lambda: t


def test_one_unknown_message_at_kavenegar_means_it_was_sent(tmp_path):
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(555, 10)]), now=an_hour_later())
    assert (result.sent, result.requeued, result.needs_review, result.deferred) == (1, 0, 0, 0)
    assert state.counts() == {SENT: 1}
    assert state.known_message_ids() == {555}
    [check] = [a for a in state.attempts_for(PHONE) if a["kind"] == "reconcile"]
    assert (check["outcome"], check["message_id"]) == ("reconciled_sent", 555)


def test_no_message_at_kavenegar_means_safe_to_send_again(tmp_path):
    state = unknown_row(tmp_path)
    result = reconcile_unknown(state, FakeProvider([]), now=an_hour_later())
    assert result.requeued == 1
    assert state.counts() == {FAILED_RETRIABLE: 1}
    assert state.list_claimable_phones() == [PHONE]


def test_several_candidates_need_review(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider([ProviderMessage(1, 10), ProviderMessage(2, 10)])
    result = reconcile_unknown(state, provider, now=an_hour_later())
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
    result = reconcile_unknown(state, FakeProvider([ProviderMessage(777, 10)]), now=an_hour_later())
    assert result.requeued == 1
    assert state.counts() == {FAILED_RETRIABLE: 1}


def test_recent_rows_wait_without_a_lookup(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider([ProviderMessage(555, 10)])
    result = reconcile_unknown(state, provider, min_age_sec=300)  # the attempt is seconds old
    assert result.deferred == 1
    assert provider.lookups == []
    assert state.counts() == {UNKNOWN: 1}


def test_lookup_window_brackets_the_attempt(tmp_path):
    state = unknown_row(tmp_path)
    [(_, attempted_at)] = state.list_unknown()
    provider = FakeProvider([])
    reconcile_unknown(state, provider, now=an_hour_later())
    assert provider.lookups == [
        (PHONE, attempted_at - WINDOW_BEFORE_SEC, attempted_at + WINDOW_AFTER_SEC),
    ]


def test_failed_lookup_leaves_the_row_unknown(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider(error=SendError(None, "http: timeout"))
    result = reconcile_unknown(state, provider, now=an_hour_later())
    assert result.deferred == 1
    assert state.counts() == {UNKNOWN: 1}


def test_account_problem_propagates_and_leaves_the_row_unknown(tmp_path):
    state = unknown_row(tmp_path)
    provider = FakeProvider(error=HaltError(403, "invalid api key"))
    with pytest.raises(HaltError):
        reconcile_unknown(state, provider, now=an_hour_later())
    assert state.counts() == {UNKNOWN: 1}
