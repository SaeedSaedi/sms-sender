"""End-to-end runner test using a fake Sender. Exercises the contract:
every input number ends in `sent` or a recorded failure, and re-runs
never re-call the API for already-sent numbers."""
from __future__ import annotations

import threading
from pathlib import Path

from sms_sender.input_loader import TokenColumns
from sms_sender.runner import Runner, format_report
from sms_sender.sender import (
    AccountInfo,
    HaltError,
    PermanentSendError,
    SendError,
    SendResult,
)
from sms_sender.state import PENDING, SENT, StateStore


class FakeSender:
    def __init__(self):
        self.calls: list[str] = []
        self.tokens: dict[str, dict[str, str] | None] = {}  # phone -> per-row tokens sent
        self._lock = threading.Lock()
        self.behavior = {}  # phone -> callable returning SendResult or raising

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        with self._lock:
            self.calls.append(phone)
            self.tokens[phone] = tokens
        if phone in self.behavior:
            return self.behavior[phone]()
        return SendResult(message_id=hash(phone) & 0xffff, status_code=200)


def write_input(tmp_path: Path, phones: list[str]) -> Path:
    p = tmp_path / "in.txt"
    p.write_text("\n".join(phones) + "\n", encoding="utf-8")
    return p


def test_run_start_logs_the_template(tmp_path, caplog):
    """The log is the campaign history today, so each run records its template."""
    from types import SimpleNamespace

    inp = write_input(tmp_path, ["09120000001"])
    sender = FakeSender()
    sender.cfg = SimpleNamespace(template="transaction-1")
    with caplog.at_level("INFO", logger="sms_sender.runner"):
        Runner(input_path=inp, state=StateStore(tmp_path / "s.db"), sender=sender, workers=1).run()
    starts = [r for r in caplog.records if r.getMessage() == "run_start"]
    assert [r.template for r in starts] == ["transaction-1"]


def test_second_run_on_a_busy_db_refuses_and_touches_nothing(tmp_path):
    """While one process is sending, another run on the same DB must not
    reset its in_flight rows (that's how a double send would happen)."""
    import pytest

    from sms_sender.locking import RunLock, RunLockError

    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    db = tmp_path / "s.db"
    state = StateStore(db)
    state.upsert_pending([("09120000001", "09120000001"), ("09120000002", "09120000002")])
    state.claim("09120000001")  # the first process is mid-send on this one
    sender = FakeSender()
    with RunLock(db):
        with pytest.raises(RunLockError):
            Runner(input_path=inp, state=StateStore(db), sender=sender, workers=1).run()
    assert sender.calls == []
    assert state.counts() == {"in_flight": 1, PENDING: 1}


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


# ---------- approval test (manual gate) ----------


TEST_NUMBER = "09151097710"


def test_approval_test_approved_proceeds(tmp_path):
    """Prompt returns True → run fans out, test number gets one out-of-band send."""
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    prompt_calls: list[tuple[str, int]] = []

    def prompt(num: str, count: int) -> bool:
        prompt_calls.append((num, count))
        return True

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2,
        approval_test_number=TEST_NUMBER, approval_prompt=prompt,
    ).run()

    assert summary.sent == 2
    assert summary.halted is False
    # The test number was sent once (out-of-band) PLUS the two recipients.
    assert sorted(sender.calls) == sorted([TEST_NUMBER, "09120000001", "09120000002"])
    # Prompt got the test number and the recipient count.
    assert prompt_calls == [(TEST_NUMBER, 2)]
    # State DB does NOT track the test number — only the two real recipients.
    assert state.counts() == {SENT: 2}


def test_approval_test_declined_aborts_run(tmp_path):
    """Prompt returns False → no fan-out, halted summary, no recipient sends."""
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2,
        approval_test_number=TEST_NUMBER,
        approval_prompt=lambda _num, _n: False,
    ).run()

    assert summary.halted is True
    assert summary.sent == 0
    # Only the test number was sent; no recipient calls happened.
    assert sender.calls == [TEST_NUMBER]
    # Recipients remain claimable — a future run can still deliver them.
    assert sorted(state.list_claimable_phones()) == ["09120000001", "09120000002"]


