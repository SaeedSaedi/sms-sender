"""The send step in the browser (plan 05, P2): a send set for tomorrow,
shown with its time, then taken back without cancelling anyone."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from sms_sender_web.dashboard.templatetags.fa import jalali
from sms_sender_web.jobs.models import Job

from ..world import build_world
from .conftest import fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def test_a_send_is_set_for_tomorrow_and_taken_back(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/approved/")
    page.get_by_text(fa("Or schedule it for later")).click()
    tomorrow = timezone.localtime(timezone.now() + timedelta(days=1))
    page.get_by_label(fa("Date (Solar Hijri)"), exact=True).fill(str(jalali(tomorrow, "%Y/%m/%d")))
    page.get_by_label(fa("Time (Tehran)"), exact=True).fill("10:00")
    page.get_by_role("button", name=fa("Schedule"), exact=True).click()
    dialog = page.get_by_role("dialog")
    assert "۳" in dialog.inner_text()  # the three still waiting get it
    with page.expect_navigation():
        dialog.get_by_role("button", name=fa("Schedule"), exact=True).click()
    page.get_by_text(fa("Scheduled for")).wait_for()
    job = Job.objects.get(campaign__slug="approved", kind=Job.Kind.SEND)
    assert job.state == Job.State.QUEUED
    assert timezone.localtime(job.not_before).strftime("%Y-%m-%d %H:%M") == f"{tomorrow:%Y-%m-%d} 10:00"

    with page.expect_navigation():
        page.get_by_role("button", name=fa("Cancel the schedule"), exact=True).click()
    page.get_by_role("button", name=fa("Start sending"), exact=True).wait_for()
    job.refresh_from_db()
    assert job.state == Job.State.CANCELLED
