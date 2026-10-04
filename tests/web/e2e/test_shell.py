"""The app shell's behaviour in the browser (plan 05, P1): live updates keep
your place, an ended session or a lost connection is handled, the
confirmation dialog and the phone menu work by keyboard, and a double
click submits once."""
from __future__ import annotations

import pytest
from django.contrib.sessions.models import Session

from sms_sender_web.jobs.models import Job
from sms_sender_web.suppression.models import Suppression

from ..world import build_world
from .conftest import expect, fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def _polls(page) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url) if "/live/" in request.url else None)
    return seen


def test_live_updates_keep_the_focus_on_your_button(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/sending/")
    polls = _polls(page)
    pause = page.locator("#pause-form button")
    pause.focus()
    page.wait_for_timeout(7_000)  # two updates, every 3 s
    assert len(polls) >= 2
    assert page.evaluate("document.activeElement.closest('#pause-form') !== null")


def test_a_session_that_ends_during_live_updates_sends_the_page_to_the_login(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/sending/")
    Session.objects.all().delete()
    page.wait_for_url("**/login/?next=/campaigns/sending/", timeout=10_000)
    assert page.get_by_role("heading", name=fa("Log in"), exact=True).is_visible()


def test_a_lost_connection_is_announced_and_cleared(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/sending/")
    notice = page.locator("#connection")
    page.route("**/live/", lambda route: route.abort())
    notice.get_by_text(fa("The connection to the server was lost. Trying again…")).wait_for(timeout=10_000)
    page.unroute("**/live/")
    notice.wait_for(state="hidden", timeout=10_000)


def test_the_confirmation_names_the_consequence_and_escape_backs_out(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/sending/")
    trigger = page.get_by_role("button", name=fa("Cancel the campaign"), exact=True)
    trigger.click()
    dialog = page.get_by_role("dialog")
    assert "لغو" in dialog.inner_text() and "۳" in dialog.inner_text()  # 3 not sent yet
    page.keyboard.press("Escape")
    dialog.wait_for(state="hidden")
    assert page.evaluate("document.activeElement.textContent.trim()") == trigger.inner_text().strip()
    send = Job.objects.get(campaign__slug="sending", kind=Job.Kind.SEND)
    assert send.control == ""  # nothing was sent

    trigger.click()
    with page.expect_navigation():
        page.get_by_role("dialog").get_by_role("button", name=fa("Cancel the campaign"), exact=True).click()
    send.refresh_from_db()
    assert send.control == Job.Control.CANCEL


def test_a_double_click_submits_once(world, open_as):
    page = open_as(world.users["operator"], "/suppression/")
    posts: list[str] = []
    page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
    page.get_by_label(fa("Numbers, one per line"), exact=True).fill("09120000077")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Add to the list"), exact=True).dblclick()
    assert len(posts) == 1
    assert Suppression.objects.filter(phone="09120000077").count() == 1


def test_the_phone_menu_opens_and_closes_by_keyboard(world, open_as):
    page = open_as(world.users["operator"], "/", 390)
    opener = page.get_by_role("button", name=fa("Open the menu"), exact=True)
    assert not page.locator("nav.sidebar").is_visible()  # the menu waits behind the button
    opener.click()
    drawer = page.locator("#drawer")
    assert drawer.get_by_role("link", name=fa("Segments"), exact=True).is_visible()
    assert opener.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")
    drawer.wait_for(state="hidden")
    expect(opener).to_have_attribute("aria-expanded", "false")  # set by the close event, a moment later
