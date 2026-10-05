"""Finding one number across every campaign (R5 of the 2026-10-05 review):
operators type it in a POST form (never in a URL), and every lookup is in
the activity log."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("django")

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.engine import campaign_db  # noqa: E402

from .test_rounds import signed_in  # noqa: E402
from .world import PHONES, build_world  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def test_operators_look_numbers_up_and_viewers_dont(world, verified):
    viewer = signed_in(world.users["viewer"], verified)
    assert viewer.get("/numbers/").status_code == 403
    assert 'href="/numbers/"' not in viewer.get("/").content.decode()
    operator = signed_in(world.users["operator"], verified)
    assert 'href="/numbers/"' in operator.get("/").content.decode()
    assert 'name="number"' in operator.get("/numbers/").content.decode()


def test_a_number_shows_every_campaign_that_sent_to_it(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.post("/numbers/", {"number": "+98 912 000 0001"}).content.decode()  # PHONES[0], typed loosely
    # Sent by four campaigns of the test world; in the lists of the two that only tested.
    for slug in ("sending", "paused", "halted", "completed"):
        assert f'href="/campaigns/{slug}/"' in html
    assert 'href="/campaigns/approved/"' in html and 'href="/campaigns/scheduled/"' in html
    figures = html.split('class="figures"')[1].split("</dl>")[0]
    assert "<dd class=\"num\">۴</dd>" in figures  # SMS accepted, one per campaign
    event = AuditEvent.objects.get(action="number_looked_up")
    assert event.detail == {"phone": PHONES[0], "campaigns": 6}


def test_a_number_sent_twice_by_one_campaign_is_called_out(world, verified):
    store = StateStore(campaign_db(world.campaigns["completed"]))
    for message_id in (1, 2):
        store.record_attempt(phone=PHONES[0], kind="send", outcome="accepted", started_at=time.time(),
                             message_id=message_id)
    html = signed_in(world.users["operator"], verified).post("/numbers/", {"number": PHONES[0]}).content.decode()
    assert "از یک کمپین بیش از یک پیامک" in html and 'class="is-danger"' in html


def test_a_suppressed_number_says_so(world, verified):
    html = signed_in(world.users["operator"], verified).post("/numbers/", {"number": "09120000050"}).content.decode()
    assert "هیچ کمپینی این شماره را ندارد" in html
    assert "در فهرست عدم ارسال" in html and "همه کمپین‌ها" in html


def test_something_that_isnt_a_number_is_refused(world, verified):
    html = signed_in(world.users["operator"], verified).post("/numbers/", {"number": "12"}).content.decode()
    assert 'aria-invalid="true"' in html and "این شماره موبایل نیست" in html
    assert not AuditEvent.objects.filter(action="number_looked_up").exists()