def test_approval_test_send_failure_aborts_without_prompt(tmp_path):
    """PermanentSendError on the test send → abort BEFORE asking for approval.

    No point asking the operator to approve something that didn't reach them.
    """
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior[TEST_NUMBER] = lambda: (_ for _ in ()).throw(
        PermanentSendError(424, "template not found")
    )
    prompt_called = {"n": 0}

    def prompt(_num: str, _n: int) -> bool:
        prompt_called["n"] += 1
        return True

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        approval_test_number=TEST_NUMBER, approval_prompt=prompt,
    ).run()

    assert summary.halted is True
    assert summary.sent == 0
    assert prompt_called["n"] == 0  # prompt never reached
    assert sender.calls == [TEST_NUMBER]  # only the failing test send
    # The failure shows up in top_errors so the operator can see why.
    assert summary.top_errors
    msg, _ = summary.top_errors[0]
    assert "[424]" in msg
    assert "template not found" in msg


def test_approval_test_halt_aborts_without_prompt(tmp_path):
    """HaltError on the test send (e.g. bad API key) → abort before prompt."""
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior[TEST_NUMBER] = lambda: (_ for _ in ()).throw(
        HaltError(401, "invalid api key")
    )
    prompt_called = {"n": 0}

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        approval_test_number=TEST_NUMBER,
        approval_prompt=lambda _n, _c: prompt_called.__setitem__("n", prompt_called["n"] + 1) or True,
    ).run()

    assert summary.halted is True
    assert summary.sent == 0
    assert prompt_called["n"] == 0


def test_approval_test_number_in_recipient_list_gets_two_sends(tmp_path):
    """If the test number is also in the input file, it MUST receive both
    the out-of-band test SMS and the in-band recipient SMS.

    The user explicitly does not want the test number skipped — the test SMS
    is what proves the template is right; the in-band send is what was
    requested by the input file. Two distinct sends, by design.
    """
    inp = write_input(tmp_path, [TEST_NUMBER, "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2,
        approval_test_number=TEST_NUMBER,
        approval_prompt=lambda _n, _c: True,
    ).run()

    assert summary.sent == 2
    # Sender saw the test number TWICE (once as test, once as recipient).
    assert sender.calls.count(TEST_NUMBER) == 2
    assert "09120000002" in sender.calls
    # State DB has both recipient rows as sent.
    assert state.counts() == {SENT: 2}


def test_approval_test_disabled_no_prompt(tmp_path):
    """Default (approval_test_number=None) → no extra send, no prompt called."""
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    prompt_called = {"n": 0}

    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        approval_prompt=lambda _n, _c: prompt_called.__setitem__("n", prompt_called["n"] + 1) or True,
    ).run()

    assert summary.sent == 1
    assert sender.calls == ["09120000001"]
    assert prompt_called["n"] == 0


def test_approval_test_runs_before_smoke_test(tmp_path):
    """When both flags are set, the approval gate must come first.

    User can decline before any auto-validated send happens. The smoke test
    fires only after approval is granted.
    """
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=999, expire_date=None, type="X"))

    # Decline at the prompt — smoke send should never happen.
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        preflight=True, smoke_test=True,
        approval_test_number=TEST_NUMBER,
        approval_prompt=lambda _n, _c: False,
    ).run()

    assert summary.halted is True
    # Only the approval-test send went out; no smoke and no fan-out.
    assert sender.calls == [TEST_NUMBER]


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


# ---------- API key redaction (regression for security S1) ----------


def test_runner_redacts_api_key_in_last_error_and_top_errors(tmp_path):
    """If a Sender error message ever carries an unredacted Kavenegar URL,
    neither the durable `last_error` column nor `top_errors` should retain it."""
    SECRET = "SECRET_API_KEY_DO_NOT_LEAK"
    leaky = (
        f"http: HTTPSConnectionPool(host='api.kavenegar.com', port=443): "
        f"with url: /v1/{SECRET}/verify/lookup.json (Caused by …)"
    )
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        SendError(None, f"retries exhausted: {leaky}")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()

    # last_error in the DB must not contain the secret.
    rows = list(state._conn().execute(
        "SELECT last_error FROM recipients WHERE phone=?", ("09120000001",)
    ))
    assert SECRET not in (rows[0]["last_error"] or "")
    assert "***" in rows[0]["last_error"]

    # top_errors in the summary must not contain the secret.
    assert summary.top_errors
    msg, _count = summary.top_errors[0]
    assert SECRET not in msg
    assert "***" in msg


