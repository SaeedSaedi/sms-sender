"""Campaign pages (spec 3, 4.6, 4.9): settings, the check, a test SMS and
its approval, then sending with live progress and pause / resume / cancel.
Every action is a job for the worker, and every one is in the activity log."""
from __future__ import annotations

import copy
import logging
import math

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from sms_sender.links import DEFAULT_RATE as DEFAULT_LINK_RATE
from sms_sender.state import FAILED_PERMANENT, SENT, StateStore
from sms_sender.rate import parse_rate
from sms_sender.sender import TOKEN_MAX_SPACES

from .. import live, undo
from ..accounts.decorators import forbidden, requires
from ..backup import BackupError
from ..accounts.models import test_phone_of
from ..accounts.roles import can
from ..audit.record import record
from ..dashboard.campaigns import read_campaign
from ..dashboard.templatetags.fa import fa_number, jalali
from ..dashboard import control
from ..dashboard.terms import DELIVERY_FILTERS, STATUS_ORDER, SUBMISSION_STATUS as STATUS_LABELS
from ..jobs import services
from ..jobs.engine import campaign_db
from ..jobs.models import Campaign, Job
from ..segments.models import Segment
from ..system import operations
from ..system.models import SystemSettings
from .checks import check_campaign
from .forms import (
    _ASCII_DIGITS, SCHEDULE_ERRORS, TOKENS, DuplicateForm, NewCampaignForm, SettingsForm, combined,
    free_slug, more_segment_choices, parse_when,
)
from .lifecycle import AWAITING, CANCELLED, COMPLETED, DRAFT, PAUSED, READY, STOPPED, lifecycle, settings_complete
from .message import placeholders
from .models import MessageTemplate
from .present import checklist, notes, result_line, say, send_summary
from .preview import ROW_CHOICES, load_segment, preview_of, recipients
from .profiles import MAX_BYTES as PROFILES_MAX_BYTES
from .profiles import ProfileFileError, drafts as profile_drafts, read as read_profiles
from .terms import (
    CHECK_PROBLEMS, CHECK_STATES, CONFLICTS, COST_EACH, COST_TOTAL, FOLLOWUPS, IMPORT_FILE_ERRORS,
    IMPORT_PROBLEMS, INVALID_ROWS, LINKS_AT_SEND, NEXT_STEP, NUMBERS_REMOVED, REQUEUE_CONFIRM, REQUEUED,
    STAGES, STEPS, stop_reason,
)

logger = logging.getLogger(__name__)

# Who may do what (spec 4.12).
_ACTIONS = {
    "test": "run_campaigns",
    "approve": "run_campaigns",
    "reject": "run_campaigns",
    "send": "run_campaigns",
    "unschedule": "run_campaigns",
    "reschedule": "run_campaigns",  # the unschedule's undo
    "start_now": "run_campaigns",
    "pause": "run_campaigns",
    "resume": "run_campaigns",
    "cancel": "run_campaigns",
    "reconcile": "update_campaigns",
    "delivery": "update_campaigns",
    "clicks": "update_campaigns",
}


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def campaign_new(request):
    form = NewCampaignForm(request.POST or None, initial={"segment": request.GET.get("segment", "")})
    if live.validating(request):  # as someone types: the errors, nothing saved
        return live.errors(form)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        segment = d["segment"]
        defaults = SystemSettings.load()
        campaign = Campaign.objects.create(
            slug=d["slug"], name=d["name"].strip(), created_by=request.user,
            settings={
                "segment": segment.slug, "input": str(segment.path),
                "user_id_column": segment.user_id_column or None,
                "template": d["template"], "tokens": {}, "token_columns": {}, "value_maps": {},
                "send_window": defaults.default_send_window or "08:00-21:00",
                "rate": defaults.default_rate or None, "workers": 5,
            },
        )
        record("campaign_created", request=request, campaign=campaign.slug)
        return redirect("campaign_settings", slug=campaign.slug)
    return render(request, "campaigns/new.html", {"form": form})


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def campaign_duplicate(request, slug: str):
    """A new campaign with this one's settings, under its own name."""
    source = get_object_or_404(Campaign, slug=slug)
    form = DuplicateForm(request.POST or None, initial={
        "name": _("%(name)s (copy)") % {"name": source.name}, "slug": free_slug(source.slug),
    })
    if live.validating(request):  # as someone types: the errors, nothing saved
        return live.errors(form)
    if request.method == "POST" and form.is_valid():
        campaign = Campaign.objects.create(
            slug=form.cleaned_data["slug"], name=form.cleaned_data["name"], created_by=request.user,
            settings=copy.deepcopy(source.settings),
        )
        record("campaign_duplicated", request=request, campaign=campaign.slug, source=source.slug)
        messages.success(request, _("Copied from “%(name)s”. Check its settings, then the list and a test SMS.")
                         % {"name": source.name})
        return redirect("campaign_settings", slug=campaign.slug)
    return render(request, "campaigns/duplicate.html", {"form": form, "source": source})


