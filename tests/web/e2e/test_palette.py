"""Plan 06, L4 in the browser: Ctrl+K (Cmd+K) goes to any page, campaign,
segment or preset with the keyboard alone; a phone number typed there is
looked up with a POST; on a phone the menu's search button opens it; and the
notifications page counts what's new."""
from __future__ import annotations

import pytest

from ..world import PHONES, build_world
from .conftest import axe_violations, fa, overflow, shot, small_targets

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def _palette(page):
    return page.get_by_role("dialog", name=fa("Go to a page, campaign, segment or preset"))


def test_the_keyboard_goes_to_a_campaign(world, open_as):
    page = open_as(world.users["operator"], "/")
    page.keyboard.press("Control+KeyK")
    _palette(page).wait_for()
    assert page.evaluate("document.activeElement.id") == "palette-input"
    page.keyboard.type("نفت")  # the campaign «نفت خام»
    first = _palette(page).get_by_role("option").first
    first.wait_for()
    assert "نفت خام" in first.inner_text() and first.get_attribute("aria-selected") == "true"
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.url.endswith("/compose/c/approved/")  # an alert of the world's preset: its composer


def test_arabic_letters_and_the_arrows(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/")
    page.keyboard.press("Control+KeyK")
    page.keyboard.type("مشتريان")  # with Arabic «ي»: still the segment «مشتریان ویژه»
    options = _palette(page).get_by_role("option")
    options.first.wait_for()
    assert "مشتریان ویژه" in options.first.inner_text()
    page.keyboard.press("Escape")
    assert not _palette(page).is_visible()

    page.keyboard.press("Control+KeyK")
    page.keyboard.press("ArrowDown")
    assert options.nth(1).get_attribute("aria-selected") == "true"
    active = page.evaluate("document.getElementById('palette-input').getAttribute('aria-activedescendant')")
    assert active == options.nth(1).get_attribute("id")


def test_a_number_is_looked_up_with_a_post(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/")
    page.keyboard.press("Control+KeyK")
    page.get_by_role("combobox").fill(PHONES[0])
    _palette(page).get_by_role("group", name=fa("Find this number")).wait_for()
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert PHONES[0] not in page.url
    page.get_by_role("region", name=fa("Its record in each campaign")).wait_for()


def test_on_a_phone_the_menu_opens_it(world, open_as):
    page = open_as(world.users["operator"], "/", 390)
    page.get_by_role("button", name=fa("Open the menu")).click()
    page.get_by_role("dialog", name=fa("Main menu")).get_by_role("button", name=fa("Search…")).click()
    _palette(page).wait_for()
    assert not page.get_by_role("dialog", name=fa("Main menu")).is_visible()  # the menu made way
    _palette(page).get_by_role("option").first.wait_for()
    shot(page, "palette@390")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []


def test_the_palette_at_desktop_width(world, open_as):
    page = open_as(world.users["admin"], "/", 1366)
    page.get_by_role("navigation", name=fa("Main menu")).first.get_by_role("button", name=fa("Search…")).click()
    page.keyboard.type("قیمت")
    _palette(page).get_by_role("option").first.wait_for()
    shot(page, "palette@1366")
    assert axe_violations(page) == []


def test_the_notifications_count_and_clear(world, open_as):
    page = open_as(world.users["operator"], "/")
    link = page.get_by_role("navigation", name=fa("Main menu")).first.get_by_role("link", name=fa("Notifications"))
    assert fa("unseen") in link.inner_text()  # the hidden words a screen reader hears
    with page.expect_navigation():
        link.click()
    page.get_by_role("heading", name=fa("Notifications"), exact=True).wait_for()
    assert page.locator(".notices > li.is-new").count() >= 1
    shot(page, "notifications@1366")
    page.reload()
    assert page.locator(".notices > li.is-new").count() == 0
    assert page.locator(".nav-count").count() == 0
