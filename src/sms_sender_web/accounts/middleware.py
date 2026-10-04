from urllib.parse import quote

from django.shortcuts import redirect
from django.urls import reverse
from django_otp import user_has_device

from .roles import needs_two_factor

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
        return redirect(f"{reverse(step)}?next={quote(request.get_full_path())}")
