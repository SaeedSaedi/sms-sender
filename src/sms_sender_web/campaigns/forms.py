"""Creating a campaign and choosing what it sends (spec 3, 4.5). Settings
are stored in the CLI's terms (`Campaign.settings`), so the engine runs
exactly what `sms-sender send` would."""
from __future__ import annotations

import re

from django import forms
from django.utils.translation import gettext_lazy as _

from sms_sender.input_loader import SLUG_RE
from sms_sender.links import STRATEGIES, allowed_domains, destination_issue, format_problem
from sms_sender.rate import parse_rate
from sms_sender.sender import TOKEN_MAX_SPACES, token_issue
from sms_sender.shortlink import shlink_base_url
from sms_sender.window import DEFAULT_WINDOW, parse_window

from ..jobs.engine import campaign_db
from ..jobs.models import Campaign
from ..segments.models import Segment
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


def clean_template(value: str) -> str:
    value = value.strip()
    if not _TEMPLATE_RE.match(value):
        raise forms.ValidationError(
            _("Write the template's name exactly as in the Kavenegar panel: English letters, digits, “-”, “_” or “.”, no spaces.")
        )
    return value


class NewCampaignForm(forms.Form):
    name = forms.CharField(max_length=200)
    slug = forms.CharField(max_length=64)
    segment = forms.ModelChoiceField(queryset=_ready_segments(), to_field_name="slug")
    template = forms.CharField(max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["segment"].queryset = _ready_segments()  # fresh on every form
        self.fields["segment"].error_messages["invalid_choice"] = _("Choose a segment whose columns are set.")

    def clean_slug(self) -> str:
        slug = self.cleaned_data["slug"].strip()
        if not SLUG_RE.match(slug):
            raise forms.ValidationError(_("Use lowercase English letters, digits and dashes, e.g. vip-users."))
        if slug == "new" or Campaign.objects.filter(slug=slug).exists():
            raise forms.ValidationError(_("A campaign with this short name already exists. Choose another."))
        # A DB of that name was made by the CLI: it's another campaign's record.
        if campaign_db(Campaign(slug=slug)).exists():
            raise forms.ValidationError(_("A campaign with this short name already exists. Choose another."))
        return slug

    def clean_template(self) -> str:
        return clean_template(self.cleaned_data["template"])


class SettingsForm(forms.Form):
    """Everything a send needs, for a campaign with a segment chosen."""

    segment = forms.ModelChoiceField(queryset=_ready_segments(), to_field_name="slug")
    template = forms.CharField(max_length=100)
    value_maps = forms.CharField(widget=forms.Textarea, required=False)
    link_destination = forms.CharField(max_length=2000, required=False)
    link_format = forms.CharField(max_length=100, required=False)
    link_strategy = forms.ChoiceField(choices=[(s, s) for s in STRATEGIES])
    link_expiry_days = forms.IntegerField(min_value=1, max_value=365)
    send_window = forms.CharField(max_length=20)
    rate = forms.CharField(max_length=20, required=False)
    workers = forms.IntegerField(min_value=1, max_value=20)

    def __init__(self, *args, columns: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.columns = columns
        self.fields["segment"].queryset = _ready_segments()
        self.fields["segment"].error_messages["invalid_choice"] = _("Choose a segment whose columns are set.")
        for name in TOKENS:
            self.fields[f"{name}_source"] = forms.ChoiceField(choices=SOURCES, required=False)
            self.fields[f"{name}_value"] = forms.CharField(max_length=200, required=False, strip=False)
            self.fields[f"{name}_column"] = forms.CharField(max_length=200, required=False)

    @classmethod
    def initial_from(cls, settings: dict) -> dict:
        links = settings.get("links") or {}
        initial = {
            "segment": settings.get("segment"),
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
        }
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

    def clean(self):
        data = super().clean()
        tokens, columns, link = {}, {}, None
        segment = data.get("segment")
        segment_columns = segment.token_columns if segment else self.columns
        for name in TOKENS:
            source = data.get(f"{name}_source") or ""
            if source == "value":
                value = data.get(f"{name}_value") or ""
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
        data["value_maps_parsed"] = self._value_maps(data.get("value_maps") or "", set(columns.values()))
        if link is not None:
            self._check_link(data)
        return data

    def _value_maps(self, text: str, used: set[str]) -> dict[str, dict[str, str]]:
        maps: dict[str, dict[str, str]] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            column, sep, rest = line.partition(":")
            source, eq, target = rest.partition("=")
            column, source, target = column.strip(), source.strip(), target.strip()
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
        if d["link_token"]:
            settings["links"] = {
                "destination": d["link_destination"], "token": d["link_token"],
                "format": d["link_format"], "strategy": d["link_strategy"],
                "expiry_days": d["link_expiry_days"],
                "utm_source": "sms", "utm_medium": "sms", "utm_campaign": None, "utm_content": None,
            }
        return settings
