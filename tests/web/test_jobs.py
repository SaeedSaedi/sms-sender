"""Durable jobs and the worker (spec 4.6, 4.8): claim, lease, heartbeat,
pause / resume / cancel, take-over after a crash, periodic updates. Every
test uses a fake Kavenegar: nothing is sent."""
import time
from datetime import timedelta

import pytest

pytest.importorskip("django")

from django.core.management import call_command  # noqa: E402
from django.utils import timezone  # noqa: E402

from sms_sender.runner import Runner  # noqa: E402
from sms_sender.sender import HaltError  # noqa: E402
from sms_sender.state import CANCELLED, PENDING, SENT, StateStore  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.worker import MAX_ATTEMPTS, SEND_KINDS, SHORT_KINDS, Worker  # noqa: E402

from ..test_runner import FakeSender  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

PHONES = [f"0912{i:07d}" for i in range(1, 7)]


class FakeKavenegar(FakeSender):
    def delivery_statuses(self, message_ids):
        return {mid: 10 for mid in message_ids}


class FakeEngine(Engine):
    """The CLI's engine with a fake Kavenegar: nothing is ever sent."""

    def __init__(self, sender=None):
        self.fake = sender or FakeKavenegar()
        self.runners = []

    def sender(self, campaign=None):
        return self.fake

    def link_client(self, campaign=None):
        raise AssertionError("no links in these tests")

    def runner(self, campaign, reporter, *, test_number=None, team_numbers=(), cost_per_sms=None,
               smoke_test=False, allow_settings_change=False):
        runner = Runner(
            input_path=campaign.settings["input"], state=self.state(campaign), sender=self.fake,
            workers=1, campaign=campaign.slug, reporter=reporter, preflight=False,
            install_signal_handlers=False, approval_test_number=test_number, approval_test_team=team_numbers,
            test_only=test_number is not None, cost_per_sms=cost_per_sms,
            allow_settings_change=allow_settings_change,
        )
        runner.asked_smoke_test = smoke_test  # without preflight it wouldn't run anyway
        self.runners.append(runner)
        return runner


@pytest.fixture
def campaign(tmp_path, settings):
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    inp = tmp_path / "in.txt"
    inp.write_text("\n".join(PHONES) + "\n", encoding="utf-8")
    return Campaign.objects.create(
        slug="coin-7", name="Coin 7", settings={"input": str(inp), "template": "t"},
    )


def queue_send(campaign):
    """A send as start_send makes it: with the hash of the settings its test
    SMS approved (the worker sends nothing without it)."""
    job = services.enqueue(campaign, Job.Kind.SEND)
    Job.objects.filter(pk=job.pk).update(settings_hash=services.settings_hash(campaign))
    job.refresh_from_db()
    return job


def make_worker(engine, worker_id="w1", **kw):
    return Worker(engine, worker_id=worker_id, heartbeat_sec=0.05, **kw)


def counts(campaign):
    return StateStore(campaign_db(campaign)).counts()


def stop_after(n, then):
    """A fake Kavenegar that, after its n-th send, does `then(engine)` and
    waits until the run has noticed (the heartbeat calls Runner.cancel)."""
    holder = {}

    class Stopping(FakeKavenegar):
        def send(self, phone, tokens=None):
            result = super().send(phone, tokens)
            if len(self.calls) == n:
                then()
                assert holder["engine"].runners[-1]._stop.wait(5), "the heartbeat never saw it"
            return result

    engine = FakeEngine(Stopping())
    holder["engine"] = engine
    return engine


# ---------- a whole run ----------

def test_a_send_job_runs_to_the_end(campaign):
    job = queue_send(campaign)
    engine = FakeEngine()
    ran = make_worker(engine).run_once()
    assert ran.pk == job.pk
    assert ran.state == Job.State.DONE
    assert sorted(engine.fake.calls) == sorted(PHONES)
    assert ran.result["sent"] == 6 and ran.progress["sent"] == 6
    assert (ran.lease_owner, ran.lease_until, ran.attempts) == ("", None, 1)
    assert ran.finished_at is not None
    assert counts(campaign) == {SENT: 6}


def test_the_smoke_test_option_reaches_the_runner(campaign):
    job = queue_send(campaign)
    Job.objects.filter(pk=job.pk).update(params={"smoke_test": True})
    engine = FakeEngine()
    make_worker(engine).run_once()
    assert engine.runners[-1].asked_smoke_test is True


def test_two_workers_never_get_the_same_job(campaign):
    queue_send(campaign)
    first = make_worker(FakeEngine(), "w1").claim()
    assert first is not None and first.lease_owner == "w1"
    assert make_worker(FakeEngine(), "w2").claim() is None


def test_a_dead_workers_job_is_taken_over(campaign):
    job = queue_send(campaign)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="dead", attempts=1,
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    taken = make_worker(FakeEngine(), "w2").claim()
    assert (taken.pk, taken.lease_owner, taken.attempts) == (job.pk, "w2", 2)


