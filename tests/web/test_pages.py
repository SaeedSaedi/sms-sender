"""The dashboard's first pages: health check, Persian sign-in, and the
campaign list read from the campaign DBs the CLI writes."""
import re

import pytest

pytest.importorskip("django")

from sms_sender.state import StateStore  # noqa: E402

from .conftest import PASSWORD  # noqa: E402

pytestmark = pytest.mark.django_db


def test_healthz_needs_no_login(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_page_needs_a_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"].startswith("/login/")


def test_the_sign_in_page_is_persian_and_right_to_left(client):
    html = client.get("/login/").content.decode()
    assert '<html lang="fa" dir="rtl">' in html
    assert "ورود" in html and "گذرواژه" in html and "نام کاربری" in html


def test_a_wrong_password_gets_a_persian_message(client, viewer):
    response = client.post("/login/", {"username": "viewer1", "password": "wrong"})
    assert "نام کاربری یا گذرواژه نادرست است" in response.content.decode()


def test_signing_in_leads_to_the_campaigns(client, viewer):
    response = client.post("/login/", {"username": "viewer1", "password": PASSWORD})
    assert response.status_code == 302 and response["Location"] == "/"


def test_campaigns_are_listed_with_persian_numbers(signed_in, settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    store = StateStore(tmp_path / "coin-7.db")
    store.bind_campaign("coin-7", {"template": "coin-price"})
    store.upsert_pending([(f"0912{i:07d}", "x") for i in range(1234)])
    store.record_invalid_many([("nope", "not a phone number")])

    html = signed_in.get("/").content.decode()
    assert '<bdi dir="ltr">coin-7</bdi>' in html
    assert '<bdi dir="ltr">coin-price</bdi>' in html
    assert "۱٬۲۳۵" in html  # every recipient, the invalid input row included
    assert "در صف ارسال" in html and "۱٬۲۳۴" in html
    assert "نامعتبر" in html  # the invalid row isn't shown as a rejection
    assert "ردشده" not in html
    # No Latin digits in any number cell.
    assert re.search(r'class="num">[^<]*[0-9]', html) is None


def test_no_campaigns_yet(signed_in, settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path / "nothing-here"
    assert "هنوز کمپینی نیست" in signed_in.get("/").content.decode()
