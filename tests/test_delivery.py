import time

from sms_sender.delivery import WINDOW_SEC, describe, sync_delivery
from sms_sender.state import StateStore


class FakeKavenegar:
    """Stands in for Sender.delivery_statuses (Kavenegar sms/status)."""

    def __init__(self, answer: dict[int, int]):
        self.answer = answer  # message_id -> delivery status
        self.calls: list[list[int]] = []

    def delivery_statuses(self, message_ids):
        self.calls.append(list(message_ids))
        return {mid: self.answer[mid] for mid in message_ids if mid in self.answer}


def sent(state: StateStore, n: int) -> None:
    """n sent rows with message ids 1..n."""
    phones = [f"0912{i:07d}" for i in range(n)]
    state.upsert_pending([(p, p) for p in phones])
    for message_id, phone in enumerate(phones, start=1):
        state.claim(phone)
        state.mark_sent(phone, message_id=message_id, status_code=200)


def test_sync_asks_in_batches_of_500_and_stores_the_answers(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sent(state, 1001)
    kavenegar = FakeKavenegar({mid: 10 for mid in range(1, 1002)})
    result = sync_delivery(state, kavenegar)
    assert [len(c) for c in kavenegar.calls] == [500, 500, 1]
    assert (result.checked, result.updated) == (1001, 1001)
    assert state.delivery_counts() == {10: 1001}


def test_final_statuses_are_not_asked_again_but_the_rest_are(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sent(state, 3)
    # delivered (final) · at the operator · undelivered (can still turn into delivered)
    sync_delivery(state, FakeKavenegar({1: 10, 2: 4, 3: 11}))
    again = FakeKavenegar({2: 10, 3: 10})
    sync_delivery(state, again)
    assert [sorted(c) for c in again.calls] == [[2, 3]]
    assert state.delivery_counts() == {10: 3}


def test_sms_older_than_48_hours_are_not_asked(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sent(state, 1)
    kavenegar = FakeKavenegar({1: 10})
    two_days_later = time.time() + WINDOW_SEC + 60
    assert sync_delivery(state, kavenegar, now=lambda: two_days_later).checked == 0
    assert kavenegar.calls == []


def test_describe_names_kavenegar_statuses():
    assert describe({10: 5, None: 2, 11: 1}) == "delivered 5 · not checked 2 · undelivered 1"
    assert describe({}) == ""
