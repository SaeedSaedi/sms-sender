"""Campaign pages (spec 3, 4.6, 4.9): settings, the check, a test SMS and
its approval, then sending with live progress and pause / resume / cancel.
Every action is a job for the worker, and every one is in the activity log."""
from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from ..accounts.decorators import forbidden, requires
from ..accounts.models import test_phone_of
from ..accounts.roles import can
from ..audit.record import record
from ..dashboard.campaigns import read_campaign
from ..dashboard.templatetags.fa import fa_number
from ..dashboard.terms import STATUS_ORDER
from ..jobs import services
from ..jobs.engine import campaign_db
from ..jobs.models import Campaign, Job
from ..segments.models import Segment
from .checks import check_campaign
from .forms import TOKENS, NewCampaignForm, SettingsForm
from .terms import CHECK_PROBLEMS, CONFLICTS, INVALID_ROWS, stop_reason

# Who may do what (spec 4.12).
_ACTIONS = {
    "test": "run_campaigns",
    "approve": "run_campaigns",
    "reject": "run_campaigns",
    "send": "run_campaigns",
    "pause": "run_campaigns",
    "resume": "run_campaigns",
    "cancel": "run_campaigns",
    "reconcile": "update_campaigns",
    "delivery": "update_campaigns",
    "clicks": "update_campaigns",
}


def _send_started(campaign: Campaign) -> bool:
    """Once a send has started, the message is fixed: what went out and what
    follows must be the same campaign."""
    return Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND).exclude(started_at=None).exists()


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def campaign_new(request):
    form = NewCampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        segment = d["segment"]
        campaign = Campaign.objects.create(
            slug=d["slug"], name=d["name"].strip(), created_by=request.user,
            settings={
                "segment": segment.slug, "input": str(segment.path),
                "user_id_column": segment.user_id_column or None,
                "template": d["template"], "tokens": {}, "token_columns": {}, "value_maps": {},
                "send_window": "08:00-21:00", "rate": None, "workers": 5,
            },
        )
        record("campaign_created", request=request, campaign=campaign.slug)
        return redirect("campaign_settings", slug=campaign.slug)
    return render(request, "campaigns/new.html", {"form": form})


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def campaign_settings(request, slug: str):
    campaign = get_object_or_404(Campaign, slug=slug)
    if _send_started(campaign):
        messages.error(request, _locked_message())
        return redirect("campaign_detail", slug=slug)
    segment = Segment.objects.filter(slug=campaign.settings.get("segment")).first()
    columns = segment.token_columns if segment else []
    form = SettingsForm(
        request.POST or None, columns=columns, initial=SettingsForm.initial_from(campaign.settings),
    )
    if request.method == "POST" and form.is_valid():
        before = dict(campaign.settings)
        campaign.settings = form.settings(campaign)
        campaign.save(update_fields=["settings"])
        changed = sorted(k for k in set(before) | set(campaign.settings) if before.get(k) != campaign.settings.get(k))
        if changed:
            record("campaign_changed", request=request, campaign=slug, changed=changed)
        return redirect("campaign_detail", slug=slug)
    return render(request, "campaigns/settings.html", {
        "campaign": campaign, "form": form, "columns": columns,
        "segments": Segment.objects.filter(status=Segment.Status.READY),
        "tokens": [
            {"name": name, "source": form[f"{name}_source"], "value": form[f"{name}_value"],
             "column": form[f"{name}_column"]}
            for name in TOKENS
        ],
    })


def _locked_message() -> str:
    return _("This campaign has started sending, so its settings can't change. For a different message, make a new campaign.")


def _links_progress(job: Job | None) -> dict | None:
    """While a job makes its short links: how far, and about how long."""
    p = (job.progress or {}) if job else {}
    if p.get("stage") != "links" or not p.get("total"):
        return None
    eta = p.get("eta_sec")
    if eta is None:
        left = ""
    elif eta >= 60:
        left = _("About %(minutes)s minutes left") % {"minutes": fa_number(-(-eta // 60))}
    else:
        left = _("Less than a minute left")
    return {
        "total": p["total"],
        "done": p.get("processed", 0),
        "label": _("Short links made: %(done)s of %(total)s") % {
            "done": fa_number(p.get("processed", 0)), "total": fa_number(p["total"]),
        },
        "left": left,
    }


def _live(request, campaign: Campaign) -> dict:
    """What the live part of the page shows: the steps, the jobs, the counts."""
    jobs = list(
        Job.objects.filter(campaign=campaign).select_related("requested_by", "decided_by")
        .order_by("-created_at", "-id")[:20]
    )
    active = [job for job in jobs if job.state in services.ACTIVE]
    test = services.latest_test(campaign)
    db = campaign_db(campaign)
    summary = read_campaign(db) if db.exists() else None
    counts = summary.counts if summary else {}
    waiting = counts.get("pending", 0) + counts.get("failed_retriable", 0)
    return {
        "cancel_confirm": _(
            "Cancel the campaign? %(n)s recipients who haven't got the SMS yet are cancelled. "
            "SMS that Kavenegar already accepted can't be recalled."
        ) % {"n": fa_number(waiting)},
        "send_confirm": (
            _("Start sending to %(n)s recipients now?") % {"n": fa_number(waiting)}
            if waiting else _("Start sending this campaign now?")
        ),
        "campaign": campaign,
        "jobs": [(job, stop_reason(job.result.get("stop_reason"), job.result.get("stop_fields"))) for job in jobs],
        "active": bool(active),
        "active_send": next((j for j in active if j.kind == Job.Kind.SEND), None),
        "active_test": next((j for j in active if j.kind == Job.Kind.TEST), None),
        "test_links": _links_progress(next((j for j in active if j.kind == Job.Kind.TEST), None)),
        "send_links": _links_progress(next((j for j in active if j.kind == Job.Kind.SEND), None)),
        "test": test,
        "test_reason": stop_reason(test.result.get("stop_reason"), test.result.get("stop_fields")) if test else "",
        "approved": services.approval(campaign),
        "test_outdated": bool(test and test.settings_hash != services.settings_hash(campaign)),
        # A finished test nobody has approved or rejected yet: that comes first.
        "awaiting_decision": bool(
            test and test.state == Job.State.DONE and not test.decision
            and test.settings_hash == services.settings_hash(campaign)
        ),
        "summary": summary,
        "status_order": STATUS_ORDER,
        "test_phone": test_phone_of(request.user),
        "send_started": _send_started(campaign),
    }


def _page(request, campaign: Campaign, **extra):
    s = campaign.settings
    return render(request, "campaigns/detail.html", {
        **_live(request, campaign),
        "segment": Segment.objects.filter(slug=s.get("segment")).first(),
        "tokens": [
            (name, (s.get("tokens") or {}).get(name), (s.get("token_columns") or {}).get(name))
            for name in TOKENS
        ],
        **extra,
    })


@requires("view_campaigns")
def campaign_detail(request, slug: str):
    return _page(request, get_object_or_404(Campaign, slug=slug))


@requires("view_campaigns")
def campaign_live(request, slug: str):
    """The live part alone, for HTMX to poll while a job is active."""
    campaign = get_object_or_404(Campaign, slug=slug)
    return render(request, "campaigns/_live.html", _live(request, campaign))


@requires("edit_campaigns")
@require_POST
def campaign_check(request, slug: str):
    campaign = get_object_or_404(Campaign, slug=slug)
    result = check_campaign(campaign)
    return _page(
        request, campaign, check=result,
        check_problems=[CHECK_PROBLEMS[key] for key in result.problems],
        check_invalid=[(INVALID_ROWS.get(key, key), n) for key, n in sorted(result.invalid.items())],
    )


def _job(campaign: Campaign, request, kinds: tuple[str, ...]) -> Job:
    pk = request.POST.get("job", "")
    if not pk.isdigit():
        raise Http404
    return get_object_or_404(Job, pk=pk, campaign=campaign, kind__in=kinds)


@require_POST
def campaign_action(request, slug: str, action: str):
    capability = _ACTIONS.get(action)
    if capability is None:
        raise Http404
    if not can(request.user, capability):
        return forbidden(request)
    campaign = get_object_or_404(Campaign, slug=slug)
    try:
        if action == "test":
            services.request_test(campaign, request.user)
            record("test_requested", request=request, campaign=slug)
            messages.success(request, _("The test SMS is queued. Its result appears here."))
        elif action in ("approve", "reject"):
            services.decide_test(_job(campaign, request, (Job.Kind.TEST,)), request.user, action == "approve")
            record("test_approved" if action == "approve" else "test_rejected", request=request, campaign=slug)
            if action == "approve":
                messages.success(request, _("The test SMS is approved. Sending can start."))
            else:
                messages.success(request, _("The test SMS is rejected. Change the settings and send a new test."))
        elif action == "send":
            services.start_send(campaign, request.user)
            record("send_started", request=request, campaign=slug)
            messages.success(request, _("Sending is queued. Progress appears here."))
        elif action == "pause":
            job = services.pause(_job(campaign, request, (Job.Kind.SEND,)))
            record("job_paused", request=request, campaign=slug, kind=job.kind)
            messages.success(request, _("Sending pauses after the requests already on their way."))
        elif action == "resume":
            job = services.resume(_job(campaign, request, (Job.Kind.SEND,)))
            record("job_resumed", request=request, campaign=slug, kind=job.kind)
            messages.success(request, _("Sending is queued again; unknown statuses are checked first."))
        elif action == "cancel":
            job = services.cancel(_job(campaign, request, (Job.Kind.SEND, Job.Kind.TEST)))
            record("job_cancelled", request=request, campaign=slug, kind=job.kind)
            messages.success(request, _("Cancelled. SMS that Kavenegar already accepted can't be recalled."))
        else:
            with transaction.atomic():
                services.enqueue(campaign, action, request.user)
            record("job_requested", request=request, campaign=slug, kind=action)
            messages.success(request, _("Queued. The result appears in the list of jobs."))
    except services.JobConflict as e:
        messages.error(request, CONFLICTS.get(e.code, CONFLICTS["busy"]))
    return redirect("campaign_detail", slug=slug)
