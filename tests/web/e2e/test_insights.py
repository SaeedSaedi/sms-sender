"""Plan 06, L5 in the browser: two segments compared on the audience page,
and a series with an alert that sent. Each fits a phone and a desktop, and
axe finds nothing."""
from __future__ import annotations

import pytest

from sms_sender.state import StateStore
from sms_sender_web.jobs.engine import campaign_db
from sms_sender_web.segments.models import Segment

from ..world import PHONES, build_world
from .conftest import axe_violations, fa, overflow, shot, small_targets

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


@pytest.mark.parametrize("width", [360, 1366])
def test_two_segments_compared(world, open_as, width):
    other = Segment.objects.create(slug="vip-two", name="مشتریان ویژه دوم", status=Segment.Status.READY,
                                   summary={"valid": 3})
    other.path.write_text("phone\n" + "".join(f"{p}\n" for p in PHONES[:3]), encoding="utf-8")
    page = open_as(world.users["viewer"], "/analytics/audience/", width)
    form = page.locator("#overlap")
    form.get_by_role("checkbox", name="مشتریان ویژه دوم").check()
    form.get_by_role("checkbox", name="مشتریان ویژه", exact=False).first.check()  # the world's «vip»
    with page.expect_navigation():
        form.get_by_role("button", name=fa("Compare")).click()
    page.get_by_role("region", name=fa("Numbers each pair shares")).wait_for()
    assert "0912" not in page.content()  # numbers only
    shot(page, f"audience.overlap@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []


@pytest.mark.parametrize("width", [360, 1366])
def test_a_series_with_an_alert_that_sent(world, open_as, width):
    store = StateStore(campaign_db(world.campaigns["approved"]))  # an alert of the world's preset
    for i, phone in enumerate(PHONES[:3]):
        store.claim(phone)
        store.mark_sent(phone, message_id=900 + i, status_code=200, cost=3020)
    store.record_delivery({PHONES[0]: 10, PHONES[1]: 10, PHONES[2]: 11}, checked_at=0)
    page = open_as(world.users["operator"], "/analytics/series/coin-price/", width)
    page.get_by_role("link", name=world.campaigns["approved"].name).wait_for()
    assert page.locator("svg.chart rect").count() == 1  # one alert, one bar: its delivered share
    assert "۶۶٫۷٪" in page.locator("#series-chart").inner_html()
    shot(page, f"series.detail@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []
