"""The settings page in the browser (plan 05, P2): it shows only what
applies, previews the message as you type, adds translation rows, and asks
before unsaved changes are left behind."""
from __future__ import annotations

import pytest

from sms_sender_web.campaigns.models import MessageTemplate

from ..world import build_world
from .conftest import expect, fa

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def source(page, token: str, choice: str) -> None:
    group = page.get_by_role("radiogroup", name=f"{fa('Filled with')}: {token}", exact=True)
    group.get_by_label(fa(choice), exact=True).check()


def test_only_what_applies_shows(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/fresh/settings/")
    value = page.get_by_label(f"{fa('Fixed value')}: token", exact=True)
    column = page.get_by_label(f"{fa('Column')}: token", exact=True)
    link = page.get_by_label(fa("The address it opens"), exact=True)
    expect(value).to_be_visible()
    expect(column).to_be_hidden()
    expect(link).to_be_visible()  # token20 carries the link
    source(page, "token20", "Not used")
    expect(link).to_be_hidden()
    source(page, "token", "A column of the segment")
    expect(value).to_be_hidden()
    expect(column).to_be_visible()
    expect(page.get_by_label(fa("Pattern"), exact=True)).to_be_hidden()
    source(page, "token3", "The short link")
    page.get_by_label(fa("A pattern, when the text has part of the address")).check()
    expect(page.get_by_label(fa("Pattern"), exact=True)).to_be_visible()


def test_the_preview_follows_what_you_type(world, open_as):
    MessageTemplate.objects.filter(name="coin-price").update(text="قیمت %token امروز")
    page = open_as(world.users["operator"], "/campaigns/fresh/settings/")
    preview = page.locator("#preview")
    expect(preview).to_contain_text("قیمت نفت امروز")
    page.get_by_label(f"{fa('Fixed value')}: token", exact=True).fill("طلا")
    expect(preview).to_contain_text("قیمت طلا امروز", timeout=5_000)


def test_a_translation_row_is_added_and_removed(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/fresh/settings/")
    rows = page.locator("#value-map-rows tr")
    expect(rows).to_have_count(1)
    page.get_by_role("button", name=fa("Add a translation"), exact=True).click()
    expect(rows).to_have_count(2)
    rows.nth(1).get_by_role("button", name=fa("Remove"), exact=True).click()
    expect(rows).to_have_count(1)


def test_unsaved_changes_are_not_left_behind_silently(world, open_as):
    page = open_as(world.users["operator"], "/campaigns/fresh/settings/")
    asked: list[str] = []

    def on_dialog(dialog):
        asked.append(dialog.type)
        dialog.dismiss()  # stay on the page

    page.on("dialog", on_dialog)
    page.get_by_label(f"{fa('Fixed value')}: token", exact=True).fill("طلا")
    page.get_by_role("link", name=fa("Back to the campaign"), exact=True).click()
    page.wait_for_timeout(500)
    assert asked == ["beforeunload"]
    assert page.url.endswith("/campaigns/fresh/settings/")