# The profiles file between reading it and importing from it.
_PROFILES_SESSION = "profile_import"


def _cli_option(key: str) -> str:
    return "--" + key.replace("_", "-")


def _import_page(request, text: str, *, error: str = "", file: str = ""):
    rows = [
        {"draft": d, "problems": [IMPORT_PROBLEMS[key] for key in d.problems],
         "reset": ", ".join(map(_cli_option, d.reset)), "left_out": ", ".join(map(_cli_option, d.left_out))}
        for d in profile_drafts(read_profiles(text.encode("utf-8")))
    ]
    return render(request, "campaigns/import.html", {
        "rows": rows, "error": error, "file": file,
        "importable": sum(1 for row in rows if row["draft"].importable),
    })


@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def campaign_import(request):
    """The CLI's profiles (sms-sender.toml) as draft campaigns, for an
    admin: read the file, see each profile as the campaign it would be,
    then import the ones chosen. The file waits in the session between the
    two steps; nothing is sent, and no campaign DB is opened."""
    if request.method == "GET":
        request.session.pop(_PROFILES_SESSION, None)
        return render(request, "campaigns/import.html", {})
    if request.POST.get("step") == "import":
        text = request.session.get(_PROFILES_SESSION)
        if text is None:
            messages.error(request, IMPORT_FILE_ERRORS["expired"])
            return redirect("campaign_import")
        chosen = set(request.POST.getlist("profile"))
        try:
            found = [d for d in profile_drafts(read_profiles(text.encode("utf-8")))
                     if d.profile in chosen and d.importable]
        except ProfileFileError:
            found = []
        if not found:
            return _import_page(request, text, error=_("Choose at least one profile to import."))
        with transaction.atomic():
            created = [
                Campaign.objects.create(slug=d.slug, name=d.name, created_by=request.user, settings=d.settings)
                for d in found
            ]
        for d, campaign in zip(found, created):
            record("campaign_imported", request=request, campaign=campaign.slug, profile=d.profile)
        request.session.pop(_PROFILES_SESSION, None)
        messages.success(request, _(
            "%(n)s campaigns were made from the profiles, each as a draft: choose its segment if it has none, "
            "check it, then send a test SMS."
        ) % {"n": fa_number(len(created))})
        return redirect("campaign_list")
    upload = request.FILES.get("file")
    if upload is None:
        return render(request, "campaigns/import.html", {"error": IMPORT_FILE_ERRORS["missing"]})
    raw = upload.read(PROFILES_MAX_BYTES + 1)
    try:
        read_profiles(raw)
    except ProfileFileError as e:
        return render(request, "campaigns/import.html", {"error": IMPORT_FILE_ERRORS[e.code]})
    text = raw.decode("utf-8-sig")
    request.session[_PROFILES_SESSION] = text
    return _import_page(request, text, file=upload.name[:255])


def _other_segments(campaign: Campaign) -> tuple[list[Segment], list[Segment]]:
    """Where this message can go next: ready segments other than the current
    one, (with every column its tokens use, without)."""
    s = campaign.settings or {}
    needed = set((s.get("token_columns") or {}).values())
    usable, lacking = [], []
    for segment in Segment.objects.filter(status=Segment.Status.READY).exclude(slug=s.get("segment")).order_by("name"):
        (usable if needed <= set(segment.token_columns or []) else lacking).append(segment)
    return usable, lacking


@require_POST
def campaign_requeue(request, slug: str):
    """Put a status's recipients back in the queue, for the next send (the
    CLI's reset --status and retry-failed --include-permanent). Each status
    has its role; sent ones also need the short name typed, and a backup."""
    campaign = get_object_or_404(Campaign, slug=slug)
    status = request.POST.get("status", "")
    capability = services.REQUEUE.get(status)
    if capability is None:
        raise Http404
    if not can(request.user, capability):
        return forbidden(request)
    if status == SENT and request.POST.get("confirm", "").strip() != campaign.slug:
        messages.error(request, _("To confirm, type the campaign's short name exactly as shown."))
        return redirect("campaign_detail", slug=slug)
    try:
        n, backup = services.requeue(campaign, status)
    except services.JobConflict as e:
        messages.error(request, CONFLICTS.get(e.code, CONFLICTS["busy"]))
        return redirect("campaign_detail", slug=slug)
    except (BackupError, OSError):
        logger.exception("requeue_backup_failed", extra={"campaign": slug})
        messages.error(request, _("The backup failed, so nothing changed. The details are in the system log."))
        return redirect("campaign_detail", slug=slug)
    record("recipients_requeued", request=request, campaign=slug, status=status, count=n,
           **({"backup": backup} if backup else {}))
    messages.success(request, say(REQUEUED, {"n": n}))
    return redirect("campaign_detail", slug=slug)


