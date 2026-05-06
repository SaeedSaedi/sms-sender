"""End-to-end runner test using a fake Sender. Exercises the contract:
every input number ends in `sent` or a recorded failure, and re-runs
never re-call the API for already-sent numbers."""
from __future__ import annotations

import threading
from pathlib import Path

from sms_sender.runner import Runner, format_report
from sms_sender.sender import (
    AccountInfo,
    HaltError,
    PermanentSendError,
    SendError,
    SendResult,
)
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


# ---------- preflight + smoke test ----------


class PreflightFakeSender(FakeSender):
    """FakeSender that also exposes an `account_info` method."""

    def __init__(self, account_result):
        super().__init__()
        self._account_result = account_result
        self.account_calls = 0

    def account_info(self):
        self.account_calls += 1
        if isinstance(self._account_result, BaseException):
            raise self._account_result
        return self._account_result


def test_preflight_account_check_passes(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=1000, expire_date=None, type="X"))
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, preflight=True,
    ).run()
    assert sender.account_calls == 1
    assert summary.sent == 2


def test_preflight_account_halt_aborts_before_fanout(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(HaltError(401, "invalid api key"))
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, preflight=True,
    ).run()
    assert summary.halted is True
    assert summary.sent == 0
    # No `send` calls should have been made — preflight halted the run.
    assert sender.calls == []


def test_preflight_zero_credit_aborts(tmp_path):
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=0, expire_date=None, type="X"))
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, preflight=True,
    ).run()
    assert summary.halted is True
    assert summary.sent == 0
    assert sender.calls == []


def test_preflight_skipped_when_disabled(tmp_path):
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    # Account would halt — but preflight is off.
    sender = PreflightFakeSender(HaltError(401, "ignored"))
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, preflight=False,
    ).run()
    assert sender.account_calls == 0
    assert summary.sent == 1


def test_preflight_tolerates_sender_without_account_info(tmp_path):
    """Bare FakeSender (no account_info attribute) should soft-skip account check."""
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, preflight=True,
    ).run()
    assert summary.sent == 1


def test_smoke_test_aborts_on_template_error(tmp_path):
    """Template error on smoke send should stop the run before fan-out."""
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=999, expire_date=None, type="X"))
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        PermanentSendError(424, "template not found")
    )
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2,
        preflight=True, smoke_test=True,
    ).run()
    # Smoke test failed → run halted.
    assert summary.halted is True
    # Only the smoke phone got called; the rest are untouched.
    assert sender.calls == ["09120000001"]
    # The two un-smoked phones should still be claimable for a future run.
    claimable = state.list_claimable_phones()
    assert "09120000002" in claimable
    assert "09120000003" in claimable


def test_smoke_test_passes_then_runs_rest(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=999, expire_date=None, type="X"))
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2,
        preflight=True, smoke_test=True,
    ).run()
    assert summary.sent == 3
    assert summary.halted is False
    # All three were sent; smoke phone counted exactly once.
    assert sorted(sender.calls) == ["09120000001", "09120000002", "09120000003"]
    assert state.counts() == {SENT: 3}


# ---------- summary metrics + report ----------


def test_summary_records_elapsed_and_rate(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=2).run()
    assert summary.elapsed_sec >= 0.0
    # Either we can compute a rate (if elapsed > 0) or the run was instant.
    if summary.elapsed_sec > 0:
        assert summary.sends_per_sec > 0
    assert summary.top_errors == ()  # no failures


def test_summary_top_errors_groups_by_message(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        PermanentSendError(424, "template not found")
    )
    sender.behavior["09120000002"] = lambda: (_ for _ in ()).throw(
        PermanentSendError(424, "template not found")
    )
    sender.behavior["09120000003"] = lambda: (_ for _ in ()).throw(
        SendError(409, "retries exhausted: server busy")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.failed_permanent == 2
    assert summary.failed_retriable == 1
    # Two distinct error messages; the [424] one wins by count.
    assert len(summary.top_errors) == 2
    msg, count = summary.top_errors[0]
    assert "[424]" in msg
    assert count == 2


def test_format_report_includes_top_errors():
    from sms_sender.runner import RunSummary
    s = RunSummary(
        total_input=10, new_recipients=10, duplicates_collapsed=0, invalid=0,
        sent=7, failed_permanent=2, failed_retriable=1, halted=False,
        elapsed_sec=4.0, sends_per_sec=1.75,
        top_errors=(("[424] template not found", 2), ("[409] retries exhausted", 1)),
    )
    out = format_report(s)
    assert "sent              7" in out
    assert "failed_permanent  2" in out
    assert "elapsed           4.00s (1.75 sends/sec)" in out
    assert "[424] template not found" in out
    assert "[409] retries exhausted" in out


def test_format_report_omits_top_errors_when_empty():
    from sms_sender.runner import RunSummary
    s = RunSummary(
        total_input=2, new_recipients=2, duplicates_collapsed=0, invalid=0,
        sent=2, failed_permanent=0, failed_retriable=0, halted=False,
        elapsed_sec=0.5, sends_per_sec=4.0, top_errors=(),
    )
    out = format_report(s)
    assert "top errors" not in out
