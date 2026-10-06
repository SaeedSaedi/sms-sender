"""The test SMS goes to the team's numbers too (plan 06, D1): the same
message, after the operator's own, each with its own test link, recorded as
test calls, never to a number restricted sending doesn't allow."""
from __future__ import annotations

import time
from pathlib import Path

from sms_sender.allowlist import Allowlist
from sms_sender.runner import Runner, _attempt_recorder
from sms_sender.sendcheck import check_sends
from sms_sender.sender import Attempt, HaltError, PermanentSendError
from sms_sender.state import PENDING, StateStore

from .test_link_runs import links
from .test_links import FakeShlink
from .test_runner import FakeSender, write_input

OWN, TEAM_A, TEAM_B = "09150000077", "09150000088", "09150000099"
R1, R2 = "09120000001", "09120000002"


class KeyedReporter:
    """Keeps each note's key and fields, as the dashboard's reporter does."""

    def __init__(self):
        self.notes: list[tuple[str, dict]] = []

    def note(self, text, key="note", **fields):
        self.notes.append((key, fields))

    def start(self, total):
        pass

    def advance(self, counts):
        pass

    def finish(self):
        pass

    def keys(self) -> list[str]:
        return [key for key, _ in self.notes]


class AuditedSender(FakeSender):
    """Reports each call like the real Sender, so the state DB's audit trail
    files it under the kind the runner says (send or test)."""

    def __init__(self, state: StateStore):
        super().__init__()
        self._record = _attempt_recorder(state)

    def send(self, phone, tokens=None):
        started = time.time()
        try:
            result = super().send(phone, tokens)
        except PermanentSendError as e:
            self._record(Attempt(phone, "rejected", started, time.time(), e.status_code))
            raise
        self._record(Attempt(phone, "accepted", started, time.time(), 200, result.message_id))
        return result


def run(tmp_path: Path, sender=None, *, phones=(R1, R2), team=(TEAM_A, TEAM_B), **kw):
    reporter = KeyedReporter()
    state = kw.pop("state", None) or StateStore(tmp_path / "s.db")
    sender = sender or FakeSender()
    kw.setdefault("test_only", True)
    summary = Runner(
        input_path=kw.pop("input_path", None) or write_input(tmp_path, list(phones)), state=state,
        sender=sender, workers=1, reporter=reporter, approval_test_number=OWN,
        approval_test_team=team, **kw,
    ).run()
    return summary, sender, reporter


def test_the_team_gets_the_same_test_sms_after_the_operator(tmp_path):
    summary, sender, reporter = run(tmp_path)
    assert sender.calls == [OWN, TEAM_A, TEAM_B]
    assert sender.tokens[TEAM_A] == sender.tokens[OWN]
    assert (summary.test_team_sent, summary.test_only, summary.halted) == (2, True, False)
    assert reporter.keys().count("team_test_sending") == 2
    assert StateStore(tmp_path / "s.db").counts() == {PENDING: 2}  # nobody else got anything


def test_the_operators_own_number_and_repeats_go_once(tmp_path):
    summary, sender, _ = run(tmp_path, team=(TEAM_A, OWN, TEAM_A))
    assert sender.calls == [OWN, TEAM_A]
    assert summary.test_team_sent == 1


def test_without_an_operators_number_the_team_gets_nothing(tmp_path):
    """A send run (the dashboard's send job) has no test SMS of its own: the
    team only ever gets one along with the operator's."""
    sender = FakeSender()
    summary = Runner(
        input_path=write_input(tmp_path, [R1]), state=StateStore(tmp_path / "s.db"), sender=sender,
        workers=1, reporter=KeyedReporter(), approval_test_team=(TEAM_A,),
    ).run()
    assert sender.calls == [R1] and summary.test_team_sent == 0


def test_the_team_hears_nothing_when_the_operators_test_fails(tmp_path):
    sender = FakeSender()
    sender.behavior[OWN] = lambda: (_ for _ in ()).throw(PermanentSendError(424, "template not found"))
    summary, sender, _ = run(tmp_path, sender)
    assert sender.calls == [OWN]
    assert summary.halted and summary.stop_reason == "test_failed"


