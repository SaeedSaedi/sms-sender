"""What operators can do with jobs: a test SMS and its approval, start,
pause, resume, cancel (spec 3, 4.6). These only change rows; the worker
does the work."""
from __future__ import annotations

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from sms_sender.locking import RunLock, RunLockError

from ..accounts.models import test_phone_of
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
    settings_changed, not_decidable."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def settings_hash(campaign: Campaign) -> str:
    s = campaign.settings or {}
    approved = {key: s.get(key) for key in APPROVED_SETTINGS}
    if approved.get("links"):
        approved["links"] = {k: v for k, v in approved["links"].items() if k != "expiry_days"}
    return hashlib.sha256(json.dumps(approved, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def latest_test(campaign: Campaign) -> Job | None:
    return Job.objects.filter(campaign=campaign, kind=Job.Kind.TEST).order_by("-created_at", "-id").first()


def approval(campaign: Campaign) -> Job | None:
    """The approved test SMS a send may rely on: the latest test, if it was
    approved for the settings the campaign has now."""
    test = latest_test(campaign)
    if test and test.decision == Job.Decision.APPROVED and test.settings_hash == settings_hash(campaign):
        return test
    return None


def request_test(campaign: Campaign, user) -> Job:
    """Queue a test SMS to the user's own number (stages 1–3: validate,
    links, pre-send checks). A test already waiting or running is returned."""
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
            params={"test_number": phone}, settings_hash=settings_hash(campaign),
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


def start_send(campaign: Campaign, user) -> Job:
    """Queue the send, given an approved test SMS for these settings. Its
    cost per SMS goes along, for the credit estimate."""
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
            params={"cost_per_sms": test.result.get("cost_per_sms")},
            settings_hash=test.settings_hash,
        )


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
