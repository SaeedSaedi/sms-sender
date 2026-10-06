"""Sign-in's second step, the users page and password change (spec 4.12)."""
from __future__ import annotations

from base64 import b32encode
from datetime import timedelta
from urllib.parse import quote

import segno
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.conf import settings
from django.contrib.auth.views import LoginView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django_otp import login as otp_login
from django_otp import user_has_device
from django_otp.plugins.otp_totp.models import TOTPDevice

from .. import live
from ..audit.models import AuditEvent
from ..audit.record import record
from ..dashboard.templatetags.fa import fa_digits
from .decorators import forbidden, requires
from .forms import CodeForm, NewUserForm, TestPhoneForm
from .models import Profile, ask_to_change_password, must_change_password
from .roles import ROLES, can, needs_two_factor, role_of, set_role


# Decision D5 (plan 06): on your Mac, a sign-in from the Mac itself may last
# 30 days instead of 8 hours. Elsewhere, sessions stay at 8 hours.
REMEMBER_FOR = timedelta(days=30)
LOOPBACK = frozenset({"127.0.0.1", "::1"})


def can_remember(request) -> bool:
    """Running locally (sms-dashboard), and asked from the Mac itself."""
    return settings.LOCAL and request.META.get("REMOTE_ADDR") in LOOPBACK


def remembered(request) -> bool:
    """This sign-in asked to be remembered, and may be."""
    return request.method == "POST" and bool(request.POST.get("remember")) and can_remember(request)


class SignIn(LoginView):
    """Django's sign-in, plus "keep me signed in on this Mac" (D5). The
    second step still follows for operators and admins; once it's passed,
    the session lasts 30 days."""
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "can_remember": can_remember(self.request)}

    def form_valid(self, form):
        response = super().form_valid(form)
        if remembered(self.request):
            self.request.session.set_expiry(REMEMBER_FOR)
        return response


def _next(request) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return target
    return reverse("home")


def _check(request, devices, code: str, during: str) -> TOTPDevice | None:
    """The device the code belongs to, or None after recording why not.
    Wrong codes slow down (django-otp's throttling); a code can't be reused."""
    throttled = False
    for device in devices:
        allowed, _info = device.verify_is_allowed()
        if not allowed:
            throttled = True
            continue
        if device.verify_token(code):
            return device
    record("2fa_failed", request=request, during=during, throttled=throttled)
    if throttled:
        messages.error(request, _("Too many wrong codes. Wait a little, then try the newest code."))
    else:
        messages.error(request, _("The code is wrong or has expired. Enter the newest code from the app."))
    return None


def _passed(request, device) -> None:
    """Mark this session as through the second step, under a new session
    key, so an ID seen before the code was entered no longer works."""
    otp_login(request, device)
    request.session.cycle_key()


@never_cache  # the page shows the secret key
@require_http_methods(["GET", "POST"])
def two_factor_setup(request):
    """First sign-in of an operator or admin: link an authenticator app.
    Only while they have none: a linked app is replaced only by an admin's
    reset, never from a session that has just the password."""
    user = request.user
    if not needs_two_factor(user):
        return redirect(_next(request))
    if user_has_device(user, confirmed=True):
        return redirect(f"{reverse('two_factor')}?next={quote(_next(request))}")
    device = (TOTPDevice.objects.filter(user=user, confirmed=False).first()
              or TOTPDevice.objects.create(user=user, name="authenticator", confirmed=False))
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if _check(request, [device], form.cleaned_data["code"], during="setup"):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            _passed(request, device)
            record("2fa_enrolled", request=request)
            return redirect(_next(request))
    secret = b32encode(device.bin_key).decode()
    return render(request, "accounts/two_factor_setup.html", {
        "form": form,
        # A viewBox and no fixed size (omitsize), so CSS scales the whole code.
        # Without it the stylesheet's smaller box cropped the drawing, cutting
        # off two of the corner squares a camera needs.
        "qr": segno.make(device.config_url, error="m").svg_inline(
            scale=5, dark="#1c1d21", light="#ffffff", omitsize=True,
        ),
        "secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "next": _next(request),
    })


@never_cache
@require_http_methods(["GET", "POST"])
def two_factor(request):
    """Every sign-in of an operator or admin: the code from the app."""
    if not needs_two_factor(request.user):
        return redirect(_next(request))
    devices = list(TOTPDevice.objects.filter(user=request.user, confirmed=True))
    if not devices:
        return redirect(f"{reverse('two_factor_setup')}?next={quote(_next(request))}")
    if request.user.is_verified():
        return redirect(_next(request))
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        device = _check(request, devices, form.cleaned_data["code"], during="sign-in")
        if device is not None:
            _passed(request, device)
            record("2fa_verified", request=request)
            return redirect(_next(request))
    return render(request, "accounts/two_factor.html", {"form": form, "next": _next(request)})


