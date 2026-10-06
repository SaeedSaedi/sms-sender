"""The in-app help (plan 05, P6): one short page, in the glossary's own words
(spec 4.11). Each status, operation and role is explained under the very
label the other pages show it with, so the help never names a thing twice.
A status or a delivery status without an explanation fails a test."""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..accounts.roles import ADMIN, OPERATOR, VIEWER
from ..accounts.terms import ROLE_LABELS
from ..campaigns.terms import STEPS
from .terms import DELIVERY_STATUS, SUBMISSION_STATUS

# What each of the campaign page's five steps (campaigns.terms.STEPS) is for.
STEP_HELP = (
    _("Choose the segment (or several, for one send), the template and what fills its tokens."),
    _("The check reads the list as a send would: the valid numbers, the template, the tokens, the short link and the sending window. The preview shows each recipient's final text."),
    _("One SMS to your own number, set on “My account”, and to the team's numbers a system admin keeps, with the final text and link. Check it on your phone, then approve it or reject it. When a system admin asks for it, someone other than whoever asked for the test approves it."),
    _("A send needs a test SMS approved for these exact settings. It starts now or at a set time: the short links are made first, then the SMS go out inside the sending window."),
    _("What was accepted, delivered and clicked, and what's left to do. Delivery is checked for 48 hours after sending, clicks for 14 days."),
)

# What every send keeps to, whoever starts it. {start} and {end}: the
# default sending window.
RULES = (
    _("Nobody gets the same campaign twice: a number Kavenegar accepted is never sent again, even after a crash or a restart."),
    _("An SMS that may have gone out (“Unknown”) is checked with Kavenegar, never sent again blindly."),
    _("SMS go out only inside the campaign's sending window, Tehran time: {start} to {end} unless the campaign sets another."),
    _("Numbers on the suppression list get no SMS."),
    _("When a system admin sets a frequency cap, nobody gets more campaign SMS than it allows."),
    _("Every short link is made before the first SMS. If one can't be made, nothing is sent."),
    _("Phone numbers are kept for {months} months after a campaign's last send, then removed: its counts stay, and it can't send again."),
    _("A test SMS goes only to your own number and to the team's numbers a system admin keeps. Settings changed after its approval need a new test."),
)

# Submission status (dashboard.terms.SUBMISSION_STATUS) → what it means.
STATUS_HELP = {
    "pending": _("Not sent to Kavenegar yet."),
    "in_flight": _("The request is on its way to Kavenegar."),
    "sent": _("Kavenegar accepted the SMS. That doesn't mean it has reached the phone."),
    "failed_retriable": _("The SMS certainly didn't go out, so sending it again is safe."),
    "failed_permanent": _("Kavenegar rejected the request. It isn't sent again until the cause is fixed."),
    "unknown": _("Kavenegar may have accepted the SMS. It's never sent again by itself: its outcome is checked with Kavenegar."),
    "needs_review": _("Reconciliation couldn't reach a clear answer, so a system admin decides."),
    "suppressed": _("This number is on the suppression list and gets no SMS."),
    "invalid": _("This row's number or data can't be used, for example a number that came with two different user IDs."),
    "cancelled": _("The campaign was cancelled before this recipient was sent."),
    "capped": _("This recipient already got as many campaign SMS as the frequency cap allows. It's checked again at the next send."),
}

# Delivery status: one of each label's codes (dashboard.terms.DELIVERY_STATUS)
# → what it means.
DELIVERY_HELP = {
    None: _("Delivery hasn't been asked of Kavenegar yet."),
    1: _("The SMS is in Kavenegar's sending queue."),
    4: _("The SMS was handed to the network and is on its way to the phone."),
    10: _("The SMS reached the recipient's phone."),
    11: _("The SMS hasn't reached the phone yet. It's checked again for up to 48 hours."),
    6: _("The network or Kavenegar reported the send as failed."),
    14: _("The recipient has blocked this kind of SMS."),
    100: _("Kavenegar reports delivery only for 48 hours after sending."),
}

# The buttons, under their own labels → what each does.
ACTIONS = (
    (_("Test SMS"), _("One SMS to the operator's own number, and to the team's numbers, before the main send, to see the final text.")),
    (_("Pause"), _("No new recipients are sent. Requests already on their way finish and are recorded.")),
    (_("Resume sending"), _("Unknown outcomes are checked with Kavenegar first, then sending continues from where it stopped.")),
    (_("Cancel the campaign"), _("Sending to the remaining recipients is cancelled. SMS Kavenegar has accepted can't be taken back.")),
    (_("Reconcile with Kavenegar"), _("Asks Kavenegar about the SMS whose outcome is unknown. No SMS is sent.")),
    (_("Update delivery statuses"), _("Asks Kavenegar which SMS were delivered. No SMS is sent.")),
    (_("Queue again"), _("Puts the recipients of one status back in the queue for the next send, after a confirmation that names how many. Only a system admin can do it for anyone who may already have the SMS, because they could get it twice.")),
    (_("Import the CLI's profiles"), _("A system admin reads sms-sender.toml, and each profile chosen becomes a draft campaign with its own message and sending settings. Nothing is sent.")),
)

# The campaign stages a send can stop in (campaigns.terms.STAGES) → what to do.
STOPS = (
    (_("Paused"), _("Someone paused the send, or the sending window ended. A send the window paused continues by itself when the window opens again; one a person paused waits for “Resume sending”.")),
    (_("Stopped with an error"), _("Sending stopped because of a problem, such as too little credit or an invalid Kavenegar key. Fix it, then resume: nobody gets the SMS twice.")),
)

ROLE_HELP = {
    VIEWER: _("Sees the campaigns and reports only; part of every phone number is hidden."),
    OPERATOR: _("Makes segments and campaigns, and runs the sends."),
    ADMIN: _("Everything an operator does, and manages the users, the suppression list and the system settings."),
    None: _("Can sign in, but sees nothing until a system admin gives them a role."),
}
TWO_STEP = _("Operators and system admins enter a six-digit code from an authenticator app after the password.")


def steps() -> list[tuple[str, str]]:
    return list(zip(STEPS, STEP_HELP, strict=True))


def statuses() -> list[tuple[str, str, str]]:
    """(status, label, meaning), in the order the pages show statuses."""
    return [(status, label, STATUS_HELP[status]) for status, label in SUBMISSION_STATUS.items()]


def deliveries() -> list[tuple[str, str]]:
    return [(DELIVERY_STATUS[code], meaning) for code, meaning in DELIVERY_HELP.items()]


def roles() -> list[tuple[str, str]]:
    return [(ROLE_LABELS[role], ROLE_HELP[role]) for role in (VIEWER, OPERATOR, ADMIN, None)]
