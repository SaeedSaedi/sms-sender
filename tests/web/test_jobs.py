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
from sms_sender_web.jobs.worker import MAX_ATTEMPTS, Worker  # noqa: E402

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

    def sender(self):
        return self.fake

    def link_client(self):
        raise AssertionError("no links in these tests")

    def runner(self, campaign, reporter, *, test_number=None, cost_per_sms=None):
        runner = Runner(
            input_path=campaign.settings["input"], state=self.state(campaign), sender=self.fake,
            workers=1, campaign=campaign.slug, reporter=reporter, preflight=False,
            install_signal_handlers=False, approval_test_number=test_number,
            test_only=test_number is not None, cost_per_sms=cost_per_sms,
        )
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
    job = services.enqueue(campaign, Job.Kind.SEND)
    engine = FakeEngine()
    ran = make_worker(engine).run_once()
    assert ran.pk == job.pk
    assert ran.state == Job.State.DONE
    assert sorted(engine.fake.calls) == sorted(PHONES)
    assert ran.result["sent"] == 6 and ran.progress["sent"] == 6
    assert (ran.lease_owner, ran.lease_until, ran.attempts) == ("", None, 1)
    assert ran.finished_at is not None
    assert counts(campaign) == {SENT: 6}


def test_two_workers_never_get_the_same_job(campaign):
    services.enqueue(campaign, Job.Kind.SEND)
    first = make_worker(FakeEngine(), "w1").claim()
    assert first is not None and first.lease_owner == "w1"
    assert make_worker(FakeEngine(), "w2").claim() is None


def test_a_dead_workers_job_is_taken_over(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="dead", attempts=1,
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    taken = make_worker(FakeEngine(), "w2").claim()
    assert (taken.pk, taken.lease_owner, taken.attempts) == (job.pk, "w2", 2)


def test_a_live_workers_job_is_left_alone(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="alive",
        lease_until=timezone.now() + timedelta(seconds=60),
    )
    assert make_worker(FakeEngine(), "w2").claim() is None


def test_a_job_that_keeps_killing_workers_is_given_up(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
    Job.objects.filter(pk=job.pk).update(
        state=Job.State.RUNNING, lease_owner="dead", attempts=MAX_ATTEMPTS,
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    assert make_worker(FakeEngine()).claim() is None
    job.refresh_from_db()
    assert job.state == Job.State.FAILED and "given up" in job.last_error


# ---------- pause, resume, cancel, shutdown ----------

def test_pause_mid_run_then_resume_sends_everyone_once(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
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
    job = services.enqueue(campaign, Job.Kind.SEND)
    engine = stop_after(2, lambda: services.cancel(job))
    make_worker(engine).run_once()
    job.refresh_from_db()
    assert job.state == Job.State.CANCELLED
    assert job.result["cancelled"] == 4
    assert counts(campaign) == {SENT: 2, CANCELLED: 4}


def test_a_worker_shutdown_queues_the_job_again(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
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
    first = services.enqueue(campaign, Job.Kind.SEND)
    assert services.enqueue(campaign, Job.Kind.SEND).pk == first.pk


def test_a_waiting_job_pauses_and_resumes_without_running(campaign):
    job = services.pause(services.enqueue(campaign, Job.Kind.SEND))
    assert job.state == Job.State.PAUSED
    assert make_worker(FakeEngine()).claim() is None
    assert services.resume(job).state == Job.State.QUEUED


def test_cancelling_a_paused_job_cancels_its_waiting_recipients(campaign):
    job = services.enqueue(campaign, Job.Kind.SEND)
    make_worker(stop_after(2, lambda: services.pause(job))).run_once()
    job = services.cancel(Job.objects.get(pk=job.pk))
    assert job.state == Job.State.CANCELLED and job.result == {"cancelled": 4}
    assert counts(campaign) == {SENT: 2, CANCELLED: 4}


# ---------- failures ----------

def test_a_run_that_cannot_start_fails_with_the_reason(campaign):
    campaign.settings = {**campaign.settings, "input": "/nonexistent/list.txt"}
    campaign.save()
    services.enqueue(campaign, Job.Kind.SEND)
    job = make_worker(FakeEngine()).run_once()
    assert job.state == Job.State.FAILED
    assert "list.txt" in job.last_error


def test_a_halted_run_fails_and_says_why(campaign):
    class NoCredit(FakeKavenegar):
        def send(self, phone, tokens=None):
            raise HaltError(418, "insufficient credit")

    services.enqueue(campaign, Job.Kind.SEND)
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
    job = services.enqueue(campaign, Job.Kind.SEND)
    seen = []

    class Slow(FakeKavenegar):
        def send(self, phone, tokens=None):
            seen.append(Job.objects.get(pk=job.pk).lease_until)
            time.sleep(0.12)
            return super().send(phone, tokens)

    make_worker(FakeEngine(Slow()), lease=timedelta(seconds=30)).run_once()
    assert seen[-1] > seen[0]  # renewed while it ran
