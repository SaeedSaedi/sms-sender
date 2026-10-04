from django import template

from .. import roles
from ..terms import ROLE_LABELS

register = template.Library()


@register.filter
def can(user, capability: str) -> bool:
    """`{% if user|can:"manage_users" %}`"""
    return roles.can(user, capability)


@register.filter
def role(user):
    return roles.role_of(user)


@register.filter
def role_label(role) -> str:
    return ROLE_LABELS.get(role, ROLE_LABELS[None])


@register.filter
def fully_signed_in(user) -> bool:
    """Signed in, and through the second step when the role has one."""
    if not getattr(user, "is_authenticated", False):
        return False
    return not roles.needs_two_factor(user) or user.is_verified()
