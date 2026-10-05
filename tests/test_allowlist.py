"""Restricted sending (plan 06, D7): while SMS_SENDER_ALLOWED_NUMBERS is set,
no SMS goes to any other number — not from a run, not from the real Sender,
not from `preview --send`."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import sms_sender.cli as cli_module
from sms_sender.allowlist import ENV_ALLOWED_NUMBERS, Allowlist, allowlist
from sms_sender.cli import cli
from sms_sender.runner import Runner
from sms_sender.sender import HaltError, SendResult, Sender, SenderConfig
from sms_sender.state import PENDING, StateStore

ME = "09151097710"


def test_it_is_off_until_set():
    assert allowlist("") is None
    assert allowlist(" , ") is None


def test_numbers_are_read_in_any_form():
    allowed = allowlist("+98 915 109 7710, ۰۹۱۲۰۰۰۰۰۰۱")
    assert allowed == Allowlist(frozenset({ME, "09120000001"}))
    assert allowed.allows("00989151097710") and allowed.allows(ME)
    assert not allowed.allows("09120000002") and not allowed.allows("not a number")


def test_a_value_that_is_not_a_number_allows_nothing():
    """A typo must never lift the rule."""
    allowed = allowlist(f"{ME}, 0915-oops")
    assert allowed.invalid == 1
    assert not allowed.allows(ME)


def test_it_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    assert allowlist() == Allowlist(frozenset({ME}))


class FakeSDK:
    def __init__(self):
        self.calls = []

    def verify_lookup(self, params):
        self.calls.append(params["receptor"])
        return [{"messageid": 1, "status": 200}]


def _cfg() -> SenderConfig:
    return SenderConfig(api_key="k", template="t", token="1", max_attempts=1, backoff_max=0.01, timeout=1)


def test_the_real_sender_refuses_any_other_number_before_calling(monkeypatch):
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    sdk = FakeSDK()
    sender = Sender(_cfg(), sdk=sdk)
    with pytest.raises(HaltError, match="restricted sending"):
        sender.send("09120000001")
    assert sdk.calls == []
    assert sender.send(ME).message_id == 1
    assert sdk.calls == [ME]


def test_the_sender_takes_the_rule_it_is_given(monkeypatch):
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    assert Sender(_cfg(), sdk=FakeSDK(), allowed=None).allowlist is None
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, "")
    assert Sender(_cfg(), sdk=FakeSDK()).allowlist is None


class FakeSender:
    """The runner's view of a Sender, with sending restricted."""

    def __init__(self, allowed: Allowlist | None):
        self.allowlist = allowed
        self.calls: list[str] = []
        self.account_calls = 0

    def send(self, phone, tokens=None):
        self.calls.append(phone)
        return SendResult(message_id=len(self.calls), status_code=200)

    def account_info(self):
        self.account_calls += 1
        raise AssertionError("the restriction refuses before the account check")


def _run(tmp_path, sender, phones, **kw):
    inp = tmp_path / "in.txt"
    inp.write_text("\n".join(phones) + "\n", encoding="utf-8")
    state = StateStore(tmp_path / "s.db")
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1, **kw).run()
    return summary, state


def test_a_run_with_anyone_else_in_its_queue_sends_nothing(tmp_path):
    sender = FakeSender(Allowlist(frozenset({ME})))
    summary, state = _run(tmp_path, sender, [ME, "09120000001", "09120000002"])
    assert (summary.halted, summary.sent, sender.calls) == (True, 0, [])
    assert (summary.stop_reason, summary.stop_fields) == ("recipients_not_allowed", {"count": 2})
    assert sender.account_calls == 0  # refused first: no account check, no links
    assert state.counts() == {PENDING: 3}  # nothing claimed; still to send once it's lifted


def test_a_run_to_allowed_numbers_goes_out(tmp_path):
    sender = FakeSender(Allowlist(frozenset({ME, "09120000001"})))
    summary, _ = _run(tmp_path, sender, [ME, "09120000001"], preflight=False)
    assert (summary.halted, summary.sent) == (False, 2)
    assert sorted(sender.calls) == sorted([ME, "09120000001"])


def test_a_test_number_outside_it_is_refused(tmp_path):
    sender = FakeSender(Allowlist(frozenset({ME})))
    summary, _ = _run(tmp_path, sender, [ME], approval_test_number="09120000009",
                      approval_prompt=lambda *_: True)
    assert (summary.halted, summary.stop_reason, sender.calls) == (True, "test_number_not_allowed", [])


def test_a_test_only_run_checks_just_the_test_number(tmp_path):
    """The dashboard's test SMS for a real list: only the test number gets
    an SMS, so only it has to be allowed. The send is refused later."""
    sender = FakeSender(Allowlist(frozenset({ME})))
    summary, _ = _run(tmp_path, sender, ["09120000001", "09120000002"], approval_test_number=ME,
                      test_only=True, preflight=False)
    assert (summary.halted, summary.stop_reason, sender.calls) == (False, None, [ME])


def test_a_wrongly_set_rule_refuses_every_run(tmp_path):
    sender = FakeSender(Allowlist(frozenset({ME}), invalid=1))
    summary, _ = _run(tmp_path, sender, [ME])
    assert (summary.halted, summary.stop_reason, sender.calls) == (True, "allowlist_invalid", [])


def test_preview_send_refuses_another_number(monkeypatch):
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    monkeypatch.setattr(cli_module, "load_api_key", lambda: "k")
    monkeypatch.setattr(Sender, "send", lambda *a, **k: pytest.fail("nothing may be sent"))
    result = CliRunner().invoke(cli, ["preview", "--phone", "09120000001", "--template", "t",
                                      "--token", "1", "--send"])
    assert result.exit_code == 2
    assert "restricted sending" in result.output and "Nothing was sent" in result.output
