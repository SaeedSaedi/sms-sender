"""Role names, worded as in the spec's glossary (4.11)."""
from django.utils.translation import gettext_lazy as _

from .roles import ADMIN, OPERATOR, VIEWER

ROLE_LABELS = {
    VIEWER: _("Viewer"),
    OPERATOR: _("Operator"),
    ADMIN: _("System admin"),
    None: _("No role"),
}
