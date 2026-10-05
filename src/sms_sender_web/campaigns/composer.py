"""The two-minute alert (plan 06, L3): a new alert from a preset on one page.

1. The preset: its template, what fills each token, its segments.
2. Today's values: one field per token the preset fills with a value, the
   last ones filled in, Kavenegar's rules checked as you type.
3. Segments: several, the total counting a number once, with who the
   suppression list and the frequency cap hold back.
4. A live preview of the SMS: its characters, parts and cost.
5. The test SMS, its approval, then sending now or at a set time.

An alert is a campaign of its own, made when its test SMS is asked for. From
then on its page shows the campaign's own steps (the same templates and
actions as the campaign page), so every gate stays: the check, the test SMS
approved for these exact settings, the window, restricted sending, nobody
twice. Values and segments can change until sending starts; a change needs
a new test SMS, as anywhere."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import jdatetime
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from sms_sender.sender import TOKEN_MAX_SPACES, token_issue
from sms_sender.window import DEFAULT_WINDOW, TEHRAN, now_tehran, parse_window

from ..accounts.decorators import requires
from ..accounts.models import test_phone_of
from ..audit.record import record
from ..dashboard.templatetags.fa import fa_digits
from ..jobs import services
from ..jobs.models import Campaign
from ..segments.models import Segment
from ..text import persian_text
from .checks import check_settings
from .forms import slug_taken
from .lifecycle import DRAFT, READY
from .models import Preset
from .present import say
from .preview import load_segment, preview_of
from .terms import CHECK_PROBLEMS, CONFLICTS, COST_EACH, COUNTS, LACKS_COLUMNS, TEST_HINT, TOKEN_ISSUES, fill
from .views import STOP_POLLING, _live

TOKENS = tuple(TOKEN_MAX_SPACES)
DEFAULT_PATTERN = "{name} · {date}"


# ---------- an alert being composed ----------

@dataclass
class Draft:
    values: dict[str, str]                  # token → today's value
    segments: list[str]                     # the chosen segments, in the order offered
    problems: dict[str, str] = field(default_factory=dict)  # token → what's wrong
    segment_problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and not self.segment_problem


def asked(settings: dict) -> list[str]:
    """The tokens each alert asks for: those the settings fill with a value
    (columns and the link fill themselves)."""
    tokens = settings.get("tokens") or {}
    return [name for name in TOKENS if name in tokens]


def suggested(preset: Preset) -> dict[str, str]:
    """The last alert's values, else the preset's own."""
    tokens = preset.settings.get("tokens") or {}
    return {name: preset.last_values.get(name, tokens[name]) for name in asked(preset.settings)}


def segment_options(settings: dict) -> list[dict]:
    """Every ready segment, by name, with its size. One without a column the
    tokens read can't be chosen, and says why."""
    needed = set((settings.get("token_columns") or {}).values())
    options = []
    for seg in Segment.objects.filter(status=Segment.Status.READY).order_by("name", "slug"):
        if not seg.path.exists():
            continue
        missing = sorted(needed - set(seg.token_columns or []))
        options.append({"segment": seg, "usable": not missing,
                        "missing": say(LACKS_COLUMNS, {"columns": ", ".join(missing)}) if missing else "",
                        "size": (seg.summary or {}).get("valid", 0)})
    return options


def default_segments(settings: dict, options: list[dict]) -> list[str]:
    wanted = {settings.get("segment"), *(settings.get("more_segments") or [])}
    return [o["segment"].slug for o in options if o["usable"] and o["segment"].slug in wanted]


def read_draft(data, settings: dict, options: list[dict]) -> Draft:
    values, problems = {}, {}
    for name in asked(settings):
        value = persian_text(data.get(f"value_{name}", "")).strip()
        values[name] = value
        if not value:
            problems[name] = _("Write today's value.")
            continue
        issue = token_issue(name, value)
        if issue:
            problems[name] = fill(TOKEN_ISSUES[issue[0]], issue[1])
    chosen = set(data.getlist("segments"))
    segments = [o["segment"].slug for o in options if o["usable"] and o["segment"].slug in chosen]
    draft = Draft(values, segments, problems)
    if not segments:
        draft.segment_problem = _("Choose at least one segment.")
    return draft