@requires("edit_campaigns")
@require_POST
def campaign_segment(request, slug: str):
    """Another round: the same message to another segment, or to several
    in one send (the CLI's `send --campaign` with another `--input`). The
    approval covers the list, so a new test SMS comes first, and anyone
    this campaign already sent to is skipped."""
    campaign = get_object_or_404(Campaign, slug=slug)
    if services.numbers_removed(campaign):
        messages.error(request, CONFLICTS["numbers_removed"])
        return redirect("campaign_detail", slug=slug)
    if Job.objects.filter(campaign=campaign, kind=Job.Kind.SEND, state__in=services.ACTIVE).exists():
        messages.error(request, CONFLICTS["send_on_its_way"])
        return redirect("campaign_detail", slug=slug)
    usable, _lacking = _other_segments(campaign)
    wanted = set(request.POST.getlist("segment"))
    if not wanted:
        messages.error(request, _("Choose at least one segment."))
        return redirect("campaign_detail", slug=slug)
    chosen = [seg for seg in usable if seg.slug in wanted]  # in the order they're listed
    if len(chosen) != len(wanted):
        raise Http404
    segment, *more = chosen
    before = campaign.settings.get("segment")
    campaign.settings.update(
        segment=segment.slug, input=str(segment.path), user_id_column=segment.user_id_column or None,
    )
    if more:
        campaign.settings["more_segments"] = [seg.slug for seg in more]
    else:
        campaign.settings.pop("more_segments", None)
    campaign.save()
    record("segment_switched", request=request, campaign=slug, before=before, after=segment.slug,
           **({"more": [seg.slug for seg in more]} if more else {}))
    if more:
        messages.success(request, _(
            "The campaign now sends to %(n)s segments in one send. Check the list, then send a new test SMS."
        ) % {"n": fa_number(len(chosen))})
    else:
        messages.success(request, _("The campaign now sends to “%(segment)s”. Check the list, then send a new test SMS.")
                         % {"segment": segment.name})
    return redirect("campaign_detail", slug=slug)


@requires("delete_campaign_data")
@require_POST
def campaign_purge(request, slug: str):
    """The campaign and its records, after a backup (the CLI's purge). Its
    numbers become new to every later campaign: the short name typed first."""
    campaign = get_object_or_404(Campaign, slug=slug)
    if request.POST.get("confirm", "").strip() != campaign.slug:
        messages.error(request, _("To confirm, type the campaign's short name exactly as shown."))
        return redirect("campaign_detail", slug=slug)
    try:
        backup = operations.purge(campaign)
    except operations.PurgeRefused:
        messages.error(request, _("A job of this campaign is on its way. Delete it after the job ends."))
        return redirect("campaign_detail", slug=slug)
    except (BackupError, OSError):
        logger.exception("purge_backup_failed", extra={"campaign": slug})
        messages.error(request, _("The backup failed, so nothing changed. The details are in the system log."))
        return redirect("campaign_detail", slug=slug)
    record("campaign_purged", request=request, campaign=slug, backup=backup)
    messages.success(request, _("The campaign and its records were deleted. Backup: %(name)s.") % {"name": backup})
    return redirect("campaign_list")


@requires("edit_campaigns")
@require_POST
def campaign_adopt(request, slug: str):
    """A campaign the CLI made, from now on on the dashboard: its follow-ups
    and report at once, sending again after a segment and a test SMS."""
    if not operations.adoptable(slug):
        raise Http404
    campaign = operations.adopt(slug, request.user)
    record("campaign_adopted", request=request, campaign=slug)
    messages.success(request, _("The campaign is on the dashboard now. To send again, choose its segment, then a test SMS."))
    return redirect("campaign_detail", slug=campaign.slug)


