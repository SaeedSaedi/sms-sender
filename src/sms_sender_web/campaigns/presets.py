"""Presets (plan 06, L3): kinds of message sent again and again, such as a
daily price alert. A preset keeps the template, what fills each token, the
default segments and the sending settings; the composer (composer.py) makes
a new alert from it with only today's values. Each alert is a campaign of
its own, so every gate (the check, the test SMS and its approval for these
exact settings, the window, nobody twice) stays the same.

A preset is made from a campaign that has its settings, or from nothing on
the same settings page campaigns use."""
from __future__ import annotations

import copy
import re
from types import SimpleNamespace

from django import forms
from django.contrib import messages
from django.db.models import Count, Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from sms_sender.input_loader import SLUG_RE
from sms_sender.sender import TOKEN_MAX_SPACES

from .. import live, undo
from ..accounts.decorators import requires
from ..accounts.roles import can
from ..audit.record import record
from ..dashboard.templatetags.fa import jalali
from ..jobs.models import Campaign
from ..segments.models import Segment
from ..system.models import SystemSettings
from ..text import persian_text
from .forms import SettingsForm, combined
from .lifecycle import settings_complete
from .models import Preset
from .present import say
from .terms import SERIES, SERIES_LAST
from .views import settings_page_context, settings_preview_response

TOKENS = tuple(TOKEN_MAX_SPACES)
SLUG_MAX = 40  # an alert's short name adds the date: coin-price-1405-07-14
# Taken by the pages' own addresses (/presets/new/, /compose/c/…).
RESERVED = ("new", "from-campaign", "c")


