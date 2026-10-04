"""Phase 5: "did we double-send?" — `sms-sender check-sends`, the approval
test filed apart from recipient sends, and an SMS Kavenegar accepted just
before the process stopped is settled from its own call record."""
from __future__ import annotations

import time

from click.testing import CliRunner

from sms_sender.cli import cli
from sms_sender.reconcile import reconcile_unknown
from sms_sender.runner import Runner, _attempt_recorder
from sms_sender.sendcheck import check_sends
from sms_sender.sender import Attempt, ProviderMessage, SendResult
from sms_sender.state import SENT, StateStore

PHONE = "09120000001"
TEST_NUMBER = "09120000077"


class ReportingSender:
    """Accepts every SMS and reports each call, as Sender(on_attempt=…) does."""

    def __init__(self, on_attempt):
        self.on_attempt = on_attempt
        self.next_id = 100

    def send(self, phone, tokens=None):
        self.next_id += 1
        now = time.time()
        self.on_attempt(Attempt(phone=phone, outcome="accepted", started_at=now, finished_at=now,
                                status_code=200, message_id=self.next_id, cost=3020))
        return SendResult(message_id=self.next_id, status_code=200, cost=3020)


class Provider:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.lookups = 0

    def find_messages(self, phone, start, end):
        self.lookups += 1
        return list(self.messages)


def call(state, kind, outcome, message_id=None, phone=PHONE):
    state.record_attempt(phone=phone, kind=kind, outcome=outcome, started_at=time.time(),
                         message_id=message_id)


def test_the_approval_test_is_filed_apart_from_the_recipient_send(tmp_path):
    """The test number may be a recipient too, by design: two SMS, not twice."""
    inp = tmp_path / "in.txt"
    inp.write_text(f"{TEST_NUMBER}\n{PHONE}\n", encoding="utf-8")
    state = StateStore(tmp_path / "s.db")
    summary = Runner(
        input_path=inp, state=state, sender=ReportingSender(_attempt_recorder(state)), workers=2,
        approval_test_number=TEST_NUMBER, approval_prompt=lambda *_: True,
    ).run()
    assert summary.sent == 2
    kinds = sorted((a["kind"], a["outcome"]) for a in state.attempts_for(TEST_NUMBER))
    assert kinds == [("send", "accepted"), ("test", "accepted")]
    result = check_sends(state)
    assert result.ok and (result.sms, result.recipients, result.test_sms) == (2, 2, 1)


def test_an_sms_accepted_just_before_a_crash_is_settled_from_its_record(tmp_path):
    """Kavenegar accepted it and the call was recorded, then the process
    stopped before the row was marked. The lookup can't help — the message
    is already known, so it would be skipped and the row sent again."""
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(PHONE, PHONE)])
    state.claim(PHONE)
    _attempt_recorder(state)(Attempt(phone=PHONE, outcome="accepted", started_at=time.time(),
                                     finished_at=time.time(), status_code=200, message_id=555, cost=3020))
    restarted = StateStore(tmp_path / "s.db")
    assert restarted.mark_orphans_unknown() == 1
    provider = Provider([ProviderMessage(555, 10)])
    result = reconcile_unknown(restarted, provider, min_age_sec=300)  # too young for a lookup, too
    assert (result.sent, result.requeued, provider.lookups) == (1, 0, 0)
    assert restarted.counts() == {SENT: 1} and restarted.list_claimable_phones() == []
    assert restarted.total_cost() == 3020
    check = check_sends(restarted)
    assert check.ok and check.sms == 1


def test_an_earlier_send_does_not_settle_a_later_claim(tmp_path):
    """After `reset --status sent`, an old accepted call isn't proof the new
    claim went out: only calls made after the claim count."""
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(PHONE, PHONE)])
    call(state, "send", "accepted", 555)  # the first SMS; then the row was reset
    time.sleep(0.01)
    state.claim(PHONE)
    state.mark_unknown(PHONE, "outcome unknown: read timed out")
    provider = Provider([ProviderMessage(555, 10)])
    result = reconcile_unknown(state, provider, min_age_sec=0)
    # Asked Kavenegar (near midnight the day is asked in parts), found nothing new.
    assert (result.sent, result.requeued) == (0, 1) and provider.lookups >= 1


def test_two_messages_to_one_phone_are_reported(tmp_path):
    state = StateStore(tmp_path / "s.db")
    # What the old reconciliation could do: found nothing new, requeued, sent again.
    for kind, outcome, mid in (("send", "accepted", 555), ("recovery", "unknown", None),
                               ("reconcile", "reconciled_not_sent", None), ("send", "accepted", 556)):
        call(state, kind, outcome, mid)
    call(state, "send", "accepted", 600, phone="09120000002")
    result = check_sends(state)
    assert not result.ok and result.sms == 3
    assert [(p.phone, p.message_ids) for p in result.twice] == [(PHONE, (555, 556))]

    out = CliRunner().invoke(cli, ["check-sends", "--state", str(tmp_path / "s.db")])
    assert out.exit_code == 1
    assert f"TWICE       {PHONE}: messages 555, 556" in out.output
    assert "1 phone(s) got it twice, 0 may have." in out.output


def test_an_undecided_call_next_to_a_sent_one_may_be_twice(tmp_path):
    state = StateStore(tmp_path / "s.db")
    call(state, "send", "accepted", 555)
    call(state, "send", "unknown")
    result = check_sends(state)
    assert [(p.phone, p.message_ids, p.undecided) for p in result.maybe_twice] == [(PHONE, (555,), 1)]
    out = CliRunner().invoke(cli, ["check-sends", "--state", str(tmp_path / "s.db")])
    assert out.exit_code == 1 and "MAYBE" in out.output


def test_a_lost_reply_settled_by_reconciliation_is_one_sms(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(PHONE, PHONE), ("09120000002", "09120000002")])
    with state._tx() as conn:
        conn.execute("UPDATE recipients SET status=?", (SENT,))
    call(state, "send", "unknown")
    call(state, "recovery", "unknown")  # it stopped before marking the row, too
    call(state, "reconcile", "reconciled_sent", 555)
    call(state, "test", "accepted", 554)
    result = check_sends(state)
    assert result.ok and (result.sms, result.test_sms) == (1, 1)
    assert result.unrecorded == 1  # 09120000002: sent before calls were recorded

    out = CliRunner().invoke(cli, ["check-sends", "--state", str(tmp_path / "s.db")])
    assert out.exit_code == 0, out.output
    assert "OK: nobody got this campaign twice." in out.output
    assert "unrecorded  1 sent row(s)" in out.output


def test_check_sends_never_creates_a_db(tmp_path):
    out = CliRunner().invoke(cli, ["check-sends", "--state", str(tmp_path / "none.db")])
    assert out.exit_code == 2 and not (tmp_path / "none.db").exists()
