"""The dashboard's background worker: one process, one job at a time (spec
4.8). Run it with `python manage.py run_worker`.

- A job is claimed with an atomic UPDATE and a lease. While a job runs, a
  heartbeat thread renews the lease and watches for an operator's pause or
  cancel; a lease that expires means the worker died, and the job is
  claimed again (each kind is safe to repeat: the campaign DB decides what
  still needs doing, and the run lock keeps two runs off one campaign).
- Pause, cancel and a worker shutdown all stop a send the same way —
  `Runner.cancel()`: nothing new is claimed, and requests in flight finish
  and are recorded. Then a paused job waits, a cancelled one cancels its
  remaining recipients, and an interrupted one is queued again.
- While an admin holds all sending, no send or test SMS is claimed, and a
  running one stops within a heartbeat: a send waits, paused, until the
  hold is lifted; a test SMS is cancelled.
- When idle, it queues delivery updates while Kavenegar still answers
  (48 h) and click updates for recent campaigns, and asks Kavenegar for the
  account's credit every 15 minutes (system/credit.py).
- Once a day, at the hour set in the system settings, it backs the data
  folder up (plan 06, L1), in a thread of its own.
- Run exactly one per data folder: `run_worker` waits while another one is
  alive (`other_worker`), and fails if it stays.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta

from django.conf import settings as django_settings
from django.db import close_old_connections, connection, transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from sms_sender.clicks import sync_clicks
from sms_sender.delivery import FINAL, WINDOW_SEC, sync_delivery
from sms_sender.input_loader import InputError
from sms_sender.locking import RunLock, RunLockError
from sms_sender.reconcile import DEFAULT_MIN_AGE_SEC, REQUEUE_NOT_FOUND, reconcile_unknown
from sms_sender.notify import notify_text
from sms_sender.sharing import clear_heartbeat, foreign_worker, write_heartbeat
from sms_sender.state import CampaignMismatchError
from sms_sender.window import DEFAULT_WINDOW, now_tehran, parse_window

from .engine import Engine, campaign_db
from .models import Campaign, Job, JobEvent, WorkerBeat
from .reporter import JobReporter
from .services import ACTIVE, held, settings_hash

logger = logging.getLogger(__name__)

LEASE = timedelta(seconds=60)
HEARTBEAT_SEC = 10.0
# A job whose worker died this many times is given up on (something about
# the job itself keeps killing the worker).
MAX_ATTEMPTS = 5
SCHEDULE_EVERY = timedelta(minutes=5)
# Why a send waits for the sending window: it closed mid-run, or it was
# never open when the send started.
WINDOW_STOPS = ("window_closed", "outside_window")
# The status page calls a worker alive if it was seen this recently.
ALIVE_WITHIN = timedelta(seconds=60)
DELIVERY_EVERY = timedelta(minutes=15)
CLICKS_EVERY = timedelta(hours=1)
CLICKS_FOR = timedelta(days=14)
# A daily backup that failed is tried again this much later.
BACKUP_RETRY = timedelta(hours=1)
BACKUP_FAILED = ("sms-sender: the daily backup failed ({error}), so nothing was kept. It's tried "
                 "again in an hour; the details are in the worker's log.")

# Why a running send was stopped; a later reason only wins if it's stronger.
# An operator's pause outranks the hold: lifting the hold doesn't resume it.
_STOP_PRIORITY = {"lost": 0, "shutdown": 1, "held": 2, "pause": 3, "cancel": 4}


class _StopReason:
    def __init__(self) -> None:
        self.value: str | None = None
        self._lock = threading.Lock()

    def set(self, why: str) -> bool:
        with self._lock:
            if self.value is None or _STOP_PRIORITY[why] > _STOP_PRIORITY[self.value]:
                self.value = why
                return True
            return False


# The worker's lanes: sends one at a time, the short jobs beside them.
SEND_KINDS = (Job.Kind.SEND,)
SHORT_KINDS = (Job.Kind.TEST, Job.Kind.RECONCILE, Job.Kind.DELIVERY, Job.Kind.CLICKS)
LANES = (("send", SEND_KINDS), ("short", SHORT_KINDS))
# Jobs that hold their campaign's run lock: never two of them for one
# campaign at a time (delivery and clicks only write their own columns).
LOCKING = (Job.Kind.SEND, Job.Kind.TEST, Job.Kind.RECONCILE)
# What the hold on all sending stops: everything that sends an SMS.
SENDING = (Job.Kind.SEND, Job.Kind.TEST)


class _Heartbeat(threading.Thread):
    """Renews the lease and turns pause / cancel / shutdown into `on_stop`."""

    def __init__(self, worker: "Worker", job_id: int, on_stop, *, sends: bool = False):
        super().__init__(name=f"heartbeat-{job_id}", daemon=True)
        self.worker = worker
        self.job_id = job_id
        self.on_stop = on_stop
        self.sends = sends  # an SMS-sending job: the hold on all sending stops it
        self._done = threading.Event()

    def run(self) -> None:
        try:
            while not self._done.wait(self.worker.heartbeat_sec):
                renewed = Job.objects.filter(pk=self.job_id, lease_owner=self.worker.id).update(
                    lease_until=timezone.now() + self.worker.lease,
                )
                if not renewed:
                    self.on_stop("lost")  # another worker took it over
                    continue
                self.worker.beat()
                control = Job.objects.filter(pk=self.job_id).values_list("control", flat=True).first()
                if control:
                    self.on_stop(control)
                elif self.sends and held():
                    self.on_stop("held")
                elif self.worker.stop.is_set():
                    self.on_stop("shutdown")
        finally:
            connection.close()

    def finish(self) -> None:
        self._done.set()
        self.join()


def _announce(job: Job, summary) -> None:
    """Every notification target an admin set (system settings) hears how
    the send ended: the CLI's report, headed with the campaign. Best-effort:
    a failed notification changes nothing about the job."""
    from sms_sender.notify import notify

    from ..system.models import SystemSettings

    for target in SystemSettings.load().notify_targets:
        notify(target, summary, heading=job.campaign.slug)


def _announce_failure(job: Job, why: str) -> None:
    """A send that stopped before it could run has no run report: the
    targets hear why, in a line. Best-effort, like the report."""
    from sms_sender.notify import notify_text

    from ..system.models import SystemSettings

    for target in SystemSettings.load().notify_targets:
        notify_text(target, f"sms-sender: {job.campaign.slug}: the send stopped before sending anything: {why}")


def _announce_test(job: Job, team: int = 0) -> None:
    """A test SMS went out: the targets hear that it waits for someone to
    approve or reject it (its number masked, and how many of the team's
    numbers got it too). Best-effort."""
    from sms_sender.notify import notify_text

    from ..privacy import mask_phone
    from ..system.models import SystemSettings

    current = SystemSettings.load()
    phone = mask_phone(str(job.params.get("test_number", "")))
    also = f" and {team} of the team's numbers" if team else ""
    who = (f"someone other than {job.requested_by.get_username()} to approve it, or anyone to reject it"
           if current.second_approver and job.requested_by else "someone to approve or reject it")
    for target in current.notify_targets:
        notify_text(target, f"sms-sender: {job.campaign.slug}: the test SMS went to {phone}{also}. "
                            f"It waits for {who} on the dashboard.")


class Worker:
    def __init__(
        self, engine: Engine | None = None, *, worker_id: str | None = None,
        lease: timedelta = LEASE, heartbeat_sec: float = HEARTBEAT_SEC,
        max_attempts: int = MAX_ATTEMPTS, stop: threading.Event | None = None,
    ):
        self.engine = engine or Engine()
        self.id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.lease = lease
        self.heartbeat_sec = heartbeat_sec
        self.max_attempts = max_attempts
        self.stop = stop or threading.Event()
        self._next_schedule = None
        self._backup: threading.Thread | None = None
        self._backup_retry_at: datetime | None = None
        self._backup_failing = False

    def beat(self) -> None:
        WorkerBeat.objects.update_or_create(worker_id=self.id, defaults={"seen_at": timezone.now()})
        # So a CLI on another kernel (the host, outside Docker Desktop's VM)
        # knows not to change this folder's DBs while it's busy here.
        write_heartbeat(django_settings.SMS_SENDER_DB_DIR, self.id)

    # ---------- claiming ----------

    def claim(self, kinds: tuple[str, ...] | None = None) -> Job | None:
        """Take the oldest waiting job of these kinds (any: None) — or a
        running one whose worker died (lease expired) — atomically: two
        workers, or two lanes, never get the same job. A job that takes its
        campaign's run lock waits while another one holds it."""
        while True:
            now = timezone.now()
            due = Q(not_before__isnull=True) | Q(not_before__lte=now)  # a scheduled send waits
            claimable = (Q(state=Job.State.QUEUED) & due) | Q(state=Job.State.RUNNING, lease_until__lt=now)
            if kinds is not None:
                claimable &= Q(kind__in=kinds)
            if held():
                claimable &= ~Q(kind__in=SENDING)  # nothing sends until the hold is lifted
            holding = Job.objects.filter(state=Job.State.RUNNING, kind__in=LOCKING, lease_until__gte=now)
            claimable &= ~Q(kind__in=LOCKING, campaign_id__in=holding.values("campaign_id"))
            with transaction.atomic():
                job = Job.objects.filter(claimable).order_by("created_at", "id").first()
                if job is None:
                    return None
                if job.attempts >= self.max_attempts:
                    Job.objects.filter(pk=job.pk).update(
                        state=Job.State.FAILED, finished_at=now, lease_owner="", lease_until=None,
                        last_error=f"the worker stopped mid-job {job.attempts} times; given up",
                        result={"stop_reason": "given_up", "stop_fields": {"attempts": job.attempts}},
                    )
                    continue
                taken = Job.objects.filter(claimable, pk=job.pk).update(
                    state=Job.State.RUNNING, lease_owner=self.id, lease_until=now + self.lease,
                    attempts=F("attempts") + 1, started_at=Coalesce(F("started_at"), Value(now)),
                )
            if taken:
                return Job.objects.select_related("campaign").get(pk=job.pk)
            # someone else took it between our read and update; look again

    # ---------- running ----------

    def run_once(self, kinds: tuple[str, ...] | None = None) -> Job | None:
        job = self.claim(kinds)
        if job is not None:
            self.execute(job)
            job.refresh_from_db()
        return job

    def run_forever(self, poll_sec: float = 2.0) -> None:
        """Two lanes: sends one at a time (the frequency cap and Kavenegar's
        rate count on that), and the short jobs (test SMS, reconcile,
        delivery, clicks) beside them, so an urgent test never waits behind
        an hour-long send. This thread keeps the heartbeat, the schedule and
        the window's resumes."""
        logger.info("worker_started", extra={"worker": self.id})
        lanes = [
            threading.Thread(target=self._lane, args=(name, kinds, poll_sec), name=f"lane-{name}", daemon=True)
            for name, kinds in LANES
        ]
        for lane in lanes:
            lane.start()
        while not self.stop.is_set():
            close_old_connections()
            try:
                self.beat()
                self.schedule()
                self.resume_when_window_opens()
                self.back_up_if_due()
            except Exception:  # noqa: BLE001 — keep the worker alive
                logger.exception("worker_error", extra={"worker": self.id})
            self.stop.wait(poll_sec)
        for lane in lanes:
            lane.join()  # each lets its job requeue itself (shutdown) first
        if self._backup is not None:
            self._backup.join()  # a half-made backup would only be thrown away
        self.sign_off()
        logger.info("worker_stopped", extra={"worker": self.id})

    def sign_off(self) -> None:
        """A clean stop: this worker is no longer alive, so the next one (an
        upgrade's, or the Mac's after Docker's) can start at once."""
        try:
            WorkerBeat.objects.filter(worker_id=self.id).delete()
        except Exception:  # noqa: BLE001 — a stale beat only delays the next start
            logger.exception("worker_error", extra={"worker": self.id})
        clear_heartbeat(django_settings.SMS_SENDER_DB_DIR, self.id)

    def _lane(self, name: str, kinds: tuple[str, ...], poll_sec: float) -> None:
        try:
            while not self.stop.is_set():
                close_old_connections()
                try:
                    if self.run_once(kinds) is None:
                        self.stop.wait(poll_sec)
                except Exception:  # noqa: BLE001 — keep the lane alive
                    logger.exception("worker_error", extra={"worker": self.id, "lane": name})
                    self.stop.wait(poll_sec)
        finally:
            connection.close()

    def execute(self, job: Job) -> None:
        handler = {
            Job.Kind.TEST: self._test,
            Job.Kind.SEND: self._send,
            Job.Kind.RECONCILE: self._reconcile,
            Job.Kind.DELIVERY: self._delivery,
            Job.Kind.CLICKS: self._clicks,
        }[job.kind]
        logger.info("job_started", extra={"job": job.pk, "kind": job.kind, "campaign": job.campaign.slug})
        try:
            state, result, error = handler(job)
        except Exception as e:  # noqa: BLE001 — recorded on the job
            logger.exception("job_crashed", extra={"job": job.pk})
            state, result, error = Job.State.FAILED, {"stop_reason": "crashed"}, f"{type(e).__name__}: {e}"
            if job.kind == Job.Kind.SEND:
                _announce_failure(job, "an unexpected error; the details are in the worker's log")
        finished = None if state == Job.State.QUEUED else timezone.now()
        Job.objects.filter(pk=job.pk, lease_owner=self.id).update(
            state=state, result=result, last_error=error, control="",
            lease_owner="", lease_until=None, finished_at=finished,
        )
        logger.info("job_finished", extra={"job": job.pk, "state": str(state)})

    def _send(self, job: Job):
        return self._run(job, test=False)

    def _test(self, job: Job):
        return self._run(job, test=True)

    def _run(self, job: Job, *, test: bool):
        """A send, or a test run (stages 1–3 and one SMS to the operator).
        Stopping a test never touches the campaign's recipients."""
        if not test and job.attempts == 1 and job.settings_hash != settings_hash(job.campaign):
            # The settings changed after its test SMS was approved, so what
            # would go out isn't what was approved. The settings page is
            # locked while a send waits; this is the backstop. Nothing is
            # sent, and the send is withdrawn as if never asked for: the
            # campaign wants a new test SMS. Only on its first claim, before
            # anything can have gone out.
            Job.objects.filter(pk=job.pk).update(started_at=None)
            return (Job.State.CANCELLED, {"withdrawn": True, "stop_reason": "not_approved"},
                    "the settings changed after the test SMS was approved; nothing was sent")
        reporter = JobReporter(job)
        # An admin changed the message of a campaign that has sent: the
        # campaign DB accepts the new settings (the CLI's --allow-settings-change).
        allow = bool(job.params.get("allow_settings_change"))
        try:
            if test:
                runner = self.engine.runner(job.campaign, reporter, test_number=job.params["test_number"],
                                            team_numbers=job.params.get("team_numbers", ()),
                                            allow_settings_change=allow)
            else:
                runner = self.engine.runner(
                    job.campaign, reporter, cost_per_sms=job.params.get("cost_per_sms"),
                    smoke_test=bool(job.params.get("smoke_test")), allow_settings_change=allow,
                )
        except (InputError, FileNotFoundError) as e:
            # A list it can't read (a segment being replaced, an opt-out file
            # gone): nothing was read or sent.
            if not test:
                _announce_failure(job, f"a list can't be read ({e})")
            return Job.State.FAILED, {"stop_reason": "input_unreadable"}, str(e)
        reason = _StopReason()

        def on_stop(why: str) -> None:
            if reason.set(why):
                logger.info("job_stop_requested", extra={"job": job.pk, "why": why})
            runner.cancel()

        heartbeat = _Heartbeat(self, job.pk, on_stop, sends=True)
        heartbeat.start()
        try:
            summary = runner.run()
        except RunLockError as e:
            why = f"another sms-sender process is sending this campaign: {e}"
            if not test:
                _announce_failure(job, why)
            return Job.State.FAILED, {"stop_reason": "busy"}, why
        except CampaignMismatchError as e:
            if not test:
                _announce_failure(job, f"the campaign's records were sent with other settings ({e})")
            return Job.State.FAILED, {"stop_reason": "settings_mismatch"}, str(e)
        except (InputError, FileNotFoundError) as e:
            if not test:
                _announce_failure(job, f"a list can't be read ({e})")
            return Job.State.FAILED, {"stop_reason": "input_unreadable"}, str(e)
        finally:
            heartbeat.finish()

        result = {
            k: v for k, v in dataclasses.asdict(summary).items() if k != "top_errors"
        }
        result["top_errors"] = [[message, count] for message, count in summary.top_errors]
        if not test and reason.value not in ("shutdown", "lost"):
            _announce(job, summary)  # the CLI's --notify: a send ended or stopped
        if reason.value == "held" and not test:
            # It goes on when the hold is lifted (services.release_sending).
            return Job.State.PAUSED, {**result, "held": True}, ""
        if reason.value == "cancel" or (test and reason.value in ("pause", "held")):
            if not test:
                with RunLock(campaign_db(job.campaign)):
                    result["cancelled"] = self.engine.state(job.campaign).cancel_remaining()
            return Job.State.CANCELLED, result, ""
        if reason.value == "pause":
            return Job.State.PAUSED, result, ""
        if reason.value in ("shutdown", "lost"):
            return Job.State.QUEUED, result, ""  # taken up again by the next worker
        if not test and result.get("stop_reason") in WINDOW_STOPS:
            # Outside the sending window: it waits, and goes on by itself when
            # the window opens (resume_when_window_opens).
            return Job.State.PAUSED, result, "outside the sending window; it continues when the window opens"
        if summary.halted:
            # A halt mid-send (e.g. credit ran out) leaves its reason in the
            # top errors; a failed pre-send check, in the last note.
            why = "; ".join(f"{message} (x{count})" for message, count in summary.top_errors)
            return Job.State.FAILED, result, why or reporter.last_note or "the run halted"
        if summary.stopped and not test:
            return Job.State.PAUSED, result, "the sending window closed; it continues when the window opens"
        if test and summary.test_message_id is not None:
            _announce_test(job, summary.test_team_sent)
        return Job.State.DONE, result, ""

    def _reconcile(self, job: Job):
        try:
            with RunLock(campaign_db(job.campaign)):
                summary = reconcile_unknown(
                    self.engine.state(job.campaign), self.engine.sender(job.campaign),
                    # The CLI's --min-age and --requeue-not-found/--review-not-found.
                    min_age_sec=float(job.params.get("min_age_sec", DEFAULT_MIN_AGE_SEC)),
                    requeue_not_found=bool(job.params.get("requeue_not_found", REQUEUE_NOT_FOUND)),
                )
        except RunLockError as e:
            return Job.State.FAILED, {}, f"the campaign is busy: {e}"
        return Job.State.DONE, dataclasses.asdict(summary), ""

    def _delivery(self, job: Job):
        # No run lock: it only writes the delivery columns (safe during a send).
        summary = sync_delivery(self.engine.state(job.campaign), self.engine.sender(job.campaign))
        return Job.State.DONE, dataclasses.asdict(summary), ""

    def _clicks(self, job: Job):
        summary = sync_clicks(self.engine.state(job.campaign), self.engine.link_client(job.campaign), job.campaign.slug)
        return Job.State.DONE, dataclasses.asdict(summary), ""

    # ---------- the sending window ----------

    def resume_when_window_opens(self) -> int:
        """Sends the sending window paused go on by themselves once it opens
        again (decided 2026-10-04). Never one an operator paused. Returns how
        many were queued again."""
        resumed = 0
        now = now_tehran()
        for job in Job.objects.filter(kind=Job.Kind.SEND, state=Job.State.PAUSED).select_related("campaign"):
            if (job.result or {}).get("stop_reason") not in WINDOW_STOPS:
                continue
            try:
                window = parse_window(job.campaign.settings.get("send_window", DEFAULT_WINDOW))
            except ValueError:
                continue
            if window is not None and not window.contains(now):
                continue
            if Job.objects.filter(pk=job.pk, state=Job.State.PAUSED).update(
                state=Job.State.QUEUED, control="", finished_at=None,
            ):
                JobEvent.objects.create(job=job, key="window_resumed",
                                        text="the sending window opened; sending continues")
                logger.info("job_resumed_by_window", extra={"job": job.pk})
                resumed += 1
        return resumed

    # ---------- periodic updates ----------

    def schedule(self, force: bool = False) -> None:
        """Queue delivery updates while Kavenegar still answers (48 h after
        sending) and click updates for campaigns sent in the last 14 days."""
        now = timezone.now()
        if not force and self._next_schedule is not None and now < self._next_schedule:
            return
        self._next_schedule = now + SCHEDULE_EVERY
        self._check_credit(now)
        for campaign in Campaign.objects.all():
            if not campaign_db(campaign).exists():
                continue
            store = self.engine.state(campaign)
            if store.messages_awaiting_delivery(sent_after=time.time() - WINDOW_SEC, final=FINAL):
                self._enqueue_if_due(campaign, Job.Kind.DELIVERY, DELIVERY_EVERY)
            last_send = Job.objects.filter(
                campaign=campaign, kind=Job.Kind.SEND, started_at__gte=now - CLICKS_FOR,
            ).exists()
            if last_send and store.link_counts().get("ready"):
                self._enqueue_if_due(campaign, Job.Kind.CLICKS, CLICKS_EVERY)

    def _check_credit(self, now) -> None:
        from ..system import credit

        if not credit.due(now):
            return
        try:
            sender = self.engine.sender()
        except RuntimeError:  # no Kavenegar key on this server
            credit.record(None, "no_key", now=now)
        else:
            credit.check_now(sender)

    def back_up_if_due(self, now: datetime | None = None) -> threading.Thread | None:
        """Start the daily backup when it's due (system settings), in a
        thread of its own, so the heartbeat goes on while it copies."""
        from ..system import operations
        from ..system.models import SystemSettings

        now = now or timezone.now()
        if self._backup is not None and self._backup.is_alive():
            return None
        if self._backup_retry_at is not None and now < self._backup_retry_at:
            return None
        current = SystemSettings.load()
        if not operations.backup_due(now, current.backup_hour, operations.newest_backup_at()):
            return None
        self._backup = threading.Thread(
            target=self._daily_backup, args=(current.backup_keep, list(current.notify_targets or [])),
            name="daily-backup",
        )
        self._backup.start()
        return self._backup

    def _daily_backup(self, keep: int, targets: list[str]) -> None:
        """The backup itself; it doesn't touch the app DB (only copies it)."""
        from pathlib import Path

        from ..backup import make_backup

        try:
            result = make_backup(Path(django_settings.DATA_DIR), django_settings.BACKUP_DIR, keep=keep)
        except Exception as e:  # noqa: BLE001 — tried again later, and announced
            logger.exception("backup_failed", extra={"worker": self.id})
            self._backup_retry_at = timezone.now() + BACKUP_RETRY
            if not self._backup_failing:  # once until one succeeds
                self._backup_failing = True
                for target in targets:
                    notify_text(target, BACKUP_FAILED.format(error=type(e).__name__))  # never raises
        else:
            self._backup_retry_at, self._backup_failing = None, False
            logger.info("backup_made", extra={"backup": result.path.name, "files": result.files,
                                              "pruned": len(result.pruned)})

    def _enqueue_if_due(self, campaign: Campaign, kind: str, every: timedelta) -> None:
        jobs = Job.objects.filter(campaign=campaign, kind=kind)
        if jobs.filter(state__in=ACTIVE).exists():
            return
        if jobs.filter(finished_at__gte=timezone.now() - every).exists():
            return
        Job.objects.create(campaign=campaign, kind=kind)