def test_a_team_number_kavenegar_refuses_is_noted_and_skipped(tmp_path):
    sender = FakeSender()
    sender.behavior[TEAM_A] = lambda: (_ for _ in ()).throw(PermanentSendError(411, "invalid receptor"))
    summary, sender, reporter = run(tmp_path, sender)
    assert sender.calls == [OWN, TEAM_A, TEAM_B]
    assert not summary.halted and summary.test_team_sent == 1
    assert ("team_test_failed", {"phone": TEAM_A, "code": 411}) in reporter.notes


def test_a_halt_on_a_team_number_stops_the_run(tmp_path):
    sender = FakeSender()
    sender.behavior[TEAM_A] = lambda: (_ for _ in ()).throw(HaltError(402, "credit"))
    summary, sender, _ = run(tmp_path, sender)
    assert sender.calls == [OWN, TEAM_A]
    assert summary.halted and summary.stop_reason == "test_refused"
    assert summary.stop_fields == {"code": 402}


def test_after_the_team_the_cli_asks_once_then_sends(tmp_path):
    asked = []
    summary, sender, _ = run(tmp_path, test_only=False, approval_prompt=lambda *a: asked.append(a) or True)
    assert sender.calls[:3] == [OWN, TEAM_A, TEAM_B]
    assert sorted(sender.calls[3:]) == [R1, R2]
    assert asked == [(OWN, 2)]
    assert summary.sent == 2 and summary.test_team_sent == 2


def test_restricted_sending_skips_the_team_numbers_it_doesnt_allow(tmp_path):
    sender = FakeSender()
    sender.allowlist = Allowlist(frozenset({OWN, TEAM_B, R1, R2}))
    summary, sender, reporter = run(tmp_path, sender)
    assert sender.calls == [OWN, TEAM_B]
    assert ("team_not_allowed", {"n": 1}) in reporter.notes
    assert summary.test_team_sent == 1


def test_the_team_sees_the_operators_message_when_tokens_come_from_the_list(tmp_path):
    from sms_sender.input_loader import TokenColumns

    inp = tmp_path / "in.csv"
    inp.write_text(f"phone,name\n{R1},Ali\n{TEAM_A},Sara\n", encoding="utf-8")
    summary, sender, _ = run(
        tmp_path, input_path=inp, token_columns=TokenColumns(columns={"token": "name"}),
    )
    # The operator isn't in the list: the first recipient's tokens, for everyone.
    assert sender.tokens[OWN] == sender.tokens[TEAM_A] == sender.tokens[TEAM_B] == {"token": "Ali"}


def test_each_team_number_gets_its_own_test_link(tmp_path):
    shlink = FakeShlink()
    state = StateStore(tmp_path / "s.db")
    summary, sender, _ = run(
        tmp_path, state=state, campaign="coin-7", links=links(), link_client=shlink, link_rate_per_sec=0,
    )
    rows = state.get_links([f"test:{OWN}", f"test:{TEAM_A}", f"test:{TEAM_B}"])
    assert len(rows) == 3
    sent = {phone: tokens["token3"] for phone, tokens in sender.tokens.items()}
    assert len(set(sent.values())) == 3
    assert sent[TEAM_A] == rows[f"test:{TEAM_A}"].short_url
    # Test links carry the test tag, so they never count as the campaign's clicks.
    assert all(tuple(created["tags"]) == ("campaign-coin-7-test",) for created in shlink.created)
    assert all("utm_content=test" in created["long_url"] for created in shlink.created)
    assert summary.links_ready == 3


def test_team_sends_are_test_calls_that_never_count_as_a_recipients(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sender = AuditedSender(state)
    # A team member who is also a recipient gets the test and the campaign.
    summary, _, _ = run(tmp_path, sender, state=state, phones=(R1, TEAM_A), test_only=False,
                        approval_prompt=lambda *a: True)
    assert summary.sent == 2
    assert [row["kind"] for row in state.attempts_for(TEAM_A)] == ["test", "send"]
    assert [row["kind"] for row in state.attempts_for(TEAM_B)] == ["test"]
    report = check_sends(state)
    assert (report.sms, report.test_sms) == (2, 3)
    assert not report.twice and not report.maybe_twice
