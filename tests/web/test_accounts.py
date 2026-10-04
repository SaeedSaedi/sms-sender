"""Step 3.4: sign-in with two-step verification for operators and admins,
the three roles, the users page, password change, and the activity log
(spec 4.12). Every page and message is Persian."""
import re

import pytest

pytest.importorskip("django")
pytest.importorskip("django_otp")

from django.contrib.auth.models import Group  # noqa: E402
from django.template.loader import render_to_string  # noqa: E402
from django.test import Client  # noqa: E402
from django_otp.oath import totp  # noqa: E402
from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: E402

from sms_sender_web.accounts.forms import CodeForm  # noqa: E402
from sms_sender_web.accounts.roles import (  # noqa: E402
    ADMIN,
    CAPABILITIES,
    OPERATOR,
    VIEWER,
    can,
    needs_two_factor,
    role_of,
    set_role,
)
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.audit.record import record  # noqa: E402

from .conftest import PASSWORD  # noqa: E402

pytestmark = pytest.mark.django_db

PERSIAN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def code(device: TOTPDevice) -> str:
    """The code the authenticator app shows right now."""
    return f"{totp(device.bin_key, device.step, device.t0, device.digits, device.drift):06d}"


def events(action: str):
    return list(AuditEvent.objects.filter(action=action).order_by("id"))


def fresh(user):
    """The user as stored now (roles are cached on the object)."""
    return type(user).objects.get(pk=user.pk)


# --- Roles -----------------------------------------------------------------


def test_each_role_can_do_everything_the_one_before_it_can():
    assert CAPABILITIES[VIEWER] < CAPABILITIES[OPERATOR] < CAPABILITIES[ADMIN]
    assert "view_campaigns" in CAPABILITIES[VIEWER]
    assert "run_campaigns" not in CAPABILITIES[VIEWER]
    assert "run_campaigns" in CAPABILITIES[OPERATOR]
    assert "manage_users" not in CAPABILITIES[OPERATOR]
    assert {"manage_users", "view_audit_log", "requeue_review"} <= CAPABILITIES[ADMIN]


def test_the_role_groups_are_created_by_the_migration():
    assert set(Group.objects.values_list("name", flat=True)) >= {VIEWER, OPERATOR, ADMIN}


def test_a_user_has_one_role(make_user, django_user_model):
    user = make_user("someone", None)
    assert role_of(user) is None and not can(user, "view_campaigns")
    set_role(user, OPERATOR)
    assert role_of(user) == OPERATOR
    set_role(user, VIEWER)  # replaces the old one
    assert role_of(fresh(user)) == VIEWER
    assert list(fresh(user).groups.values_list("name", flat=True)) == [VIEWER]
    set_role(user, None)
    assert role_of(fresh(user)) is None
    with pytest.raises(ValueError):
        set_role(user, "owner")


def test_the_highest_role_counts_and_superusers_are_admins(make_user, django_user_model):
    user = make_user("both", VIEWER)
    user.groups.add(Group.objects.get(name=OPERATOR))
    assert role_of(fresh(user)) == OPERATOR
    boss = django_user_model.objects.create_superuser("boss", password=PASSWORD)
    assert role_of(boss) == ADMIN and can(boss, "manage_users")


def test_only_operators_and_admins_need_a_second_step(make_user):
    assert not needs_two_factor(make_user("v", VIEWER))
    assert needs_two_factor(make_user("o", OPERATOR))
    assert needs_two_factor(make_user("a", ADMIN))


# --- What each role can open ---------------------------------------------


def test_a_user_without_a_role_sees_why_in_persian(client, make_user):
    client.force_login(make_user("newcomer", None))
    response = client.get("/")
    assert response.status_code == 403
    html = response.content.decode()
    assert "به این صفحه دسترسی ندارید" in html
    assert "هنوز نقشی به حساب شما داده نشده است" in html


def test_a_viewer_sees_campaigns_but_not_users_or_activity(signed_in):
    assert signed_in.get("/").status_code == 200
    for page in ("/users/", "/activity/"):
        response = signed_in.get(page)
        assert response.status_code == 403
        assert "نقش شما اجازه دیدن این صفحه را نمی‌دهد" in response.content.decode()


