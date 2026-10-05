"""Holding all sending, an admin's emergency stop (R5 of the 2026-10-05
review): a running send stops within a heartbeat and waits, a test SMS is
cancelled, nothing new starts, and the CLI on the same folder refuses to
send. Lifting the hold resumes exactly the sends it stopped; nobody gets an
SMS twice."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("django")

from click.testing import CliRunner  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender import sharing  # noqa: E402
from sms_sender.cli import cli  # noqa: E402
from sms_sender.state import PENDING, SENT  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Job  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .test_jobs import PHONES, FakeEngine, campaign, counts, make_worker, queue_send, stop_after  # noqa: E402,F401

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def admin(make_user):
    return make_user("admin1", "admin")


def signed_in(user, verified) -> Client:
    client = Client()
    verified(client, user)
    return client


# ---------- holding and lifting ----------

def test_a_running_send_stops_and_goes_on_when_the_hold_is_lifted(campaign, admin):
    job = queue_send(campaign)
    engine = stop_after(2, lambda: services.hold_sending(admin))
    make_worker(engine).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.PAUSED and job.result["held"] is True
    assert counts(campaign) == {SENT: 2, PENDING: 4}

    # Nothing that sends is taken while held; the rest of the queue still is.
    assert make_worker(FakeEngine()).claim() is None
    reconcile = services.enqueue(campaign, Job.Kind.RECONCILE)
    assert make_worker(FakeEngine()).run_once().pk == reconcile.pk

    assert services.release_sending(admin) == 1
    job.refresh_from_db()
    assert job.state == Job.State.QUEUED
    rest = FakeEngine()
    make_worker(rest).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.DONE
    assert sorted(engine.fake.calls + rest.fake.calls) == sorted(PHONES)  # each exactly once


def test_a_send_an_operator_paused_stays_paused_after_the_hold(campaign, admin):
    job = queue_send(campaign)
    make_worker(stop_after(2, lambda: services.pause(job))).run_once()
    services.hold_sending(admin)
    assert services.release_sending(admin) == 0
    job.refresh_from_db()
    assert job.state == Job.State.PAUSED


def test_nothing_new_starts_while_held(campaign, admin, make_user):
    operator = make_user("operator1", "operator")
    Profile.objects.create(user=operator, test_phone="09120000099")
    waiting = services.request_test(campaign, operator)
    services.hold_sending(admin)
    waiting.refresh_from_db()
    assert waiting.state == Job.State.CANCELLED  # a test SMS on its way is cancelled
    for start in (lambda: services.request_test(campaign, operator),
                  lambda: services.start_send(campaign, operator)):
        with pytest.raises(services.JobConflict) as e:
            start()
        assert e.value.code == "held"
    paused = Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.PAUSED)
    with pytest.raises(services.JobConflict):
        services.resume(paused)


def test_the_hold_marks_the_folder_for_the_cli(campaign, admin, settings, tmp_path, monkeypatch):
    services.hold_sending(admin)
    folder = Path(settings.SMS_SENDER_DB_DIR)
    assert sharing.held(folder)["by"] == "admin1"

    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.txt").write_text(f"{PHONES[0]}\n", encoding="utf-8")
    result = CliRunner().invoke(cli, [
        "send", "--input", "in.txt", "--template", "t", "--token", "x",
        "--state", str(folder / "coin-7.db"),
    ])
    assert result.exit_code == 2 and "held from the dashboard (by admin1" in result.output
    assert counts(campaign) == {}  # nothing was even loaded

    services.release_sending(admin)
    assert sharing.held(folder) is None


def test_a_marker_that_cant_be_read_still_holds(tmp_path):
    (tmp_path / sharing.HOLD).write_text("not json", encoding="utf-8")
    assert sharing.held(tmp_path) == {}
    assert sharing.held(tmp_path / "elsewhere") is None


# ---------- the page ----------

def test_only_an_admin_holds_and_everyone_sees_it(campaign, admin, verified, make_user, monkeypatch, settings):
    settings.SANDBOX = True  # the status page asks the simulated Kavenegar and Shlink
    told = []
    monkeypatch.setattr("sms_sender_web.system.views.notify_text", lambda target, text: told.append(text) or True)
    SystemSettings.objects.update_or_create(pk=1, defaults={"notify_targets": ["slack:https://hooks.slack.com/x"]})
    operator = signed_in(make_user("operator1", "operator"), verified)
    assert 'id="hold-form"' not in operator.get("/status/").content.decode()
    assert operator.post("/system/hold/", {"action": "hold"}).status_code == 403
    assert not services.held()

    client = signed_in(admin, verified)
    page = client.get("/status/").content.decode()
    assert 'id="hold-form"' in page and "۰ ارسال در جریان" in page  # the confirmation counts them
    response = client.post("/system/hold/", {"action": "hold"})
    assert response.status_code == 302 and response["Location"] == "/status/"
    assert services.held().sending_held_by == admin
    assert 'id="hold-banner"' in operator.get("/").content.decode()  # on every page
    assert told == ["sms-sender: admin1 held all sending on the dashboard. Nothing is sent until an admin lifts the hold."]

    assert 'id="release-form"' in client.get("/status/").content.decode()
    client.post("/system/hold/", {"action": "release"})
    assert not services.held()
    assert 'id="hold-banner"' not in operator.get("/").content.decode()
    assert [e.action for e in AuditEvent.objects.filter(action__startswith="sending_").order_by("id")] == [
        "sending_held", "sending_released",
    ]
