"""A keyboard-only pass (plan 05, P6): signing in without a mouse, the skip
link first, and a visible focus ring on everything Tab reaches."""
from __future__ import annotations

import pytest

from ..world import PASSWORD, build_world
from .conftest import fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def _focus_ring(page) -> dict:
    """What the focused element is, and whether it shows a focus ring."""
    return page.evaluate(
        """() => {
          const el = document.activeElement;
          const style = getComputedStyle(el);
          const own = parseFloat(style.outlineWidth) > 0 && style.outlineStyle !== "none";
          const shadow = style.boxShadow && style.boxShadow !== "none";
          // A file input or a checkbox shows its ring on its visible stand-in.
          const proxy = el.closest(".file-field, .choice, .segment-option, label");
          const proxied = proxy && parseFloat(getComputedStyle(proxy).outlineWidth) > 0;
          return {tag: el.tagName, text: (el.innerText || el.value || el.name || "").trim().slice(0, 30),
                  visible: own || shadow || proxied};
        }"""
    )


def test_signing_in_with_the_keyboard_alone(world, open_as):
    page = open_as(None, "/login/")
    # Focus starts in the username: typing can begin at once.
    assert page.evaluate("document.activeElement.name") == "username"
    page.keyboard.type(world.users["viewer"].username)
    page.keyboard.press("Tab")
    page.keyboard.type(PASSWORD)
    with page.expect_navigation():
        page.keyboard.press("Enter")
    page.get_by_role("heading", name=fa("Campaigns"), exact=True).wait_for()


def test_the_skip_link_moves_focus_to_the_content(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/fresh/")
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.id") == "main"


@pytest.mark.parametrize("path", ["/", "/campaigns/fresh/", "/reports/completed/", "/segments/vip/", "/suppression/"])
def test_everything_tab_reaches_shows_where_it_is(world, open_as, path):
    page = open_as(world.users["operator"], path)
    hidden = []
    for _ in range(40):
        page.keyboard.press("Tab")
        ring = _focus_ring(page)
        if ring["tag"] == "BODY":
            break
        if not ring["visible"]:
            hidden.append(f"{ring['tag']} {ring['text']!r}")
    assert hidden == []
