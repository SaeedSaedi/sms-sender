"""End-to-end runner test using a fake Sender. Exercises the contract:
every input number ends in `sent` or a recorded failure, and re-runs
never re-call the API for already-sent numbers."""
from __future__ import annotations

import threading
from pathlib import Path

from sms_sender.runner import Runner
from sms_sender.sender import HaltError, PermanentSendError, SendError, SendResult
from sms_sender.state import SENT, StateStore


class FakeSender:
    def __init__(self):
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.behavior = {}  # phone -> callable returning SendResult or raising

    def send(self, phone: str) -> SendResult:
        with self._lock:
            self.calls.append(phone)
        if phone in self.behavior:
            return self.behavior[phone]()
        return SendResult(message_id=hash(phone) & 0xffff, status_code=200)


def write_input(tmp_path: Path, phones: list[str]) -> Path:
    p = tmp_path / "in.txt"
    p.write_text("\n".join(phones) + "\n", encoding="utf-8")
    return p


def test_happy_path_marks_all_sent(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=2).run()
    assert summary.sent == 3
    assert summary.failed_permanent == 0
    assert summary.failed_retriable == 0
    assert state.counts() == {SENT: 3}
    assert sorted(sender.calls) == ["09120000001", "09120000002", "09120000003"]


def test_resume_skips_already_sent(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    Runner(input_path=inp, state=state, sender=sender, workers=2).run()
    assert len(sender.calls) == 2

    # Second run with the same DB and same input — no API calls expected.
    sender2 = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender2, workers=2).run()
    assert sender2.calls == []
    assert summary.sent == 0


def test_permanent_failure_does_not_retry_on_resume(tmp_path):
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        PermanentSendError(424, "template missing")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.failed_permanent == 1

    # Resume — fixed sender wouldn't even be asked.
    sender2 = FakeSender()
    summary2 = Runner(input_path=inp, state=state, sender=sender2, workers=1).run()
    assert sender2.calls == []
    assert summary2.sent == 0


def test_retriable_failure_is_retried_on_resume(tmp_path):
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")

    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        SendError(409, "retries exhausted: server busy")
    )
    Runner(input_path=inp, state=state, sender=sender, workers=1).run()

    # Resume with healthy sender — phone is reclaimable.
    sender2 = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender2, workers=1).run()
    assert sender2.calls == ["09120000001"]
    assert summary.sent == 1


def test_invalid_inputs_recorded_without_api_call(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "not-a-phone", "0812345678"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.sent == 1
    assert summary.invalid == 2
    # Sender saw only the valid phone.
    assert sender.calls == ["09120000001"]


def test_halt_aborts_run(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        HaltError(418, "insufficient credit")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.halted is True
    # The run aborts; remaining rows stay claimable for a future run after credit is fixed.
    claimable = state.list_claimable_phones()
    assert "09120000002" in claimable or "09120000003" in claimable
