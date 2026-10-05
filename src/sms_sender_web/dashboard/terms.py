"""The dashboard's names for statuses: one place, translated in the `fa`
catalog, and worded as in the spec's glossary (4.11). «اپراتور» is only ever
the dashboard role; the mobile network is «مخابرات»."""
from django.utils.translation import gettext_lazy as _

# Submission status (the CLI's names) → label, in the order pages show them.
SUBMISSION_STATUS = {
    "pending": _("Queued"),
    "in_flight": _("Sending"),
    "sent": _("Accepted"),
    "failed_retriable": _("Not sent"),
    "failed_permanent": _("Rejected"),
    "unknown": _("Unknown"),
    "needs_review": _("Needs review"),
    "suppressed": _("On the suppression list"),
    "invalid": _("Invalid"),
    "cancelled": _("Cancelled"),
    "capped": _("Over the frequency cap"),
}
STATUS_ORDER = tuple(SUBMISSION_STATUS)

# Kavenegar delivery status codes (sms/status) → label.
DELIVERY_STATUS = {
    None: _("Not checked"),
    1: _("Queued at Kavenegar"),
    2: _("Queued at Kavenegar"),
    4: _("Sent to the network"),
    5: _("Sent to the network"),
    6: _("Failed"),
    10: _("Delivered"),
    11: _("Undelivered"),
    13: _("Failed"),
    14: _("Blocked"),
    100: _("Beyond the status window"),
}

# The recipients list's delivery filter: what it means for the person.
DELIVERY_FILTERS = (
    ("delivered", _("Delivered")),
    ("not_delivered", _("Not delivered")),
    ("on_its_way", _("On its way")),
    ("unchecked", _("Not checked")),
    ("expired", _("Beyond the status window")),
)
CLICK_FILTERS = (("yes", _("Clicked")), ("no", _("Didn't click")))