@requires("change_sent_message")
@require_POST
def campaign_unlock(request, slug: str):
    """An admin continues with a changed message (the CLI's
    --allow-settings-change): the short name typed to confirm, a backup
    first, and an activity-log entry."""
    campaign = get_object_or_404(Campaign, slug=slug)
    if request.POST.get("confirm", "").strip() != campaign.slug:
        messages.error(request, _("To confirm, type the campaign's short name exactly as shown."))
        return redirect("campaign_detail", slug=slug)
    try:
        backup, superseded = services.unlock_message(campaign, request.user)
    except services.JobConflict as e:
        messages.error(request, CONFLICTS.get(e.code, CONFLICTS["busy"]))
        return redirect("campaign_detail", slug=slug)
    except (BackupError, OSError):
        logger.exception("unlock_backup_failed", extra={"campaign": slug})
        messages.error(request, _("The backup failed, so nothing changed. The details are in the system log."))
        return redirect("campaign_detail", slug=slug)
    record("message_unlocked", request=request, campaign=slug, backup=backup, superseded=superseded)
    messages.success(request, _(
        "Backed up. The settings are open for you: what you save goes to those still waiting, after a new test SMS."
    ))
    return redirect("campaign_settings", slug=slug)


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def campaign_settings(request, slug: str):
    campaign = get_object_or_404(Campaign, slug=slug)
    locked = services.settings_locked(campaign, request.user)
    if locked:
        messages.error(request, _locked_message(locked))
        return redirect("campaign_detail", slug=slug)
    segment = Segment.objects.filter(slug=campaign.settings.get("segment")).first()
    columns = segment.token_columns if segment else []
    initial = SettingsForm.initial_from(campaign.settings)
    form = SettingsForm(
        combined(request.POST) if request.method == "POST" else None, columns=columns, initial=initial,
        advanced=can(request.user, "manage_settings"),
    )
    if live.validating(request):  # as someone types: the errors, nothing saved
        return live.errors(form)
    if request.method == "POST" and form.is_valid():
        before = dict(campaign.settings)
        campaign.settings = form.settings(campaign)
        campaign.save(update_fields=["settings"])
        changed = sorted(k for k in set(before) | set(campaign.settings) if before.get(k) != campaign.settings.get(k))
        if changed:
            record("campaign_changed", request=request, campaign=slug, changed=changed)
        return redirect("campaign_detail", slug=slug)
    ui = request.POST if request.method == "POST" else initial
    rows = (
        [{"column": c, "source": f, "target": t} for c, f, t in zip(
            request.POST.getlist("vm_column"), request.POST.getlist("vm_source"),
            request.POST.getlist("vm_target"))]
        if request.method == "POST" else initial["value_map_rows"]
    )
    return render(request, "campaigns/settings.html", {
        **settings_page_context(request, form, campaign.settings, segment, columns, ui, rows),
        # An admin unlocked a campaign that has sent: say what saving means.
        "changing_sent_message": services.may_have_sent(campaign),
        "campaign": campaign,
        "page_title": _("Campaign settings"), "subject_name": campaign.name,
        "crumbs": [(reverse("campaign_list"), _("Campaigns")), (reverse("campaign_detail", args=[slug]), campaign.name)],
        "back_url": reverse("campaign_detail", args=[slug]), "back_label": _("Back to the campaign"),
        "preview_url": reverse("campaign_settings_preview", args=[slug]),
        "save_note": _("Changing the message, its tokens, the link or the segment needs a new test SMS before sending."),
    })


def settings_page_context(request, form, base_settings: dict, segment, columns, ui, rows) -> dict:
    """What the settings page shows besides its title and links: the same
    for a campaign and for a preset."""
    # The tokens the template's text uses come first; when its text is in
    # the library, the rest wait under "the tokens the text doesn't use"
    # (any of them already filled in stays in view).
    known = MessageTemplate.objects.filter(name=(form["template"].value() or "").strip()).first()
    used = set(placeholders(known.text)) if known else None
    tokens = [
        {"name": name, "source": form[f"{name}_source"], "value": form[f"{name}_value"],
         "column": form[f"{name}_column"], "max_spaces": TOKEN_MAX_SPACES[name],
         "shown": used is None or name in used or bool(form[f"{name}_source"].value())}
        for name in TOKENS
    ]
    return {
        "form": form, "columns": columns, "ui": ui,
        "value_map_rows": rows + [{"column": "", "source": "", "target": ""}],
        "segments": Segment.objects.filter(status=Segment.Status.READY),
        "more_segment_choices": more_segment_choices(),
        "chosen_more": set(form["more_segments"].value() or []),
        "templates": MessageTemplate.objects.all(),
        "advanced": can(request.user, "manage_settings"),
        **_with_cost("preview", preview_of(base_settings, segment)),
        "tokens": tokens,
        "hidden_tokens": sum(1 for t in tokens if not t["shown"]),
    }


