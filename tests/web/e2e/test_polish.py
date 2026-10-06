"""Plan 06, L6 in the browser: a form checks itself as you type (the error
shows where a submit would put it, only once you've left the field, and
clears when it's fixed), and a slow page shows the bar at the top."""
from __future__ import annotations

import pytest

from ..world import build_world
from .conftest import fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def test_a_form_checks_itself_as_you_type(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/new/")
    slug = page.locator("#id_slug")
    error = page.locator("#id_slug_error")
    slug.fill("Bad Slug")
    assert error.is_hidden()  # still typing: nothing yet
    slug.press("Tab")  # left the field
    error.get_by_text(fa("Use lowercase English letters, digits and dashes, e.g. vip-users."), exact=False).wait_for()
    assert slug.get_attribute("aria-invalid") == "true"
    assert "id_slug_error" in slug.get_attribute("aria-describedby")
    # The name hasn't been touched: no "required" under it.
    assert page.locator("#id_template_error").is_hidden()

    slug.fill("good-slug-7")
    page.wait_for_function("document.getElementById('id_slug_error').hidden")
    assert slug.get_attribute("aria-invalid") is None


# Click a link from inside the page, then stop the navigation at once, so
# the page stays and its script's decision can be read 300 ms later.
CLICK_AND_STAY = """async (selector) => {
  document.documentElement.classList.remove("is-navigating");
  document.querySelector(selector).click();
  window.stop();
  await new Promise((resolve) => setTimeout(resolve, 300));
  return document.documentElement.classList.contains("is-navigating");
}"""


def test_a_page_that_takes_a_moment_shows_the_bar(world, open_as):
    page = open_as(world.users["viewer"], "/")
    assert page.evaluate(CLICK_AND_STAY, 'nav.sidebar a[href="/campaigns/"]')  # another page: the bar
    page.evaluate("""() => {
      document.body.insertAdjacentHTML("beforeend",
        '<a id="probe-file" href="/reports/x/summary.csv" download>f</a><a id="probe-here" href="#main">h</a>');
    }""")
    assert not page.evaluate(CLICK_AND_STAY, "#probe-file")  # a download stays on the page
    assert not page.evaluate(CLICK_AND_STAY, "#probe-here")  # so does a part of this page


def test_a_preset_put_away_comes_back_with_undo(world, open_as):
    from sms_sender_web.campaigns.models import Preset

    from .conftest import axe_violations, small_targets

    page = open_as(world.users["operator"], "/presets/")
    with page.expect_navigation():
        page.get_by_role("button", name=fa("Put it away"), exact=True).click()
    toast = page.locator("[data-toast][data-undo]")
    toast.wait_for()
    assert Preset.objects.get(slug="coin-price").archived_at is not None
    assert axe_violations(page) == [] and small_targets(page) == []
    with page.expect_navigation():
        toast.get_by_role("button", name=fa("Undo")).click()
    assert Preset.objects.get(slug="coin-price").archived_at is None
