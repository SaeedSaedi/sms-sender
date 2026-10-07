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


class RecordingReporter:
    """Captures what a run reports, instead of drawing a tqdm bar."""

    def __init__(self):
        self.notes: list[str] = []
        self.starts: list[int] = []
        self.ticks: list[int] = []
        self.finished = 0

    def note(self, text, key="note", **fields):
        self.notes.append(text)

    def start(self, total):
        self.starts.append(total)

    def advance(self, counts):
        self.ticks.append(counts.processed + counts.already_done)

    def finish(self):
        self.finished += 1


def test_progress_goes_to_the_reporter(tmp_path):
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    reporter = RecordingReporter()
    Runner(
        input_path=inp, state=StateStore(tmp_path / "s.db"), sender=FakeSender(),
        workers=1, reporter=reporter,
    ).run()
    assert reporter.starts == [3]
    assert reporter.ticks == [1, 2, 3]
    assert reporter.finished == 1


def test_cancel_stops_claiming_and_the_next_run_sends_the_rest(tmp_path):
    """The dashboard's pause: a request in flight finishes and is recorded,
    nothing new is claimed, and the remaining rows stay claimable."""
    phones = [f"0912000000{i}" for i in range(1, 6)]
    inp = write_input(tmp_path, phones)
    state = StateStore(tmp_path / "s.db")

    class CancelOnSecondSend(FakeSender):
        def send(self, phone, tokens=None):
            result = super().send(phone, tokens)
            if len(self.calls) == 2:
                runner.cancel()  # e.g. the operator pressed Pause mid-send
            return result

    sender = CancelOnSecondSend()
    runner = Runner(
        input_path=inp, state=state, sender=sender, workers=1, reporter=RecordingReporter(),
    )
    summary = runner.run()
    assert summary.stopped is True
    assert (summary.sent, len(sender.calls)) == (2, 2)
    assert state.counts() == {SENT: 2, PENDING: 3}
    assert "stopped" in format_report(summary)

    rest = FakeSender()
    Runner(
        input_path=inp, state=state, sender=rest, workers=1, reporter=RecordingReporter(),
    ).run()
    assert sorted(rest.calls) == sorted(set(phones) - set(sender.calls))


def test_signal_handlers_are_restored_after_the_run(tmp_path):
    import signal

    before = signal.getsignal(signal.SIGINT)
    seen = []

    class Peek(FakeSender):
        def send(self, phone, tokens=None):
            seen.append(signal.getsignal(signal.SIGINT))
            return super().send(phone, tokens)

    Runner(
        input_path=write_input(tmp_path, ["09120000001"]), state=StateStore(tmp_path / "s.db"),
        sender=Peek(), workers=1, reporter=RecordingReporter(),
    ).run()
    assert seen and seen[0] is not before  # the run's own handler was active
    assert signal.getsignal(signal.SIGINT) is before


def test_an_embedding_process_keeps_its_signal_handlers(tmp_path):
    import signal

    before = signal.getsignal(signal.SIGINT)
    seen = []

    class Peek(FakeSender):
        def send(self, phone, tokens=None):
            seen.append(signal.getsignal(signal.SIGINT))
            return super().send(phone, tokens)

    Runner(
        input_path=write_input(tmp_path, ["09120000001"]), state=StateStore(tmp_path / "s.db"),
        sender=Peek(), workers=1, reporter=RecordingReporter(),
        install_signal_handlers=False,
    ).run()
    assert seen == [before]


def test_campaign_settings_capture_what_is_sent_not_how_fast():
    from sms_sender.runner import campaign_settings
    from sms_sender.sender import SenderConfig

    columns = TokenColumns(columns={"token20": "coin"}, value_maps={"coin": {"BTC": "بیت‌کوین"}})
    cfg = SenderConfig(api_key="k", template="transaction-1", token="x", token10="y")
    assert campaign_settings(cfg, columns) == {
        "template": "transaction-1",
        "tokens": {"token": "x", "token10": "y"},
        "token_columns": {"token20": "coin"},
        "value_maps": {"coin": {"BTC": "بیت‌کوین"}},
    }
    faster = SenderConfig(api_key="other", template="transaction-1", token="x", token10="y",
                          timeout=99, max_attempts=1)
    assert campaign_settings(faster, columns) == campaign_settings(cfg, columns)


