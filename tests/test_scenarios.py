"""End-to-end scenario tests.

These exercise full data flows across the input loader, state store,
runner, sender, and reporting layer — the kind of behavior that would
break in production if any one layer's contract drifted.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from sms_sender.runner import Runner, format_report
from sms_sender.sender import (
    HaltError,
    PermanentSendError,
    SendError,
    SendResult,
)
from sms_sender.state import (
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SENT,
    StateStore,
)


class FakeSender:
    def __init__(self):
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.behavior = {}  # phone -> callable

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        with self._lock:
            self.calls.append(phone)
        if phone in self.behavior:
            return self.behavior[phone]()
        return SendResult(message_id=hash(phone) & 0xffff, status_code=200)


def write_input(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "in.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------- mixed input handling ----------


def test_mixed_input_valid_invalid_duplicate(tmp_path):
    """A real-world file with valid + invalid + duplicate phones is handled
    in a single run: valids sent, invalids recorded permanent, dups collapsed."""
    inp = write_input(tmp_path, [
        "09120000001",
        "09120000002",
        "+989120000002",      # duplicate of 09120000002
        "0098 912 000 0003",  # alternate normalization
        "not-a-phone",
        "0812345678",         # not Iranian mobile
        "# comment",
        "",
    ])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=2).run()

    assert summary.sent == 3                 # 1, 2, 3
    assert summary.invalid == 2              # bad-phone, 0812
    assert summary.duplicates_collapsed == 1
    assert summary.failed_permanent == 0
    assert summary.failed_retriable == 0

    counts = state.counts()
    assert counts.get(SENT) == 3
    # Invalid rows are persisted as failed_permanent (synthetic INVALID: keys).
    assert counts.get(FAILED_PERMANENT) == 2


def test_empty_input_file(tmp_path):
    """An empty input file produces a clean zero-summary, doesn't crash."""
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=p, state=state, sender=sender, workers=1).run()
    assert summary.sent == 0
    assert summary.invalid == 0
    assert summary.failed_permanent == 0
    assert sender.calls == []


def test_only_invalid_input(tmp_path):
    """If every row is invalid, no API calls happen; all rows go to failed_permanent."""
    inp = write_input(tmp_path, ["junk1", "junk2", "junk3"])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.sent == 0
    assert summary.invalid == 3
    assert sender.calls == []
    counts = state.counts()
    assert counts.get(FAILED_PERMANENT) == 3


def test_only_duplicates(tmp_path):
    """All-same input collapses to one send."""
    inp = write_input(tmp_path, [
        "09120000001",
        "+989120000001",
        "9120000001",
        "0912 000 0001",
    ])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1).run()
    assert summary.sent == 1
    assert summary.duplicates_collapsed == 3
    assert sender.calls == ["09120000001"]


# ---------- mixed outcomes in one run ----------


def test_run_with_every_outcome(tmp_path):
    """One run that produces sent + permanent + retriable + invalid records.
    All four buckets should be visible in the summary and the state DB."""
    inp = write_input(tmp_path, [
        "09120000001",     # will succeed
        "09120000002",     # will fail permanent (e.g. bad receptor)
        "09120000003",     # will fail retriable (retries exhausted)
        "junk-phone",      # invalid input
    ])
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    sender.behavior["09120000002"] = lambda: (_ for _ in ()).throw(
        PermanentSendError(411, "invalid receptor")
    )
    sender.behavior["09120000003"] = lambda: (_ for _ in ()).throw(
        SendError(409, "retries exhausted: server busy")
    )
    summary = Runner(input_path=inp, state=state, sender=sender, workers=2).run()

    assert summary.sent == 1
    assert summary.failed_permanent == 1
    assert summary.failed_retriable == 1
    assert summary.invalid == 1
    assert summary.halted is False
    # Two error buckets in top_errors.
    msgs = {m for m, _ in summary.top_errors}
    assert any("[411]" in m for m in msgs)
    assert any("[409]" in m for m in msgs)


# ---------- resume scenarios ----------


def test_resume_after_halt_picks_up_remaining(tmp_path):
    """A run halted on credit exhaustion → fix → re-run → completes the rest."""
    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")

    # First run halts on the first phone.
    halting_sender = FakeSender()
    halting_sender.behavior["09120000001"] = lambda: (_ for _ in ()).throw(
        HaltError(418, "insufficient credit")
    )
    summary1 = Runner(
        input_path=inp, state=state, sender=halting_sender, workers=1,
    ).run()
    assert summary1.halted is True
    assert summary1.sent == 0

    # Second run, with credit back. The previously-halted row stays claimable
    # (failed_retriable), and the un-attempted ones too.
    healthy_sender = FakeSender()
    summary2 = Runner(
        input_path=inp, state=state, sender=healthy_sender, workers=2,
    ).run()
    assert summary2.halted is False
    assert summary2.sent == 3
    assert state.counts() == {SENT: 3}


def test_resume_after_crash_reclaims_in_flight(tmp_path):
    """An in_flight row from a prior process must be reclaimed on the next run."""
    inp = write_input(tmp_path, ["09120000001", "09120000002"])
    db = tmp_path / "s.db"

    # Simulate a crash: claim a row but never mark it sent.
    state1 = StateStore(db)
    state1.upsert_pending([("09120000001", "09120000001"), ("09120000002", "09120000002")])
    state1.claim("09120000001")  # status now in_flight

    # New "process" reopens the DB and runs.
    state2 = StateStore(db)
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state2, sender=sender, workers=1).run()
    assert summary.sent == 2
    assert state2.counts() == {SENT: 2}


