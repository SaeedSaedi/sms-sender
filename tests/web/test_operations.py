"""Plan 05, P5: operations from the dashboard. Backups made and checked
(the server's backup / verify_backup), a campaign's records purged after
a typed confirmation and a backup (the CLI's purge), a CLI campaign
brought to the dashboard, and the status page's version and queue."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns import lifecycle as lc  # noqa: E402
from sms_sender_web.jobs.engine import campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402

from .world import build_world  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    settings.BACKUP_DIR = tmp_path / "backups"
    return build_world(tmp_path)


def signed_in(user, verified) -> Client:
    client = Client()
    verified(client, user)
    return client


def test_an_admin_backs_up_and_checks_a_backup(world, verified, tmp_path):
    admin = signed_in(world.users["admin"], verified)
    html = admin.post("/system/backups/", {"action": "create"}, follow=True).content.decode()
    (backup,) = list((tmp_path / "backups").iterdir())
    assert "نسخه پشتیبان گرفته شد" in html and (backup / "db" / "completed.db").is_file()
    assert AuditEvent.objects.get(action="backup_made").detail == {"backup": backup.name}
    html = admin.post("/system/backups/", {"action": "verify", "name": backup.name}, follow=True).content.decode()
    assert "کامل است" in html
    (backup / "segments" / "vip.csv").write_text("tampered\n", encoding="utf-8")
    html = admin.post("/system/backups/", {"action": "verify", "name": backup.name}, follow=True).content.decode()
    assert "مشکل دارد" in html
    assert [e.detail["ok"] for e in AuditEvent.objects.filter(action="backup_verified").order_by("id")] == [True, False]
    assert signed_in(world.users["operator"], verified).get("/system/backups/").status_code == 403


def test_a_campaign_is_purged_only_after_its_name_and_a_backup(world, verified, tmp_path):
    admin = signed_in(world.users["admin"], verified)
    db = campaign_db(world.campaigns["completed"])
    admin.post("/campaigns/completed/purge/", {"confirm": "Completed"})
    assert db.exists() and not (tmp_path / "backups").exists()
    response = admin.post("/campaigns/completed/purge/", {"confirm": "completed"})
    assert response.status_code == 302 and response["Location"] == "/campaigns/"
    assert not db.exists() and not Campaign.objects.filter(slug="completed").exists()
    (backup,) = list((tmp_path / "backups").iterdir())
    assert (backup / "db" / "completed.db").is_file()  # what it recorded is in the backup
    assert AuditEvent.objects.get(action="campaign_purged").detail == {"backup": backup.name}
    operator = signed_in(world.users["operator"], verified)
    assert operator.post("/campaigns/approved/purge/", {"confirm": "approved"}).status_code == 403


def test_no_purge_while_a_job_is_on_its_way(world, verified):
    admin = signed_in(world.users["admin"], verified)
    admin.post("/campaigns/sending/purge/", {"confirm": "sending"})
    assert Campaign.objects.filter(slug="sending").exists() and campaign_db(world.campaigns["sending"]).exists()


def test_a_cli_campaign_comes_to_the_dashboard(world, verified, tmp_path):
    cli_db = StateStore(tmp_path / "db" / "coin-cli.db")
    cli_db.bind_campaign("coin-cli", {"template": "coin-price", "tokens": {"token": "نفت"},
                                      "token_columns": {"token10": "first_name"}, "value_maps": {}})
    cli_db.upsert_pending([("09120000041", "09120000041")])
    cli_db.claim("09120000041")
    cli_db.mark_sent("09120000041", 7001, 200, 3020)
    operator = signed_in(world.users["operator"], verified)
    assert 'action="/campaigns/coin-cli/adopt/"' in operator.get("/campaigns/").content.decode()
    response = operator.post("/campaigns/coin-cli/adopt/")
    assert response.status_code == 302 and response["Location"] == "/campaigns/coin-cli/"
    campaign = Campaign.objects.get(slug="coin-cli")
    assert campaign.settings["template"] == "coin-price" and campaign.settings["tokens"] == {"token": "نفت"}
    assert lc.lifecycle(campaign).stage == lc.DRAFT
    # Its message is fixed by what it sent: it only needs its next segment.
    html = operator.get("/campaigns/coin-cli/").content.decode()
    assert 'id="segment-form"' in html and 'value="vip-2"' in html
    operator.post("/campaigns/coin-cli/segment/", {"segment": "vip-2"})
    campaign.refresh_from_db()
    assert campaign.settings["segment"] == "vip-2" and lc.lifecycle(campaign).stage == lc.READY
    assert AuditEvent.objects.filter(action="campaign_adopted", campaign="coin-cli").exists()
    assert operator.post("/campaigns/coin-cli/adopt/").status_code == 404  # once
    assert signed_in(world.users["viewer"], verified).post("/campaigns/completed/adopt/").status_code == 403


def test_the_status_page_shows_the_version_queue_and_last_backup(world, verified, tmp_path):
    Job.objects.create(campaign=world.campaigns["fresh"], kind=Job.Kind.DELIVERY)  # one waiting
    admin = signed_in(world.users["admin"], verified)
    html = admin.get("/status/").content.decode()
    assert "نسخه" in html and "هنوز هیچ" in html  # no backup yet
    admin.post("/system/backups/", {"action": "create"})
    html = admin.get("/status/").content.decode()
    assert "هنوز هیچ" not in html and 'href="/system/backups/"' in html
    assert "قدیمی‌ترین، در انتظار از" in html
    manifest = json.loads(next((tmp_path / "backups").iterdir()).joinpath("manifest.json").read_text())
    assert manifest["files"]