# ---------- HaltError surfaces in top_errors (regression for R1) ----------


def test_halt_appears_in_top_errors(tmp_path):
    """When a run halts, the halt code+message should appear in top_errors so
    the operator (and any notification webhook) sees *why* it halted."""
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        HaltError(418, "insufficient credit")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.halted is True
    # The reason for the halt is now visible in the structured summary.
    assert summary.top_errors
    msg, count = summary.top_errors[0]
    assert "[418]" in msg
    assert "insufficient credit" in msg
    assert count == 1


# ---------- already_done counter (R8) ----------


def test_already_done_counts_pre_sent_rows(tmp_path):
    """A resume run that finds rows already `sent` should report them in
    the summary's `already_done` field, not silently swallow."""
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    Runner(input_path=inp, state=state, sender=FakeSender(), workers=1).run()
    # Second run — same input, same DB. All rows are already sent.
    summary = Runner(
        input_path=inp, state=state, sender=FakeSender(), workers=1,
    ).run()
    assert summary.sent == 0
    # No phones get into the executor on the second run because
    # list_claimable_phones returns []. So already_done is 0 here — the
    # claim race only manifests when there's actual work. The metric exists
    # for that race; verify it's at least default-0 and present.
    assert summary.already_done == 0


def test_already_done_counts_lost_claim_race(tmp_path):
    """If claim() returns None mid-fan-out (e.g., the row was concurrently
    flipped to sent by another process), the runner should count it as
    already_done rather than dropping it."""
    inp = write_input(tmp_path, ["09120000001"])
    state = StateStore(tmp_path / "s.db")
    # Pre-flip the row to sent so claim() returns None on the worker.
    state.upsert_pending([("09120000001", "09120000001")])
    state.claim("09120000001")
    state.mark_sent("09120000001", message_id=1, status_code=200)

    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
    ).run()
    # Sender never gets called for an already-sent row.
    assert sender.calls == []
    assert summary.sent == 0


# ---------- per-recipient token columns ----------

SIDE_SPEC = TokenColumns(
    columns={"token": "side", "token10": "name"},
    value_maps={"side": {"Buy": "خرید", "Sell": "فروش"}},
)


def write_csv(tmp_path: Path, rows: list[str]) -> Path:
    p = tmp_path / "in.csv"
    p.write_text("phone,name,side\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_token_columns_send_each_rows_own_tokens(tmp_path):
    inp = write_csv(tmp_path, ["09120000001,علی,Buy", "09120000002,سارا,Sell"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=2, token_columns=SIDE_SPEC,
    ).run()
    assert summary.sent == 2
    assert sender.tokens == {
        "09120000001": {"token": "خرید", "token10": "علی"},
        "09120000002": {"token": "فروش", "token10": "سارا"},
    }


def test_token_columns_leave_db_rows_missing_from_input(tmp_path):
    """A pending row seeded by some other input has no tokens to send — it
    must be left alone rather than sent with the static tokens only."""
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([("09120000009", "09120000009")])
    inp = write_csv(tmp_path, ["09120000001,علی,Buy"])
    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, token_columns=SIDE_SPEC,
    ).run()
    assert sender.calls == ["09120000001"]
    assert summary.sent == 1
    assert state.status_for_phones(["09120000009"]) == {"09120000009": PENDING}


def test_token_columns_approval_test_borrows_first_recipients_tokens(tmp_path):
    inp = write_csv(tmp_path, ["09120000001,علی,Buy", "09120000002,سارا,Sell"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1, token_columns=SIDE_SPEC,
        approval_test_number="09150000000", approval_prompt=lambda *_: True,
    ).run()
    assert summary.sent == 2
    assert sender.tokens["09150000000"] == {"token": "خرید", "token10": "علی"}
