"""Plan 06, D2 in the browser: the pages of a campaign whose numbers were
removed, its report and a segment whose file went, at a phone's width and
a desktop's (axe finds nothing, nothing overflows, targets are big
enough)."""
from __future__ import annotations

import pytest
from django.utils import timezone

from sms_sender.state import StateStore
from sms_sender_web.jobs.engine import campaign_db
from sms_sender_web.segments.models import Segment

from ..world import build_world
from .conftest import axe_violations, overflow, shot, small_targets

pytestmark = pytest.mark.e2e

REMOVED_LINE = "شماره‌های آن در"


@pytest.fixture
def world(sandbox):
    world = build_world(sandbox)
    StateStore(campaign_db(world.campaigns["completed"])).remove_numbers()
    plain = Segment.objects.get(slug="plain")
    plain.delete_files()
    Segment.objects.filter(pk=plain.pk).update(status=Segment.Status.REMOVED, numbers_removed_at=timezone.now())
    return world


@pytest.mark.parametrize("width", [390, 1366])
@pytest.mark.parametrize("path, said", [
    ("/campaigns/completed/", REMOVED_LINE),
    ("/reports/completed/", REMOVED_LINE),
    ("/segments/plain/", "فایل آن در"),
])
def test_pages_without_the_numbers(world, open_as, width, path, said):
    page = open_as(world.users["operator"], path, width)
    page.get_by_text(said).first.wait_for()
    shot(page, f"retention{path.replace('/', '.')}@{width}")
    assert overflow(page) <= 1
    assert axe_violations(page) == []
    assert small_targets(page) == []


def test_the_report_lists_recipients_without_their_numbers(world, open_as):
    page = open_as(world.users["operator"], "/reports/completed/#recipients")
    cells = page.locator("#recipients tbody th")
    cells.first.wait_for()
    assert all(text.strip() == "حذف‌شده" for text in cells.all_inner_texts())
    assert page.get_by_role("button", name="نمایش").count() == 0  # nothing to reveal