def test_rerun_with_other_settings_refuses_before_touching_rows(tmp_path):
    import json

    import pytest

    from sms_sender.state import CampaignMismatchError

    db = tmp_path / "s.db"
    Runner(
        input_path=write_input(tmp_path, ["09120000001"]), state=StateStore(db),
        sender=FakeSender(), workers=1, campaign="promo", settings={"template": "a"},
    ).run()
    assert json.loads(StateStore(db).get_meta("last_run"))["sent"] == 1

    later = FakeSender()
    with pytest.raises(CampaignMismatchError):
        Runner(
            input_path=write_input(tmp_path, ["09120000002"]), state=StateStore(db),
            sender=later, workers=1, campaign="promo", settings={"template": "b"},
        ).run()
    assert later.calls == []
    assert StateStore(db).counts() == {SENT: 1}  # the new phone wasn't even seeded


def test_opted_out_recipients_are_never_sent(tmp_path):
    from sms_sender.state import SUPPRESSED

    db = tmp_path / "s.db"
    already = StateStore(db)
    already.upsert_pending([("09120000003", "09120000003")])
    already.claim("09120000003")
    already.mark_sent("09120000003", message_id=1, status_code=200)

    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    reporter = RecordingReporter()
    sender = FakeSender()
    summary = Runner(
        input_path=inp, state=StateStore(db), sender=sender, workers=1, reporter=reporter,
        opt_out=frozenset({"09120000002", "09120000003"}),
    ).run()
    assert sender.calls == ["09120000001"]
    # The opted-out one is suppressed; the one that already had it stays sent.
    assert StateStore(db).counts() == {SENT: 2, SUPPRESSED: 1}
    assert summary.suppressed == 1
    assert any("opt-out" in n for n in reporter.notes)
    assert "suppressed" in format_report(summary)


def test_run_outside_the_sending_window_does_not_start(tmp_path):
    from datetime import datetime

    from sms_sender.window import TEHRAN, parse_window

    sender = FakeSender()
    reporter = RecordingReporter()
    summary = Runner(
        input_path=write_input(tmp_path, ["09120000001"]), state=StateStore(tmp_path / "s.db"),
        sender=sender, workers=1, reporter=reporter,
        send_window=parse_window("08:00-21:00"),
        clock=lambda: datetime(2026, 10, 4, 22, 15, tzinfo=TEHRAN),
    ).run()
    assert summary.halted is True
    assert sender.calls == []
    assert any("outside the sending window" in n and "22:15" in n for n in reporter.notes)
    # The dashboard says it in Persian from the key, never the note.
    assert (summary.stop_reason, summary.stop_fields) == (
        "outside_window", {"start": "08:00", "end": "21:00", "now": "22:15"},
    )


def test_run_stops_when_the_sending_window_closes(tmp_path):
    """Inside the window for the preflight check and two sends, then 21:00:
    nothing more is claimed, and the rest wait for the next run."""
    from datetime import datetime

    from sms_sender.window import TEHRAN, parse_window

    inside = iter([datetime(2026, 10, 4, 20, 59, tzinfo=TEHRAN)] * 3)
    closed = datetime(2026, 10, 4, 21, 0, tzinfo=TEHRAN)
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    reporter = RecordingReporter()
    summary = Runner(
        input_path=write_input(tmp_path, [f"0912000000{i}" for i in range(1, 5)]),
        state=state, sender=sender, workers=1, reporter=reporter,
        send_window=parse_window("08:00-21:00"), clock=lambda: next(inside, closed),
    ).run()
    assert len(sender.calls) == 2
    assert summary.stopped is True and summary.halted is False
    assert state.counts() == {SENT: 2, PENDING: 2}
    assert any("sending window" in n and "closed" in n for n in reporter.notes)
    assert (summary.stop_reason, summary.stop_fields) == ("window_closed", {"start": "08:00", "end": "21:00"})


