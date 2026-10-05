"""Plan 05, P2: a campaign's later rounds. The same message to another
segment (only the list changes, with a new test SMS, and anyone already
sent to is skipped); a new campaign from another's settings (the
dashboard's presets); and what fixes the settings: an SMS that may have
gone out, not a send that failed before sending anything."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.sendcheck import check_sends  # noqa: E402
from sms_sender.state import SENT, StateStore  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.backup import BackupError  # noqa: E402
from sms_sender_web.campaigns import lifecycle as lc  # noqa: E402
from sms_sender_web.campaigns.forms import free_slug  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.sandbox import read_outbox  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from .world import TEST_PHONE, build_world  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)  # the worker's heartbeat writes from a thread


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


def run(kind) -> Job:
    job = Worker(worker_id="w1", heartbeat_sec=0.05).run_once()
    assert job is not None and job.kind == kind
    job.refresh_from_db()
    return job


# ---------- the same message to another segment ----------

def test_a_finished_campaign_offers_the_segments_its_message_fits(world, verified):
    html = signed_in(world.users["operator"], verified).get("/campaigns/completed/").content.decode()
    form = html.split('id="segment-form"')[1].split("</form>")[0]
    assert 'value="vip-2"' in form and 'value="plain"' not in form and 'value="vip"' not in form
    assert "فقط شماره" in html  # listed apart: it has no first_name column
    viewer = signed_in(world.users["viewer"], verified).get("/campaigns/completed/").content.decode()
    assert 'id="segment-form"' not in viewer


def test_a_second_round_sends_only_to_whoever_hasnt_had_it(world, verified):
    campaign = world.campaigns["completed"]
    # One of the new list already got round one's SMS.
    Segment.objects.get(slug="vip-2").path.write_text(
        "phone,user_id,first_name\n09120000001,u-1,Ali\n09120000021,u-21,Sara\n", encoding="utf-8",
    )
    client = signed_in(world.users["operator"], verified)
    response = client.post("/campaigns/completed/segment/", {"segment": "vip-2"})
    assert response.status_code == 302
    campaign.refresh_from_db()
    assert (campaign.settings["segment"], campaign.settings["user_id_column"]) == ("vip-2", "user_id")
    assert campaign.settings["input"].endswith("vip-2.csv")
    assert lc.lifecycle(campaign).stage == lc.READY  # a new test SMS comes first
    event = AuditEvent.objects.get(action="segment_switched")
    assert event.detail == {"before": "vip", "after": "vip-2"}

    client.post("/campaigns/completed/test/")
    test = run(Job.Kind.TEST)
    assert test.state == Job.State.DONE, test.last_error
    client.post("/campaigns/completed/approve/", {"job": test.pk})
    client.post("/campaigns/completed/send/", {"when": "now"})
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.DONE, send.last_error

    # The test SMS and the one new recipient: 09120000001 isn't sent twice,
    # and round one's leftovers (not in this list) wait.
    assert sorted(sms["phone"] for sms in read_outbox()) == sorted([TEST_PHONE, "09120000021"])
    store = StateStore(campaign_db(campaign))
    assert store.status_for_phones(["09120000021"]) == {"09120000021": SENT}
    assert check_sends(store).ok


def test_the_list_stays_while_a_send_is_on_its_way(world, verified):
    client = signed_in(world.users["operator"], verified)
    client.post("/campaigns/sending/segment/", {"segment": "vip-2"})
    assert Campaign.objects.get(slug="sending").settings["segment"] == "vip"
    assert "پس از پایان آن گروه مخاطبان را تغییر دهید" in client.get("/campaigns/sending/").content.decode()


def test_only_a_segment_with_the_messages_columns_can_be_chosen(world, verified):
    client = signed_in(world.users["operator"], verified)
    assert client.post("/campaigns/completed/segment/", {"segment": "plain"}).status_code == 404
    assert client.post("/campaigns/completed/segment/", {"segment": "draft"}).status_code == 404
    assert Campaign.objects.get(slug="completed").settings["segment"] == "vip"
    viewer = signed_in(world.users["viewer"], verified)
    assert viewer.post("/campaigns/completed/segment/", {"segment": "vip-2"}).status_code == 403


# ---------- a new campaign from another's settings ----------

def test_a_campaign_is_duplicated_with_its_settings_only(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.get("/campaigns/completed/duplicate/").content.decode()
    assert 'value="پایان‌یافته (رونوشت)"' in html and 'value="completed-2"' in html
    response = client.post("/campaigns/completed/duplicate/", {"name": "دور دوم", "slug": "completed-2"})
    assert response.status_code == 302 and response["Location"] == "/campaigns/completed-2/settings/"
    copy = Campaign.objects.get(slug="completed-2")
    assert copy.settings == world.campaigns["completed"].settings and copy.name == "دور دوم"
    assert copy.created_by == world.users["operator"]
    assert not Job.objects.filter(campaign=copy).exists() and not campaign_db(copy).exists()
    assert lc.lifecycle(copy).stage == lc.READY
    assert AuditEvent.objects.get(action="campaign_duplicated").detail == {"source": "completed"}


def test_a_duplicate_needs_a_short_name_of_its_own(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.post("/campaigns/completed/duplicate/", {"name": "x", "slug": "approved"}).content.decode()
    assert 'id="id_slug_error"' in html and not Campaign.objects.filter(name="x").exists()
    viewer = signed_in(world.users["viewer"], verified)
    assert viewer.get("/campaigns/completed/duplicate/").status_code == 403


def test_the_suggested_short_name_is_the_next_free_one(world):
    assert free_slug("vip") == "vip-2"
    assert free_slug("coin-price-7") == "coin-price-8"
    Campaign.objects.create(slug="coin-price-8", name="x")
    (campaign_db(Campaign(slug="coin-price-9"))).write_bytes(b"")  # a CLI campaign's record
    assert free_slug("coin-price-7") == "coin-price-10"


# ---------- an admin continues with a changed message ----------

@pytest.fixture
def backups(settings, tmp_path):
    settings.BACKUP_DIR = tmp_path / "backups"
    return settings.BACKUP_DIR


def bind_as_sent(campaign) -> StateStore:
    """The campaign DB as a real run leaves it: bound to the settings it sent."""
    store = StateStore(campaign_db(campaign))
    store.bind_campaign(campaign.slug, Engine().runner(campaign, reporter=None).settings)
    return store


def test_an_admin_sees_the_change_and_an_operator_doesnt(world, verified):
    admin = signed_in(world.users["admin"], verified).get("/campaigns/halted/").content.decode()
    assert 'id="unlock-form"' in admin and 'pattern="halted"' in admin
    operator = signed_in(world.users["operator"], verified)
    assert 'id="unlock-form"' not in operator.get("/campaigns/halted/").content.decode()
    assert operator.post("/campaigns/halted/unlock/", {"confirm": "halted"}).status_code == 403


def test_a_wrong_confirmation_changes_nothing(world, verified, backups):
    client = signed_in(world.users["admin"], verified)
    client.post("/campaigns/halted/unlock/", {"confirm": "Halted"})
    assert Campaign.objects.get(slug="halted").unlocked_at is None
    assert not backups.exists()  # not even a backup
    assert not AuditEvent.objects.filter(action="message_unlocked").exists()


def test_the_rest_get_the_changed_message_after_a_backup_and_a_new_test(world, verified, backups):
    campaign = world.campaigns["halted"]
    store = bind_as_sent(campaign)
    client = signed_in(world.users["admin"], verified)

    # Locked: even an admin needs the unlock first.
    assert client.get("/campaigns/halted/settings/").status_code == 302
    response = client.post("/campaigns/halted/unlock/", {"confirm": "halted"})
    assert response.status_code == 302 and response["Location"] == "/campaigns/halted/settings/"
    (backup,) = list(backups.iterdir())
    assert (backup / "db" / "halted.db").is_file()
    event = AuditEvent.objects.get(action="message_unlocked")
    assert event.detail["backup"] == backup.name
    html = client.get("/campaigns/halted/settings/").content.decode()
    assert "برخی گیرندگان پیام قبلی را دریافت کرده‌اند" in html
    # Only for the admin: an operator still finds it fixed.
    operator = signed_in(world.users["operator"], verified)
    assert operator.get("/campaigns/halted/settings/").status_code == 302

    # The message changes: a new round, a test SMS the campaign DB accepts.
    campaign.refresh_from_db()
    campaign.settings["tokens"] = {"token": "طلا"}
    campaign.save()
    assert lc.lifecycle(campaign).stage == lc.READY
    Profile.objects.update_or_create(user=world.users["admin"], defaults={"test_phone": TEST_PHONE})
    client.post("/campaigns/halted/test/")
    test = run(Job.Kind.TEST)
    assert test.state == Job.State.DONE, test.last_error
    assert test.params["allow_settings_change"] is True
    client.post("/campaigns/halted/approve/", {"job": test.pk})
    client.post("/campaigns/halted/send/", {"when": "now"})
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.DONE, send.last_error

    # Those still waiting got «طلا»; the two sent before weren't sent again.
    outbox = {sms["phone"]: sms["tokens"]["token"] for sms in read_outbox()}
    assert outbox == {TEST_PHONE: "طلا", "09120000004": "طلا", "09120000005": "طلا", "09120000006": "طلا"}
    assert check_sends(store).ok
    # The send started, so the message is fixed again.
    assert not services.message_unlocked(campaign)
    assert client.get("/campaigns/halted/settings/").status_code == 302


def test_a_changed_message_without_the_unlock_is_refused_by_the_engine(world):
    """The CLI's own guard, under the dashboard's: a campaign DB that sent
    one message refuses a run with another."""
    campaign = world.campaigns["halted"]
    bind_as_sent(campaign)
    campaign.settings["tokens"] = {"token": "طلا"}
    campaign.save()
    Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, params={"test_number": TEST_PHONE})
    test = run(Job.Kind.TEST)
    assert test.state == Job.State.FAILED and test.result["stop_reason"] == "settings_mismatch"
    assert read_outbox() == []


def test_unlocking_a_paused_send_supersedes_it_without_cancelling_anyone(world, verified, backups):
    campaign = world.campaigns["paused"]
    before = StateStore(campaign_db(campaign)).counts()
    signed_in(world.users["admin"], verified).post("/campaigns/paused/unlock/", {"confirm": "paused"})
    send = Job.objects.get(campaign=campaign, kind=Job.Kind.SEND)
    assert send.state == Job.State.CANCELLED and send.result.get("superseded") is True
    assert StateStore(campaign_db(campaign)).counts() == before  # nobody cancelled
    assert lc.lifecycle(campaign).stage == lc.APPROVED  # the same message can simply go on


def test_no_unlock_while_a_send_runs_or_when_the_backup_fails(world, verified, backups, monkeypatch):
    client = signed_in(world.users["admin"], verified)
    client.post("/campaigns/sending/unlock/", {"confirm": "sending"})
    assert Campaign.objects.get(slug="sending").unlocked_at is None

    def broken(*args, **kwargs):
        raise BackupError("disk full")

    monkeypatch.setattr(services, "make_backup", broken)
    client.post("/campaigns/halted/unlock/", {"confirm": "halted"})
    assert Campaign.objects.get(slug="halted").unlocked_at is None
    assert "پشتیبان‌گیری انجام نشد، پس چیزی تغییر نکرد" in client.get("/campaigns/halted/").content.decode()