# ---------- preflight + smoke ordering ----------


class PreflightFakeSender(FakeSender):
    def __init__(self, account_result):
        super().__init__()
        self._account = account_result

    def account_info(self):
        if isinstance(self._account, BaseException):
            raise self._account
        return self._account


def test_smoke_passes_then_run_halts_midway(tmp_path):
    """Preflight + smoke succeed, then a later phone hits HaltError → halt with
    smoke-phone marked sent and the rest claimable."""
    from sms_sender.sender import AccountInfo

    inp = write_input(tmp_path, ["09120000001", "09120000002", "09120000003"])
    state = StateStore(tmp_path / "s.db")
    sender = PreflightFakeSender(AccountInfo(remaining_credit=999, expire_date=None, type="X"))
    sender.behavior["09120000003"] = lambda: (_ for _ in ()).throw(
        HaltError(418, "insufficient credit")
    )
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        preflight=True, smoke_test=True,
    ).run()
    assert summary.halted is True
    # Smoke phone (first claimable = 09120000001) was sent before the halt.
    counts = state.counts()
    assert counts.get(SENT, 0) >= 1
    # Halted phone is failed_retriable (claimable on next run).
    assert counts.get(FAILED_RETRIABLE, 0) == 1


# ---------- format_report rendering with all fields ----------


def test_format_report_renders_already_done(tmp_path):
    """The already_done line should appear when non-zero, omitted when zero."""
    from sms_sender.runner import RunSummary
    with_skips = RunSummary(
        total_input=5, new_recipients=2, duplicates_collapsed=0, invalid=0,
        sent=2, failed_permanent=0, failed_retriable=0, halted=False,
        elapsed_sec=1.0, sends_per_sec=2.0, already_done=3,
    )
    out = format_report(with_skips)
    assert "already_done      3" in out

    without_skips = RunSummary(
        total_input=2, new_recipients=2, duplicates_collapsed=0, invalid=0,
        sent=2, failed_permanent=0, failed_retriable=0, halted=False,
        elapsed_sec=1.0, sends_per_sec=2.0, already_done=0,
    )
    assert "already_done" not in format_report(without_skips)


def test_format_report_with_halt_and_top_errors():
    """A halted run with top_errors should render both clearly."""
    from sms_sender.runner import RunSummary
    s = RunSummary(
        total_input=5, new_recipients=5, duplicates_collapsed=0, invalid=0,
        sent=2, failed_permanent=0, failed_retriable=1, halted=True,
        elapsed_sec=2.5, sends_per_sec=0.8,
        top_errors=(("[418] insufficient credit", 1),),
    )
    out = format_report(s)
    assert "halted            True" in out
    assert "[418] insufficient credit" in out


# ---------- notify payload includes new fields ----------


def test_notify_generic_payload_includes_already_done(monkeypatch):
    """The structured webhook payload must surface every RunSummary field,
    including the new `already_done`."""
    from sms_sender import notify as notify_module
    from sms_sender.notify import notify
    from sms_sender.runner import RunSummary

    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass

    def fake_post(url, *, json=None, timeout=None, **_):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(notify_module.requests, "post", fake_post)
    summary = RunSummary(
        total_input=10, new_recipients=10, duplicates_collapsed=0, invalid=0,
        sent=8, failed_permanent=1, failed_retriable=0, halted=False,
        elapsed_sec=2.0, sends_per_sec=4.0, already_done=1,
        top_errors=(("[424] template not found", 1),),
    )
    notify("https://example.com/hook", summary)
    payload = captured["json"]["summary"]
    assert payload["already_done"] == 1
    assert payload["sent"] == 8
    assert payload["top_errors"] == [{"message": "[424] template not found", "count": 1}]


# ---------- profile + redaction interplay ----------


def test_redaction_survives_state_reload(tmp_path):
    """Once last_error is scrubbed in the DB, reading it back stays scrubbed —
    no path can re-leak the secret after the redact-at-write boundary."""
    SECRET = "SECRET_API_KEY_DO_NOT_LEAK"
    leak = (
        f"http: HTTPSConnectionPool(host='api.kavenegar.com'): "
        f"with url: /v1/{SECRET}/verify/lookup.json"
    )
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([("09120000001", "09120000001")])
    state.claim("09120000001")
    state.mark_failed("09120000001", None, leak, permanent=True)

    rows = list(state.iter_failed_permanent())
    assert SECRET not in rows[0]["last_error"]
    assert "***" in rows[0]["last_error"]


# ---------- concurrent claim correctness ----------


def test_high_concurrency_no_double_send(tmp_path):
    """Stress the claim race: 50 workers, 50 phones, every phone sent exactly once."""
    phones = [f"0912{i:07d}" for i in range(50)]
    inp = write_input(tmp_path, phones)
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=20).run()
    assert summary.sent == 50
    assert sorted(sender.calls) == sorted(phones)
    # Every claim is unique — no phone got sent twice.
    assert len(set(sender.calls)) == 50
