"""The three roles and what each may do (decided 2026-10-04, spec 4.12).

A role is a Django group with the role's name. Each role includes
everything the role before it can do. Superusers count as admins. A
user in no role group can sign in, but sees nothing until an admin gives
them a role.
"""
from __future__ import annotations

from django.contrib.auth.models import Group

VIEWER, OPERATOR, ADMIN = "viewer", "operator", "admin"
ROLES = (VIEWER, OPERATOR, ADMIN)  # lowest first

# What each role adds to the one before it (the spec's permission matrix).
_ADDS = {
    VIEWER: {
        "view_campaigns",       # campaigns, reports, delivery, clicks (phones masked)
        "download_aggregates",  # reports without personal data
    },
    OPERATOR: {
        "reveal_phone",         # one full number at a time, audited
        "edit_campaigns",       # upload segments, create and edit campaigns
        "run_campaigns",        # stages, the test SMS, start / pause / resume / cancel
        "update_campaigns",     # reconcile, delivery, retry not-sent, requeue rejected
        "export_people",        # attribution (no phones) and clickers (phones)
        "add_suppression",
    },
    ADMIN: {
        "requeue_review",       # needs_review → queue again (may send a duplicate)
        "remove_suppression",
        "delete_campaign_data",
        "manage_users",
        "manage_settings",
        "view_audit_log",
    },
}

CAPABILITIES: dict[str, frozenset[str]] = {}
_so_far: set[str] = set()
for _role in ROLES:
    _so_far |= _ADDS[_role]
    CAPABILITIES[_role] = frozenset(_so_far)


def role_of(user) -> str | None:
    """The user's role, or None (signed out, or no role yet). Looked up once
    per user object, so a page can ask as often as it likes."""
    if not getattr(user, "is_authenticated", False):
        return None
    if user.is_superuser:
        return ADMIN
    try:
        return user._sms_role
    except AttributeError:
        pass
    names = set(user.groups.values_list("name", flat=True))
    held = [role for role in ROLES if role in names]
    user._sms_role = held[-1] if held else None
    return user._sms_role


def can(user, capability: str) -> bool:
    role = role_of(user)
    return role is not None and capability in CAPABILITIES[role]


def needs_two_factor(user) -> bool:
    """Operators and admins confirm a code after the password; viewers don't."""
    return role_of(user) in (OPERATOR, ADMIN)


def set_role(user, role: str | None) -> None:
    """Give the user exactly one role (or none)."""
    if role is not None and role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    user.groups.remove(*Group.objects.filter(name__in=ROLES))
    if role is not None:
        # The migration creates the groups; a missing one is made again.
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    try:
        del user._sms_role
    except AttributeError:
        pass
