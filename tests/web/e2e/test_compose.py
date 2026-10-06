"""The two-minute alert in the browser (plan 06, L3): the routine of
2 September, one alert to six segments, from a preset. Counted from the
"new alert" page: pick the preset, type today's value, send the test SMS,
approve it, start sending and confirm. At most six actions besides typing,
every number of the six lists once, and one test SMS."""
from __future__ import annotations

import time

import pytest

from sms_sender_web.campaigns.models import Preset
from sms_sender_web.jobs.models import Campaign, Job
from sms_sender_web.jobs.sandbox import read_outbox
from sms_sender_web.segments.models import Segment

from ..world import PHONES, SETTINGS, TEST_PHONE, build_world, open_window
from .conftest import expect, fa, shot

pytestmark = pytest.mark.e2e

# Four lists more beside the world's vip and vip-2; vip-3 repeats a vip number.
MORE = {
    "vip-3": ("گروه ۳", [("09120000031", "u-31"), (PHONES[2], "u-2")]),
    "vip-4": ("گروه ۴", [("09120000041", "u-41"), ("09120000042", "u-42")]),
    "vip-5": ("گروه ۵", [("09120000051", "u-51"), ("09120000052", "u-52")]),
    "vip-6": ("گروه ۶", [("09120000061", "u-61"), ("09120000062", "u-62")]),
}
UNIQUE = len(PHONES) + 2 + 1 + 2 + 2 + 2


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
    vip = Segment.objects.get(slug="vip")
    Preset.objects.create(
        slug="daily-coin", name="هشدار روزانه", labels={"token": "نام کالا"},
        settings={**SETTINGS, "input": str(vip.path), "send_window": open_window(),
                  "more_segments": ["vip-2", *MORE]},
        last_values={"token": "نفت"},
    )
    return world


def _wait_for(check, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def test_the_daily_alert_to_six_segments_in_a_few_actions(world, sandbox_worker, open_as):
    page = open_as(world.users["operator"], "/compose/")
    actions = 0

    page.get_by_role("link", name="هشدار روزانه").click()  # 1. the preset
    actions += 1
    value = page.get_by_label("نام کالا")
    expect(value).to_have_value("نفت")  # the last alert's value
    value.fill("طلا")  # typing, not counted
    expect(page.locator(".sms-bubble")).to_contain_text("طلا")  # the preview keeps up
    expect(page.locator(".segment-choices input:checked")).to_have_count(6)  # the preset's six
    expect(page.locator("#compose-counts")).to_contain_text(f"{fa_number(UNIQUE)} نفر")
    shot(page, "compose-routine-1")

    started = time.monotonic()
    page.get_by_role("button", name=fa("Send a test SMS"), exact=True).click()  # 2. the test SMS
    actions += 1
    approve = page.get_by_role("button", name=fa("Yes, approve"), exact=True)
    approve.wait_for(timeout=60_000)  # the panel follows the test as it runs
    to_approval = time.monotonic() - started
    shot(page, "compose-routine-2")
    approve.click()  # 3. it arrived and reads right
    actions += 1
    page.get_by_role("button", name=fa("Start sending"), exact=True).click()  # 4.
    page.get_by_role("dialog").get_by_role("button", name=fa("Start sending"), exact=True).click()  # 5. confirm
    actions += 2
    alert = Campaign.objects.get(preset__slug="daily-coin")
    _wait_for(lambda: Job.objects.filter(campaign=alert, kind=Job.Kind.SEND, state=Job.State.DONE).exists())
    assert page.url.endswith(f"/compose/c/{alert.slug}/")  # it never left the page
    shot(page, "compose-routine-3")

    assert actions <= 6
    assert to_approval < 20, f"the approval took {to_approval:.1f} s to show"
    assert alert.settings["tokens"]["token"] == "طلا"
    assert [alert.settings["segment"], *alert.settings["more_segments"]] == [
        "vip", "vip-2", "vip-3", "vip-4", "vip-5", "vip-6",
    ]
    phones = [sms["phone"] for sms in read_outbox()]
    assert phones.count(TEST_PHONE) == 1
    recipients = [p for p in phones if p != TEST_PHONE]
    assert len(recipients) == len(set(recipients)) == UNIQUE


def fa_number(n: int) -> str:
    from sms_sender_web.dashboard.templatetags.fa import fa_number as convert

    return str(convert(n))
