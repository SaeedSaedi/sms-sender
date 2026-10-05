"""Plan 05, P4: a segment's actions. Start a campaign from it, download its
prepared copy, and replace its file while nobody has been sent from it; a
replaced file is another list, so campaigns using it need a new test."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns import lifecycle as lc  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from .world import SETTINGS, _test_job, build_world, open_window  # noqa: E402

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


def on_vip2(world) -> Campaign:
    """A campaign of the second list, its test approved, nothing sent."""
    vip2 = Segment.objects.get(slug="vip-2")
    campaign = Campaign.objects.create(slug="second", name="دوم", settings={
        **SETTINGS, "segment": "vip-2", "input": str(vip2.path), "send_window": open_window(),
    })
    _test_job(campaign, world.users["operator"], approved=True)
    return campaign


def test_a_campaign_starts_from_a_segment(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.get("/segments/vip-2/").content.decode()
    assert 'href="/campaigns/new/?segment=vip-2"' in html
    form = client.get("/campaigns/new/", {"segment": "vip-2"}).content.decode()
    assert '<option value="vip-2" selected>' in form


def test_operators_download_the_prepared_copy_and_its_recorded(world, verified):
    operator = signed_in(world.users["operator"], verified)
    response = operator.get("/segments/vip/download/")
    body = response.content.decode("utf-8")
    assert response["Content-Disposition"] == 'attachment; filename="vip.csv"'
    assert body.startswith("﻿phone,user_id,first_name\n")  # Excel shows Persian with the BOM
    assert AuditEvent.objects.get(action="segment_downloaded").detail == {"segment": "vip"}
    assert signed_in(world.users["viewer"], verified).get("/segments/vip/download/").status_code == 403


def test_a_file_nobody_was_sent_from_can_be_replaced(world, verified):
    campaign = on_vip2(world)
    assert lc.lifecycle(campaign).stage == lc.APPROVED
    client = signed_in(world.users["operator"], verified)
    assert "دوم" in client.get("/segments/vip-2/replace/").content.decode()  # who'll need a new test
    upload = SimpleUploadedFile("vip-2-new.csv", b"phone,user_id,first_name\n09120000031,u-31,Reza\n")
    response = client.post("/segments/vip-2/replace/", {"file": upload})
    assert response.status_code == 302 and response["Location"] == "/segments/vip-2/columns/"
    segment = Segment.objects.get(slug="vip-2")
    assert (segment.status, segment.version, segment.original_name) == (Segment.Status.DRAFT, 1, "vip-2-new.csv")
    assert segment.upload_path.read_bytes().startswith(b"phone,user_id")
    # Another list: the approval doesn't cover it.
    assert services.approval(campaign) is None and lc.lifecycle(campaign).stage == lc.READY
    assert AuditEvent.objects.get(action="segment_replaced").detail == {"segment": "vip-2", "file": "vip-2-new.csv"}


def test_a_file_someone_was_sent_from_stays(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.get("/segments/vip/").content.decode()
    assert "از این گروه مخاطبان ارسال کرده است" in html and 'href="/segments/vip/replace/"' not in html
    assert client.get("/segments/vip/replace/").status_code == 302
    upload = SimpleUploadedFile("x.csv", b"phone\n09120000031\n")
    client.post("/segments/vip/replace/", {"file": upload})
    assert Segment.objects.get(slug="vip").version == 0


def test_nor_while_a_send_that_uses_it_is_on_its_way(world, verified):
    campaign = on_vip2(world)
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.QUEUED,
                       settings_hash=services.settings_hash(campaign))
    client = signed_in(world.users["operator"], verified)
    assert "در جریان است" in client.get("/segments/vip-2/").content.decode()
    assert client.get("/segments/vip-2/replace/").status_code == 302


def test_an_unreplaced_segment_leaves_existing_approvals_alone(world):
    campaign = world.campaigns["approved"]
    test = services.latest_test(campaign)
    assert test.settings_hash == services.settings_hash(campaign)  # version 0 isn't part of the hash


def test_the_flow_shows_its_steps(world, verified):
    client = signed_in(world.users["operator"], verified)
    upload = client.get("/segments/upload/").content.decode()
    assert upload.count('aria-current="step"') == 1 and "فایل" in upload.split('aria-current="step"')[1][:200]
    done = client.get("/segments/vip/").content.decode()
    assert done.count("is-done") == 3  # file, columns, summary: all done