def test_a_live_workers_job_is_left_alone(campaign):
    job = queue_send(campaign)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="alive",
        lease_until=timezone.now() + timedelta(seconds=60),
    )
    assert make_worker(FakeEngine(), "w2").claim() is None


def test_a_job_that_keeps_killing_workers_is_given_up(campaign):
    job = queue_send(campaign)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="dead", attempts=MAX_ATTEMPTS,
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    assert make_worker(FakeEngine()).claim() is None
    job.refresh_from_db()
    assert job.state == Job.State.FAILED and "given up" in job.last_error


# ---------- pause, resume, cancel, shutdown ----------

def test_pause_mid_run_then_resume_sends_everyone_once(campaign):
    job = queue_send(campaign)
    engine = stop_after(2, lambda: services.pause(job))
    make_worker(engine).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.PAUSED and job.control == ""
    assert len(engine.fake.calls) == 2
    assert counts(campaign) == {SENT: 2, PENDING: 4}

    services.resume(job)
    rest = FakeEngine()
    make_worker(rest).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.DONE
    assert sorted(engine.fake.calls + rest.fake.calls) == sorted(PHONES)  # each exactly once
    assert counts(campaign) == {SENT: 6}


def test_cancel_mid_run_cancels_everyone_still_waiting(campaign):
    job = queue_send(campaign)
    engine = stop_after(2, lambda: services.cancel(job))
    make_worker(engine).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.CANCELLED
    assert job.result["cancelled"] == 4
    assert counts(campaign) == {SENT: 2, CANCELLED: 4}


def test_a_worker_shutdown_queues_the_job_again(campaign):
    job = queue_send(campaign)
    worker = None
    engine = stop_after(3, lambda: worker.stop.set())
    worker = make_worker(engine)
    worker.run_once()
    job.refresh_from_db()
    assert job.state == Job.State.QUEUED and job.lease_owner == ""
    assert counts(campaign) == {SENT: 3, PENDING: 3}

    rest = FakeEngine()
    make_worker(rest, "w2").run_once()
    job.refresh_from_db()
    assert job.state == Job.State.DONE and job.attempts == 2
    assert sorted(engine.fake.calls + rest.fake.calls) == sorted(PHONES)


def test_one_active_send_job_per_campaign(campaign):
    first = queue_send(campaign)
    assert queue_send(campaign).pk == first.pk


def test_a_waiting_job_pauses_and_resumes_without_running(campaign):
    job = services.pause(queue_send(campaign))
    assert job.state == Job.State.PAUSED
    assert make_worker(FakeEngine()).claim() is None
    assert services.resume(job).state == Job.State.QUEUED


def test_cancelling_a_paused_job_cancels_its_waiting_recipients(campaign):
    job = queue_send(campaign)
    make_worker(stop_after(2, lambda: services.pause(job))).run_once()
    job = services.cancel(Job.objects.get(pk=job.pk))
    assert job.state == Job.State.CANCELLED and job.result == {"cancelled": 4}
    assert counts(campaign) == {SENT: 2, CANCELLED: 4}


# ---------- failures ----------

def test_a_run_that_cannot_start_fails_with_the_reason(campaign):
    campaign.settings = {**campaign.settings, "input": "/nonexistent/list.txt"}
    campaign.save()
    queue_send(campaign)
    job = make_worker(FakeEngine()).run_once()
    assert job.state == Job.State.FAILED
    assert "list.txt" in job.last_error


def test_a_halted_run_fails_and_says_why(campaign):
    class NoCredit(FakeKavenegar):
        def send(self, phone, tokens=None):
            raise HaltError(418, "insufficient credit")

    queue_send(campaign)
    job = make_worker(FakeEngine(NoCredit())).run_once()
    assert job.state == Job.State.FAILED
    assert "insufficient credit" in job.last_error


# ---------- periodic updates ----------

def test_delivery_updates_are_scheduled_while_kavenegar_answers(campaign):
    store = StateStore(campaign_db(campaign))
    store.upsert_pending([(PHONES[0], PHONES[0])])
    store.claim(PHONES[0])
    store.mark_sent(PHONES[0], message_id=4242, status_code=200)
    worker = make_worker(FakeEngine())
    worker.schedule(force=True)
    worker.schedule(force=True)  # not twice
    jobs = Job.objects.filter(campaign=campaign, kind=Job.Kind.DELIVERY)
    assert jobs.count() == 1
    job = worker.run_once()
    assert job.state == Job.State.DONE and job.result == {"checked": 1, "updated": 1}
    assert store.delivery_counts() == {10: 1}
    worker.schedule(force=True)  # just done: not due again yet
    assert jobs.count() == 1


def test_the_worker_command_runs_one_job(campaign, capsys):
    call_command("run_worker", "--once")
    assert "no job waiting" in capsys.readouterr().out


def test_the_heartbeat_keeps_the_lease_alive(campaign):
    job = queue_send(campaign)
    seen = []

    class Slow(FakeKavenegar):
        def send(self, phone, tokens=None):
            seen.append(Job.objects.get(pk=job.pk).lease_until)
            time.sleep(0.12)
            return super().send(phone, tokens)

    make_worker(FakeEngine(Slow()), lease=timedelta(seconds=30)).run_once()
    assert seen[-1] > seen[0]  # renewed while it ran


