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

# Insights (plan 06, L5).
RIALS = _("{n} rials")
METRIC_LABELS = {
    "delivered_rate": _("Delivered"),
    "click_rate": _("Click rate"),
    "cost_per_sms": _("Cost per SMS"),
    "cost_per_click": _("Cost per click"),
}
# How a figure stands against another: rates in percentage points, costs in percent.
CHANGE = {
    "same": _("about the same"),
    "points_up": _("{n} percentage points higher"),
    "points_down": _("{n} percentage points lower"),
    "dearer": _("{n}% dearer"),
    "cheaper": _("{n}% cheaper"),
}
TREND = _("{what}: the latest {n} alerts {now}, the ones before them {before} ({change}).")
WITHIN_HOURS = _("Within {h} hours")
FATIGUE_ROWS = (_("1 SMS"), _("2 SMS"), _("3 SMS"), _("4 SMS"), _("5 SMS or more"))
TOO_MANY_SEGMENTS = _("Choose at most {n} segments: the first {n} are compared.")
FATIGUE_LINE = _("In the last 7 days {p7} people got {s7} SMS; in the last 30 days, {p30} people got {s30}.")
CAP_LINE = _("Frequency cap: at most {sms} SMS in {days} days. {n} people are at it now, so a send today would hold them back.")
BEST_LINE = _("SMS sent at {hour} were clicked the most: {rate} of {n} people.")
BUSIEST_LINE = _("People click the most at {hour}: {n} clicks over every campaign.")
HOURS_HINT = _("Only hours with at least {n} people sent a link of their own are compared, and the sending window limits which hours have been tried. Clicks are counted by the hour, give or take half an hour.")
CHOOSE_SEGMENTS = _("Choose 2 to {n} segments")
OVERLAP_LINE = _("Counted once, they hold {once} numbers; {several} are in more than one of them.")
VERSUS_LINE = _("Against the other {n} alerts of «{preset}» that sent, all their SMS together.")
ALERT_COUNT = _("{n} alerts")
