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

# How a status reads at a glance (the overview's bars and legends): done,
# on its way, needs a look, failed, or left out on purpose.
STATUS_TONE = {
    "sent": "success",
    "pending": "info",
    "in_flight": "info",
    "failed_retriable": "warning",
    "unknown": "warning",
    "needs_review": "warning",
    "capped": "warning",
    "failed_permanent": "danger",
    "invalid": "danger",
    "suppressed": "neutral",
    "cancelled": "neutral",
}

# The campaign list's tabs.
LIST_VIEWS = {
    "all": _("All"),
    "active": _("In progress"),
    "attention": _("Needs attention"),
    "finished": _("Ended"),
}


# The control room (plan 06, L4).
TIME_LEFT = {
    "under_a_minute": _("under a minute left"),
    "minutes": _("about {m} minutes left"),
    "hours": _("about {h} h {m} min left"),
}
RUNWAY = {
    "both": _("At the recent pace, enough for about {sends} more sends, or {days} days."),
    "sends": _("At the recent pace, enough for about {sends} more sends."),
    "days": _("At the recent pace, enough for about {days} days."),
}
# The header's line about today.
TODAY_LINE = _("{date} · today {sends} sends, {active} on their way")
# The control room's figures: today's, with the last 7 days under each.
FIGURES = {
    "accepted": _("SMS accepted · today"),
    "delivered": _("Delivered · 7 days"),
    "clicks": _("Clicks · today"),
    "click_rate": _("Click rate · 7 days"),
    "spend": _("Spent · today"),
    "week": _("7 days: {n}"),
    "week_rials": _("7 days: {n} rials"),
    "rials": _("{n} rials"),
    "reports": _("of {n} with a delivery report"),
    "no_reports": _("No delivery report yet"),
    "clicked": _("{n} of {of} clicked their own link"),
    "no_links": _("No links of their own yet"),
}
WEEK_DAY = _("{day}: {sent} sent, {later} set for later")
PACE = _("{n} per second")
