"""Plan 05, P1: the app shell every page shares. The menu marks where you
are; a live update from an ended session sends the whole page to the login
(never the login page inside the update); confirmations, messages and the
connection notice are on every page."""
import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

pytestmark = pytest.mark.django_db


def test_the_menu_marks_the_section_you_are_in(signed_in):
    html = signed_in.get("/segments/").content.decode()
    assert '<a href="/segments/" aria-current="true">' in html
    assert '<a href="/" aria-current="true">' not in html
    assert 'class="skip-link" href="#main"' in html and '<main id="main"' in html


def test_every_page_has_the_confirmation_dialog_and_the_connection_notice(signed_in):
    html = signed_in.get("/").content.decode()
    assert 'id="confirm-dialog"' in html and "انصراف" in html
    assert 'id="connection"' in html and "ارتباط با سرور قطع شد" in html


def test_a_live_update_from_an_ended_session_sends_the_page_to_the_login():
    response = Client().get(
        "/campaigns/coin-7/live/", HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL="http://testserver/campaigns/coin-7/?tab=1",
    )
    assert response.status_code == 200 and response.content == b""
    assert response["HX-Redirect"] == "/login/?next=/campaigns/coin-7/%3Ftab%3D1"
    # An ordinary request still gets an ordinary redirect.
    response = Client().get("/campaigns/coin-7/")
    assert response.status_code == 302 and response["Location"] == "/login/?next=/campaigns/coin-7/"


def test_a_live_update_before_the_second_step_sends_the_page_there(make_user):
    client = Client()
    client.force_login(make_user("operator1", "operator"))
    response = client.get("/campaigns/coin-7/live/", HTTP_HX_REQUEST="true",
                          HTTP_HX_CURRENT_URL="http://testserver/campaigns/coin-7/")
    assert response["HX-Redirect"] == "/2fa/setup/?next=/campaigns/coin-7/"


def test_a_field_with_an_error_points_to_its_hint_and_its_error(make_user, verified):
    client = Client()
    verified(client, make_user("operator1", "operator"))
    html = client.post("/campaigns/new/", {"name": "x", "slug": "Not A Slug", "template": "t"}).content.decode()
    assert 'aria-describedby="id_slug_hint id_slug_error" aria-invalid="true"' in html
    assert '<p class="hint" id="id_slug_hint">' in html
    assert '<div class="field-errors" id="id_slug_error" role="alert">' in html
    # A field without an error says nothing extra.
    assert 'id="id_name" name="name" type="text" maxlength="200" required value="x">' in html
