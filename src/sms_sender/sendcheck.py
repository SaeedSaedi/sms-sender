"""Did anyone get the campaign twice? (`sms-sender check-sends`)

Counts each phone's SMS from the record of every call to Kavenegar (the
`attempts` table), not from the recipient rows: a row only holds its last
send. Approval-test SMS (kind `test`) are counted apart, since the
operator's own number may also be a recipient, by design.

Per phone, in the order the calls were made:
- a `send` that Kavenegar accepted, or a reconciliation that found the
  message, is one SMS (each message ID once);
- a `send` or a `recovery` whose outcome is `unknown` is maybe one SMS,
  until reconciliation decides: found (counted above), not sent, or needs
  review (still maybe). A `recovery` right after an accepted or unknown
  call is that same call: the process stopped before marking the row.

Twice: two or more message IDs. Maybe twice: one or more, plus an
undecided call.

DBs written before sms-sender filed approval-test calls as `test` hold
them as `send`. If the test number was also a recipient there, it shows
as twice, and its first message is the test. Nothing here calls Kavenegar.
"""
from __future__ import annotations

from dataclasses import dataclass

from .state import NEEDS_REVIEW, SENT, UNKNOWN, StateStore


@dataclass(frozen=True)
class PhoneSends:
    phone: str
    message_ids: tuple[int, ...]
    undecided: int  # calls that may have sent an SMS, not settled yet


@dataclass(frozen=True)
class SendCheck:
    sms: int                              # accepted for recipients
    recipients: int                       # phones with at least one
    twice: tuple[PhoneSends, ...]
    maybe_twice: tuple[PhoneSends, ...]
    test_sms: int                         # approval tests, not counted above
    unsettled: int                        # rows still unknown / needs_review
    unrecorded: int                       # sent rows without a call record

    @property
    def ok(self) -> bool:
        return not self.twice and not self.maybe_twice


def _walk(calls: list) -> PhoneSends:
    ids: dict[int, None] = {}  # insertion-ordered set
    undecided = 0
    last = None
    for call in calls:
        kind, outcome, message_id = call["kind"], call["outcome"], call["message_id"]
        if kind == "send":
            if outcome == "accepted" and message_id is not None:
                ids[message_id] = None
            elif outcome == "unknown":
                undecided += 1
            last = outcome
        elif kind == "recovery":
            if last not in ("accepted", "unknown"):
                undecided += 1
            last = "recovery"
        elif kind == "reconcile":
            if outcome == "reconciled_sent" and message_id is not None:
                ids[message_id] = None
            if outcome in ("reconciled_sent", "reconciled_not_sent"):
                undecided = max(undecided - 1, 0)
            last = None
    return PhoneSends(calls[0]["phone"], tuple(ids), undecided)


def check_sends(state: StateStore) -> SendCheck:
    by_phone: dict[str, list] = {}
    for call in state.recipient_calls():
        by_phone.setdefault(call["phone"], []).append(call)
    phones = [_walk(calls) for calls in by_phone.values()]
    recorded = {p.phone for p in phones if p.message_ids}
    counts = state.counts()
    return SendCheck(
        sms=sum(len(p.message_ids) for p in phones),
        recipients=len(recorded),
        twice=tuple(p for p in phones if len(p.message_ids) >= 2),
        maybe_twice=tuple(
            p for p in phones if len(p.message_ids) < 2 and len(p.message_ids) + p.undecided >= 2
        ),
        test_sms=state.test_sms_count(),
        unsettled=counts.get(UNKNOWN, 0) + counts.get(NEEDS_REVIEW, 0),
        unrecorded=len(state.phones_with_status(SENT) - recorded),
    )
