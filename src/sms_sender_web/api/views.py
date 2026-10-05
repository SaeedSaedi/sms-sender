"""The attribution API (plan 05, decision 8), read only, for the company's
backend over NetBird: which recipient each link's `r` belongs to, by user
ID, with delivery and clicks — never a phone number. Every call needs an
admin-issued token and is recorded. And the admins' page that issues and
revokes the tokens."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from sms_sender.clicks import ATTRIBUTION_HEADER, attribution_rows
from sms_sender.input_loader import SLUG_RE
from sms_sender.state import StateStore

from ..accounts.decorators import requires
from ..audit.record import record
from ..jobs.models import Campaign
from ..text import persian_text
from . import tokens
from .models import ApiToken

PAGE = 1000


def _error(status: int, code: str) -> JsonResponse:
    response = JsonResponse({"error": code}, status=status)
    if status == 401:
        response["WWW-Authenticate"] = 'Bearer realm="sms-sender"'
    return response


def api_view(view):
    """No session or CSRF: a bearer token instead, checked on every call."""
    @login_not_required
    @csrf_exempt
    @never_cache
    @require_GET
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        token = tokens.authenticate(request.headers.get("Authorization", ""))
        if token is None:
            return _error(401, "unauthorized")
        return view(request, token, *args, **kwargs)
    return wrapper


def _db(slug: str) -> Path | None:
    path = Path(django_settings.SMS_SENDER_DB_DIR) / f"{slug}.db"
    return path if SLUG_RE.match(slug) and path.exists() else None


def _iso(moment: float | None) -> str | None:
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat(timespec="seconds") if moment else None


@api_view
def campaigns(request, token: ApiToken):
    names = dict(Campaign.objects.values_list("slug", "name"))
    out = []
    for path in sorted(Path(django_settings.SMS_SENDER_DB_DIR).glob("*.db")):
        if not SLUG_RE.match(path.stem):
            continue
        store = StateStore(path)
        out.append({
            "slug": path.stem, "name": names.get(path.stem) or store.get_meta("campaign") or path.stem,
            "accepted": store.display_counts().get("sent", 0), "last_accepted_at": _iso(store.last_sent_at()),
        })
    record("api_called", request=request, username=f"api:{token.name}", path=request.path, rows=len(out))
    return JsonResponse({"campaigns": out})


@api_view
def attribution(request, token: ApiToken, slug: str):
    path = _db(slug)
    if path is None:
        return _error(404, "not_found")
    rows = list(attribution_rows(StateStore(path)))
    pages = max(1, -(-len(rows) // PAGE))
    page = request.GET.get("page", "1")
    page = min(max(1, int(page)), pages) if page.isdigit() else 1
    chunk = rows[(page - 1) * PAGE:page * PAGE]
    record("api_called", request=request, username=f"api:{token.name}", campaign=slug, path=request.path,
           page=page, rows=len(chunk))
    return JsonResponse({
        "campaign": slug, "columns": ATTRIBUTION_HEADER,
        "rows": chunk, "page": page, "pages": pages, "total": len(rows),
    })


# ---------- the admins' page ----------

@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def token_list(request):
    shown = None
    if request.method == "POST":
        name = persian_text(request.POST.get("name", "").strip())[:100]
        if not name:
            messages.error(request, _("Name the system that will use the token."))
        else:
            token, shown = tokens.issue(name, request.user)
            record("api_token_created", request=request, name=name, prefix=token.prefix)
    return render(request, "api/tokens.html", {"tokens": ApiToken.objects.select_related("created_by"),
                                               "shown": shown})


@requires("manage_settings")
@require_POST
def token_revoke(request, pk: int):
    token = get_object_or_404(ApiToken, pk=pk)
    tokens.revoke(token)
    record("api_token_revoked", request=request, name=token.name, prefix=token.prefix)
    messages.success(request, _("The token is revoked. Calls with it are refused from now on."))
    return redirect("api_tokens")