@requires("manage_users")
@require_http_methods(["GET", "POST"])
def users(request):
    User = get_user_model()
    new_user_form = NewUserForm(request.POST if request.POST.get("action") == "create" else None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            if new_user_form.is_valid():
                data = new_user_form.cleaned_data
                created = User.objects.create_user(username=data["username"], password=data["password"])
                set_role(created, data["role"])
                if request.POST.get("must_change") == "on":
                    ask_to_change_password(created)
                record("user_created", request=request, target=created.username, role=data["role"])
                messages.success(request, _(
                    "The user was created. Give them the first password safely; "
                    "they can change it after signing in."
                ))
                return redirect("users")
        else:
            pk = request.POST.get("user", "")
            if not pk.isdigit():
                raise Http404
            target = get_object_or_404(User, pk=pk)
            if target == request.user:
                messages.error(request, _("You can't change your own role or account status."))
            elif action == "role" and not target.is_superuser:  # superusers are admins, set on the server
                role = request.POST.get("role") or None
                before = role_of(target)
                if (role is None or role in ROLES) and role != before:
                    set_role(target, role)
                    record("role_changed", request=request, target=target.username,
                           before=before, after=role)
                    messages.success(request, _("The role was changed."))
            elif action == "reset_2fa":
                TOTPDevice.objects.filter(user=target).delete()
                record("2fa_reset", request=request, target=target.username)
                messages.success(request, _(
                    "Two-step verification was reset. At the next sign-in, they set up their app again."
                ))
            elif action == "reset_password":
                password = request.POST.get("password", "")
                try:
                    validate_password(password, target)
                except ValidationError as error:
                    messages.error(request, " ".join(fa_digits(m) for m in error.messages))
                    return redirect("users")
                target.set_password(password)  # their open sessions end with it
                target.save(update_fields=["password"])
                ask_to_change_password(target)
                record("password_reset", request=request, target=target.username)
                messages.success(request, _(
                    "The password was reset. Give it to them safely: at their next sign-in, they choose their own."
                ))
            elif action in ("deactivate", "activate"):
                target.is_active = action == "activate"
                target.save(update_fields=["is_active"])
                record("user_activated" if target.is_active else "user_deactivated",
                       request=request, target=target.username)
                messages.success(request, _("The account status was changed."))
            return redirect("users")

    rows = []
    last_action = dict(
        AuditEvent.objects.filter(user__isnull=False).values("user").annotate(at=Max("at")).values_list("user", "at")
    )
    for person in User.objects.order_by("username"):
        rows.append({
            "user": person,
            "role": role_of(person),
            "needs_two_factor": needs_two_factor(person),
            "two_factor": TOTPDevice.objects.filter(user=person, confirmed=True).exists(),
            "is_me": person == request.user,
            "last_action": last_action.get(person.pk),
            "must_change": must_change_password(person),
        })
    return render(request, "accounts/users.html", {
        "rows": rows, "roles": ROLES, "form": new_user_form,
    })


@require_http_methods(["GET", "POST"])
def my_account(request):
    """Your role, your two-step verification, and your own number for test
    SMS (spec 3: the test SMS goes to the operator who asks for it). Only
    people who can ask for a test SMS see that number."""
    profile, _created = Profile.objects.get_or_create(user=request.user)
    sends_tests = can(request.user, "run_campaigns")
    if request.method == "POST" and not sends_tests:
        return forbidden(request)
    form = TestPhoneForm(request.POST or None, initial={"test_phone": profile.test_phone})
    if live.validating(request):  # as someone types: the errors, nothing saved
        return live.errors(form)
    if request.method == "POST" and form.is_valid():
        phone = form.cleaned_data["test_phone"]
        if phone != profile.test_phone:
            profile.test_phone = phone
            profile.save(update_fields=["test_phone"])
            record("test_number_changed", request=request, phone=phone)
        messages.success(request, _("Saved."))
        return redirect("my_account")
    return render(request, "accounts/account.html", {
        "form": form,
        "sends_tests": sends_tests,
        "profile": profile,
        "role": role_of(request.user),
        "two_factor": TOTPDevice.objects.filter(user=request.user, confirmed=True).exists(),
    })


class PasswordChange(PasswordChangeView):
    """Keeps this session signed in (and through the second step); the
    user's other sessions end."""
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("home")

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "required": must_change_password(self.request.user)}

    def form_valid(self, form):
        response = super().form_valid(form)
        ask_to_change_password(self.request.user, ask=False)
        record("password_changed", request=self.request)
        messages.success(self.request, _("Your password was changed."))
        return response