class AccountFakeSender(FakeSender):
    """FakeSender with an account: credit, settings, and a cost per SMS."""

    def __init__(self, credit=10_000, config=None, cost=None):
        super().__init__()
        self.credit, self.config, self.cost = credit, config, cost

    def account_info(self):
        return AccountInfo(remaining_credit=self.credit, expire_date=None, type="master")

    def account_config(self):
        from sms_sender.sender import AccountConfig

        if isinstance(self.config, BaseException):
            raise self.config
        return self.config or AccountConfig(debug_mode=False, resend_failed=False)

    def send(self, phone, tokens=None):
        super().send(phone, tokens)
        return SendResult(message_id=1, status_code=200, cost=self.cost)


def _run(tmp_path, sender, phones, **kw):
    reporter = RecordingReporter()
    summary = Runner(
        input_path=write_input(tmp_path, phones), state=StateStore(tmp_path / "s.db"),
        sender=sender, workers=1, reporter=reporter, **kw,
    ).run()
    return summary, reporter


def test_debug_mode_account_stops_before_sending(tmp_path):
    from sms_sender.sender import AccountConfig

    sender = AccountFakeSender(config=AccountConfig(debug_mode=True, resend_failed=False))
    summary, reporter = _run(tmp_path, sender, ["09120000001"])
    assert summary.halted is True
    assert sender.calls == []
    assert any("debug mode" in n for n in reporter.notes)


def test_resend_failed_setting_is_flagged_but_does_not_block(tmp_path):
    from sms_sender.sender import AccountConfig

    sender = AccountFakeSender(config=AccountConfig(debug_mode=False, resend_failed=True))
    summary, reporter = _run(tmp_path, sender, ["09120000001"])
    assert summary.sent == 1
    assert any("resend failed" in n for n in reporter.notes)


def test_unreadable_account_settings_do_not_block(tmp_path):
    sender = AccountFakeSender(config=HaltError(407, "no access to this method"))
    summary, _ = _run(tmp_path, sender, ["09120000001"])
    assert summary.sent == 1


def test_not_enough_credit_for_the_estimate_stops_after_the_test_sms(tmp_path):
    """The approval test costs 1,200; 3 recipients need ~3,600; 3,000 left."""
    sender = AccountFakeSender(credit=3_000, cost=1_200)
    summary, reporter = _run(
        tmp_path, sender, ["09120000001", "09120000002", "09120000003"],
        approval_test_number="09150000077", approval_prompt=lambda *_: True,
    )
    assert summary.halted is True
    assert sender.calls == ["09150000077"]  # only the approval test went out
    assert summary.cost == 1_200
    assert any("not enough credit" in n and "3600" in n for n in reporter.notes)
    assert (summary.stop_reason, summary.stop_fields) == (
        "not_enough_credit", {"estimate": 3_600, "recipients": 3, "credit": 3_000},
    )


def test_enough_credit_shows_the_estimate_and_sums_the_real_cost(tmp_path):
    sender = AccountFakeSender(credit=10_000, cost=1_200)
    summary, reporter = _run(
        tmp_path, sender, ["09120000001", "09120000002", "09120000003"],
        approval_test_number="09150000077", approval_prompt=lambda *_: True,
    )
    assert summary.sent == 3
    assert any("3 SMS × 1200 = 3600 rials" in n for n in reporter.notes)
    assert summary.cost == 4 * 1_200  # approval test + 3 recipients
    assert "4,800 rials" in format_report(summary)


