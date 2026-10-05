"""Plan 05, P5: an admin's care of users (a temporary password, a new one
chosen at the next sign-in, last activity) and the activity log's filters
and download."""
from __future__ import annotations

import csv
import io

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender_web.accounts.models import ask_to_change_password, must_change_password  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.audit.record import record  # noqa: E402

from .conftest import PASSWORD  # noqa: E402

pytestmark = pytest.mark.django_db
TEMPORARY = "a-temporary-password-7"


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def test_an_admin_resets_a_password_and_its_sessions_end(admin_client, make_user):
    person = make_user("viewer2", "viewer")
    theirs = Client()
    theirs.force_login(person)
    assert theirs.get("/").status_code == 200
    admin_client.post("/users/", {"action": "reset_password", "user": person.pk, "password": TEMPORARY})
    person.refresh_from_db()
    assert person.check_password(TEMPORARY) and must_change_password(person)
    assert theirs.get("/").status_code == 302  # signed out: the old password is gone
    event = AuditEvent.objects.get(action="password_reset")
    assert event.detail == {"target": "viewer2"}  # never the password
    admin_client.post("/users/", {"action": "reset_password", "user": person.pk, "password": "short"})
    person.refresh_from_db()
    assert person.check_password(TEMPORARY)  # a weak one is refused


def test_a_new_password_comes_before_anything_else(make_user, verified):
    person = make_user("viewer3", "viewer")
    ask_to_change_password(person)
    client = Client()
    client.force_login(person)
    response = client.get("/")
    assert response.status_code == 302 and response["Location"] == "/password/"
    assert "گذرواژه‌ای از آن خودتان" in client.get("/password/").content.decode()
    client.post("/password/", {"old_password": PASSWORD, "new_password1": "my-own-password-42",
                               "new_password2": "my-own-password-42"})
    person.refresh_from_db()
    assert not must_change_password(person) and client.get("/").status_code == 200


def test_the_second_step_comes_first(make_user, verified):
    person = make_user("operator2", "operator")
    ask_to_change_password(person)
    signed = Client()
    signed.force_login(person)
    assert signed.get("/")["Location"].startswith("/2fa/")  # not yet past the code
    passed = Client()
    verified(passed, person)
    assert passed.get("/")["Location"] == "/password/"


def test_new_accounts_choose_their_own_password_when_asked(admin_client):
    admin_client.post("/users/", {"action": "create", "username": "new1", "password": TEMPORARY,
                                  "role": "viewer", "must_change": "on"})
    admin_client.post("/users/", {"action": "create", "username": "new2", "password": TEMPORARY, "role": "viewer"})
    from django.contrib.auth import get_user_model

    User = get_user_model()
    assert must_change_password(User.objects.get(username="new1"))
    assert not must_change_password(User.objects.get(username="new2"))


def test_the_users_page_shows_last_activity(admin_client, make_user):
    person = make_user("viewer4", "viewer")
    record("report_downloaded", user=person, kind="summary")
    html = admin_client.get("/users/").content.decode()
    assert "آخرین فعالیت" in html and "۱۴۰۵/" in html


def test_the_activity_log_filters_and_downloads(admin_client, make_user, signed_in):
    person = make_user("operator3", "operator")
    record("phone_revealed", user=person, campaign="coin-7", phone="09120000001")
    record("report_downloaded", user=person, campaign="other", kind="summary")
    html = admin_client.get("/activity/", {"action": "phone_revealed"}).content.decode()
    assert "نمایش شماره تلفن" in html and "دریافت گزارش" not in html.split("<tbody>")[1]
    html = admin_client.get("/activity/", {"campaign": "other"}).content.decode()
    assert "دریافت گزارش" in html.split("<tbody>")[1]
    response = admin_client.get("/activity/export.csv", {"user": "operator3"})
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["at", "username", "action", "campaign", "ip", "detail"]
    assert {r[2] for r in rows[1:]} == {"phone_revealed", "report_downloaded"}
    assert "09120000001" not in response.content.decode() and "0912*****01" in response.content.decode()
    assert AuditEvent.objects.get(action="audit_exported").detail == {"filters": {"user": "operator3"}}
    assert signed_in.get("/activity/export.csv").status_code == 403