def last_seen() -> "datetime | None":
    """When any worker was last alive (None: never)."""
    beat = WorkerBeat.objects.order_by("-seen_at").first()
    return beat.seen_at if beat else None


def worker_alive() -> bool:
    seen = last_seen()
    return seen is not None and seen >= timezone.now() - ALIVE_WITHIN


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process
    return True


def other_worker(worker_id: str | None = None) -> str | None:
    """Another worker alive on this data folder, in a sentence; None when
    there's none. Run exactly one: two would race each other for the sends,
    and from another kernel (Docker Desktop's VM) not even the run lock keeps
    them apart. `worker_id`: the one asking (default: this process)."""
    me = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    foreign = foreign_worker(django_settings.SMS_SENDER_DB_DIR)
    if foreign is not None:
        return (f"a worker in another VM or on another machine ({foreign.get('worker') or '?'}) "
                f"is using {django_settings.SMS_SENDER_DB_DIR}")
    host = me.rpartition(":")[0]
    for beat in WorkerBeat.objects.filter(seen_at__gte=timezone.now() - ALIVE_WITHIN).exclude(worker_id=me):
        name, _, pid = beat.worker_id.rpartition(":")
        if name == host and pid.isdigit():
            if _process_alive(int(pid)):
                return f"worker {beat.worker_id} is running on this machine (process {pid})"
            continue  # it ended without signing off
        age = int((timezone.now() - beat.seen_at).total_seconds())
        return f"worker {beat.worker_id} (another machine or container) was alive {age} s ago"
    return None
