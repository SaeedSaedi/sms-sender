from collections import defaultdict

from django.shortcuts import render

from ..accounts.decorators import requires
from ..campaigns.lifecycle import (
    AWAITING, CANCELLED, COMPLETED, PAUSED, SCHEDULED, SENDING, STOPPED, lifecycle,
)
from ..campaigns.present import say
from ..campaigns.terms import CLI_CAMPAIGN, FOLLOWUPS, NEXT_STEP, STAGES, stop_reason
from ..jobs.models import Campaign, Job
from ..jobs.worker import worker_alive
from .campaigns import CampaignSummary, list_campaigns
from .terms import STATUS_ORDER

# The overview's tiles: how many campaigns are at each of these stages.
TILES = (SENDING, AWAITING, STOPPED, SCHEDULED)


def _attention(campaign: Campaign, life, counts: dict) -> list[str]:
    """What this campaign needs from someone, in a line or two."""
    if life.stage == AWAITING:
        return [str(NEXT_STEP[AWAITING])]
    if life.stage == STOPPED:
        result = life.send.result or {}
        return [stop_reason(result.get("stop_reason"), result.get("stop_fields")) or str(NEXT_STEP[STOPPED])]
    if life.stage in (COMPLETED, CANCELLED):
        return [
            say(FOLLOWUPS[key], {"n": counts[status]})
            for status, key in (("unknown", "unknown"), ("needs_review", "needs_review"),
                                ("failed_retriable", "not_sent"))
            if counts.get(status)
        ]
    return []


@requires("view_campaigns")
def home(request):
    """Every campaign with where it stands, and what needs someone now. The
    campaigns in data/db (the CLI's and the dashboard's), and dashboard
    campaigns that haven't sent yet. A dashboard campaign links to its page."""
    managed = {c.slug: c for c in Campaign.objects.all()}
    summaries = list_campaigns()
    on_disk = {s.slug for s in summaries}
    fresh = [
        CampaignSummary(slug=c.slug, name=c.name, template=c.settings.get("template"),
                        counts={}, last_run_at=None)
        for c in sorted(managed.values(), key=lambda c: c.created_at, reverse=True)
        if c.slug not in on_disk
    ]
    jobs: dict[int, list[Job]] = defaultdict(list)
    for job in Job.objects.filter(kind__in=(Job.Kind.TEST, Job.Kind.SEND)).order_by("-created_at", "-id"):
        jobs[job.campaign_id].append(job)

    rows, attention, tiles = [], [], dict.fromkeys(TILES, 0)
    for summary in fresh + summaries:
        campaign = managed.get(summary.slug)
        life = lifecycle(campaign, jobs[campaign.pk]) if campaign else None
        if life is not None:
            if life.stage in tiles:
                tiles[life.stage] += 1
            lines = _attention(campaign, life, summary.counts)
            if lines:
                attention.append({"campaign": campaign, "life": life, "stage": STAGES[life.stage], "lines": lines})
        progress = None
        if life is not None and life.stage in (SENDING, PAUSED) and life.send is not None:
            p = life.send.progress or {}
            if p.get("total") and p.get("stage") != "links":
                progress = {"done": p.get("processed", 0), "total": p["total"]}
        rows.append({
            "summary": summary, "campaign": campaign, "life": life, "progress": progress,
            "stage": STAGES[life.stage] if life else CLI_CAMPAIGN,
            "tone": life.tone if life else "neutral",
        })
    return render(request, "dashboard/home.html", {
        "campaigns": rows,
        "status_order": STATUS_ORDER,
        "tiles": [(STAGES[stage], n, stage) for stage, n in tiles.items()],
        "attention": attention,
        "worker_alive": worker_alive(),
    })
