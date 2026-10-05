"""The run of 2 September in the browser (G1 of the 2026-10-05 review): one
message to six segments. The CLI needed a run per segment; here the
campaign chooses five more segments after its own, one test SMS is
approved, and one send reaches every number once."""
from __future__ import annotations

import re
import time

import pytest

from sms_sender_web.dashboard.templatetags.fa import fa_number
from sms_sender_web.jobs.models import Job
from sms_sender_web.jobs.sandbox import read_outbox
from sms_sender_web.segments.models import Segment

from ..world import PHONES, TEST_PHONE, build_world
from .conftest import expect, fa

pytestmark = pytest.mark.e2e

# Five more lists with vip's columns; the second one repeats a vip number.
MORE = {
    "vip-3": ("گروه ۳", [("09120000031", "u-31"), (PHONES[2], "u-2")]),
    "vip-4": ("گروه ۴", [("09120000041", "u-41"), ("09120000042", "u-42")]),
    "vip-5": ("گروه ۵", [("09120000051", "u-51"), ("09120000052", "u-52")]),
    "vip-6": ("گروه ۶", [("09120000061", "u-61"), ("09120000062", "u-62")]),
}


@pytest.fixture
def world(sandbox):
    world = build_world(sandbox)
    for slug, (name, rows) in MORE.items():
        segment = Segment.objects.create(
            slug=slug, name=name, status=Segment.Status.READY, columns=["phone", "user_id", "first_name"],
            user_id_column="user_id", token_columns=["first_name"], summary={"valid": len(rows)},
        )
        segment.path.write_text(
            "phone,user_id,first_name\n" + "".join(f"{p},{uid},Sara\n" for p, uid in rows), encoding="utf-8",
        )
    return world


def _wait_for(check, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def test_one_test_sms_approves_a_send_to_six_segments(world, sandbox_worker, open_as):
    page = open_as(world.users["operator"], "/campaigns/fresh/settings/")
    page.get_by_text(fa("More segments in the same send (optional)")).click()
    group = page.get_by_role("group", name=fa("More segments in the same send (optional)"), exact=True)
    for name in ["مشتریان ویژه ۲", *(name for name, _rows in MORE.values())]:
        group.get_by_role("checkbox", name=re.compile(re.escape(name))).check()
    page.get_by_role("button", name=fa("Save"), exact=True).click()

    # The check reads all six, in the order they're sent; a number in two
    # lists counts once.
    page.get_by_text(fa("Ready for a test SMS.")).wait_for()
    expect(page.locator("#check-segments li")).to_have_count(6)
    expect(page.locator("#send-segments li")).to_have_count(6)
    unique = len(PHONES) + 2 + 1 + 2 + 2 + 2

    page.get_by_role("button", name=fa("Send a test SMS"), exact=True).click()
    page.get_by_role("button", name=fa("Yes, approve"), exact=True).click(timeout=60_000)
    page.get_by_text(fa("The test SMS is approved. Sending can start.")).wait_for()
    page.get_by_role("button", name=fa("Start sending"), exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name=fa("Start sending"), exact=True).click()
    _wait_for(lambda: Job.objects.filter(campaign__slug="fresh", kind=Job.Kind.SEND, state=Job.State.DONE).exists())

    # One test SMS in all, and every number of the six lists once.
    phones = [sms["phone"] for sms in read_outbox()]
    assert phones.count(TEST_PHONE) == 1
    recipients = [p for p in phones if p != TEST_PHONE]
    assert len(recipients) == len(set(recipients)) == unique
    page.reload()
    assert f"{fa('Accepted')} {fa_number(unique)}" in " ".join(page.locator("#recipients-step").inner_text().split())