def test_resumed_campaign_estimates_from_what_it_already_paid(tmp_path):
    db = tmp_path / "s.db"
    earlier = StateStore(db)
    earlier.upsert_pending([("09120000009", "09120000009")])
    earlier.claim("09120000009")
    earlier.mark_sent("09120000009", message_id=1, status_code=200, cost=1_100)

    sender = AccountFakeSender(credit=2_000, cost=1_100)
    summary, _ = _run(tmp_path, sender, ["09120000001", "09120000002"])
    assert summary.halted is True  # 2 × 1,100 > 2,000
    assert sender.calls == []


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


def test_uncertain_send_is_parked_as_unknown_and_never_resent(tmp_path):
    from sms_sender.sender import UncertainSendError
    from sms_sender.state import UNKNOWN

    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        UncertainSendError(None, "outcome unknown: read timed out")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.sent == 1
    assert summary.unknown == 1
    assert state.counts() == {SENT: 1, UNKNOWN: 1}
    assert "unknown" in format_report(summary)

    # A later run with a healthy provider still doesn't touch it.
    healthy = FakeSender()
    Runner(input_path=inp, state=state, sender=healthy, workers=1).run()
    assert healthy.calls == []


def test_make_runner_records_each_call_in_the_db(tmp_path):
    from sms_sender.runner import make_runner
    from sms_sender.sender import Attempt, SenderConfig

    inp = write_input(tmp_path, ["09120000001"])
    runner = make_runner(
        input_path=inp, db_path=tmp_path / "s.db",
        sender_cfg=SenderConfig(api_key="k", template="t"),
    )
    runner.sender._on_attempt(Attempt(
        phone="09120000001", outcome="accepted", started_at=1.0, finished_at=2.0,
        status_code=200, message_id=7, cost=1100,
    ))
    rows = runner.state.attempts_for("09120000001")
    assert [(r["kind"], r["outcome"], r["message_id"], r["cost"]) for r in rows] == [
        ("send", "accepted", 7, 1100),
    ]


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


TEST_NUMBER = "09150000077"


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
    """claim() is the dedup gate: a row another process sent after this run
    read its queue is never sent again, and counts as already done."""
    first, second = "09120000001", "09120000002"
    inp = write_input(tmp_path, [first, second])
    state = StateStore(tmp_path / "s.db")

    class AnotherProcessSendsTheSecond(FakeSender):
        def send(self, phone, tokens=None):
            if phone == first:
                with StateStore(tmp_path / "s.db") as other:
                    assert other.claim(second) is not None
                    other.mark_sent(second, message_id=2, status_code=200)
            return super().send(phone, tokens)

    sender = AnotherProcessSendsTheSecond()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert sender.calls == [first]
    assert (summary.sent, summary.already_done) == (1, 1)
    assert state.counts() == {SENT: 2}


def test_a_stop_while_waiting_for_the_rate_limit_claims_nothing(tmp_path):
    """The next recipient waits for the rate limiter before its claim; a
    pause that comes meanwhile leaves it unclaimed and unsent."""
    phones = ["09120000001", "09120000002", "09120000003"]
    inp = write_input(tmp_path, phones)
    state = StateStore(tmp_path / "s.db")

    class PauseSoonAfterTheFirst(FakeSender):
        def send(self, phone, tokens=None):
            if not self.calls:
                threading.Timer(0.1, runner.cancel).start()
            return super().send(phone, tokens)

    sender = PauseSoonAfterTheFirst()
    # 1/s: the first goes at once, the next waits a second for its turn.
    runner = Runner(input_path=inp, state=state, sender=sender, workers=1, rate_per_sec=1.0)
    summary = runner.run()
    assert summary.stopped and sender.calls == [phones[0]]
    assert state.counts() == {SENT: 1, PENDING: 2}


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


# ---------- the dashboard's test step (test_only) ----------


