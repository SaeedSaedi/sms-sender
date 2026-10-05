from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@login_not_required
@require_GET
def healthz(request):
    """For Docker and DevOps health checks: the app and its database answer.
    Says nothing else, so it can stay open without a login."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


def sandbox(request) -> dict:
    """Every page knows whether this is the sandbox, for its banner."""
    return {"sandbox": settings.SANDBOX}


# Which menu entry a page belongs to, by its URL name.
_SECTIONS = {
    "home": "campaigns", "campaign_new": "campaigns", "campaign_detail": "campaigns",
    "campaign_settings": "campaigns", "campaign_check": "campaigns", "report": "campaigns",
    "segment_list": "segments", "segment_upload": "segments", "segment_detail": "segments",
    "segment_map": "segments", "suppression": "suppression", "status": "status",
    "users": "users", "audit_log": "activity", "my_account": "account", "password_change": "account",
    "template_list": "templates", "template_new": "templates", "template_edit": "templates",
    "analytics": "analytics", "campaign_duplicate": "campaigns",
}


def navigation(request) -> dict:
    """The menu marks the section of the page you're on."""
    match = getattr(request, "resolver_match", None)
    return {"nav_section": _SECTIONS.get(match.url_name if match else None, "")}
