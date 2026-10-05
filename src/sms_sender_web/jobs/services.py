"""What operators can do with jobs: a test SMS and its approval, start,
pause, resume, cancel (spec 3, 4.6). These only change rows; the worker
does the work."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from sms_sender.locking import RunLock, RunLockError
from sms_sender.state import (
    CANCELLED, FAILED_PERMANENT, INVALID, NEEDS_REVIEW, SENT, SUPPRESSED, UNKNOWN, StateStore,
)

from ..accounts.models import test_phone_of
from ..accounts.roles import can
from ..backup import make_backup
from .engine import Engine, campaign_db
from .models import Campaign, Job

ACTIVE = (Job.State.QUEUED, Job.State.RUNNING, Job.State.PAUSED)
# What a test SMS shows the operator, and so what their approval covers:
# the message (template, tokens, links) and the list it goes to. Throughput
# settings (window, rate, workers) don't change it.
APPROVED_SETTINGS = ("segment", "template", "tokens", "token_columns", "value_maps", "links")


class JobConflict(Exception):
    """The action can't be done right now. `code` picks the Persian message:
    busy, no_test_number, send_active, test_active, not_approved,
    settings_changed, not_decidable, not_scheduled, send_on_its_way, not_needed,
    requeue_while_sending."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def settings_hash(campaign: Campaign) -> str:
    s = campaign.settings or {}
    approved = {key: s.get(key) for key in APPROVED_SETTINGS}
    if approved.get("links"):
        approved["links"] = {k: v for k, v in approved["links"].items() if k != "expiry_days"}
    return hashlib.sha256(json.dumps(approved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def may_have_sent(campaign: Campaign) -> bool:
    db = campaign_db(campaign)
    return db.exists() and StateStore(db).may_have_sent()  # never creates the DB


def message_unlocked(campaign: Campaign) -> bool:
    """An admin opened the settings of a campaign that has sent, and no send
    has started since."""
    return campaign.unlocked_at is not None and not Job.objects.filter(
        campaign=campaign, kind=Job.Kind.SEND, started_at__gte=campaign.unlocked_at,
    ).exists()


def settings_locked(campaign: Campaign, user=None) -> str:
    """Why the settings can't change now ("" when they can):
    - "scheduled": a send waits for its time; cancel the schedule first;
    - "started": an SMS may have gone out (the CLI's rule): what went out
      and what follows must be one message. An admin who unlocked it
      (`unlock_message`) may still change it;
    - "waiting": a send is on its way, though nothing has gone out yet.
    A send that failed before sending anything locks nothing, so a wrong
    template can still be fixed."""
    active = Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND, state__in=ACTIVE)
    if active.filter(state=Job.State.QUEUED, not_before__gt=timezone.now()).exists():
        return "scheduled"
    if active.exists():
        return "started" if may_have_sent(campaign) else "waiting"
    if may_have_sent(campaign):
        unlocked = message_unlocked(campaign) and user is not None and can(user, "change_sent_message")
        return "" if unlocked else "started"
    return ""


def _allowance(campaign: Campaign) -> dict:
    """For a test or a send after an admin's unlock: the campaign DB accepts
    the changed settings (the worker passes it on)."""
    return {"allow_settings_change": True} if message_unlocked(campaign) else {}


def can_unlock(campaign: Campaign) -> bool:
    """An admin can change the message now: it has gone out to someone, no
    send is queued or running (a paused one is superseded), and it isn't
    unlocked already."""
    busy = Job.objects.filter(
        campaign=campaign, kind=Job.Kind.SEND, state__in=(Job.State.QUEUED, Job.State.RUNNING),
    ).exists()
    return not busy and may_have_sent(campaign) and not message_unlocked(campaign)


def unlock_message(campaign: Campaign, user) -> tuple[str, list[int]]:
    """Let an admin change the message of a campaign that has sent (the
    CLI's --allow-settings-change). Everything is backed up first; if that
    fails (BackupError, OSError) nothing changes. A paused send ends,
    superseded, without cancelling anyone: those still waiting get the new
    message, after a new test SMS. Returns the backup's name and the
    superseded jobs."""
    if not can_unlock(campaign):
        raise JobConflict("send_on_its_way" if may_have_sent(campaign) else "not_needed")
    backup = make_backup(Path(django_settings.DATA_DIR), django_settings.BACKUP_DIR, keep=None)
    superseded = []
    with transaction.atomic():
        for job in Job.objects.select_for_update().filter(
            campaign=campaign, kind=Job.Kind.SEND, state=Job.State.PAUSED,
        ):
            job.state, job.finished_at = Job.State.CANCELLED, timezone.now()
            job.result = {**(job.result or {}), "superseded": True}
            job.save(update_fields=["state", "finished_at", "result"])
            superseded.append(job.pk)
        campaign.unlocked_at, campaign.unlocked_by = timezone.now(), user
        campaign.save(update_fields=["unlocked_at", "unlocked_by"])
    return backup.path.name, superseded


# Who may put each status back in the queue (the CLI's reset --status;
# spec 4.12, plan 05 decision 2). Rejected: an operator. What may already
# have the SMS, or was held back on purpose: an admin. Sent: an admin, after
# a backup. (Not sent rows are in the queue already.)
REQUEUE = {
    FAILED_PERMANENT: "update_campaigns",
    UNKNOWN: "requeue_review",
    NEEDS_REVIEW: "requeue_review",
    SUPPRESSED: "requeue_review",
    INVALID: "requeue_review",
    CANCELLED: "requeue_review",
    SENT: "requeue_review",
}


def requeue(campaign: Campaign, status: str) -> tuple[int, str | None]:
    """Put a status's recipients back in the queue, for the next send.
    Refused while a send is on its way; holds the run lock. `sent`: each of
    them gets a second SMS, so everything is backed up first (a failed
    backup changes nothing). Returns (rows, backup name)."""
    if status not in REQUEUE:
        raise ValueError(status)
    if Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND, state__in=ACTIVE).exists():
        raise JobConflict("requeue_while_sending")
    db = campaign_db(campaign)
    if not db.exists():
        return 0, None
    backup = None
    if status == SENT:
        backup = make_backup(Path(django_settings.DATA_DIR), django_settings.BACKUP_DIR, keep=None).path.name
    try:
        with RunLock(db):
            return StateStore(db).reset_status(status), backup
    except RunLockError as e:
        raise JobConflict("busy") from e


