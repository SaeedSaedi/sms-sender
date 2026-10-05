"""Campaigns and the durable jobs that act on them (spec 4.6, 4.8).

A job is a row in the app DB. One worker process claims it with an atomic
UPDATE, holds a lease it renews while working, and records the result. If
the worker dies, the lease expires and the job is claimed again; every job
kind is safe to repeat, because the campaign DB (the CLI's state store)
decides what still needs doing.
"""
from __future__ import annotations

from django.conf import settings as django_settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Campaign(models.Model):
    """What to send and to whom. `slug` names the campaign DB
    (data/db/<slug>.db), shared with the CLI."""

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    # The send settings, in the CLI's terms: input, template, tokens,
    # token_columns, value_maps, user_id_column, segment, links, opt_out,
    # send_window, rate, workers, link_rate. Validated where they're edited.
    settings = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # An admin opened the settings of a campaign that has sent (the CLI's
    # --allow-settings-change), after a backup. It lasts until a send starts.
    unlocked_at = models.DateTimeField(null=True, blank=True)
    unlocked_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self) -> str:
        return self.slug


class Job(models.Model):
    class Kind(models.TextChoices):
        # Validate, links and pre-send checks, then one SMS to the operator's
        # own number; the operator approves it before a send can start.
        TEST = "test", _("Test SMS")
        SEND = "send", _("Send")
        RECONCILE = "reconcile", _("Reconcile")
        DELIVERY = "delivery", _("Delivery update")
        CLICKS = "clicks", _("Clicks update")

    class State(models.TextChoices):
        QUEUED = "queued", _("Waiting")
        RUNNING = "running", _("Running")
        PAUSED = "paused", _("Paused")
        CANCELLED = "cancelled", _("Cancelled")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    class Control(models.TextChoices):
        NONE = "", ""
        PAUSE = "pause", _("Pause")
        CANCEL = "cancel", _("Cancel")

    class Decision(models.TextChoices):
        NONE = "", ""
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    # An operator's request to a running job; the worker sees it within a
    # heartbeat and stops the run gracefully.
    control = models.CharField(max_length=8, choices=Control.choices, default="", blank=True)
    requested_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # A scheduled send: the worker leaves it queued until then (plan 05, P2).
    not_before = models.DateTimeField(null=True, blank=True)
    # Which worker holds the job, and until when: past this, it's taken as dead.
    lease_owner = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)  # times claimed
    last_error = models.TextField(blank=True)
    progress = models.JSONField(default=dict, blank=True)  # live counts while running
    result = models.JSONField(default=dict, blank=True)    # the run's summary
    # What the job was given: a test's phone number, a send's cost per SMS.
    params = models.JSONField(default=dict, blank=True)
    # The campaign's message settings when a test was asked for (or a send
    # started): an approval holds only while they're unchanged.
    settings_hash = models.CharField(max_length=64, blank=True)
    # A test SMS, approved or rejected by the operator who received it.
    decision = models.CharField(max_length=8, choices=Decision.choices, default="", blank=True)
    decided_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "created_at"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.kind} {self.campaign.slug} ({self.state})"


class JobEvent(models.Model):
    """What a job reported while it ran, in order."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    # `note` for the engine's notes; later, one key per event (spec 4.11:
    # the dashboard renders keys in Persian, never the engine's English).
    key = models.CharField(max_length=64, default="note")
    text = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]


class WorkerBeat(models.Model):
    """A worker's last sign of life, for the status page: written every
    loop while idle, and with each heartbeat while a job runs."""

    worker_id = models.CharField(max_length=64, unique=True)
    seen_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.worker_id} at {self.seen_at:%Y-%m-%d %H:%M:%S}"
