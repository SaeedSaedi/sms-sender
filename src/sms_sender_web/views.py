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
    """Every page knows whether this is the sandbox, whether it runs on your
    Mac, whether all sending is held and whether sending is restricted to
    allowed numbers, for their badge and banners."""
    from sms_sender.allowlist import allowlist

    from .jobs.services import held

    signed_in = getattr(request, "user", None) is not None and request.user.is_authenticated
    return {
        "sandbox": settings.SANDBOX,
        "local": settings.LOCAL,
        "sending_held": held() if signed_in else None,
        # The sandbox sends nothing, so the restriction doesn't bind it.
        "allowlist": allowlist() if signed_in and not settings.SANDBOX else None,
    }


# Which menu entry a page belongs to, by its URL name.
_SECTIONS = {
    "home": "control", "campaign_list": "campaigns", "campaign_new": "campaigns", "campaign_detail": "campaigns",
    "campaign_settings": "campaigns", "campaign_check": "campaigns", "report": "campaigns",
    "segment_list": "segments", "segment_upload": "segments", "segment_detail": "segments",
    "segment_map": "segments", "suppression": "suppression", "status": "status",
    "users": "users", "audit_log": "activity", "my_account": "account", "password_change": "account",
    "template_list": "templates", "template_new": "templates", "template_edit": "templates",
    "analytics": "analytics", "campaign_duplicate": "campaigns", "api_tokens": "api", "system_settings": "system", "backups": "backups",
    "help": "help", "campaign_import": "campaigns", "number_lookup": "numbers",
    "compose_start": "compose", "compose": "compose", "compose_campaign": "compose",
    "preset_list": "presets", "preset_new": "presets", "preset_edit": "presets",
}


def navigation(request) -> dict:
    """The menu marks the section of the page you're on."""
    match = getattr(request, "resolver_match", None)
    return {"nav_section": _SECTIONS.get(match.url_name if match else None, "")}
