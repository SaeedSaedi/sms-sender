"""The operations' journeys in the browser (plan 05, P6): pause and resume,
a stopped send resumed, a send the window paused going on by itself,
reconciling and the "did anyone get it twice?" check, recipients queued
again, the report's filters, an audience and a download, a test
notification, an admin's password reset, backups, deleting a campaign and
bringing in one the command line made. Found by their Persian names, like
a person would."""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from sms_sender.state import FAILED_PERMANENT, SENT, UNKNOWN, StateStore
from sms_sender.window import now_tehran
from sms_sender_web.jobs import sandbox as sandbox_mode
from sms_sender_web.jobs.engine import campaign_db
from sms_sender_web.jobs.models import Campaign, Job
from sms_sender_web.reports.views import SENDS_OK
from sms_sender_web.segments.models import Segment
from sms_sender_web.system import views as system_views

from ..world import PHONES, build_world, open_window
from .conftest import expect, fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def send_of(slug: str) -> Job:
    return Job.objects.filter(campaign__slug=slug, kind=Job.Kind.SEND).order_by("-id").first()


def wait_for(check, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def closed_window() -> str:
    """A sending window that's closed now: it opens in three hours."""
    now = now_tehran()
    return f"{now + timedelta(hours=3):%H:%M}-{now + timedelta(hours=4):%H:%M}"


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


def test_a_send_the_window_paused_goes_on_by_itself_when_it_opens(world, sandbox_worker, open_as):
    campaign, send = world.campaigns["paused"], send_of("paused")
    campaign.settings = {**campaign.settings, "send_window": closed_window()}
    campaign.save()
    send.result = {"stop_reason": "window_closed", "stop_fields": {"start": "08:00", "end": "21:00"}}
    send.save()
    page = open_as(world.users["operator"], "/campaigns/paused/")
    expect(page.locator("#stage")).to_contain_text(
        fa("Paused: the sending window closed. Sending continues by itself when it opens again."))
    time.sleep(1)  # the worker has looked at it a few times by now
    send.refresh_from_db()
    assert send.state == Job.State.PAUSED  # still closed: it waits

    # The window opens (as if the clock reached it): nobody presses anything.
    campaign.settings = {**campaign.settings, "send_window": open_window()}
    campaign.save()
    wait_for(lambda: send_of("paused").state == Job.State.DONE)
    assert send_of("paused").pk == send.pk  # the same send went on
    assert send.events.filter(key="window_resumed").exists()
    page.reload()
    expect(page.locator("#stage")).to_contain_text(fa("Completed"))
    counts = StateStore(campaign_db(campaign)).counts()
    assert counts[SENT] == len(PHONES) - 1  # all but the one Kavenegar rejected before


def test_an_unknown_outcome_is_reconciled_and_nobody_got_it_twice(world, sandbox_worker, open_as):
    unknown = PHONES[3]
    # Kavenegar's records (the sandbox's outbox) hold its SMS: it did go out.
    with sandbox_mode.outbox_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time(), "phone": unknown, "message_id": 777001,
                            "template": "coin-price", "tokens": {}}) + "\n")
    page = open_as(world.users["operator"], "/campaigns/completed/")
    form = page.locator("#followup-reconcile")
    form.get_by_text(fa("Options")).click()
    form.get_by_label(fa("Only those last tried at least this many minutes ago"), exact=True).fill("0")
    with page.expect_navigation():
        form.get_by_role("button", name=fa("Reconcile with Kavenegar"), exact=True).click()
    store = StateStore(campaign_db(world.campaigns["completed"]))
    wait_for(lambda: store.counts().get(UNKNOWN, 0) == 0)
    assert store.counts()[SENT] == 3  # found sent, never sent again

    page = open_as(world.users["viewer"], "/reports/completed/")
    check = page.locator("#sends-check")
    expect(check.locator(".callout-success")).to_contain_text(fa(str(SENDS_OK)).split(":")[0])


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



def test_an_admin_backs_up_checks_the_backup_and_deletes_a_campaign(world, open_as):
    page = open_as(world.users["admin"], "/system/backups/")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Back up now"), exact=True).click()
    expect(page.locator(".callout-success")).to_contain_text(fa("Backed up: %(name)s.").split(":")[0])
    with page.expect_navigation():
        page.locator("tbody tr").first.get_by_role("button", name=fa("Check it"), exact=True).click()
    expect(page.locator(".callout-success")).to_contain_text(
        fa("%(name)s is whole: every file matches its fingerprint.").split(":")[0].replace("%(name)s", "").strip())

    db = campaign_db(world.campaigns["completed"])
    page = open_as(world.users["admin"], "/campaigns/completed/")
    page.get_by_text(fa("Delete the campaign and its records")).click()
    page.get_by_label(fa("To delete it, type the campaign's short name:")).fill("completed")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Back up and delete"), exact=True).click()
    page.get_by_role("heading", name=fa("Campaigns"), exact=True).wait_for()
    assert not Campaign.objects.filter(slug="completed").exists() and not db.exists()


def test_a_campaign_the_command_line_made_comes_to_the_dashboard(world, open_as, sandbox):
    store = StateStore(sandbox / "db" / "cli-7.db")
    store.bind_campaign("cli-7", {"template": "coin-price"})
    store.upsert_pending([(phone, phone) for phone in PHONES[:2]])
    page = open_as(world.users["operator"], "/")
    row = page.get_by_role("row").filter(has_text="cli-7")
    expect(row).to_contain_text(fa("Made with the command line"))
    with page.expect_navigation():
        row.get_by_role("button", name=fa("Bring to the dashboard"), exact=True).click()
    assert page.url.endswith("/campaigns/cli-7/")
    assert Campaign.objects.get(slug="cli-7").settings["template"] == "coin-price"

