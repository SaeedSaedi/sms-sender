"""Plan 05, P3: putting recipients back in the queue, by status (the CLI's
reset --status and retry-failed), each with its role and a confirmation
that names the number; reconcile options; and "did anyone get it twice?"
on the report."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import FAILED_PERMANENT, NEEDS_REVIEW, PENDING, SENT, UNKNOWN, StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.engine import campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Job  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402

from .world import PHONES, build_world  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)  # the worker's heartbeat writes from a thread


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


def statuses(slug: str) -> dict:
    from sms_sender_web.jobs.models import Campaign

    return StateStore(campaign_db(Campaign.objects.get(slug=slug))).counts()


def requeue_forms(html: str) -> set[str]:
    return {part.split('"')[0] for part in html.split('id="requeue-')[1:]}


def test_each_role_queues_again_what_it_may(world, verified):
    operator = signed_in(world.users["operator"], verified)
    admin = signed_in(world.users["admin"], verified)
    viewer = signed_in(world.users["viewer"], verified)
    # The completed campaign: one rejected, one unknown (and one not sent, queued already).
    assert requeue_forms(operator.get("/campaigns/completed/").content.decode()) == {FAILED_PERMANENT}
    # An admin also may send again to those who got it (after a typed name and a backup).
    assert requeue_forms(admin.get("/campaigns/completed/").content.decode()) == {FAILED_PERMANENT, UNKNOWN, SENT}
    assert requeue_forms(viewer.get("/campaigns/completed/").content.decode()) == set()
    assert operator.post("/campaigns/completed/requeue/", {"status": UNKNOWN}).status_code == 403
    assert operator.post("/campaigns/completed/requeue/", {"status": "pending"}).status_code == 404


def test_the_confirmation_names_the_number_and_the_consequence(world, verified):
    html = signed_in(world.users["admin"], verified).get("/campaigns/completed/").content.decode()
    form = html.split('id="requeue-unknown"')[0].rsplit("<form", 1)[1]
    assert "۱ گیرنده ممکن است پیامک را گرفته باشند" in form and "تطبیق با کاوه‌نگار" in form


def test_rejected_recipients_go_back_in_the_queue_and_its_recorded(world, verified):
    before = statuses("completed")
    operator = signed_in(world.users["operator"], verified)
    response = operator.post("/campaigns/completed/requeue/", {"status": FAILED_PERMANENT}, follow=True)
    assert "۱ گیرنده دوباره در صف است" in response.content.decode()
    after = statuses("completed")
    assert after.get(FAILED_PERMANENT, 0) == 0 and after[PENDING] == before[PENDING] + 1
    event = AuditEvent.objects.get(action="recipients_requeued")
    assert event.detail == {"status": FAILED_PERMANENT, "count": 1}


def test_nothing_goes_back_while_a_send_is_on_its_way(world, verified):
    before = statuses("sending")
    admin = signed_in(world.users["admin"], verified)
    admin.post("/campaigns/sending/requeue/", {"status": FAILED_PERMANENT})
    assert statuses("sending") == before
    assert "پس از پایان آن گیرندگان را دوباره در صف بگذارید" in admin.get("/campaigns/sending/").content.decode()


def test_sending_again_to_those_who_got_it_needs_the_name_and_a_backup(world, verified, tmp_path):
    admin = signed_in(world.users["admin"], verified)
    admin.post("/campaigns/completed/requeue/", {"status": SENT, "confirm": "Completed"})
    assert statuses("completed")[SENT] == 2 and not (tmp_path / "backups").exists()
    admin.post("/campaigns/completed/requeue/", {"status": SENT, "confirm": "completed"})
    assert SENT not in statuses("completed")
    (backup,) = list((tmp_path / "backups").iterdir())
    assert AuditEvent.objects.get(action="recipients_requeued").detail == {
        "status": SENT, "count": 2, "backup": backup.name,
    }


def test_reconcile_takes_the_clis_options(world, verified):
    operator = signed_in(world.users["operator"], verified)
    html = operator.get("/campaigns/completed/").content.decode()
    assert 'data-cli="reconcile --min-age"' in html and 'data-cli="reconcile --requeue-not-found"' in html
    operator.post("/campaigns/completed/reconcile/", {"min_age_minutes": "۰", "review_not_found": "on"})
    job = Job.objects.get(campaign__slug="completed", kind=Job.Kind.RECONCILE)
    assert job.params == {"min_age_sec": 0.0, "requeue_not_found": False}
    # Kavenegar (the sandbox) has no record of the unknown one: held for review, not queued.
    ran = Worker(worker_id="w1", heartbeat_sec=0.05).run_once()
    assert ran.pk == job.pk
    assert statuses("completed").get(NEEDS_REVIEW) == 1


def test_the_report_says_whether_anyone_got_it_twice(world, verified):
    viewer = signed_in(world.users["viewer"], verified)
    store = StateStore(campaign_db(world.campaigns["completed"]))
    for message_id in (5001, 5002):  # PHONES[0] was accepted twice
        store.record_attempt(phone=PHONES[0], kind="send", outcome="accepted", started_at=time.time(),
                             message_id=message_id)
    html = viewer.get("/reports/completed/").content.decode()
    section = html.split('id="sends-check"')[1].split("</section>")[0]
    assert "۱ گیرنده بیش از یک بار پیامک گرفته‌اند" in section and "۰۹۱۲*****۰۱" in section
    assert PHONES[0] not in html
    # Accepted rows with no call recorded (from before schema 2) can't be compared.
    older = viewer.get("/reports/halted/").content.decode()
    assert "۲ پیامک مربوط به پیش از ثبت تماس‌ها است" in older
