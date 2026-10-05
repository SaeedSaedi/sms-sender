"""The reports' words (Persian in the catalog)."""
from django.utils.translation import gettext_lazy as _

FUNNEL_LABELS = {
    "accepted": _("Accepted"),
    "delivered": _("Delivered"),
    "clicked": _("Clicked"),
    "converted": _("Converted"),
}
CHART_BY_HOUR = _("{total} clicks in all; the most in one hour: {peak}, at {when}. The earliest is on the right.")
CHART_BY_DAY = _("{total} clicks in all; the most in one day: {peak}, on {when}. The earliest is on the right.")

# Ready-made audiences: each a filter of the recipients list.
AUDIENCES = (
    ("clicked", _("Clicked"), {"status": "sent", "clicked": "yes"}),
    ("not_clicked", _("Didn't click"), {"status": "sent", "clicked": "no"}),
    ("delivered_not_clicked", _("Delivered, didn't click"), {"delivery": "delivered", "clicked": "no"}),
    ("not_delivered", _("Not delivered"), {"delivery": "not_delivered"}),
    ("rejected", _("Rejected"), {"status": "failed_permanent"}),
)
AUDIENCE_NAME = _("From the report of %(campaign)s")
AUDIENCE_HINT = _("{n} recipients become a new segment, with their user IDs and, where their list's file still has them, its token columns.")
AUDIENCE_DONE = _("A segment of {n} recipients is ready. It can be used by a new campaign.")
CONVERSIONS_IMPORTED = _("{added} conversions added ({by_ref} by r, {by_user_id} by user ID). Not matched to anyone sent: {unmatched}; already imported: {duplicates}; unusable rows: {invalid}.")
