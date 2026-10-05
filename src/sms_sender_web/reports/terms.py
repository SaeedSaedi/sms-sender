"""The reports' words (Persian in the catalog)."""
from django.utils.translation import gettext_lazy as _

FUNNEL_LABELS = {
    "accepted": _("Accepted"),
    "delivered": _("Delivered"),
    "clicked": _("Clicked"),
}
CHART_BY_HOUR = _("{total} clicks in all; the most in one hour: {peak}, at {when}. The earliest is on the right.")
CHART_BY_DAY = _("{total} clicks in all; the most in one day: {peak}, on {when}. The earliest is on the right.")