def latest_test(campaign: Campaign) -> Job | None:
    return Job.objects.filter(campaign=campaign, kind=Job.Kind.TEST).order_by("-created_at", "-id").first()


def approval(campaign: Campaign) -> Job | None:
    """The approved test SMS a send may rely on: the latest test, if it was
    approved for the settings the campaign has now."""
    test = latest_test(campaign)
    if test and test.decision == Job.Decision.APPROVED and test.settings_hash == settings_hash(campaign):
        return test
    return None


def request_test(campaign: Campaign, user, *, parts: int | None = None) -> Job:
    """Queue a test SMS to the user's own number (stages 1–3: validate,
    links, pre-send checks). A test already waiting or running is returned.
    `parts`: the message's SMS parts, when the template's text is known, so
    its cost gives a price per part for estimates."""
    phone = test_phone_of(user)
    if not phone:
        raise JobConflict("no_test_number")
    with transaction.atomic():
        if Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND, state__in=ACTIVE).exists():
            raise JobConflict("send_active")
        existing = Job.objects.filter(
            campaign=campaign, kind=Job.Kind.TEST, state__in=ACTIVE,
        ).first()
        if existing is not None:
            return existing
        return Job.objects.create(
            campaign=campaign, kind=Job.Kind.TEST, requested_by=user,
            params={"test_number": phone, **({"parts": parts} if parts else {}), **_allowance(campaign)},
            settings_hash=settings_hash(campaign),
        )


def decide_test(job: Job, user, approve: bool) -> Job:
    """The operator received the test SMS: the text is right (approve) or
    not (reject). Only a finished test, once, and an approval only while
    the campaign's settings are the ones tested."""
    if job.kind != Job.Kind.TEST or job.state != Job.State.DONE or job.decision:
        raise JobConflict("not_decidable")
    if approve and job.settings_hash != settings_hash(job.campaign):
        raise JobConflict("settings_changed")
    decided = Job.objects.filter(pk=job.pk, decision="").update(
        decision=Job.Decision.APPROVED if approve else Job.Decision.REJECTED,
        decided_by=user, decided_at=timezone.now(),
    )
    if not decided:
        raise JobConflict("not_decidable")
    job.refresh_from_db()
    return job