@requires("edit_campaigns")
@require_POST
def settings_preview(request, slug: str):
    """The message as these (unsaved) settings would send it, for the
    settings page's preview panel. Saves nothing."""
    return settings_preview_response(request, get_object_or_404(Campaign, slug=slug).settings)


def settings_preview_response(request, base_settings: dict):
    """The preview panel for unsaved settings (a campaign's or a preset's)."""
    data = combined(request.POST)
    segment = Segment.objects.filter(slug=data.get("segment")).first()
    form = SettingsForm(data, columns=segment.token_columns if segment else [],
                        initial=SettingsForm.initial_from(base_settings))
    form.is_valid()  # the preview uses whatever is filled in, errors or not
    d = form.cleaned_data
    settings = {
        "template": (data.get("template") or "").strip(),
        "tokens": d.get("tokens", {}), "token_columns": d.get("token_columns", {}),
        "value_maps": d.get("value_maps_parsed", {}),
        "links": {"token": d.get("link_token"), "format": d.get("link_format") or "url"}
        if d.get("link_token") else None,
    }
    return render(request, "campaigns/_preview.html", _with_cost("preview", preview_of(settings, segment)))


def _locked_message(why: str) -> str:
    if why == "numbers_removed":
        return str(CONFLICTS["numbers_removed"])
    if why == "scheduled":
        return _("A send is scheduled for this campaign. To change its settings, cancel the schedule first.")
    if why == "waiting":
        return _("A send is on its way, so the settings can't change until it ends.")
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


def _with_cost(name: str, preview, recipients: int = 0) -> dict:
    """The preview under `name`, and `<name>_cost`: about what one SMS costs,
    and for `recipients` when known (an estimate from the latest test SMS;
    the test gives the real figure)."""
    line = ""
    if preview.cost and recipients:
        line = say(COST_TOTAL, {"cost": preview.cost, "total": preview.cost * recipients, "n": recipients})
    elif preview.cost:
        line = say(COST_EACH, {"cost": preview.cost})
    return {name: preview, f"{name}_cost": line}


def _links_note(campaign: Campaign, waiting: int) -> str:
    """How long the recipients' links take when sending starts (Shlink is
    capped, 10 a second by default): only for one link per recipient."""
    links = (campaign.settings or {}).get("links") or {}
    if not waiting or links.get("strategy", "recipient") != "recipient":
        return ""
    per_sec = parse_rate((campaign.settings or {}).get("link_rate") or DEFAULT_LINK_RATE) or 10.0
    minutes = max(1, math.ceil(waiting / per_sec / 60))
    return say(LINKS_AT_SEND, {"n": waiting, "minutes": minutes})


def _requeue_items(campaign: Campaign, user) -> list[dict]:
    """What the user may put back in the queue, with how many and what it
    means (the CLI's reset --status). Counted as the requeue counts them:
    an input row that wasn't a number never goes back."""
    db = campaign_db(campaign)
    if not db.exists():
        return []
    store = StateStore(db)
    raw, shown = store.counts(), store.display_counts()
    items = []
    for status, capability in services.REQUEUE.items():
        n = shown.get(status, 0) if status == FAILED_PERMANENT else raw.get(status, 0)
        if n and can(user, capability):
            items.append({"status": status, "label": STATUS_LABELS[status], "count": n,
                          "confirm": say(REQUEUE_CONFIRM[status], {"n": n}), "risky": status != FAILED_PERMANENT})
    return items


def _reconcile_params(data) -> dict:
    """The CLI's reconcile --min-age (in minutes here) and --review-not-found."""
    minutes = data.get("min_age_minutes", "").strip().translate(_ASCII_DIGITS)
    params = {"requeue_not_found": data.get("review_not_found") != "on"}
    if minutes.isdigit():
        params["min_age_sec"] = min(int(minutes), 7 * 24 * 60) * 60.0
    return params


def _followups(counts: dict) -> list[dict]:
    """What's left after a send, each with its action (if any)."""
    items = []
    for status, key, tone, icon, action in (
        ("unknown", "unknown", "warning", "clock", "reconcile"),
        ("failed_retriable", "not_sent", "info", "send", "send"),
        ("needs_review", "needs_review", "warning", "triangle-alert", ""),
        ("failed_permanent", "rejected", "error", "circle-x", ""),
    ):
        if counts.get(status):
            items.append({"tone": tone, "icon": icon, "action": action,
                          "text": say(FOLLOWUPS[key], {"n": counts[status]})})
    return items


def _steps(current: int) -> list[dict]:
    return [
        {"n": n, "label": label, "state": "done" if n < current else "current" if n == current else "todo"}
        for n, label in enumerate(STEPS, start=1)
    ]