def test_the_worker_leaves_a_heartbeat_for_the_cli(settings, tmp_path):
    """A CLI outside Docker Desktop's VM reads it before changing any DB in
    the folder (sharing.py)."""
    import json

    from sms_sender import sharing

    settings.SMS_SENDER_DB_DIR = tmp_path
    Worker(worker_id="w1").beat()
    data = json.loads((tmp_path / sharing.HEARTBEAT).read_text())
    assert data["worker"] == "w1" and data["kernel"] == sharing.kernel_id()
    assert sharing.foreign_worker(tmp_path) is None  # this kernel: nothing refused


# ---------- two lanes: sends, and the short jobs beside them ----------

def other_campaign(tmp_path, slug="coin-8"):
    inp = tmp_path / f"{slug}.txt"
    inp.write_text("09120001001\n09120001002\n", encoding="utf-8")
    return Campaign.objects.create(slug=slug, name=slug, settings={"input": str(inp), "template": "t"})


def test_each_lane_takes_only_its_kinds(campaign):
    send = queue_send(campaign)
    delivery = services.enqueue(campaign, Job.Kind.DELIVERY)
    worker = make_worker(FakeEngine())
    assert worker.claim(SHORT_KINDS).pk == delivery.pk
    assert worker.claim(SEND_KINDS).pk == send.pk


def test_a_test_sms_is_taken_while_another_campaign_sends(campaign, tmp_path):
    other = other_campaign(tmp_path)
    worker = make_worker(FakeEngine())
    queue_send(campaign)
    assert worker.claim(SEND_KINDS) is not None  # coin-7's send is running now
    test = services.enqueue(other, Job.Kind.TEST, params={"test_number": "09120009999"})
    assert worker.claim(SHORT_KINDS).pk == test.pk


def test_a_campaigns_own_locking_jobs_wait_for_each_other(campaign):
    worker = make_worker(FakeEngine())
    send = queue_send(campaign)
    assert worker.claim(SEND_KINDS).pk == send.pk
    reconcile = services.enqueue(campaign, Job.Kind.RECONCILE)
    delivery = services.enqueue(campaign, Job.Kind.DELIVERY)
    # The reconcile would need the run lock the send holds: it waits. Delivery
    # only writes its own columns, so it may go beside the send.
    assert worker.claim(SHORT_KINDS).pk == delivery.pk
    assert worker.claim(SHORT_KINDS) is None
    Job.objects.filter(pk=send.pk).update(state=Job.State.DONE, lease_until=None, lease_owner="")
    assert worker.claim(SHORT_KINDS).pk == reconcile.pk


def test_a_send_waits_while_its_campaigns_test_runs(campaign):
    worker = make_worker(FakeEngine())
    test = services.enqueue(campaign, Job.Kind.TEST, params={"test_number": "09120009999"})
    assert worker.claim(SHORT_KINDS).pk == test.pk
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, settings_hash=services.settings_hash(campaign))
    assert worker.claim(SEND_KINDS) is None


def test_a_dead_workers_send_is_still_taken_over(campaign):
    send = queue_send(campaign)
    assert make_worker(FakeEngine(), "dead").claim(SEND_KINDS).pk == send.pk
    Job.objects.filter(pk=send.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
    assert make_worker(FakeEngine(), "w2").claim(SEND_KINDS).pk == send.pk


def test_a_test_sms_finishes_while_a_long_send_runs(campaign, tmp_path):
    """The real case: an hour-long send, and an urgent alert to test."""
    import threading

    release = threading.Event()
    recipients = set(PHONES)

    class SlowForTheSend(FakeKavenegar):
        def send(self, phone, tokens=None):
            if phone in recipients:
                assert release.wait(30), "never released"
            return super().send(phone, tokens)

    engine = FakeEngine(SlowForTheSend())
    other = other_campaign(tmp_path)
    send = queue_send(campaign)
    worker = make_worker(engine)
    thread = threading.Thread(target=worker.run_forever, kwargs={"poll_sec": 0.05}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while Job.objects.get(pk=send.pk).state != Job.State.RUNNING:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        test = services.enqueue(other, Job.Kind.TEST, params={"test_number": "09120009999"})
        while Job.objects.get(pk=test.pk).state != Job.State.DONE:
            assert time.monotonic() < deadline, Job.objects.get(pk=test.pk).state
            time.sleep(0.05)
        assert Job.objects.get(pk=send.pk).state == Job.State.RUNNING  # still sending
    finally:
        release.set()
        deadline = time.monotonic() + 10
        while Job.objects.get(pk=send.pk).state == Job.State.RUNNING and time.monotonic() < deadline:
            time.sleep(0.05)
        worker.stop.set()
        thread.join(10)
    assert Job.objects.get(pk=send.pk).state == Job.State.DONE
    assert counts(campaign)[SENT] == len(PHONES)