def test_the_menu_shows_only_what_the_role_can_open(signed_in, client, make_user, verified):
    html = signed_in.get("/").content.decode()
    assert 'href="/users/"' not in html and 'href="/activity/"' not in html
    assert 'href="/account/"' in html
    assert "مشاهده‌گر" in html  # the role, next to the username

    admin = Client()
    verified(admin, make_user("admin1", ADMIN))
    html = admin.get("/").content.decode()
    assert 'href="/users/"' in html and 'href="/activity/"' in html
    assert "مدیر سامانه" in html


# --- Two-step verification -------------------------------------------------


def test_viewers_never_see_the_two_step_pages(signed_in):
    for page in ("/2fa/", "/2fa/setup/"):
        response = signed_in.get(page)
        assert response.status_code == 302 and response["Location"] == "/"
    assert not TOTPDevice.objects.exists()


def test_an_operator_without_an_app_sets_one_up_first(client, make_user):
    operator = make_user("operator1", OPERATOR)
    client.force_login(operator)
    response = client.get("/?status=sent")
    assert response.status_code == 302
    assert response["Location"] == "/2fa/setup/?next=/%3Fstatus%3Dsent"

    page = client.get(response["Location"])
    html = page.content.decode()
    assert page.status_code == 200
    assert "راه‌اندازی تأیید دومرحله‌ای" in html and "<svg" in html
    assert "no-store" in page["Cache-Control"]  # the page shows the secret key
    # The QR code scales with the page: a viewBox, no fixed size (a fixed
    # size inside a smaller box got cropped, and phones couldn't read it).
    svg = re.search(r"<svg[^>]*>", html).group(0)
    assert "viewBox=" in svg and "width=" not in svg and "height=" not in svg
    device = TOTPDevice.objects.get(user=operator)
    assert not device.confirmed
    assert page.context["secret"].replace(" ", "") in html.replace(" ", "")
    # Reloading keeps the same key, so a half-finished scan still works.
    assert client.get("/2fa/setup/").context["secret"] == page.context["secret"]

    # Typed on a Persian keyboard, with a space in the middle.
    typed = code(device).translate(PERSIAN)
    response = client.post("/2fa/setup/?next=/%3Fstatus%3Dsent",
                           {"code": f"{typed[:3]} {typed[3:]}", "next": "/?status=sent"})
    assert response.status_code == 302 and response["Location"] == "/?status=sent"
    device.refresh_from_db()
    assert device.confirmed
    assert client.get("/").status_code == 200
    assert [e.username for e in events("2fa_enrolled")] == ["operator1"]


def test_a_wrong_setup_code_is_refused_and_recorded(client, make_user):
    operator = make_user("operator1", OPERATOR)
    client.force_login(operator)
    client.get("/2fa/setup/")
    response = client.post("/2fa/setup/", {"code": "000000"}, follow=True)
    html = response.content.decode()
    assert "کد نادرست است یا اعتبار آن تمام شده است" in html
    assert not TOTPDevice.objects.get(user=operator).confirmed
    assert client.get("/").status_code == 302
    (failed,) = events("2fa_failed")
    assert failed.detail == {"during": "setup", "throttled": False}


def test_a_code_that_is_not_six_digits_gets_a_persian_message(client, make_user):
    client.force_login(make_user("operator1", OPERATOR))
    html = client.post("/2fa/setup/", {"code": "12ab"}).content.decode()
    assert "کد شش‌رقمی برنامه احراز هویت را وارد کنید" in html
    assert not events("2fa_failed")  # not a wrong code: never checked


@pytest.mark.parametrize("typed", ["123456", " 123 456 ", "۱۲۳۴۵۶", "١٢٣٤٥٦"])
def test_codes_typed_in_any_digits_are_read_as_ascii(typed):
    form = CodeForm({"code": typed})
    assert form.is_valid() and form.cleaned_data["code"] == "123456"


def test_a_linked_app_cannot_be_replaced_with_just_the_password(client, make_user):
    operator = make_user("operator1", OPERATOR)
    TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    client.force_login(operator)
    response = client.get("/2fa/setup/?next=/users/")
    assert response.status_code == 302
    assert response["Location"] == "/2fa/?next=/users/"
    assert TOTPDevice.objects.filter(user=operator).count() == 1


