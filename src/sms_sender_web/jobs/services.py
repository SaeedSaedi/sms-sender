"""What operators can do with jobs: start, pause, resume, cancel (spec 4.6).
These only change rows; the worker does the work."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from sms_sender.locking import RunLock, RunLockError

from .engine import Engine, campaign_db
from .models import Campaign, Job

ACTIVE = (Job.State.QUEUED, Job.State.RUNNING, Job.State.PAUSED)


class JobConflict(Exception):
    """The action can't be done right now (e.g. another process holds the
    campaign)."""


def enqueue(campaign: Campaign, kind: str, user=None) -> Job:
    """Queue a job, unless the campaign already has one of this kind that
    isn't finished — then that one is returned."""
    with transaction.atomic():
        existing = Job.objects.filter(campaign=campaign, kind=kind, state__in=ACTIVE).first()
        if existing is not None:
            return existing
        return Job.objects.create(campaign=campaign, kind=kind, requested_by=user)


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
        raise JobConflict(str(e)) from e
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
