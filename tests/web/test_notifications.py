"""Plan 05, P5: notifications (the CLI's --notify) and defaults, on the
admins' system settings page. A target's secret never reaches a page or
the activity log; a send that ends is announced to every target."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

import sms_sender.notify as notify_module  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.system import views as system_views  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .test_jobs import FakeEngine, make_worker, queue_send  # noqa: E402
from .test_jobs import campaign as campaign  # noqa: E402,F401  (the fixture)

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "slack:https://hooks.slack.com/services/T000/B000/not-a-real-secret"


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def test_a_target_is_kept_but_only_ever_shown_masked(admin_client):
    admin_client.post("/system/", {"section": "notify_add", "target": SECRET})
    assert SystemSettings.load().notify_targets == [SECRET]
    html = admin_client.get("/system/").content.decode()
    assert "slack:&lt;redacted&gt;" in html and "not-a-real-secret" not in html
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert event.detail == {"notify_added": "slack:<redacted>"}
    html = admin_client.post("/system/", {"section": "notify_add", "target": "ftp://nope"}).content.decode()
    assert "slack:https://hooks.slack.com" in html and SystemSettings.load().notify_targets == [SECRET]


def test_a_test_notification_and_removing_a_target(admin_client, monkeypatch):
    sent = []
    monkeypatch.setattr(system_views, "notify_text", lambda target, text: sent.append((target, text)) or True)
    admin_client.post("/system/", {"section": "notify_add", "target": SECRET})
    html = admin_client.post("/system/", {"section": "notify_test", "index": "0"}, follow=True).content.decode()
    assert sent and sent[0][0] == SECRET and "test notification" in sent[0][1]
    assert "فرستاده شد" in html
    admin_client.post("/system/", {"section": "notify_remove", "index": "0"})
    assert SystemSettings.load().notify_targets == []


def test_a_finished_send_is_announced_to_every_target(campaign, monkeypatch):
    SystemSettings.objects.update_or_create(pk=1, defaults={"notify_targets": [SECRET, "https://example.invalid/hook"]})
    heard = []
    monkeypatch.setattr(notify_module, "notify", lambda target, summary, **kw: heard.append((target, summary.sent, kw)))
    queue_send(campaign)
    make_worker(FakeEngine()).run_once()
    assert [(t, sent) for t, sent, _ in heard] == [(SECRET, 6), ("https://example.invalid/hook", 6)]
    assert heard[0][2] == {"heading": "coin-7"}
    # A test SMS is nobody's send: no announcement.
    heard.clear()
    Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, params={"test_number": "09120000099"},
                       settings_hash=services.settings_hash(campaign))
    make_worker(FakeEngine()).run_once()
    assert heard == []


def test_new_campaigns_start_with_the_defaults(admin_client, make_user, verified, settings, tmp_path):
    admin_client.post("/system/", {"section": "defaults", "default_window": "۰۹:۰۰-۲۰:۰۰", "default_rate": "5/s"})
    current = SystemSettings.load()
    assert (current.default_send_window, current.default_rate) == ("09:00-20:00", "5/s")
    html = admin_client.post("/system/", {"section": "defaults", "default_window": "late", "default_rate": ""}).content.decode()
    assert "HH:MM-HH:MM" in html and SystemSettings.load().default_send_window == "09:00-20:00"
    from sms_sender_web.segments.models import Segment

    settings.DATA_DIR = tmp_path
    Segment.objects.create(slug="vip", name="VIP", status=Segment.Status.READY, columns=["phone"])
    operator = Client()
    verified(operator, make_user("operator1", "operator"))
    operator.post("/campaigns/new/", {"name": "x", "slug": "x-1", "segment": "vip", "template": "coin-price"})
    made = Campaign.objects.get(slug="x-1").settings
    assert (made["send_window"], made["rate"]) == ("09:00-20:00", "5/s")