class PresetFields(forms.Form):
    """What a preset has besides a campaign's settings: its names, and a
    label for each token typed per alert."""

    name = forms.CharField(max_length=120)
    slug = forms.CharField(max_length=SLUG_MAX, required=False)
    name_pattern = forms.CharField(max_length=160, required=False)

    def __init__(self, *args, creating: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.creating = creating
        for name in TOKENS:
            self.fields[f"{name}_label"] = forms.CharField(max_length=60, required=False)

    def clean_name(self) -> str:
        return persian_text(self.cleaned_data["name"].strip())

    def clean_slug(self) -> str:
        if not self.creating:
            return ""
        slug = self.cleaned_data["slug"].strip()
        if not SLUG_RE.match(slug):
            raise forms.ValidationError(_("Use lowercase English letters, digits and dashes, e.g. coin-price."))
        if slug in RESERVED or Preset.objects.filter(slug=slug).exists():
            raise forms.ValidationError(_("A preset with this short name already exists. Choose another."))
        return slug

    def clean_name_pattern(self) -> str:
        pattern = persian_text(self.cleaned_data["name_pattern"].strip())
        try:
            pattern.format(name="", date="")
        except (KeyError, IndexError, ValueError) as e:
            raise forms.ValidationError(_("Use only {name} and {date} in braces.")) from e
        return pattern

    def labels(self) -> dict[str, str]:
        return {
            name: persian_text(self.cleaned_data[f"{name}_label"].strip())
            for name in TOKENS if self.cleaned_data.get(f"{name}_label", "").strip()
        }


def free_preset_slug(base: str) -> str:
    """`base` without a campaign's numbering (coin-price-7 → coin-price),
    then the first one free: coin-price, coin-price-2, …"""
    stem = re.sub(r"(-\d+)+$", "", base)[:SLUG_MAX].strip("-") or "preset"
    candidate, n = stem, 1
    while candidate in RESERVED or Preset.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{stem[:SLUG_MAX - len(str(n)) - 1]}-{n}"
    return candidate


def _defaults() -> dict:
    """A new preset's settings: the defaults new campaigns get."""
    system = SystemSettings.load()
    return {
        "tokens": {}, "token_columns": {}, "value_maps": {},
        "send_window": system.default_send_window or "08:00-21:00",
        "rate": system.default_rate or None, "workers": 5,
    }


@requires("view_campaigns")
def preset_list(request):
    presets = list(Preset.objects.annotate(alerts=Count("campaigns"), last_alert=Max("campaigns__created_at")))
    for p in presets:
        p.series = (say(SERIES_LAST, {"n": p.alerts, "date": jalali(p.last_alert)}) if p.last_alert
                    else say(SERIES, {"n": p.alerts}))
    return render(request, "campaigns/presets/list.html", {
        "presets": [p for p in presets if p.archived_at is None],
        "archived": [p for p in presets if p.archived_at is not None],
        # Campaigns that could become one: their settings are complete.
        "candidates": [c for c in Campaign.objects.filter(preset__isnull=True).order_by("-created_at")[:50]
                       if settings_complete(c.settings)],
    })


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def preset_new(request):
    return _edit(request, None)


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def preset_edit(request, slug: str):
    return _edit(request, get_object_or_404(Preset, slug=slug))


def _edit(request, preset: Preset | None):
    """The campaign settings page, for a preset: its message, tokens, link,
    default segments and sending settings, plus its names and labels."""
    base = preset.settings if preset else _defaults()
    posting = request.method == "POST"
    data = combined(request.POST) if posting else None
    segment = Segment.objects.filter(slug=(data or base).get("segment")).first()
    columns = segment.token_columns if segment else []
    initial = SettingsForm.initial_from(base)
    form = SettingsForm(data, columns=columns, initial=initial, advanced=can(request.user, "manage_settings"))
    fields = PresetFields(request.POST if posting else None, creating=preset is None, initial={
        "name": preset.name if preset else "", "slug": "", "name_pattern": preset.name_pattern if preset else "",
        **{f"{name}_label": (preset.labels if preset else {}).get(name, "") for name in TOKENS},
    })
    if live.validating(request):  # as someone types: the errors, nothing saved
        return live.errors(form, fields)
    if posting and form.is_valid() & fields.is_valid():  # both, so each shows its errors
        target = preset or Preset(slug=fields.cleaned_data["slug"], created_by=request.user)
        target.name = fields.cleaned_data["name"]
        target.name_pattern = fields.cleaned_data["name_pattern"]
        target.labels = fields.labels()
        target.settings = form.settings(SimpleNamespace(settings=base))
        target.save()
        record("preset_changed" if preset else "preset_created", request=request, preset=target.slug)
        messages.success(request, _("Saved. New alerts from this preset use it; alerts already made keep their own."))
        return redirect("preset_list")
    ui = request.POST if posting else initial
    rows = (
        [{"column": c, "source": f, "target": t} for c, f, t in zip(
            request.POST.getlist("vm_column"), request.POST.getlist("vm_source"), request.POST.getlist("vm_target"))]
        if posting else initial["value_map_rows"]
    )
    title = _("Preset") if preset else _("New preset")
    return render(request, "campaigns/settings.html", {
        **settings_page_context(request, form, base, segment, columns, ui, rows),
        "preset": preset, "preset_fields": fields, "extra_errors": bool(fields.errors),
        "extra_fields": "campaigns/presets/_fields.html",
        "page_title": title, "subject_name": preset.name if preset else "",
        "crumbs": [(reverse("preset_list"), _("Presets"))],
        "back_url": reverse("preset_list"), "back_label": _("Back to the presets"),
        "preview_url": reverse("preset_settings_preview", args=[preset.slug]) if preset
        else reverse("preset_new_preview"),
        "save_note": _("New alerts from this preset use these settings; alerts already made keep their own."),
    })


@requires("edit_campaigns")
@require_POST
def preset_settings_preview(request, slug: str | None = None):
    base = get_object_or_404(Preset, slug=slug).settings if slug else _defaults()
    return settings_preview_response(request, base)


@requires("edit_campaigns")
@require_POST
def preset_from_campaign(request):
    """A campaign's settings become a preset (its tokens' values the last
    used), and the campaign the first of its series."""
    campaign = get_object_or_404(Campaign, slug=request.POST.get("campaign", ""))
    if not settings_complete(campaign.settings):
        messages.error(request, _("This campaign's settings aren't complete yet: a segment, a template and the first token."))
        return redirect("campaign_detail", slug=campaign.slug)
    settings = copy.deepcopy(campaign.settings)
    preset = Preset.objects.create(
        slug=free_preset_slug(campaign.slug), name=campaign.name, settings=settings,
        last_values=dict(settings.get("tokens") or {}), created_by=request.user,
    )
    if campaign.preset_id is None:
        Campaign.objects.filter(pk=campaign.pk).update(preset=preset)
    record("preset_created", request=request, preset=preset.slug, campaign=campaign.slug)
    messages.success(request, _("Saved as a preset. Name it, and label the values each alert asks for."))
    return redirect("preset_edit", slug=preset.slug)


@requires("edit_campaigns")
@require_POST
def preset_archive(request, slug: str):
    """Put a preset away (no new alerts from it), or bring it back. Its
    series stays either way."""
    preset = get_object_or_404(Preset, slug=slug)
    restoring = preset.archived_at is not None
    preset.archived_at = None if restoring else timezone.now()
    preset.save(update_fields=["archived_at", "updated_at"])
    record("preset_restored" if restoring else "preset_archived", request=request, preset=slug)
    if restoring:
        messages.success(request, _("The preset is back."))
    else:  # safe to take back: offer it
        undo.offer(request, _("The preset is put away. Its alerts stay."), reverse("preset_archive", args=[slug]))
    return redirect("preset_list")
