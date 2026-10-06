import re
from collections import defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from sms_sender.window import DEFAULT_WINDOW

from ..accounts.decorators import requires
from ..accounts.roles import can
from ..campaigns.lifecycle import (
    AWAITING, CANCELLED, COMPLETED, PAUSED, SCHEDULED, SENDING, STOPPED, lifecycle,
)
from ..campaigns.present import say
from ..campaigns.terms import CLI_CAMPAIGN, FOLLOWUPS, NEXT_STEP, STAGES, fill, stop_reason
from ..jobs.models import Campaign, Job
from ..jobs.worker import worker_alive
from ..system import credit
from ..system.models import SystemSettings
from . import calendar as months
from . import control
from . import help as guide
from . import notices
from .activity import folder_activity, overall, starts
from .campaigns import CampaignSummary, list_campaigns
from .templatetags.fa import jalali_long
from .terms import LIST_VIEWS, STATUS_ORDER, TODAY_LINE

# The overview's tiles: how many campaigns are at each of these stages.
TILES = (SENDING, AWAITING, STOPPED, SCHEDULED)
FINISHED = (COMPLETED, CANCELLED)
PAGE = 50  # campaigns a page
ATTENTION_SHOWN = 5  # the rest are one tab away


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


def campaign_rows() -> tuple[list[dict], list[dict], dict]:
    """Every campaign with where it stands (the newest first), what needs
    someone now, and how many are at each tile's stage. The campaigns in
    data/db (the CLI's and the dashboard's), and dashboard campaigns that
    haven't sent yet."""
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
    return rows, attention, tiles


@requires("view_campaigns")
def home(request):
    """The control room (plan 06, L4): how sending is going today and this
    week, the sends on their way, the credit, what needs someone, this
    week's sends, and the latest campaigns."""
    rows, attention, _tiles = campaign_rows()
    activity = folder_activity()
    total = overall(activity)
    sends = control.active_sends()
    started_today = Job.objects.filter(kind=Job.Kind.SEND, started_at__gte=datetime.fromtimestamp(
        starts()["today"], tz=dt_timezone.utc)).count()
    return render(request, "dashboard/home.html", {
        "today_line": say(TODAY_LINE, {"date": jalali_long(timezone.now()), "sends": started_today,
                                       "active": len(sends)}),
        "figures": control.figures(total["today"], total["week"]),
        "active_sends": sends,
        "credit": control.credit(total["month"]),
        "credit_warning": credit.warning(),
        "attention": attention[:ATTENTION_SHOWN],
        "attention_more": max(0, len(attention) - ATTENTION_SHOWN),
        "this_week": control.week(),
        "recent": control.recent(rows, activity),
        "campaigns_total": len(rows),
        "worker_alive": worker_alive(),
    })


# HTMX swaps a 286 answer in, then stops polling: nothing is on its way.
STOP_POLLING = 286


@requires("view_campaigns")
def home_sends(request):
    """The sends on their way alone, for the control room to poll."""
    sends = control.active_sends()
    return render(request, "dashboard/_active_sends.html", {"active_sends": sends},
                  status=200 if sends else STOP_POLLING)