def test_signing_in_takes_the_password_then_the_code(client, make_user):
    operator = make_user("operator1", OPERATOR)
    device = TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    response = client.post("/login/", {"username": "operator1", "password": PASSWORD})
    assert response["Location"] == "/"
    response = client.get("/")
    assert response.status_code == 302 and response["Location"] == "/2fa/?next=/"

    html = client.get("/2fa/?next=/").content.decode()
    assert "تأیید دومرحله‌ای" in html
    assert 'href="/account/"' not in html  # no menu before the second step
    session_before = client.cookies["sessionid"].value
    response = client.post("/2fa/?next=/", {"code": code(device), "next": "/"})
    assert response.status_code == 302 and response["Location"] == "/"
    assert client.cookies["sessionid"].value != session_before  # a new session key
    assert client.get("/").status_code == 200
    assert [e.action for e in AuditEvent.objects.order_by("id")] == ["login", "2fa_verified"]


def test_a_code_works_only_once(client, make_user):
    operator = make_user("operator1", OPERATOR)
    device = TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    used = code(device)
    client.force_login(operator)
    assert client.post("/2fa/", {"code": used})["Location"] == "/"
    client.post("/logout/")

    client.force_login(operator)
    html = client.post("/2fa/", {"code": used}, follow=True).content.decode()
    assert "کد نادرست است یا اعتبار آن تمام شده است" in html
    assert client.get("/").status_code == 302


def test_too_many_wrong_codes_slow_the_next_try_down(client, make_user):
    from django.utils import timezone

    operator = make_user("operator1", OPERATOR)
    device = TOTPDevice.objects.create(
        user=operator, name="authenticator", confirmed=True,
        throttling_failure_count=3, throttling_failure_timestamp=timezone.now(),
    )
    client.force_login(operator)
    html = client.post("/2fa/", {"code": code(device)}, follow=True).content.decode()
    assert "چند بار کد نادرست وارد شده است" in html
    assert client.get("/").status_code == 302
    assert events("2fa_failed")[-1].detail == {"during": "sign-in", "throttled": True}


def test_after_the_code_only_pages_on_this_site_are_followed(client, make_user):
    operator = make_user("operator1", OPERATOR)
    device = TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    client.force_login(operator)
    response = client.post("/2fa/", {"code": code(device), "next": "https://evil.example/"})
    assert response["Location"] == "/"


def test_before_the_code_only_signing_out_and_the_health_check_work(client, make_user):
    operator = make_user("operator1", OPERATOR)
    TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    client.force_login(operator)
    for page in ("/password/", "/users/", "/activity/"):
        assert client.get(page)["Location"].startswith("/2fa/?next=")
    assert client.get("/healthz").status_code == 200
    assert client.post("/logout/")["Location"] == "/login/"


def test_the_two_step_pages_need_the_password_first(client):
    for page in ("/2fa/", "/2fa/setup/"):
        assert client.get(page)["Location"].startswith("/login/")


# --- Users page (admins) -----------------------------------------------------


@pytest.fixture
def admin(make_user):
    return make_user("admin1", ADMIN)


@pytest.fixture
def admin_client(client, admin, verified):
    verified(client, admin)
    return client


def test_only_admins_open_the_users_page(make_user, verified, admin_client):
    operator_client = Client()
    verified(operator_client, make_user("operator1", OPERATOR))
    assert operator_client.get("/users/").status_code == 403
    html = admin_client.get("/users/").content.decode()
    assert "کاربران" in html and '<bdi dir="ltr">admin1</bdi>' in html


def test_an_admin_creates_a_user_with_a_role(admin_client, django_user_model):
    response = admin_client.post("/users/", {
        "action": "create", "username": "new.operator", "password": "a-fresh-long-password-7",
        "role": OPERATOR,
    }, follow=True)
    assert "کاربر ساخته شد" in response.content.decode()
    created = django_user_model.objects.get(username="new.operator")
    assert created.check_password("a-fresh-long-password-7")
    assert role_of(created) == OPERATOR
    (event,) = events("user_created")
    assert event.username == "admin1"
    assert event.detail == {"target": "new.operator", "role": OPERATOR}


