"""Plan 06, D1 and D3 in the browser: an admin keeps the team's numbers and
asks for a second approver; a test SMS reaches the requester and the team
(the sandbox's outbox), and only a colleague approves it."""
from __future__ import annotations

import pytest

from sms_sender_web.jobs.models import Job
from sms_sender_web.jobs.sandbox import read_outbox
from sms_sender_web.system.models import SystemSettings

from ..world import TEST_PHONE, build_world
from .conftest import axe_violations, fa, overflow, shot, small_targets

pytestmark = pytest.mark.e2e

TEAM = ("09150000088", "09150000099")


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


@pytest.mark.parametrize("width", [360, 1366])
def test_an_admin_keeps_the_team_numbers_and_asks_for_a_second_approver(world, open_as, width):
    page = open_as(world.users["admin"], "/system/", width)
    for phone in TEAM:
        page.get_by_label(fa("Mobile number"), exact=True).fill(phone)
        with page.expect_navigation():
            page.get_by_role("button", name=fa("Add a number"), exact=True).click()
    assert SystemSettings.load().team_test_numbers == list(TEAM)
    assert "۰۹۱۵*****۸۸" in page.locator("#team-numbers").inner_text()
    assert TEAM[0] not in page.content()  # masked, like every number on a page
    page.get_by_label(fa("Someone other than whoever asked for the test SMS approves it")).check()
    with page.expect_navigation():
        page.locator("#approval-form").get_by_role("button", name=fa("Save"), exact=True).click()
    assert SystemSettings.load().second_approver is True
    shot(page, f"system.team@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []


def test_the_team_gets_the_test_sms_and_a_colleague_approves_it(world, open_as, sandbox_worker):
    SystemSettings.objects.update_or_create(pk=1, defaults={"team_test_numbers": list(TEAM), "second_approver": True})
    page = open_as(world.users["operator"], "/campaigns/fresh/")
    page.get_by_role("button", name=fa("Send a test SMS"), exact=True).click()
    page.get_by_role("button", name=fa("No, something's wrong"), exact=True).wait_for(timeout=60_000)
    assert [sms["phone"] for sms in read_outbox()] == [TEST_PHONE, *TEAM]
    # Whoever asked for it can only reject it.
    page.get_by_text(fa("Someone else approves this test SMS, since you asked for it. You can still reject it.")).wait_for()
    assert page.get_by_role("button", name=fa("Yes, approve"), exact=True).count() == 0
    assert fa("and the team's numbers") + " (۲)" in page.locator("#current-step").inner_text()
    shot(page, "campaign.own-test@1366")
    assert axe_violations(page) == []

    colleague = open_as(world.users["admin"], "/campaigns/fresh/")
    colleague.get_by_role("button", name=fa("Yes, approve"), exact=True).click()
    colleague.get_by_text(fa("The test SMS is approved. Sending can start.")).wait_for()
    test = Job.objects.get(campaign__slug="fresh", kind=Job.Kind.TEST)
    assert (test.decision, test.decided_by) == (Job.Decision.APPROVED, world.users["admin"])
    assert test.result["test_team_sent"] == 2