def draft_settings(base: dict, draft: Draft) -> dict:
    """`base` (the preset's settings, or the alert's own) with today's
    values and segments: the first chosen is the send's segment, the rest
    go in the same send."""
    settings = copy.deepcopy(base)
    settings["tokens"] = {**(settings.get("tokens") or {}), **draft.values}
    by_slug = {seg.slug: seg for seg in Segment.objects.filter(slug__in=draft.segments)}
    if draft.segments:
        first = by_slug[draft.segments[0]]
        settings.update(segment=first.slug, input=str(first.path), user_id_column=first.user_id_column or None)
        if len(draft.segments) > 1:
            settings["more_segments"] = draft.segments[1:]
        else:
            settings.pop("more_segments", None)
    return settings


def _today():
    return jdatetime.date.fromgregorian(date=timezone.now().astimezone(TEHRAN).date())


def alert_name(preset: Preset) -> str:
    day = _today()
    date = fa_digits(f"{day.year}/{day.month:02d}/{day.day:02d}")
    return (preset.name_pattern or DEFAULT_PATTERN).format(name=preset.name, date=date)[:200]


def alert_slug(preset: Preset) -> str:
    """The preset's short name and today's date (coin-price-1405-07-14);
    a second alert the same day gets -2, then -3."""
    day = _today()
    base = f"{preset.slug}-{day.year}-{day.month:02d}-{day.day:02d}"
    candidate, n = base, 1
    while slug_taken(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# ---------- what the page shows ----------

def _fields(settings: dict, preset: Preset, draft: Draft) -> list[dict]:
    """One entry per token the template can use: a field for each value,
    and what fills the others (a column, the short link)."""
    columns = settings.get("token_columns") or {}
    link = (settings.get("links") or {}).get("token")
    out = []
    for name in TOKENS:
        entry = {"name": name, "label": preset.labels.get(name, "")}
        if name in draft.values:
            out.append({**entry, "kind": "value", "value": draft.values[name], "problem": draft.problems.get(name, ""),
                        "max_spaces": TOKEN_MAX_SPACES[name]})
        elif name in columns:
            out.append({**entry, "kind": "column", "column": columns[name]})
        elif name == link:
            out.append({**entry, "kind": "link"})
    return out


def _window(settings: dict) -> dict:
    window = parse_window(settings.get("send_window") or DEFAULT_WINDOW)
    return {"window": window, "open": window is None or window.contains(now_tehran())}


def _message(settings: dict, loaded=None) -> dict:
    """The SMS as the first recipient of the first segment gets it. Without
    the lists already read, only the first is read, so it keeps up with
    typing."""
    segment = Segment.objects.filter(slug=settings.get("segment")).first()
    if loaded is None:
        settings = {k: v for k, v in settings.items() if k != "more_segments"}
    message = preview_of(settings, segment, loaded)
    return {"message": message, "message_cost": say(COST_EACH, {"cost": message.cost}) if message.cost else ""}


def _counts(settings: dict, campaign: Campaign | None = None, loaded=None) -> dict:
    """Who gets it: every chosen list read as a send would."""
    if not settings.get("segment"):
        return {"check": None}
    segment = Segment.objects.filter(slug=settings.get("segment")).first()
    loaded = loaded if loaded is not None else load_segment(settings, segment)
    check = check_settings(settings, campaign=campaign, loaded=loaded)
    message = preview_of(settings, segment, loaded)
    total = message.cost * check.to_send if message.cost and check.to_send else None
    return {
        "check": check,
        "check_problems": [CHECK_PROBLEMS[key] for key in check.problems],
        "count_lines": count_lines(check, total),
    }


def count_lines(check, total_cost: int | None) -> list[tuple[str, str]]:
    """Who it reaches, a line each, with its tone: the main one, then what
    holds people back."""
    lines = [(say(COUNTS["to_send"], {"n": check.to_send}), "main"),
             (say(COUNTS["lists"], {"lists": len(check.segments), "n": check.valid}), "")]
    for key, n, tone in (("repeated", check.duplicates, ""), ("suppressed", check.suppressed, ""),
                         ("already", check.already_sent, ""), ("not_allowed", check.not_allowed or 0, "warn"),
                         ("invalid", check.invalid_total, "")):
        if n:
            lines.append((say(COUNTS[key], {"n": n}), tone))
    if check.cap and check.capped:
        lines.append((say(COUNTS["capped"], {"n": check.capped, "cap": check.cap}), ""))
    if total_cost:
        lines.append((say(COUNTS["cost"], {"total": total_cost}), ""))
    return lines


def _page(request, preset: Preset, settings: dict, draft: Draft, options: list[dict], *,
          campaign: Campaign | None = None, errors: list[str] | None = None, status: int = 200):
    chosen = set(draft.segments)
    segment = Segment.objects.filter(slug=settings.get("segment")).first()
    loaded = load_segment(settings, segment)  # read once for the message and the counts
    context = {
        "preset": preset, "campaign": campaign, "draft": draft,
        "fields": _fields(settings, preset, draft),
        "options": [{**o, "chosen": o["segment"].slug in chosen} for o in options],
        "errors": errors or [],
        **_test_context(request, settings),
        "form_action": reverse("compose_campaign", args=[campaign.slug]) if campaign
        else reverse("compose", args=[preset.slug]),
        "preview_url": reverse("compose_campaign_preview", args=[campaign.slug]) if campaign
        else reverse("compose_preview", args=[preset.slug]),
        **_message(settings, loaded), **_counts(settings, campaign, loaded),
    }
    if campaign is not None:
        context.update(_panel(request, campaign))
    return render(request, "campaigns/compose/page.html", context, status=status)


def _test_context(request, settings: dict) -> dict:
    """What the test button says: where the SMS goes, and whether the
    sending window lets it go now."""
    phone = test_phone_of(request.user)
    return {
        "test_phone": phone, **_window(settings),
        "test_hint": say(TEST_HINT, {"phone": phone}) if phone else "",
    }


def _panel(request, campaign: Campaign) -> dict:
    """The right-hand step: the campaign's own (test SMS, approval, send,
    progress), or the composer's test button before its first test."""
    live = {**_live(request, campaign, ready_step=False), **_test_context(request, campaign.settings)}
    live["next_url"] = reverse("compose_campaign", args=[campaign.slug])
    stage = live["life"].stage
    # Before a test (or after a changed value): the composer's own button.
    live["needs_test"] = stage in (DRAFT, READY)
    live["locked"] = live["settings_locked"]
    return live


# ---------- the pages ----------

@requires("run_campaigns")
def compose_start(request):
    """Pick a preset; the one used last comes first, with its values."""
    presets = list(Preset.objects.filter(archived_at__isnull=True))
    last = (Campaign.objects.filter(preset__in=presets).select_related("preset").order_by("-created_at").first())
    if last is not None:
        presets.sort(key=lambda p: p.pk != last.preset_id)
    return render(request, "campaigns/compose/start.html", {
        "presets": [{"preset": p, "last": last is not None and p.pk == last.preset_id,
                     "values": "، ".join(suggested(p).values())} for p in presets],
    })


@requires("run_campaigns")
@require_http_methods(["GET", "POST"])
def compose(request, slug: str):
    """A new alert from the preset. Nothing is saved until the test SMS is
    asked for: then the alert (a campaign) is made, and its page takes over."""
    preset = get_object_or_404(Preset, slug=slug, archived_at__isnull=True)
    options = segment_options(preset.settings)
    if request.method != "POST":
        draft = Draft(suggested(preset), default_segments(preset.settings, options))
        return _page(request, preset, draft_settings(preset.settings, draft), draft, options)

    draft = read_draft(request.POST, preset.settings, options)
    settings = draft_settings(preset.settings, draft)
    errors = _refusals(request)
    if errors or not draft.ok:
        return _page(request, preset, settings, draft, options, errors=errors, status=400)
    check = check_settings(settings)
    if check.problems:
        return _page(request, preset, settings, draft, options,
                     errors=[CHECK_PROBLEMS[key] for key in check.problems], status=400)
    campaign = Campaign.objects.create(
        slug=alert_slug(preset), name=alert_name(preset), settings=settings, preset=preset,
        created_by=request.user,
    )
    record("campaign_created", request=request, campaign=campaign.slug, preset=preset.slug)
    Preset.objects.filter(pk=preset.pk).update(last_values=draft.values)  # suggested next time
    _request_test(request, campaign)
    return redirect(reverse("compose_campaign", args=[campaign.slug]))


def _refusals(request) -> list[str]:
    """What stops a test SMS before anything is made."""
    if not test_phone_of(request.user):
        return [CONFLICTS["no_test_number"]]
    if services.held():
        return [CONFLICTS["held"]]
    return []


def _request_test(request, campaign: Campaign) -> None:
    segment = Segment.objects.filter(slug=campaign.settings.get("segment")).first()
    try:
        services.request_test(campaign, request.user, parts=preview_of(campaign.settings, segment).parts or None)
    except services.JobConflict as e:
        messages.error(request, CONFLICTS.get(e.code, CONFLICTS["busy"]))
        return
    record("test_requested", request=request, campaign=campaign.slug)


@requires("run_campaigns")
@require_http_methods(["GET", "POST"])
def compose_campaign(request, slug: str):
    """An alert after its first test SMS: the same page, with the campaign's
    own steps on the right. Values and segments change until sending starts;
    a change needs a new test SMS (its approval covers exact settings)."""
    campaign = get_object_or_404(Campaign.objects.select_related("preset"), slug=slug)
    preset = campaign.preset
    if preset is None:
        return redirect("campaign_detail", slug=slug)
    options = segment_options(campaign.settings)
    if request.method != "POST":
        draft = Draft({name: (campaign.settings.get("tokens") or {})[name] for name in asked(campaign.settings)},
                      [s for s in [campaign.settings.get("segment"), *(campaign.settings.get("more_segments") or [])]
                       if s])
        return _page(request, preset, campaign.settings, draft, options, campaign=campaign)

    if services.settings_locked(campaign, request.user):
        messages.error(request, _("Sending has started, so the values and segments stay as they are."))
        return redirect("compose_campaign", slug=slug)
    draft = read_draft(request.POST, campaign.settings, options)
    settings = draft_settings(campaign.settings, draft)
    errors = _refusals(request) if request.POST.get("action") == "test" else []
    if errors or not draft.ok:
        return _page(request, preset, settings, draft, options, campaign=campaign, errors=errors, status=400)
    changed = settings != campaign.settings
    if changed:
        check = check_settings(settings, campaign=campaign)
        if check.problems:
            return _page(request, preset, settings, draft, options, campaign=campaign,
                         errors=[CHECK_PROBLEMS[key] for key in check.problems], status=400)
        changed = sorted(k for k in set(settings) | set(campaign.settings) if settings.get(k) != campaign.settings.get(k))
        campaign.settings = settings
        campaign.save(update_fields=["settings"])
        record("campaign_changed", request=request, campaign=slug, changed=changed)
        Preset.objects.filter(pk=preset.pk).update(last_values=draft.values)
    if request.POST.get("action") == "test":
        _request_test(request, campaign)
    elif changed:
        messages.success(request, _("Saved. Send a new test SMS: its approval covers these exact values."))
    return redirect("compose_campaign", slug=slug)


@requires("run_campaigns")
@require_POST
def compose_preview(request, slug: str):
    """The message, or who gets it, for the unsaved form (`part`). Saves nothing."""
    preset = get_object_or_404(Preset, slug=slug)
    return _preview(request, preset, preset.settings, None)


@requires("run_campaigns")
@require_POST
def compose_campaign_preview(request, slug: str):
    campaign = get_object_or_404(Campaign.objects.select_related("preset"), slug=slug)
    if campaign.preset is None:
        return redirect("campaign_detail", slug=slug)
    return _preview(request, campaign.preset, campaign.settings, campaign)


def _preview(request, preset: Preset, base: dict, campaign: Campaign | None):
    options = segment_options(base)
    draft = read_draft(request.POST, base, options)
    settings = draft_settings(base, draft)
    context = {"preset": preset, "draft": draft, "fields": _fields(settings, preset, draft), "oob": True}
    if request.GET.get("part") == "counts":
        return render(request, "campaigns/compose/_counts.html", {**context, **_counts(settings, campaign)})
    return render(request, "campaigns/compose/_message.html", {**context, **_message(settings)})


@requires("run_campaigns")
@require_GET
def compose_panel(request, slug: str):
    """The right-hand step alone, for HTMX to poll while a job is active."""
    campaign = get_object_or_404(Campaign, slug=slug)
    context = _panel(request, campaign)
    return render(request, "campaigns/compose/_panel.html", {"campaign": campaign, **context},
                  status=200 if context["active"] else STOP_POLLING)