def test_a_test_only_run_sends_just_the_test_sms(tmp_path):
    """Validate, pre-send checks and the test SMS, then stop: the operator
    approves in the dashboard. Recipients are queued but nobody is sent."""
    sender = AccountFakeSender(credit=10_000, cost=1_200)
    prompt_calls = []
    summary, reporter = _run(
        tmp_path, sender, ["09120000001", "09120000002"], smoke_test=True,
        approval_test_number=TEST_NUMBER, test_only=True,
        approval_prompt=lambda *a: prompt_calls.append(a) or True,
    )
    assert sender.calls == [TEST_NUMBER]  # no recipient, not even a smoke test
    assert prompt_calls == []
    assert (summary.test_only, summary.halted, summary.stopped, summary.sent) == (True, False, False, 0)
    assert (summary.cost, summary.cost_per_sms, summary.estimate, summary.credit) == (1_200, 1_200, 2_400, 10_000)
    assert summary.test_message_id == 1
    state = StateStore(tmp_path / "s.db")
    assert state.counts() == {PENDING: 2}
    assert state.get_meta("last_run") is None  # a test isn't the campaign's last run


def test_a_test_only_run_needs_a_test_number(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        Runner(
            input_path=write_input(tmp_path, ["09120000001"]), state=StateStore(tmp_path / "s.db"),
            sender=FakeSender(), test_only=True,
        )


def test_a_test_only_run_still_refuses_what_the_credit_cannot_cover(tmp_path):
    sender = AccountFakeSender(credit=2_000, cost=1_200)
    summary, _ = _run(
        tmp_path, sender, ["09120000001", "09120000002"],
        approval_test_number=TEST_NUMBER, test_only=True,
    )
    assert summary.halted is True and sender.calls == [TEST_NUMBER]


def test_a_send_after_the_test_estimates_from_its_cost(tmp_path):
    """The send run has no test SMS of its own: the test's cost per SMS is
    handed over, so the credit check still works on a fresh campaign."""
    sender = AccountFakeSender(credit=3_000, cost=1_200)
    summary, reporter = _run(
        tmp_path, sender, ["09120000001", "09120000002", "09120000003"], cost_per_sms=1_200,
    )
    assert summary.halted is True and sender.calls == []
    assert any("3 SMS × 1200 = 3600 rials" in n for n in reporter.notes)

    sender = AccountFakeSender(credit=10_000, cost=1_200)
    summary, _ = _run(tmp_path, sender, ["09120000001", "09120000002", "09120000003"], cost_per_sms=1_200)
    assert summary.sent == 3 and summary.estimate == 3_600 and not summary.test_only


def test_a_halt_while_sending_names_kavenegars_code(tmp_path):
    sender = FakeSender()
    sender.behavior["09120000002"] = lambda: (_ for _ in ()).throw(HaltError(418, "no credit"))
    summary, _ = _run(tmp_path, sender, ["09120000001", "09120000002", "09120000003"])
    assert summary.halted is True
    assert (summary.stop_reason, summary.stop_fields) == ("provider_halt", {"code": 418})


def test_a_debug_mode_account_is_named(tmp_path):
    from sms_sender.sender import AccountConfig

    sender = AccountFakeSender(config=AccountConfig(debug_mode=True, resend_failed=False))
    summary, _ = _run(tmp_path, sender, ["09120000001"])
    assert summary.stop_reason == "debug_mode"


def test_a_run_to_the_end_has_no_stop_reason(tmp_path):
    summary, _ = _run(tmp_path, FakeSender(), ["09120000001"])
    assert (summary.stop_reason, summary.stop_fields) == (None, {})


def test_a_send_on_a_sqlite_with_the_wal_reset_bug_says_so(tmp_path, monkeypatch, caplog):
    from sms_sender import runner as runner_module

    monkeypatch.setattr(runner_module, "wal_reset_bug", lambda: True)
    inp = write_input(tmp_path, ["09120000001"])
    with caplog.at_level("WARNING"):
        Runner(input_path=inp, state=StateStore(tmp_path / "s.db"), sender=FakeSender(), workers=1).run()
    assert [r.fixed_in for r in caplog.records if r.message == "sqlite_wal_reset_bug"] == ["3.51.3"]
