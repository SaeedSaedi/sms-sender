"""Importing the CLI's profiles in the browser (G4 of the 2026-10-05
review): an admin reads sms-sender.toml, sees each profile as the campaign
it would be (the preview fits a phone, and axe finds nothing on it), and
imports the ticked ones as drafts."""
from __future__ import annotations

import pytest

from sms_sender_web.jobs.models import Campaign

from ..test_profile_import import PROFILES
from ..world import build_world
from .conftest import axe_violations, fa, overflow, small_targets, shot

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


@pytest.mark.parametrize("width", [390, 1366])
def test_an_admin_imports_the_profiles_as_drafts(world, open_as, tmp_path, width):
    toml = tmp_path / "sms-sender.toml"
    toml.write_text(PROFILES, encoding="utf-8")
    page = open_as(world.users["admin"], "/", width)
    page.get_by_role("link", name=fa("Import the CLI's profiles"), exact=True).click()
    page.get_by_label(fa("Profiles file (sms-sender.toml)"), exact=True).set_input_files(str(toml))
    page.get_by_role("button", name=fa("Read the profiles"), exact=True).click()

    page.get_by_role("heading", name=fa("Profiles in the file")).wait_for()
    shot(page, f"import.preview@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []
    broken = page.get_by_role("checkbox", name=f"{fa('Import')}: broken", exact=True)
    assert broken.is_disabled()

    with page.expect_navigation():
        page.get_by_role("button", name=fa("Import the ticked profiles"), exact=True).click()
    assert page.url.endswith("/")
    assert set(Campaign.objects.filter(slug__in=["coin-price-7", "transaction-1-seg2", "broken"])
               .values_list("slug", flat=True)) == {"coin-price-7", "transaction-1-seg2"}
    page.get_by_role("link", name="coin-price-7", exact=True).wait_for()
