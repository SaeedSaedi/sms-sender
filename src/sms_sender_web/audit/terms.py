"""What the activity page calls each recorded action, and how it
describes the details — in Persian, never the raw key=value."""
from django.utils.html import format_html
from django.utils.translation import gettext as gettext_now
from django.utils.translation import gettext_lazy as _

from ..accounts.terms import ROLE_LABELS

ACTION_LABELS = {
    "login": _("Signed in"),
    "logout": _("Signed out"),
    "login_failed": _("Failed sign-in"),
    "2fa_enrolled": _("Two-step verification set up"),
    "2fa_verified": _("Two-step code confirmed"),
    "2fa_failed": _("Wrong two-step code"),
    "2fa_reset": _("Two-step verification reset"),
    "user_created": _("User created"),
    "role_changed": _("Role changed"),
    "user_deactivated": _("Account deactivated"),
    "user_activated": _("Account activated"),
    "password_changed": _("Password changed"),
}


def _who(name: str):
    return format_html('<bdi dir="ltr">{}</bdi>', name)


def describe(event) -> str:
    """One short Persian line about an event's details ("" when none)."""
    d = event.detail or {}
    target = d.get("target")
    if event.action == "role_changed":
        return format_html(
            gettext_now("{who}: from {before} to {after}"), who=_who(target),
            before=ROLE_LABELS.get(d.get("before"), ROLE_LABELS[None]),
            after=ROLE_LABELS.get(d.get("after"), ROLE_LABELS[None]),
        )
    if event.action == "user_created":
        return format_html(
            gettext_now("{who}, role: {role}"), who=_who(target),
            role=ROLE_LABELS.get(d.get("role"), ROLE_LABELS[None]),
        )
    if event.action in ("2fa_reset", "user_activated", "user_deactivated"):
        return _who(target)
    if event.action == "2fa_failed":
        when = gettext_now("while setting up") if d.get("during") == "setup" else gettext_now("while signing in")
        if d.get("throttled"):
            return format_html("{}، {}", when, gettext_now("blocked after repeated wrong codes"))
        return when
    return ""
