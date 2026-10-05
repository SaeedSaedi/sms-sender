"""On the Mac (plan 06, L1): the badge naming the data a page is on, the
banner while sending is restricted, and "keep me signed in" on the sign-in
page. Each fits a phone, passes axe-core and has big enough targets, in
light and dark mode."""
from __future__ import annotations

import pytest

from sms_sender.allowlist import ENV_ALLOWED_NUMBERS

from ..world import build_world
from .conftest import axe_violations, fa, overflow, shot, small_targets

pytestmark = pytest.mark.e2e

VIEWS = ((360, "light"), (390, "light"), (1366, "light"), (1366, "dark"))


def _checked(page, key: str) -> None:
    shot(page, key)
    assert overflow(page) <= 1, f"{key}: wider than the window"
    assert axe_violations(page) == [], key
    assert small_targets(page) == [], key


def test_the_badge_and_the_restricted_banner(sandbox, settings, open_as, monkeypatch):
    world = build_world(sandbox)
    settings.LOCAL = True
    for real in (False, True):
        settings.SANDBOX = not real
        monkeypatch.setenv(ENV_ALLOWED_NUMBERS, "09151097710" if real else "")
        badge = fa("Local · real" if real else "Local · sandbox")
        for width, scheme in VIEWS:
            page = open_as(world.users["viewer"], "/", width, color_scheme=scheme)
            shown = page.locator(".env-badge:visible")  # the sidebar's, or the phone's top bar
            assert shown.count() == 1 and shown.inner_text().strip() == badge
            banner = page.locator("#restricted-banner")
            assert banner.count() == (1 if real else 0)
            _checked(page, f"local-home-{'real' if real else 'sandbox'}@{width}-{scheme}")
            page.context.close()


def test_keep_me_signed_in_on_the_sign_in_page(sandbox, settings, open_as):
    settings.LOCAL = True
    for width, scheme in VIEWS:
        page = open_as(None, "/login/", width, color_scheme=scheme)
        box = page.get_by_label(fa("Keep me signed in on this Mac for 30 days"))
        assert box.is_visible() and not box.is_checked()
        _checked(page, f"local-login@{width}-{scheme}")
        page.context.close()
