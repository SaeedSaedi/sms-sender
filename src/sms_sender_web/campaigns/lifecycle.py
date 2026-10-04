"""Where a campaign stands, and what comes next (plan 05, P2).

One stage for the campaign list, the overview and the campaign's own page.
It's derived from the settings, the latest test and send jobs and their
results, never stored, so it can't drift from what really happened.

The steps people see: 1 message and list, 2 check and preview, 3 test SMS,
4 send, 5 results.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from ..jobs.models import Job

DRAFT = "draft"            # the settings aren't complete
READY = "ready"            # complete; no valid test SMS yet
TESTING = "testing"        # a test SMS is on its way
AWAITING = "awaiting"      # the test SMS arrived; approve or reject it
APPROVED = "approved"      # approved for these settings; nothing sent yet
SCHEDULED = "scheduled"    # a send waits for its time
SENDING = "sending"        # a send is queued or running
PAUSED = "paused"          # by an operator, or by the sending window
STOPPED = "stopped"        # a send stopped on an error
COMPLETED = "completed"    # the latest send finished
CANCELLED = "cancelled"    # the latest send was cancelled

STEP = {
    DRAFT: 1, READY: 2, TESTING: 3, AWAITING: 3, APPROVED: 4, SCHEDULED: 4,
    SENDING: 4, PAUSED: 4, STOPPED: 4, COMPLETED: 5, CANCELLED: 5,
}
# The pill's colour (app.css: pill-<tone>).
TONE = {
    DRAFT: "neutral", READY: "info", TESTING: "info", AWAITING: "warning", APPROVED: "success",
    SCHEDULED: "info", SENDING: "info", PAUSED: "warning", STOPPED: "danger",
    COMPLETED: "success", CANCELLED: "neutral",
}
ACTIVE = (Job.State.QUEUED, Job.State.RUNNING, Job.State.PAUSED)


def settings_complete(settings: dict | None) -> bool:
    """Enough to try a test SMS: a segment, a template, and something in the
    first token (Kavenegar needs it in every request)."""
    s = settings or {}
    first = (
        "token" in (s.get("tokens") or {})
        or "token" in (s.get("token_columns") or {})
        or (s.get("links") or {}).get("token") == "token"
    )
    return bool(s.get("segment") and s.get("template") and first)


@dataclass(frozen=True)
class Lifecycle:
    stage: str
    test: Job | None = None   # the latest test SMS
    send: Job | None = None   # the latest send
    test_failed: bool = False  # the latest test SMS didn't go out
    paused_by_window: bool = False

    @property
    def step(self) -> int:
        return STEP[self.stage]

    @property
    def tone(self) -> str:
        return TONE[self.stage]


def _scheduled(job: Job) -> bool:
    not_before = getattr(job, "not_before", None)
    return job.state == Job.State.QUEUED and not_before is not None and not_before > timezone.now()


def lifecycle(campaign, jobs: list[Job] | None = None) -> Lifecycle:
    """`jobs`: the campaign's test and send jobs, newest first (a page that
    lists many campaigns passes them in, to avoid a query per campaign)."""
    from ..jobs.services import settings_hash

    if jobs is None:
        jobs = list(
            Job.objects.filter(campaign=campaign, kind__in=(Job.Kind.TEST, Job.Kind.SEND))
            .order_by("-created_at", "-id")
        )
    test = next((j for j in jobs if j.kind == Job.Kind.TEST), None)
    send = next(
        (j for j in jobs if j.kind == Job.Kind.SEND and not (j.result or {}).get("unscheduled")), None,
    )
    test_failed = bool(test and test.state == Job.State.FAILED)

    # A send decides the stage, unless a newer test SMS came after it.
    if send is not None and (test is None or send.created_at >= test.created_at):
        if send.state == Job.State.QUEUED:
            return Lifecycle(SCHEDULED if _scheduled(send) else SENDING, test, send, test_failed)
        if send.state == Job.State.RUNNING:
            return Lifecycle(SENDING, test, send, test_failed)
        if send.state == Job.State.PAUSED:
            by_window = (send.result or {}).get("stop_reason") in ("window_closed", "outside_window")
            return Lifecycle(PAUSED, test, send, test_failed, paused_by_window=by_window)
        if send.state == Job.State.FAILED:
            return Lifecycle(STOPPED, test, send, test_failed)
        if send.state == Job.State.DONE:
            return Lifecycle(COMPLETED, test, send, test_failed)
        return Lifecycle(CANCELLED, test, send, test_failed)

    if test is not None and test.state in ACTIVE:
        return Lifecycle(TESTING, test, send)
    current = test is not None and test.settings_hash == settings_hash(campaign)
    if current and test.state == Job.State.DONE and not test.decision:
        return Lifecycle(AWAITING, test, send)
    if current and test.decision == Job.Decision.APPROVED:
        return Lifecycle(APPROVED, test, send)
    if settings_complete(campaign.settings):
        return Lifecycle(READY, test, send, test_failed)
    return Lifecycle(DRAFT, test, send, test_failed)
