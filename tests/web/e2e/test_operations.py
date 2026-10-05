"""The operations' journeys in the browser (plan 05, P6): pause and resume,
a stopped send resumed, recipients queued again, the report's filters, an
audience and a download, a test notification, and an admin's password
reset. Found by their Persian names, like a person would."""
from __future__ import annotations

import pytest

from sms_sender.state import FAILED_PERMANENT, StateStore
from sms_sender_web.jobs.engine import campaign_db
from sms_sender_web.jobs.models import Job
from sms_sender_web.segments.models import Segment
from sms_sender_web.system import views as system_views

from ..world import build_world
from .conftest import expect, fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def send_of(slug: str) -> Job:
    return Job.objects.filter(campaign__slug=slug, kind=Job.Kind.SEND).order_by("-id").first()


def test_a_send_pauses_and_resumes_from_its_page(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/sending/")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Pause"), exact=True).click()
    assert send_of("sending").control == Job.Control.PAUSE
    page = open_as(world.users["operator"], "/campaigns/paused/")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Resume sending"), exact=True).click()
    assert send_of("paused").state == Job.State.QUEUED


def test_a_stopped_send_resumes_after_its_confirmation(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/halted/")
    page.get_by_role("button", name=fa("Resume sending"), exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    with page.expect_navigation():
        dialog.get_by_role("button", name=fa("Resume sending"), exact=True).click()
    assert send_of("halted").state == Job.State.QUEUED  # a new send, for those still waiting


def test_recipients_go_back_in_the_queue_after_the_count_is_named(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/completed/")
    page.get_by_text(fa("Queue recipients again")).click()
    page.get_by_role("button", name=fa("Queue again"), exact=True).click()
    dialog = page.get_by_role("dialog")
    assert "۱" in dialog.inner_text()  # one rejected
    with page.expect_navigation():
        dialog.get_by_role("button", name=fa("Queue again"), exact=True).click()
    page.get_by_text("دوباره در صف است").wait_for()
    assert StateStore(campaign_db(world.campaigns["completed"])).counts().get(FAILED_PERMANENT, 0) == 0


def test_the_report_filters_makes_an_audience_and_downloads(world, open_as):
    page = open_as(world.users["operator"], "/reports/completed/")
    page.get_by_label(fa("Status"), exact=True).select_option("sent")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Apply"), exact=True).click()
    assert "status=sent" in page.url
    with page.expect_download() as download:
        page.get_by_role("link", name=fa("These recipients, with phone numbers (CSV)"), exact=True).click()
    assert download.value.suggested_filename == "completed-recipients.csv"
    page.get_by_text(fa("Make a segment from these recipients")).click()
    page.get_by_label(fa("Short name"), exact=True).fill("completed-sent")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Make the segment"), exact=True).click()
    page.get_by_role("heading", level=1).wait_for()
    assert page.url.endswith("/segments/completed-sent/")
    assert Segment.objects.get(slug="completed-sent").summary["valid"] == 2


def test_an_admin_tests_a_notification_target(world, open_as, monkeypatch):
    sent = []
    monkeypatch.setattr(system_views, "notify_text", lambda target, text: sent.append(target) or True)
    page = open_as(world.users["admin"], "/system/")
    page.get_by_label(fa("Add a target"), exact=True).fill("slack:https://hooks.slack.com/services/T0/B0/x")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Add"), exact=True).click()
    expect(page.get_by_text("slack:<redacted>")).to_be_visible()
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Send a test"), exact=True).click()
    page.get_by_text(fa("Sent. Check that it arrived.")).wait_for()
    assert sent == ["slack:https://hooks.slack.com/services/T0/B0/x"]


def test_an_admin_resets_a_password(world, open_as):
    viewer = world.users["viewer"]
    page = open_as(world.users["admin"], "/users/")
    row = page.get_by_role("row").filter(has_text=viewer.username)
    row.get_by_text(fa("Reset the password")).click()
    row.get_by_placeholder(fa("A temporary password")).fill("a-temporary-password-9")
    with page.expect_navigation():
        row.get_by_role("button", name=fa("Set"), exact=True).click()
    page.get_by_text("گذرواژه بازنشانی شد").wait_for()
    viewer.refresh_from_db()
    assert viewer.check_password("a-temporary-password-9")