def _live(request, campaign: Campaign, *, ready_step: bool = True, **preview) -> dict:
    """What the live part of the page shows: where the campaign stands and
    what comes next, the current step, the counts and the history. `preview`
    is the ready step's search (`query`, `rows`). `ready_step`: build the
    ready step's check and preview (the composer shows its own)."""
    jobs = list(
        Job.objects.filter(campaign=campaign).select_related("requested_by", "decided_by")
        .prefetch_related("events").order_by("-created_at", "-id")[:20]
    )
    active = [job for job in jobs if job.state in services.ACTIVE]
    life = lifecycle(campaign)
    test = services.latest_test(campaign)
    db = campaign_db(campaign)
    summary = read_campaign(db) if db.exists() else None
    counts = summary.counts if summary else {}
    waiting = counts.get("pending", 0) + counts.get("failed_retriable", 0)
    # Plan 06, D2: once its numbers are removed, only the history is offered.
    removed_at = services.numbers_removed(campaign)
    ready = {}
    if life.stage == READY and ready_step and not removed_at:  # the poll renders this step too, so it's built here
        segment = Segment.objects.filter(slug=campaign.settings.get("segment")).first()
        loaded = load_segment(campaign.settings, segment)  # one read for the check, the message and the rows
        ready = {**_check_context(campaign, loaded),
                 **_preview_context(request, campaign, segment=segment, loaded=loaded, **preview)}
        ready.update(_with_cost("message", ready["message"], ready["check"].to_send))
        ready["checklist"] = checklist(
            ready["check"], ready["message"], campaign.settings,
            Segment.objects.filter(slug=campaign.settings.get("segment")).first(),
        )
    elif not removed_at and can(request.user, "edit_campaigns") and (
        life.stage in (COMPLETED, CANCELLED) or (life.stage == DRAFT and services.may_have_sent(campaign))
    ):
        ready["other_segments"], ready["lacking_segments"] = _other_segments(campaign)
    if life.stage in (COMPLETED, CANCELLED, STOPPED) and not removed_at:
        ready["requeue_items"] = _requeue_items(campaign, request.user)
    active_job = next((j for j in active if j.kind == Job.Kind.SEND), None)
    # With a second approver (plan 06, D3), whoever asked for the test only rejects it.
    own_test = life.stage == AWAITING and services.needs_another_approver(test, request.user)
    test_phone = test_phone_of(request.user)
    return {
        **_live_send(campaign, db, active_job, counts),
        **ready,
        **_sends(campaign),
        "cancel_confirm": _(
            "Cancel the campaign? %(n)s recipients who haven't got the SMS yet are cancelled. "
            "SMS that Kavenegar already accepted can't be recalled."
        ) % {"n": fa_number(waiting)},
        # The same for starting now and for a set time: who gets it, and that it's final.
        "send_confirm": (
            _("%(n)s recipients will get this SMS. An SMS Kavenegar has accepted can't be recalled.")
            % {"n": fa_number(waiting)}
            if waiting else _("Everyone still waiting will get this SMS. An SMS Kavenegar has accepted can't be recalled.")
        ),
        "today_jalali": jalali(timezone.now(), "%Y/%m/%d"),
        "links_note": _links_note(campaign, waiting),
        "campaign": campaign,
        "jobs": jobs,
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
        "test_phone": test_phone,
        "test_team": len(services.team_numbers(test_phone)),
        "own_test": own_test,
        "settings_locked": bool(services.settings_locked(campaign, request.user)),
        "can_unlock": life.stage in (PAUSED, STOPPED, COMPLETED, CANCELLED)
        and can(request.user, "change_sent_message") and services.can_unlock(campaign),
        "message_unlocked": services.message_unlocked(campaign),
        "can_purge": can(request.user, "delete_campaign_data"),
        "life": life,
        "stage_label": STAGES[life.stage],
        "stage_template": "campaigns/stage/_numbers_removed.html" if removed_at
        else f"campaigns/stage/_{life.stage}.html",
        "numbers_removed_line": say(NUMBERS_REMOVED, {
            "when": jalali(removed_at, "%Y/%m/%d"), "months": SystemSettings.load().retention_months,
        }) if removed_at else "",
        "next_step": NEXT_STEP["paused_by_window" if life.paused_by_window
                               else "awaiting_other" if own_test else life.stage],
        "steps": _steps(life.step),
        "send_reason": stop_reason(
            (life.send.result or {}).get("stop_reason"), (life.send.result or {}).get("stop_fields"),
        ) if life.send else "",
        "send_summary": send_summary(life.send),
        "followup_items": [] if removed_at else _followups(counts),
        "history": [
            {"job": job, "result": result_line(job), "notes": notes(job)} for job in jobs
        ],
    }


