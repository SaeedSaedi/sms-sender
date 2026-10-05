"""R5 of the 2026-10-05 review in the browser: an admin holds all sending
from the status page (the confirmation counts what stops), every page says
so, and the hold is lifted; an operator looks a number up across every
campaign (the result fits a phone, and axe finds nothing on it)."""
from __future__ import annotations

import pytest

from sms_sender_web.jobs import services

from ..world import PHONES, build_world
from .conftest import axe_violations, fa, overflow, shot, small_targets

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def test_an_admin_holds_all_sending_and_lifts_it(world, open_as):
    page = open_as(world.users["admin"], "/status/")
    page.get_by_role("button", name=fa("Hold all sending"), exact=True).click()
    dialog = page.get_by_role("dialog")
    assert "۲ ارسال" in dialog.inner_text()  # the test world's running send and its scheduled one
    with page.expect_navigation():
        dialog.get_by_role("button", name=fa("Hold all sending"), exact=True).click()
    assert services.held() is not None

    page.goto(page.url.replace("/status/", "/campaigns/approved/"))
    banner = page.get_by_role("region", name=fa("All sending is held"))
    assert "admin1" in banner.inner_text()
    shot(page, "hold.banner@1366")
    assert axe_violations(page) == []

    page.goto(page.url.replace("/campaigns/approved/", "/status/"))
    page.get_by_role("button", name=fa("Lift the hold"), exact=True).click()
    with page.expect_navigation():
        page.get_by_role("dialog").get_by_role("button", name=fa("Lift the hold"), exact=True).click()
    assert services.held() is None
    assert page.get_by_role("region", name=fa("All sending is held")).count() == 0


@pytest.mark.parametrize("width", [390, 1366])
def test_an_operator_finds_a_number(world, open_as, width):
    page = open_as(world.users["operator"], "/numbers/", width)
    page.get_by_label(fa("Mobile number"), exact=True).fill(PHONES[0])
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Find"), exact=True).click()
    assert PHONES[0] not in page.url  # it went in a POST
    page.get_by_role("region", name=fa("Its record in each campaign")).wait_for()
    shot(page, f"numbers.result@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []
