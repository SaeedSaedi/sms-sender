"""Plan 05, P4: the sending policy. The frequency cap, an admin's setting
for every campaign (decision 6: off until set), counted by the check and
applied by every send; and suppression for every campaign or just one
(the CLI's --opt-out), numbers or a file."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender.frequency import FrequencyCap  # noqa: E402
from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns.checks import check_campaign  # noqa: E402
from sms_sender_web.jobs.engine import Engine  # noqa: E402
from sms_sender_web.suppression.models import Suppression  # noqa: E402
from sms_sender_web.suppression.service import phones_for  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .world import PHONES, build_world  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def signed_in(user, verified) -> Client:
    client = Client()
    verified(client, user)
    return client


def test_the_cap_is_off_until_an_admin_sets_it(world, verified):
    assert SystemSettings.load().frequency_cap is None
    admin = signed_in(world.users["admin"], verified)
    admin.post("/system/", {"cap": "on", "cap_sms": "۲", "cap_days": "7"})
    assert SystemSettings.load().frequency_cap == FrequencyCap(2, 7)
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert event.detail == {"frequency_cap": {"before": "off", "after": "2/7"}}
    html = admin.post("/system/", {"cap": "on", "cap_sms": "500", "cap_days": "7"}).content.decode()
    assert "حداکثر ۱ تا ۲۰ پیامک" in html and SystemSettings.load().frequency_cap == FrequencyCap(2, 7)
    admin.post("/system/", {})  # unticked: off again
    assert SystemSettings.load().frequency_cap is None
    operator = signed_in(world.users["operator"], verified)
    assert operator.get("/system/").status_code == 403


def test_every_send_and_the_check_apply_it(world, settings, tmp_path):
    settings_row = SystemSettings.load()
    settings_row.frequency_cap_sms, settings_row.frequency_cap_days = 1, 7
    settings_row.save()
    campaign = world.campaigns["fresh"]
    assert Engine().runner(campaign, reporter=None).frequency_cap == FrequencyCap(1, 7)
    # Another campaign sent one of fresh's list an SMS yesterday.
    other = StateStore(tmp_path / "db" / "elsewhere.db")
    other.record_attempt(phone=PHONES[3], kind="send", outcome="accepted", started_at=time.time() - 86400,
                         message_id=99)
    result = check_campaign(campaign)
    assert (result.capped, result.cap, result.to_send) == (1, "1/7", len(PHONES) - 1)


def test_the_check_shows_the_cap_only_when_one_is_set(world, verified):
    operator = signed_in(world.users["operator"], verified)
    assert "بیش از سقف ارسال" not in operator.get("/campaigns/fresh/").content.decode()
    SystemSettings.objects.update_or_create(pk=1, defaults={"frequency_cap_sms": 3, "frequency_cap_days": 30})
    assert "بیش از سقف ارسال" in operator.get("/campaigns/fresh/").content.decode()


def test_numbers_can_be_suppressed_for_one_campaign(world, verified):
    operator = signed_in(world.users["operator"], verified)
    operator.post("/suppression/", {"action": "add", "numbers": "09120000071", "scope": "fresh"})
    entry = Suppression.objects.get(phone="09120000071")
    assert entry.campaign == world.campaigns["fresh"]
    assert "09120000071" in phones_for(world.campaigns["fresh"])
    assert "09120000071" not in phones_for(world.campaigns["approved"])  # only that one
    assert AuditEvent.objects.get(action="suppression_added").campaign == "fresh"
    # A file for one campaign: the CLI's --opt-out.
    upload = SimpleUploadedFile("optout.csv", b"phone\n09120000072\n")
    operator.post("/suppression/", {"action": "add", "file": upload, "scope": "approved"})
    assert Suppression.objects.get(phone="09120000072").campaign == world.campaigns["approved"]
    html = operator.post("/suppression/", {"action": "add", "numbers": "09120000073", "scope": "nope"}).content.decode()
    assert "یک کمپین از فهرست انتخاب کنید" in html and not Suppression.objects.filter(phone="09120000073").exists()