# The delivery bar's parts, in the order they show, with their tone.
_DELIVERY_PARTS = (("delivered", "success"), ("on_its_way", "info"), ("unchecked", "neutral"),
                   ("not_delivered", "danger"), ("expired", "warning"))


def _live_send(campaign: Campaign, db, active_job: Job | None, counts: dict) -> dict:
    """The send as it goes (plan 06, L4): its pace and time left, and a bar
    per segment when it reads several; afterwards, delivery as Kavenegar
    reports it, which goes on for 48 hours."""
    out: dict = {"send_pace": control.active_send(active_job) if active_job else None,
                 "segment_bars": [], "delivery_bar": []}
    if not db.exists():
        return out
    store = StateStore(db)
    if active_job is not None:
        rows = store.segment_progress()
        if len(rows) > 1:
            names = dict(Segment.objects.filter(slug__in=[r["segment"] for r in rows]).values_list("slug", "name"))
            out["segment_bars"] = [{"name": names.get(r["segment"]) or r["segment"] or "—", "total": r["total"],
                                    "sent": r["sent"] or 0, "waiting": r["waiting"] or 0} for r in rows]
    if counts.get("sent"):
        groups = store.delivery_groups()
        labels = dict(DELIVERY_FILTERS)
        out["delivery_bar"] = [{"key": key, "tone": tone, "label": labels[key], "n": groups[key]}
                               for key, tone in _DELIVERY_PARTS if groups.get(key)]
    return out


def _check_context(campaign: Campaign, loaded=None) -> dict:
    result = check_campaign(campaign, loaded)
    return {
        "check": result,
        "check_problems": [CHECK_PROBLEMS[key] for key in result.problems],
        "check_invalid": [(INVALID_ROWS.get(key, key), n) for key, n in sorted(result.invalid.items())],
        "check_states": CHECK_STATES,
    }


def _preview_context(request, campaign: Campaign, *, query: str = "", rows: int = ROW_CHOICES[0],
                     segment=None, loaded=None) -> dict:
    """The ready step's preview, before any test SMS: the message and, for
    whoever sends, each recipient's message or one number's. Read only:
    nothing is sent, and no link is made. The segment is read once here
    (or by the caller, who passes it on)."""
    s = campaign.settings
    if segment is None:
        segment = Segment.objects.filter(slug=s.get("segment")).first()
    if loaded is None:
        loaded = load_segment(s, segment)
    context = _with_cost("message", preview_of(s, segment, loaded))
    if can(request.user, "run_campaigns"):
        context.update(
            row_choices=ROW_CHOICES, rows=rows, preview_query=query,
            recipients=recipients(s, segment, campaign.slug, limit=rows, phone=query or None, loaded=loaded),
        )
    return context


def _preview_params(data) -> dict:
    """The search from the form: a number (never in a URL) and how many rows."""
    rows = data.get("rows", "")
    return {
        "query": data.get("preview", "").strip()[:32],
        "rows": int(rows) if rows.isdigit() and int(rows) in ROW_CHOICES else ROW_CHOICES[0],
    }


def _page(request, campaign: Campaign, *, preview: dict | None = None, **extra):
    return render(request, "campaigns/detail.html", {
        **_live(request, campaign, **(preview or {})),
        "settings_complete": settings_complete(campaign.settings),
        **extra,
    })


def _sends(campaign: Campaign) -> dict:
    """What the campaign sends, for the side card (the poll renders it too):
    its segment and any more segments, and what fills each token."""
    s = campaign.settings or {}
    more = {seg.slug: seg for seg in Segment.objects.filter(slug__in=s.get("more_segments") or [])}
    return {
        "segment": Segment.objects.filter(slug=s.get("segment")).first(),
        # One send for several: (slug, the segment or None if it's gone), in order.
        "more_segments": [(slug, more.get(slug)) for slug in s.get("more_segments") or []],
        "tokens": [
            (name, (s.get("tokens") or {}).get(name), (s.get("token_columns") or {}).get(name))
            for name in TOKENS
        ],
    }


@requires("view_campaigns")
def campaign_detail(request, slug: str):
    return _page(request, get_object_or_404(Campaign, slug=slug))


# HTMX swaps a 286 answer in, then stops polling: the jobs have finished.
STOP_POLLING = 286


@requires("view_campaigns")
def campaign_live(request, slug: str):
    """The live part alone, for HTMX to poll while a job is active."""
    campaign = get_object_or_404(Campaign, slug=slug)
    context = _live(request, campaign)
    return render(request, "campaigns/_live.html", context,
                  status=200 if context["active"] else STOP_POLLING)


