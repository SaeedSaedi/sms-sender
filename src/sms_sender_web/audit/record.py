"""Write audit events from anywhere: views, services, signal handlers."""
from __future__ import annotations

from .models import AuditEvent

# Every action the dashboard records, in one place (labels: audit/terms.py).
ACTIONS = (
    "login", "logout", "login_failed",
    "2fa_enrolled", "2fa_verified", "2fa_failed", "2fa_reset",
    "user_created", "role_changed", "user_deactivated", "user_activated",
    "password_changed",
    "segment_uploaded", "segment_mapped", "segment_deleted",
    "suppression_added", "suppression_removed",
    "test_number_changed",
    "campaign_created", "campaign_changed",
    "test_requested", "test_approved", "test_rejected", "send_started",
    "job_requested", "job_paused", "job_resumed", "job_cancelled",
    "phone_revealed", "report_downloaded",
)


def record(action: str, *, request=None, user=None, campaign: str = "", **detail) -> AuditEvent:
    """Append one event. `user` defaults to the request's signed-in user;
    pass `username=` instead for someone who isn't signed in (a failed
    sign-in records the name that was tried)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    username = detail.pop("username", None)
    if user is None and username is None and request is not None and request.user.is_authenticated:
        user = request.user
    if user is not None:
        username = user.get_username()
    return AuditEvent.objects.create(
        user=user if getattr(user, "pk", None) else None,
        username=(username or "")[:150],
        action=action,
        campaign=campaign,
        detail=detail,
        ip=(request.META.get("REMOTE_ADDR") or None) if request is not None else None,
    )