def test_password_rules_and_taken_names_are_explained_in_persian(admin_client, viewer):
    html = admin_client.post("/users/", {
        "action": "create", "username": "short.one", "password": "short", "role": VIEWER,
    }).content.decode()
    assert "این گذرواژه کوتاه است؛ باید دست‌کم ۱۲ کاراکتر داشته باشد" in html
    assert "رمز عبور" not in html  # Django's own wording, which the glossary replaces

    html = admin_client.post("/users/", {
        "action": "create", "username": "VIEWER1", "password": "a-fresh-long-password-7", "role": VIEWER,
    }).content.decode()
    assert "این نام کاربری قبلاً ثبت شده است" in html
    assert not events("user_created")


def test_an_admin_changes_a_role(admin_client, viewer):
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": OPERATOR})
    assert role_of(fresh(viewer)) == OPERATOR
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": ""})
    assert role_of(fresh(viewer)) is None
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": "owner"})
    assert role_of(fresh(viewer)) is None
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": ""})  # unchanged
    assert [e.detail for e in events("role_changed")] == [
        {"target": "viewer1", "before": VIEWER, "after": OPERATOR},
        {"target": "viewer1", "before": OPERATOR, "after": None},
    ]


def test_a_new_operator_sets_up_the_app_at_their_next_page(admin_client, viewer):
    viewer_client = Client()
    viewer_client.force_login(viewer)
    assert viewer_client.get("/").status_code == 200
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": OPERATOR})
    assert viewer_client.get("/")["Location"] == "/2fa/setup/?next=/"


def test_an_admin_resets_two_step_verification(admin_client, make_user, verified):
    operator = make_user("operator1", OPERATOR)
    operator_client = Client()
    verified(operator_client, operator)
    assert operator_client.get("/").status_code == 200

    html = admin_client.post("/users/", {"action": "reset_2fa", "user": operator.pk}, follow=True)
    assert "تأیید دومرحله‌ای بازنشانی شد" in html.content.decode()
    assert not TOTPDevice.objects.filter(user=operator).exists()
    # Their open session goes back to setting the app up.
    assert operator_client.get("/")["Location"] == "/2fa/setup/?next=/"
    assert events("2fa_reset")[0].detail == {"target": "operator1"}


def test_a_deactivated_account_is_signed_out_at_once(admin_client, viewer):
    viewer_client = Client()
    viewer_client.force_login(viewer)
    admin_client.post("/users/", {"action": "deactivate", "user": viewer.pk})
    assert not fresh(viewer).is_active
    assert viewer_client.get("/")["Location"].startswith("/login/")
    response = Client().post("/login/", {"username": "viewer1", "password": PASSWORD})
    assert response.status_code == 200  # refused

    admin_client.post("/users/", {"action": "activate", "user": viewer.pk})
    assert fresh(viewer).is_active
    assert [e.action for e in AuditEvent.objects.filter(action__startswith="user_")] == [
        "user_activated", "user_deactivated",  # newest first
    ]


def test_admins_cannot_change_their_own_account(admin_client, admin):
    for action in ({"action": "role", "role": VIEWER}, {"action": "deactivate"}, {"action": "reset_2fa"}):
        response = admin_client.post("/users/", {**action, "user": admin.pk}, follow=True)
        assert "نقش یا وضعیت حساب خودتان را نمی‌توانید تغییر دهید" in response.content.decode()
    admin = fresh(admin)
    assert role_of(admin) == ADMIN and admin.is_active
    assert TOTPDevice.objects.filter(user=admin).exists()


def test_a_superusers_role_is_set_on_the_server(admin_client, django_user_model):
    boss = django_user_model.objects.create_superuser("boss", password=PASSWORD)
    admin_client.post("/users/", {"action": "role", "user": boss.pk, "role": VIEWER})
    assert role_of(fresh(boss)) == ADMIN and not fresh(boss).groups.exists()
    assert not events("role_changed")
    assert "تعیین‌شده در سرور" in admin_client.get("/users/").content.decode()