@requires("edit_campaigns")
@require_POST
def campaign_check(request, slug: str):
    """Check again: the page reads the segment afresh, as on every view."""
    return _page(request, get_object_or_404(Campaign, slug=slug))


@requires("run_campaigns")
@require_POST
def campaign_preview(request, slug: str):
    """Each recipient's message, or one number's (the CLI's preview). A POST,
    so the number searched for never appears in a URL or an access log."""
    campaign = get_object_or_404(Campaign, slug=slug)
    params = _preview_params(request.POST)
    if request.headers.get("HX-Request"):
        return render(request, "campaigns/stage/_recipients_preview.html",
                      {"campaign": campaign, **_preview_context(request, campaign, **params)})
    return _page(request, campaign, preview=params)


def _job(campaign: Campaign, request, kinds: tuple[str, ...]) -> Job:
    pk = request.POST.get("job", "")
    if not pk.isdigit():
        raise Http404
    return get_object_or_404(Job, pk=pk, campaign=campaign, kind__in=kinds)


def _back(request, slug: str):
    """Where an action returns: the campaign page, or the composer it came
    from (only that campaign's own, on this site)."""
    target = request.POST.get("next", "")
    if target.startswith(f"/compose/c/{slug}/") and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return redirect(target)
    return redirect("campaign_detail", slug=slug)


@require_POST
def campaign_action(request, slug: str, action: str):
    capability = _ACTIONS.get(action)
    if capability is None:
        raise Http404
    if not can(request.user, capability):
        return forbidden(request)
    campaign = get_object_or_404(Campaign, slug=slug)
    composing = bool(request.POST.get("next"))
    try:
        if action == "test":
            segment = Segment.objects.filter(slug=campaign.settings.get("segment")).first()
            services.request_test(campaign, request.user, parts=preview_of(campaign.settings, segment).parts or None)
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
            smoke_test = request.POST.get("smoke_test") == "on"
            at = None
            if request.POST.get("when") == "later":
                date, time = request.POST.get("date", "").strip(), request.POST.get("time", "").strip()
                at, problem = parse_when(date, time)
                if problem and composing:
                    messages.error(request, SCHEDULE_ERRORS[problem])
                    return _back(request, slug)
                if problem:
                    # Back to the form, with what was typed and the reason under it.
                    return _page(request, campaign, schedule_error=SCHEDULE_ERRORS[problem],
                                 schedule_bad_date=problem != "bad_time",
                                 schedule_bad_time=problem in ("bad_time", "past", "too_far"),
                                 schedule_date=date[:10], schedule_time=time[:5], schedule_smoke=smoke_test)
            services.start_send(campaign, request.user, at=at, smoke_test=smoke_test)
            if at is None:
                record("send_started", request=request, campaign=slug, smoke_test=smoke_test)
                messages.success(request, _("Sending is queued. Progress appears here."))
            else:
                record("send_scheduled", request=request, campaign=slug, at=at.isoformat(),
                       smoke_test=smoke_test)
                messages.success(request, _("Sending is scheduled for %(when)s.") % {"when": jalali(at)})
        elif action == "unschedule":
            services.unschedule(_job(campaign, request, (Job.Kind.SEND,)))
            record("send_unscheduled", request=request, campaign=slug)
            undo.offer(request, _("The schedule is cancelled. The campaign is ready to send, now or at another time."),
                       reverse("campaign_action", args=[slug, "reschedule"]))
        elif action == "reschedule":
            job = services.reschedule(campaign, request.user)
            record("send_scheduled", request=request, campaign=slug, at=job.not_before.isoformat(),
                   smoke_test=bool(job.params.get("smoke_test")), undone=True)
            messages.success(request, _("Sending is scheduled for %(when)s.") % {"when": jalali(job.not_before)})
        elif action == "start_now":
            job = services.start_now(_job(campaign, request, (Job.Kind.SEND,)))
            record("send_started", request=request, campaign=slug,
                   smoke_test=bool(job.params.get("smoke_test")), was_scheduled=True)
            messages.success(request, _("Sending starts now. Progress appears here."))
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
            params = _reconcile_params(request.POST) if action == "reconcile" else {}
            with transaction.atomic():
                services.enqueue(campaign, action, request.user, params=params)
            record("job_requested", request=request, campaign=slug, kind=action, **params)
            messages.success(request, _("Queued. The result appears in the list of jobs."))
    except services.JobConflict as e:
        messages.error(request, CONFLICTS.get(e.code, CONFLICTS["busy"]))
    return _back(request, slug)