@requires("view_campaigns")
def campaign_list(request):
    """Every campaign with where it stands: tabs (in progress, needs
    attention, ended), a stage from a tile, and a search."""
    rows, attention, tiles = campaign_rows()
    # The list: a tab (in progress, needs attention, finished), a stage from
    # a tile, and a search by name, short name or template. Nothing personal
    # goes in the address.
    needing = {item["campaign"].slug for item in attention}
    groups = {
        "all": rows,
        "active": [r for r in rows if r["life"] is not None and r["life"].stage not in FINISHED],
        "attention": [r for r in rows if r["summary"].slug in needing],
        "finished": [r for r in rows if r["life"] is None or r["life"].stage in FINISHED],
    }
    view = request.GET.get("view", "all")
    if view not in groups:
        view = "all"
    shown = groups[view]
    stage = request.GET.get("stage", "")
    if stage in tiles:
        shown = [r for r in shown if r["life"] is not None and r["life"].stage == stage]
    else:
        stage = ""
    query = request.GET.get("q", "").strip()
    if query:
        needle = query.casefold()
        shown = [r for r in shown if needle in " ".join(filter(None, (
            r["summary"].slug, r["summary"].name, r["summary"].template,
            r["campaign"].name if r["campaign"] else None,
            (r["campaign"].settings or {}).get("template") if r["campaign"] else None,
        ))).casefold()]
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    pages = max(1, -(-len(shown) // PAGE))
    page = min(page, pages)
    return render(request, "dashboard/campaigns.html", {
        "campaigns": rows,
        "shown": shown[(page - 1) * PAGE: page * PAGE],
        "matching": len(shown),
        "page": page,
        "pages": pages,
        "views": [(key, label, len(groups[key])) for key, label in LIST_VIEWS.items()],
        "view": view,
        "stage": stage,
        "query": query,
        "status_order": STATUS_ORDER,
        "tiles": [(STAGES[s], n, s) for s, n in tiles.items()],
        "attention": attention[:ATTENTION_SHOWN],
        "attention_more": max(0, len(attention) - ATTENTION_SHOWN),
        "worker_alive": worker_alive(),
        "credit_warning": credit.warning(),
    })


@requires("view_campaigns")
def notifications(request):
    """The last 7 days' notifications, the new ones marked. Opening the
    page marks them all seen, so the menu's count goes back to zero."""
    now = timezone.now()
    seen = notices.seen_at(request.user)
    items = [{"notice": n, "new": seen is None or n.at > seen} for n in notices.notices(request.user, now)]
    notices.mark_seen(request.user, now)
    return render(request, "dashboard/notifications.html", {"items": items, "days": notices.WINDOW.days})


_SLUG = re.compile(r"^[-a-zA-Z0-9_]+$")


def _changed(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # deleted since: last
        return 0.0


@requires("view_campaigns")
def palette(request):
    """What Ctrl+K (Cmd+K) finds besides the menu's pages, which the page
    has already: every campaign (the CLI's too, by its DB's name), segment
    and preset, the newest first, each with where it opens for you. Names
    and short names only, nothing personal."""
    from ..campaigns.models import Preset
    from ..segments.models import Segment

    managed = list(Campaign.objects.order_by("-created_at", "-id").only("slug", "name", "preset_id"))
    known = {c.slug for c in managed}
    on_disk = sorted(Path(settings.SMS_SENDER_DB_DIR).glob("*.db"), key=_changed, reverse=True)
    campaigns = [{"name": c.name, "hint": c.slug, "url": control._url(c)} for c in managed]
    campaigns += [{"name": p.stem, "hint": p.stem, "url": reverse("report", args=[p.stem])}
                  for p in on_disk if p.stem not in known and _SLUG.match(p.stem)]
    segments = [{"name": s.name or s.slug, "hint": s.slug, "url": reverse("segment_detail", args=[s.slug])}
                for s in Segment.objects.order_by("-uploaded_at", "-id").only("slug", "name")]
    run, edit = can(request.user, "run_campaigns"), can(request.user, "edit_campaigns")
    presets = []
    for p in Preset.objects.only("slug", "name", "archived_at"):
        if run and p.archived_at is None:
            url = reverse("compose", args=[p.slug])  # a new alert from it
        elif edit:
            url = reverse("preset_edit", args=[p.slug])
        else:
            url = reverse("preset_list")
        presets.append({"name": p.name, "hint": p.slug, "url": url})
    return JsonResponse({"campaigns": campaigns, "segments": segments, "presets": presets})


@requires("view_campaigns")
def calendar_page(request):
    """A month of sends, Solar Hijri: ?month=1405-07, else this month."""
    year, month = months.month_of(request.GET.get("month"))
    return render(request, "dashboard/calendar.html", {"month": months.month(year, month)})


@requires("view_campaigns")
def help_page(request):
    """The short in-app help: the steps, the rules every send keeps, what each
    status and button means, and the roles."""
    start, _sep, end = (SystemSettings.load().default_send_window or DEFAULT_WINDOW).partition("-")
    return render(request, "dashboard/help.html", {
        "steps": guide.steps(),
        "rules": [fill(rule, {"start": start, "end": end}) for rule in guide.RULES],
        "statuses": guide.statuses(),
        "deliveries": guide.deliveries(),
        "actions": guide.ACTIONS,
        "stops": guide.STOPS,
        "roles": guide.roles(),
        "two_step": guide.TWO_STEP,
    })