@pytest.mark.parametrize("user", ["abc", "", "999999"])
def test_an_unknown_user_is_not_found(admin_client, user):
    assert admin_client.post("/users/", {"action": "deactivate", "user": user}).status_code == 404


# --- Password change ---------------------------------------------------------


def test_changing_the_password_keeps_this_session(client, make_user, verified):
    operator = make_user("operator1", OPERATOR)
    verified(client, operator)
    response = client.post("/password/", {
        "old_password": PASSWORD, "new_password1": "a-brand-new-password-3",
        "new_password2": "a-brand-new-password-3",
    }, follow=True)
    assert response.redirect_chain == [("/", 302)]
    assert "گذرواژه شما تغییر کرد" in response.content.decode()
    assert fresh(operator).check_password("a-brand-new-password-3")
    assert client.get("/").status_code == 200  # still signed in, still verified
    assert [e.username for e in events("password_changed")] == ["operator1"]


def test_password_change_mistakes_are_explained_in_persian(signed_in):
    html = signed_in.post("/password/", {
        "old_password": "not-the-password", "new_password1": "a-brand-new-password-3",
        "new_password2": "a-brand-new-password-3",
    }).content.decode()
    assert "گذرواژه فعلی نادرست است" in html
    html = signed_in.post("/password/", {
        "old_password": PASSWORD, "new_password1": "a-brand-new-password-3",
        "new_password2": "a-different-password-4",
    }).content.decode()
    assert "گذرواژه تازه و تکرار آن یکسان نیستند" in html
    assert not events("password_changed")


# --- Activity log ------------------------------------------------------------


def test_signing_in_and_out_is_recorded(client, viewer):
    client.post("/login/", {"username": "viewer1", "password": PASSWORD}, REMOTE_ADDR="10.20.30.40")
    client.post("/logout/")
    first, second = AuditEvent.objects.order_by("id")
    assert (first.action, first.username, first.user_id, first.ip) == ("login", "viewer1", viewer.pk, "10.20.30.40")
    assert (second.action, second.username) == ("logout", "viewer1")


def test_a_failed_sign_in_records_the_name_tried_never_the_password(client, viewer):
    client.post("/login/", {"username": "viewer1", "password": "guess-number-one"})
    client.post("/login/", {"username": "nobody", "password": "guess-number-two"})
    failed = events("login_failed")
    assert [(e.username, e.user_id) for e in failed] == [("viewer1", None), ("nobody", None)]
    assert all(e.detail == {} for e in failed)
    assert not AuditEvent.objects.filter(detail__icontains="guess").exists()


def test_the_activity_log_is_persian(admin_client, viewer):
    admin_client.post("/users/", {"action": "role", "user": viewer.pk, "role": OPERATOR})
    html = admin_client.get("/activity/").content.decode()
    assert "سابقه فعالیت‌ها" in html
    assert "تغییر نقش" in html
    assert '<bdi dir="ltr">viewer1</bdi>: از مشاهده‌گر به اپراتور' in html
    assert "۱۴۰۵/" in html  # Solar Hijri, Persian digits
    assert "role_changed" not in html


def test_the_activity_log_pages_through_old_events(admin_client):
    for _ in range(120):
        record("login_failed", username="someone")
    html = admin_client.get("/activity/").content.decode()
    assert "قدیمی‌تر" in html and "۱ / ۲" in html
    html = admin_client.get("/activity/?page=2").content.decode()
    assert "جدیدتر" in html


def test_only_known_actions_are_recorded():
    with pytest.raises(ValueError):
        record("made_up")


# --- Persian error pages -------------------------------------------------


def test_an_expired_form_gets_a_persian_page(viewer):
    strict = Client(enforce_csrf_checks=True)
    response = strict.post("/login/", {"username": "viewer1", "password": PASSWORD})
    assert response.status_code == 403
    assert "اعتبار این فرم تمام شده است" in response.content.decode()


def test_a_missing_page_gets_a_persian_page(signed_in):
    response = signed_in.get("/no-such-page/")
    assert response.status_code == 404
    assert "صفحه پیدا نشد" in response.content.decode()


def test_the_server_error_page_is_persian_and_stands_alone():
    html = render_to_string("500.html")
    assert '<html lang="fa" dir="rtl">' in html and "خطایی رخ داد" in html
