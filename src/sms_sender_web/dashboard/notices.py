"""Notifications inside the panel (plan 06, L4): what happened in the last 7
days that someone may need to act on or know.

- a test SMS waiting for someone to approve or reject it, or one that
  didn't go out (for those who run campaigns);
- a send that ended, stopped, or was cancelled;
- the credit under the warning level;
- the daily backup overdue (for admins).

They're read from what the panel already records (jobs, the provider check,
the backups), so nothing new is written when something happens; the same
events still reach Slack and Telegram. Each one keeps the moment it
happened, so the menu counts those newer than when this person last opened
the list (`Profile.notifications_seen_at`). A refused account check has no
such moment (it's asked again every 15 minutes): the credit card and the
status page show it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.urls import reverse
from django.utils import timezone

from ..accounts.models import Profile
from ..accounts.roles import can
from ..campaigns.present import say
from ..campaigns.terms import stop_reason
from ..jobs import services
from ..jobs.models import Campaign, Job
from ..system import operations
from ..system.models import ProviderCheck, SystemSettings
from .terms import NOTICES

WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class Notice:
    at: datetime
    kind: str       # test, test_failed, sent, stopped, cancelled, credit, backup
    tone: str       # the pill's: warning, success, danger, neutral
    text: str
    url: str


def _url(campaign: Campaign) -> str:
    """An alert opens in the composer; any other campaign on its page."""
    return reverse("compose_campaign" if campaign.preset_id else "campaign_detail", args=[campaign.slug])


def _tests(since: datetime) -> list[Notice]:
    """Each campaign's latest test SMS, if it's waiting for a decision for
    the settings it still has, or didn't go out."""
    latest: dict[int, Job] = {}
    for job in (Job.objects.filter(kind=Job.Kind.TEST, created_at__gte=since - WINDOW)
                .select_related("campaign").order_by("-created_at", "-id")):
        latest.setdefault(job.campaign_id, job)
    out = []
    for job in latest.values():
        if not job.finished_at or job.finished_at < since:
            continue
        name = {"name": job.campaign.name}
        if job.state == Job.State.DONE and not job.decision \
                and job.settings_hash == services.settings_hash(job.campaign):
            out.append(Notice(job.finished_at, "test", "warning", say(NOTICES["test"], name), _url(job.campaign)))
        elif job.state == Job.State.FAILED:
            result = job.result or {}
            reason = stop_reason(result.get("stop_reason"), result.get("stop_fields"))
            out.append(Notice(job.finished_at, "test_failed", "danger",
                              " ".join(filter(None, [say(NOTICES["test_failed"], name), reason])),
                              _url(job.campaign)))
    return out


def _sends(since: datetime) -> list[Notice]:
    out = []
    for job in Job.objects.filter(
        kind=Job.Kind.SEND, finished_at__gte=since,
        state__in=(Job.State.DONE, Job.State.FAILED, Job.State.CANCELLED),
    ).select_related("campaign"):
        result = job.result or {}
        name = job.campaign.name
        if job.state == Job.State.DONE:
            out.append(Notice(job.finished_at, "sent", "success",
                              say(NOTICES["sent"], {"name": name, "n": result.get("sent", 0)}), _url(job.campaign)))
        elif job.state == Job.State.FAILED:
            reason = stop_reason(result.get("stop_reason"), result.get("stop_fields"))
            out.append(Notice(job.finished_at, "stopped", "danger",
                              " ".join(filter(None, [say(NOTICES["stopped"], {"name": name}), reason])),
                              _url(job.campaign)))
        # Withdrawn (unscheduled, not approved) and superseded sends sent
        # nothing and nobody cancelled them.
        elif not (result.get("withdrawn") or result.get("superseded")):
            out.append(Notice(job.finished_at, "cancelled", "neutral", say(NOTICES["cancelled"], {"name": name}),
                              _url(job.campaign)))
    return out


def notices(user, now: datetime | None = None) -> list[Notice]:
    """What this person should hear about, the newest first."""
    now = now or timezone.now()
    since = now - WINDOW
    out = _sends(since)
    if can(user, "run_campaigns"):
        out += _tests(since)
    check = ProviderCheck.load()
    if check.below_since and check.below_since >= since:
        out.append(Notice(check.below_since, "credit", "warning",
                          say(NOTICES["credit"], {"credit": check.credit or 0}), reverse("home") + "#credit"))
    system = SystemSettings.load()
    if can(user, "manage_settings") and operations.backup_overdue(now, system.backup_hour,
                                                                  operations.newest_backup_at()):
        # Since it became overdue: two hours after the moment it was due.
        late = operations.scheduled_moment(now - operations.OVERDUE_AFTER, system.backup_hour)
        out.append(Notice(late + operations.OVERDUE_AFTER, "backup", "warning", str(NOTICES["backup"]),
                          reverse("backups")))
    out.sort(key=lambda n: n.at, reverse=True)
    return out


def seen_at(user) -> datetime | None:
    return Profile.objects.filter(user=user).values_list("notifications_seen_at", flat=True).first()


def unseen(user, now: datetime | None = None) -> int:
    """How many are newer than when this person last looked."""
    since = seen_at(user)
    return sum(1 for n in notices(user, now) if since is None or n.at > since)


def mark_seen(user, now: datetime | None = None) -> None:
    Profile.objects.update_or_create(user=user, defaults={"notifications_seen_at": now or timezone.now()})
