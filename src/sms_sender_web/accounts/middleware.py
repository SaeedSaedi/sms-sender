from urllib.parse import quote, urlsplit

from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.http import HttpResponse
from django.shortcuts import redirect, resolve_url
from django.urls import reverse
from django_otp import user_has_device

from .models import must_change_password
from .roles import needs_two_factor


def _shown_page(request) -> str:
    """The page in the browser: for an HTMX request (a live update), the page
    that made it, not the fragment's own URL."""
    current = request.headers.get("HX-Current-URL")
    if current:
        parts = urlsplit(current)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return request.get_full_path()


def _redirect(request, url: str):
    """A redirect the browser follows as a whole page. For HTMX, HX-Redirect:
    a plain one would swap the target page into the part being updated."""
    if request.headers.get("HX-Request"):
        response = HttpResponse()
        response["HX-Redirect"] = url
        return response
    return redirect(url)


class LoginRequired(LoginRequiredMiddleware):
    """Django's LoginRequiredMiddleware, except that a live update from a
    session that has ended sends the whole page to the login."""

    def handle_no_permission(self, request, view_func):
        if not request.headers.get("HX-Request"):
            return super().handle_no_permission(request, view_func)
        login = resolve_url(self.get_login_url(view_func))
        field = self.get_redirect_field_name(view_func)
        return _redirect(request, f"{login}?{field}={quote(_shown_page(request))}")

# Pages reachable before the second step: signing in and out, the 2FA pages
# themselves, and the health check.
EXEMPT = {"login", "logout", "healthz", "two_factor", "two_factor_setup"}


class TwoFactorMiddleware:
    """Operators and admins confirm a code from their authenticator app after
    the password (spec 4.12). Until this session has, every other page leads
    to that step — to set the app up first if they have none yet."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if not user.is_authenticated or user.is_verified() or not needs_two_factor(user):
            return None
        match = request.resolver_match
        if match is not None and match.url_name in EXEMPT:
            return None
        step = "two_factor" if user_has_device(user, confirmed=True) else "two_factor_setup"
        return _redirect(request, f"{reverse(step)}?next={quote(_shown_page(request))}")


class PasswordChangeMiddleware:
    """An admin asked this person to choose their own password (a new
    account, or a reset one): after the second step, every page leads to the
    password change until they do."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if not user.is_authenticated or (needs_two_factor(user) and not user.is_verified()):
            return None
        match = request.resolver_match
        if match is not None and match.url_name in EXEMPT | {"password_change"}:
            return None
        if not must_change_password(user):
            return None
        return _redirect(request, reverse("password_change"))
