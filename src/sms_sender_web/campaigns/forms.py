"""Creating a campaign and choosing what it sends (spec 3, 4.5). Settings
are stored in the CLI's terms (`Campaign.settings`), so the engine runs
exactly what `sms-sender send` would."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from datetime import time as dtime

import jdatetime
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from sms_sender.input_loader import SLUG_RE
from sms_sender.links import STRATEGIES, allowed_domains, destination_issue, format_problem
from sms_sender.rate import parse_rate
from sms_sender.sender import TOKEN_MAX_SPACES, token_issue
from sms_sender.shortlink import shlink_base_url
from sms_sender.window import DEFAULT_WINDOW, TEHRAN, parse_window

from ..jobs.engine import campaign_db
from ..jobs.models import Campaign
from ..segments.models import Segment
from ..text import persian_text
from .terms import DESTINATION_ISSUES, TOKEN_ISSUES, fill

TOKENS = tuple(TOKEN_MAX_SPACES)  # token, token2, token3, token10, token20
SOURCES = [
    ("", _("Not used")),
    ("value", _("A fixed value")),
    ("column", _("A column of the segment")),
    ("link", _("The short link")),
]
_ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# The template's name at Kavenegar, as typed in its panel.
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _ready_segments():
    return Segment.objects.filter(status=Segment.Status.READY)


def more_segment_choices():
    """The segments a campaign can send to after its own, in the order a
    send reads them (by name)."""
    return _ready_segments().order_by("name", "slug")


def clean_template(value: str) -> str:
    value = value.strip()
    if not _TEMPLATE_RE.match(value):
        raise forms.ValidationError(
            _("Write the template's name exactly as in the Kavenegar panel: English letters, digits, “-”, “_” or “.”, no spaces.")
        )
    return value


def slug_taken(slug: str) -> bool:
    """A campaign has it, or a DB of that name exists (made by the CLI: it's
    another campaign's record)."""
    return slug == "new" or Campaign.objects.filter(slug=slug).exists() or campaign_db(Campaign(slug=slug)).exists()


def free_slug(base: str) -> str:
    """The next short name after `base` that nobody has: coin-price-7 →
    coin-price-8, vip → vip-2."""
    match = re.fullmatch(r"(.*?)-(\d+)", base)
    stem, n = (match.group(1), int(match.group(2))) if match else (base, 1)
    while True:
        n += 1
        candidate = f"{stem[:60 - len(str(n))]}-{n}"
        if SLUG_RE.match(candidate) and not slug_taken(candidate):
            return candidate


class _NamedForm(forms.Form):
    name = forms.CharField(max_length=200)
    slug = forms.CharField(max_length=64)

    def clean_name(self) -> str:
        return persian_text(self.cleaned_data["name"].strip())

    def clean_slug(self) -> str:
        slug = self.cleaned_data["slug"].strip()
        if not SLUG_RE.match(slug):
            raise forms.ValidationError(_("Use lowercase English letters, digits and dashes, e.g. vip-users."))
        if slug_taken(slug):
            raise forms.ValidationError(_("A campaign with this short name already exists. Choose another."))
        return slug


class NewCampaignForm(_NamedForm):
    segment = forms.ModelChoiceField(queryset=_ready_segments(), to_field_name="slug")
    template = forms.CharField(max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["segment"].queryset = _ready_segments()  # fresh on every form
        self.fields["segment"].error_messages["invalid_choice"] = _("Choose a segment whose columns are set.")

    def clean_template(self) -> str:
        return clean_template(self.cleaned_data["template"])


class DuplicateForm(_NamedForm):
    """A new campaign with another's settings: the dashboard's presets (the
    CLI's profiles). Tests, approvals and sends stay with the original."""


class SettingsForm(forms.Form):
    """Everything a send needs, for a campaign with a segment chosen."""

    segment = forms.ModelChoiceField(queryset=_ready_segments(), to_field_name="slug")
    # One send for several segments: these after the first, with one test SMS.
    more_segments = forms.MultipleChoiceField(required=False)
    template = forms.CharField(max_length=100)
    value_maps = forms.CharField(widget=forms.Textarea, required=False)
    link_destination = forms.CharField(max_length=2000, required=False)
    link_format = forms.CharField(max_length=100, required=False)
    link_strategy = forms.ChoiceField(choices=[(s, s) for s in STRATEGIES])
    link_expiry_days = forms.IntegerField(min_value=1, max_value=365)
    send_window = forms.CharField(max_length=20)
    rate = forms.CharField(max_length=20, required=False)
    workers = forms.IntegerField(min_value=1, max_value=20)
    # Tracking values added to the link (empty: sms, sms, the campaign, the segment).
    utm_source = forms.CharField(max_length=100, required=False)
    utm_medium = forms.CharField(max_length=100, required=False)
    utm_campaign = forms.CharField(max_length=100, required=False)
    utm_content = forms.CharField(max_length=100, required=False)
    # Advanced, for admins: how a run retries and waits (the CLI's flags).
    max_attempts = forms.IntegerField(min_value=1, max_value=10, required=False)
    timeout = forms.FloatField(min_value=5, max_value=120, required=False)
    backoff_max = forms.FloatField(min_value=1, max_value=300, required=False)
    link_rate = forms.CharField(max_length=20, required=False)

    def __init__(self, *args, columns: list[str], advanced: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.columns = columns
        self.advanced = advanced  # the advanced settings apply only from an admin
        self.fields["segment"].queryset = _ready_segments()
        self.fields["segment"].error_messages["invalid_choice"] = _("Choose a segment whose columns are set.")
        self.fields["more_segments"].choices = [(seg.slug, seg.name) for seg in more_segment_choices()]
        self.fields["more_segments"].error_messages["invalid_choice"] = _(
            "One of the chosen segments isn't ready any more. Choose again."
        )
        for name in TOKENS:
            self.fields[f"{name}_source"] = forms.ChoiceField(choices=SOURCES, required=False)
            self.fields[f"{name}_value"] = forms.CharField(max_length=200, required=False, strip=False)
            self.fields[f"{name}_column"] = forms.CharField(max_length=200, required=False)

    @classmethod
    def initial_from(cls, settings: dict) -> dict:
        links = settings.get("links") or {}
        initial = {
            "segment": settings.get("segment"),
            "more_segments": list(settings.get("more_segments") or []),
            "template": settings.get("template", ""),
            "value_maps": "\n".join(
                f"{column}:{source}={target}"
                for column, mapping in (settings.get("value_maps") or {}).items()
                for source, target in mapping.items()
            ),
            "link_destination": links.get("destination", ""),
            "link_format": links.get("format", "url"),
            "link_strategy": links.get("strategy", "recipient"),
            "link_expiry_days": links.get("expiry_days", 7),
            "send_window": settings.get("send_window", DEFAULT_WINDOW),
            "rate": settings.get("rate") or "",
            "workers": settings.get("workers", 5),
            "utm_source": links.get("utm_source") or "",
            "utm_medium": links.get("utm_medium") or "",
            "utm_campaign": links.get("utm_campaign") or "",
            "utm_content": links.get("utm_content") or "",
            "max_attempts": settings.get("max_attempts"),
            "timeout": settings.get("timeout"),
            "backoff_max": settings.get("backoff_max"),
            "link_rate": settings.get("link_rate") or "",
        }
        # The page's split controls for the combined settings (ui_fields).
        start, _sep, end = initial["send_window"].partition("-")
        initial.update(window_start=start, window_end=end)
        value, _sep, unit = initial["rate"].partition("/")
        initial.update(rate_value=value, rate_unit=unit or "s")
        fmt = initial["link_format"]
        initial.update(link_format_kind=fmt if fmt in ("url", "code") else "pattern",
                       link_pattern="" if fmt in ("url", "code") else fmt)
        initial["value_map_rows"] = [
            {"column": column, "source": source, "target": target}
            for column, mapping in (settings.get("value_maps") or {}).items()
            for source, target in mapping.items()
        ]
        for name in TOKENS:
            if name in (settings.get("tokens") or {}):
                initial[f"{name}_source"] = "value"
                initial[f"{name}_value"] = settings["tokens"][name]
            elif name in (settings.get("token_columns") or {}):
                initial[f"{name}_source"] = "column"
                initial[f"{name}_column"] = settings["token_columns"][name]
            elif links.get("token") == name:
                initial[f"{name}_source"] = "link"
        return initial

    def clean_template(self) -> str:
        return clean_template(self.cleaned_data["template"])

    def clean_send_window(self) -> str:
        value = self.cleaned_data["send_window"].strip()
        try:
            window = parse_window(value)
        except ValueError:
            window = None
        if window is None:  # "off" is for the CLI only (decided: prohibited hours apply)
            raise forms.ValidationError(_("Write the daily window as 08:00-21:00 (Tehran time)."))
        return f"{window.start:%H:%M}-{window.end:%H:%M}"

    def clean_rate(self) -> str:
        # Typed on a Persian keyboard, "۱۰/s" works; it's stored as "10/s".
        value = self.cleaned_data["rate"].strip().translate(_ASCII_DIGITS)
        if value:
            try:
                parse_rate(value)
            except ValueError as e:
                raise forms.ValidationError(_("Write the rate like 10/s, 600/m or 3600/h.")) from e
        return value

    def clean_link_rate(self) -> str:
        value = self.cleaned_data["link_rate"].strip().translate(_ASCII_DIGITS)
        if value:
            try:
                parse_rate(value)
            except ValueError as e:
                raise forms.ValidationError(_("Write the rate like 10/s, 600/m or 3600/h.")) from e
        return value

    def _clean_utm(self, name: str) -> str:
        return persian_text(self.cleaned_data[name].strip())

    def clean_utm_source(self) -> str:
        return self._clean_utm("utm_source")

    def clean_utm_medium(self) -> str:
        return self._clean_utm("utm_medium")

    def clean_utm_campaign(self) -> str:
        return self._clean_utm("utm_campaign")

    def clean_utm_content(self) -> str:
        return self._clean_utm("utm_content")

    def clean(self):
        data = super().clean()
        tokens, columns, link = {}, {}, None
        segment = data.get("segment")
        segment_columns = segment.token_columns if segment else self.columns
        for name in TOKENS:
            source = data.get(f"{name}_source") or ""
            if source == "value":
                value = persian_text(data.get(f"{name}_value") or "")
                if not value.strip():
                    self.add_error(f"{name}_value", _("Write the value, or choose “Not used”."))
                    continue
                issue = token_issue(name, value)
                if issue:
                    self.add_error(f"{name}_value", fill(TOKEN_ISSUES[issue[0]], issue[1]))
                    continue
                tokens[name] = value
            elif source == "column":
                column = data.get(f"{name}_column") or ""
                if column not in segment_columns:
                    self.add_error(f"{name}_column", _("Choose one of the segment's columns."))
                    continue
                columns[name] = column
            elif source == "link":
                if link is not None:
                    self.add_error(f"{name}_source", _("Only one token can carry the short link."))
                    continue
                link = name
        if "token" not in tokens and "token" not in columns and link != "token":
            # Kavenegar's lookup needs the first token in every request.
            self.add_error("token_source", _("Kavenegar needs the first token (token) in every SMS."))
        data["tokens"], data["token_columns"], data["link_token"] = tokens, columns, link
        data["more_segments"] = self._more_segments(data, set(columns.values()))
        data["value_maps_parsed"] = self._value_maps(data.get("value_maps") or "", set(columns.values()))
        if link is not None:
            self._check_link(data)
        return data

    def _more_segments(self, data: dict, needed: set[str]) -> list[str]:
        """The segments after the first, in the order they're listed: never
        the first one again, and each with every column the tokens use."""
        first = data.get("segment")
        chosen = [slug for slug in dict.fromkeys(data.get("more_segments") or []) if not first or slug != first.slug]
        by_slug = {seg.slug: seg for seg in more_segment_choices().filter(slug__in=chosen)}
        lacking = [by_slug[slug].name for slug in chosen if not needed <= set(by_slug[slug].token_columns or [])]
        if lacking:
            self.add_error("more_segments", _(
                "These segments don't have every column the tokens use: %(names)s."
            ) % {"names": "، ".join(lacking)})
        order = list(by_slug)  # more_segment_choices' order
        return sorted(chosen, key=order.index)

    def _value_maps(self, text: str, used: set[str]) -> dict[str, dict[str, str]]:
        maps: dict[str, dict[str, str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            column, sep, rest = line.partition(":")
            source, eq, target = rest.partition("=")
            column, source, target = column.strip(), source.strip(), persian_text(target.strip())
            if not sep or not eq or not column or not source or not target:
                self.add_error("value_maps", _("Write each translation as column:value=translation, e.g. side:Buy=خرید."))
                return {}
            if column not in used:
                self.add_error("value_maps", _("A translation's column isn't used by any token."))
                return {}
            maps.setdefault(column, {})[source] = target
        return maps

    def _check_link(self, data: dict) -> None:
        destination = (data.get("link_destination") or "").strip()
        if not destination:
            self.add_error("link_destination", _("Write the address the short link opens."))
        else:
            issue = destination_issue(destination, allowed_domains(), shlink_base_url())
            if issue:
                self.add_error("link_destination", fill(DESTINATION_ISSUES[issue[0]], issue[1]))
        data["link_destination"] = destination
        fmt = (data.get("link_format") or "url").strip()
        if format_problem(fmt):
            self.add_error("link_format", _("Choose url or code, or write a pattern with {code} once and no spaces, e.g. u/{code}."))
        data["link_format"] = fmt

    def settings(self, campaign: Campaign) -> dict:
        """The campaign's new settings, in the CLI's terms."""
        d = self.cleaned_data
        segment = d["segment"]
        settings = {
            **{k: v for k, v in (campaign.settings or {}).items() if k not in ("links",)},
            "segment": segment.slug,
            "input": str(segment.path),
            "user_id_column": segment.user_id_column or None,
            "template": d["template"],
            "tokens": d["tokens"],
            "token_columns": d["token_columns"],
            "value_maps": d["value_maps_parsed"],
            "send_window": d["send_window"],
            "rate": d["rate"] or None,
            "workers": d["workers"],
        }
        if d["more_segments"]:
            settings["more_segments"] = d["more_segments"]
        else:
            settings.pop("more_segments", None)
        if d["link_token"]:
            settings["links"] = {
                "destination": d["link_destination"], "token": d["link_token"],
                "format": d["link_format"], "strategy": d["link_strategy"],
                "expiry_days": d["link_expiry_days"],
                "utm_source": d.get("utm_source") or "sms", "utm_medium": d.get("utm_medium") or "sms",
                "utm_campaign": d.get("utm_campaign") or None, "utm_content": d.get("utm_content") or None,
            }
        if self.advanced:
            for key in ("max_attempts", "timeout", "backoff_max", "link_rate"):
                if d.get(key) in (None, ""):
                    settings.pop(key, None)  # the engine's default
                else:
                    settings[key] = d[key]
        return settings


def combined(data):
    """The settings page's split controls, back into the form's fields: the
    window's two times, the rate's number and unit, the link format's
    choice and pattern, and the translation rows. A post that already has
    the combined fields (the CLI-shaped form) passes through unchanged."""
    data = data.copy()
    if "window_start" in data or "window_end" in data:
        start = data.get("window_start", "").strip().translate(_ASCII_DIGITS)
        end = data.get("window_end", "").strip().translate(_ASCII_DIGITS)
        data["send_window"] = f"{start}-{end}"
    if "rate_value" in data:
        value = data.get("rate_value", "").strip().translate(_ASCII_DIGITS)
        data["rate"] = f"{value}/{data.get('rate_unit') or 's'}" if value else ""
    if "link_format_kind" in data:
        kind = data.get("link_format_kind")
        data["link_format"] = kind if kind in ("url", "code") else data.get("link_pattern", "").strip()
    if "vm_column" in data:
        rows = zip(data.getlist("vm_column"), data.getlist("vm_source"), data.getlist("vm_target"))
        data["value_maps"] = "\n".join(
            f"{column}:{source}={target}" for column, source, target in rows
            if column.strip() or source.strip() or target.strip()
        )
    return data


# A send can be set this far ahead at most: further is almost surely a typo.
# "too_far" below says the number.
SCHEDULE_MAX_DAYS = 30

SCHEDULE_ERRORS = {
    "bad_date": _("Write the date as year/month/day in the Solar Hijri calendar, e.g. 1405/07/20."),
    "gregorian": _("That looks like a Gregorian date. Write it in the Solar Hijri calendar, e.g. 1405/07/20."),
    "bad_time": _("Write the time as hours:minutes, e.g. 09:30."),
    "past": _("That time has passed. Choose a later one."),
    "too_far": _("That's more than 30 days away. Choose a nearer time."),
}


def parse_when(date_text: str, time_text: str, now: datetime | None = None):
    """(an aware datetime, None) for a Solar Hijri date and a time in Tehran
    time, or (None, error key). Persian or Latin digits; "/", "-" or "."
    between the date's parts."""
    parts = re.split(r"[/\-.]", (date_text or "").strip().translate(_ASCII_DIGITS))
    try:
        year, month, day = (int(p) for p in parts)
    except ValueError:
        return None, "bad_date"
    if year >= 1900:  # this century's Solar Hijri years are 13xx and 14xx
        return None, "gregorian"
    try:
        day_g = jdatetime.date(year, month, day).togregorian()
    except ValueError:
        return None, "bad_date"
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (time_text or "").strip().translate(_ASCII_DIGITS))
    try:
        at_time = dtime(int(match.group(1)), int(match.group(2))) if match else None
    except ValueError:
        at_time = None
    if at_time is None:
        return None, "bad_time"
    at = datetime.combine(day_g, at_time, tzinfo=TEHRAN)
    now = now or timezone.now()
    if at <= now + timedelta(minutes=1):
        return None, "past"
    if at > now + timedelta(days=SCHEDULE_MAX_DAYS):
        return None, "too_far"
    return at, None