def start_send(campaign: Campaign, user, *, at=None, smoke_test: bool = False) -> Job:
    """Queue the send, given an approved test SMS for these settings. Its
    cost per SMS goes along, for the credit estimate. `at`: start then
    instead (the worker leaves it queued until that time). `smoke_test`:
    send to one recipient first, and stop if that SMS doesn't go out."""
    test = approval(campaign)
    if test is None:
        latest = latest_test(campaign)
        approved_before = latest is not None and latest.decision == Job.Decision.APPROVED
        raise JobConflict("settings_changed" if approved_before else "not_approved")
    with transaction.atomic():
        existing = Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND, state__in=ACTIVE).first()
        if existing is not None:
            return existing
        if Job.objects.filter(campaign=campaign, kind=Job.Kind.TEST, state__in=ACTIVE).exists():
            raise JobConflict("test_active")
        return Job.objects.create(
            campaign=campaign, kind=Job.Kind.SEND, requested_by=user,
            params={"cost_per_sms": test.result.get("cost_per_sms"), "smoke_test": smoke_test,
                    **_allowance(campaign)},
            settings_hash=test.settings_hash, not_before=at,
        )


def unschedule(job: Job) -> Job:
    """Take a scheduled send off the queue. Unlike cancelling, nobody is
    cancelled: the campaign is ready to send again, now or at another time."""
    if job.kind != Job.Kind.SEND or job.not_before is None:
        raise JobConflict("not_scheduled")
    changed = Job.objects.filter(pk=job.pk, state=Job.State.QUEUED, not_before__gt=timezone.now()).update(
        state=Job.State.CANCELLED, finished_at=timezone.now(), result={"withdrawn": True},
    )
    if not changed:
        raise JobConflict("not_scheduled")
    job.refresh_from_db()
    return job


def start_now(job: Job) -> Job:
    """A scheduled send starts now instead of at its time."""
    if not Job.objects.filter(pk=job.pk, state=Job.State.QUEUED, kind=Job.Kind.SEND).update(not_before=None):
        raise JobConflict("not_scheduled")
    job.refresh_from_db()
    return job


def enqueue(campaign: Campaign, kind: str, user=None, params: dict | None = None) -> Job:
    """Queue a job, unless the campaign already has one of this kind that
    isn't finished — then that one is returned."""
    with transaction.atomic():
        existing = Job.objects.filter(campaign=campaign, kind=kind, state__in=ACTIVE).first()
        if existing is not None:
            return existing
        return Job.objects.create(campaign=campaign, kind=kind, requested_by=user, params=params or {})


def pause(job: Job) -> Job:
    """A waiting job pauses at once. A running one is asked to stop: no new
    recipient is taken, and requests already in flight finish and are
    recorded (the worker sees the request within a heartbeat)."""
    with transaction.atomic():
        if not Job.objects.filter(pk=job.pk, state=Job.State.QUEUED).update(state=Job.State.PAUSED):
            Job.objects.filter(pk=job.pk, state=Job.State.RUNNING).update(control=Job.Control.PAUSE)
    job.refresh_from_db()
    return job


def resume(job: Job) -> Job:
    """Queue a paused job again. The run reconciles `unknown` rows first,
    then continues from the campaign DB: nobody is sent twice."""
    Job.objects.filter(pk=job.pk, state=Job.State.PAUSED).update(
        state=Job.State.QUEUED, control="", finished_at=None,
    )
    job.refresh_from_db()
    return job


def cancel(job: Job, engine: Engine | None = None) -> Job:
    """Stop for good. Every recipient still waiting becomes `cancelled`;
    accepted SMS are final (Kavenegar can't recall a lookup SMS) and
    `unknown` rows stay, to be reconciled. A running job is asked to stop
    and the worker cancels the rest."""
    engine = engine or Engine()
    if Job.objects.filter(pk=job.pk, state=Job.State.RUNNING).update(control=Job.Control.CANCEL):
        job.refresh_from_db()
        return job
    if job.kind != Job.Kind.SEND:
        Job.objects.filter(pk=job.pk, state__in=(Job.State.QUEUED, Job.State.PAUSED)).update(
            state=Job.State.CANCELLED, control="", finished_at=timezone.now(),
        )
        job.refresh_from_db()
        return job
    lock = RunLock(campaign_db(job.campaign))
    try:
        lock.acquire()
    except RunLockError as e:
        raise JobConflict("busy", str(e)) from e
    try:
        with transaction.atomic():
            if Job.objects.filter(pk=job.pk, state=Job.State.RUNNING).update(
                control=Job.Control.CANCEL,
            ):
                changed = False  # the worker took it meanwhile; it cancels the rest
            else:
                changed = bool(Job.objects.filter(
                    pk=job.pk, state__in=(Job.State.QUEUED, Job.State.PAUSED),
                ).update(state=Job.State.CANCELLED, control="", finished_at=timezone.now()))
        if changed:
            cancelled = engine.state(job.campaign).cancel_remaining()
            Job.objects.filter(pk=job.pk).update(result={"cancelled": cancelled})
    finally:
        lock.release()
    job.refresh_from_db()
    return job
