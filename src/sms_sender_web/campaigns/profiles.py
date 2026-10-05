"""The CLI's profiles as draft campaigns (review G4). The team kept its
presets in sms-sender.toml; an admin uploads it, and each named profile,
merged over [profile.default] as the CLI merges it, becomes a draft
campaign: what it sends (the template, fixed tokens, token columns, value
translations, the short link) and how (window, rate, parallel sends, the
advanced settings).

Paths belong to the machine the CLI ran on and stay there: a profile's
input picks a segment only when a ready segment has its name, and its state
DB is never taken over (the campaign list brings a CLI campaign over, with
its records). A send's own choices (one recipient first, the test number)
are made when it starts, and notifications, opt-outs and the frequency cap
are system-wide here. Nothing is sent, and no campaign DB is opened."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from sms_sender.input_loader import SLUG_RE, slugify
from sms_sender.links import DEFAULT_EXPIRY_DAYS, STRATEGIES, allowed_domains, destination_issue, format_problem
from sms_sender.profile import DEFAULT_PROFILE
from sms_sender.rate import parse_rate
from sms_sender.sender import TOKEN_MAX_SPACES, token_issue
from sms_sender.shortlink import shlink_base_url
from sms_sender.window import parse_window

from ..jobs.models import Campaign
from ..segments.models import Segment
from ..system.models import SystemSettings
from ..system.operations import adoptable
from ..text import persian_text
from .forms import TEMPLATE_RE, free_slug, slug_taken

MAX_BYTES = 256 * 1024
TOKENS = tuple(TOKEN_MAX_SPACES)
LINK_KEYS = ("link_url", "link_token", "link_format", "link_strategy", "link_expiry_days",
             "utm_source", "utm_medium", "utm_campaign", "utm_content")
# What a profile gives a campaign; any other key is listed as left out.
USED = {"template", *TOKENS, "token_column", "value_map", *LINK_KEYS, "send_window", "rate", "workers",
        "max_attempts", "timeout", "backoff_max", "link_rate", "input", "segment", "campaign"}
# The advanced settings (admins only, as on the settings page): their ranges.
ADVANCED = {"max_attempts": (int, 1, 10), "timeout": (float, 5, 120), "backoff_max": (float, 1, 300)}


class ProfileFileError(ValueError):
    """The file can't be read as profiles. `code`: too_big, not_utf8,
    not_toml or no_profiles."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Draft:
    profile: str
    name: str
    slug: str
    settings: dict = field(default_factory=dict)
    segment: Segment | None = None
    problems: list[str] = field(default_factory=list)   # why it can't be imported (terms.IMPORT_PROBLEMS)
    reset: list[str] = field(default_factory=list)      # options set back to the default
    left_out: list[str] = field(default_factory=list)   # options a campaign doesn't take from a profile
    exists: bool = False                                # a campaign has this name already
    adoptable: str = ""   # a CLI campaign DB of its name, with records: bring that over instead

    @property
    def importable(self) -> bool:
        return not self.problems


def read(raw: bytes) -> dict[str, dict | None]:
    """Each named profile merged over [profile.default] (or the default
    alone, if it's the only one), in the file's order. None: not a table."""
    if len(raw) > MAX_BYTES:
        raise ProfileFileError("too_big")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ProfileFileError("not_utf8") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileFileError("not_toml") from e
    profiles = data.get("profile")
    if not isinstance(profiles, dict) or not profiles:
        raise ProfileFileError("no_profiles")
    base = profiles.get(DEFAULT_PROFILE)
    base = dict(base) if isinstance(base, dict) else {}
    names = [name for name in profiles if name != DEFAULT_PROFILE] or [DEFAULT_PROFILE]
    return {name: {**base, **profiles[name]} if isinstance(profiles[name], dict) else None for name in names}


def _listed(value) -> list[str]:
    """A profile's repeatable option: a TOML list, or one string."""
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _columns(values: dict, d: Draft) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """--token-column and --value-map, read as the CLI reads them."""
    columns: dict[str, str] = {}
    for spec in _listed(values.get("token_column")):
        name, sep, column = (s.strip() for s in spec.partition("="))
        if not (sep and name in TOKENS and column) or name in columns:
            d.problems.append("bad_token_column")
            return {}, {}
        columns[name] = column
    maps: dict[str, dict[str, str]] = {}
    for spec in _listed(values.get("value_map")):
        column, sep_col, pair = (s.strip() for s in spec.partition(":"))
        source, sep_eq, target = (s.strip() for s in pair.partition("="))
        if not (sep_col and sep_eq and column and source and target) or column not in columns.values():
            d.problems.append("bad_value_map")
            return columns, {}
        maps.setdefault(column, {})[source] = persian_text(target)
    return columns, maps


def _links(values: dict, taken: set[str], d: Draft) -> dict | None:
    if not values.get("link_url"):
        return None
    url, token = str(values["link_url"]).strip(), str(values.get("link_token") or "")
    fmt, strategy = str(values.get("link_format") or "url"), str(values.get("link_strategy") or "recipient")
    try:
        expiry = int(values.get("link_expiry_days") or DEFAULT_EXPIRY_DAYS)
    except (TypeError, ValueError):
        expiry = 0
    if (token not in TOKENS or token in taken or format_problem(fmt) or strategy not in STRATEGIES
            or not 1 <= expiry <= 365 or destination_issue(url, allowed_domains(), shlink_base_url())):
        d.problems.append("bad_link")
        return None
    utm = {key: persian_text(str(values[key])) if values.get(key) else None
           for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content")}
    return {"destination": url, "token": token, "format": fmt, "strategy": strategy, "expiry_days": expiry,
            **utm, "utm_source": utm["utm_source"] or "sms", "utm_medium": utm["utm_medium"] or "sms"}


def _segment(values: dict, needed: set[str]) -> Segment | None:
    """A ready segment the profile names: its `segment`, or its input's
    file name (as the segment's short name, or the file it was uploaded
    from), when it has every column the tokens use."""
    ready = Segment.objects.filter(status=Segment.Status.READY)
    found = []
    if values.get("segment"):
        found.append(ready.filter(slug=str(values["segment"])).first())
    if values.get("input"):
        path = Path(str(values["input"]))
        found += [ready.filter(slug=slugify(path.stem)).first(), ready.filter(original_name=path.name).first()]
    return next((s for s in found if s and s.path.exists() and needed <= set(s.token_columns or [])), None)


def _run_settings(values: dict, d: Draft) -> dict:
    """Window, rate, parallel sends and the advanced settings: one the
    dashboard can't take is set back to its default, and listed."""
    defaults = SystemSettings.load()
    window = defaults.default_send_window or "08:00-21:00"
    if values.get("send_window"):
        try:
            parsed = parse_window(str(values["send_window"]))
        except ValueError:
            parsed = None
        if parsed is None:  # "off" too: the dashboard always keeps a window
            d.reset.append("send_window")
        else:
            window = f"{parsed.start:%H:%M}-{parsed.end:%H:%M}"
    rate = defaults.default_rate or None
    if values.get("rate") not in (None, ""):
        try:
            rate = str(values["rate"]).strip() if parse_rate(str(values["rate"]).strip()) else None
        except ValueError:
            d.reset.append("rate")
    workers = values.get("workers", 5)
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 20:
        d.reset.append("workers")
        workers = 5
    out = {"send_window": window, "rate": rate, "workers": workers}
    for key, (kind, low, high) in ADVANCED.items():
        if key in values:
            try:
                value = kind(values[key])
            except (TypeError, ValueError):
                value = None
            if value is None or isinstance(values[key], bool) or not low <= value <= high:
                d.reset.append(key)
            else:
                out[key] = value
    if values.get("link_rate") not in (None, ""):
        try:
            parse_rate(str(values["link_rate"]))
            out["link_rate"] = str(values["link_rate"]).strip()
        except ValueError:
            d.reset.append("link_rate")
    return out


def draft(profile: str, values: dict | None, *, used: set[str], names: set[str]) -> Draft:
    """One profile as a draft campaign. `used`: short names this batch has
    given out already; `names`: the campaigns' names, to flag a repeat."""
    suggested = str((values or {}).get("campaign") or "")
    base = suggested if SLUG_RE.match(suggested) else (slugify(profile) or "profile")
    slug = base
    while slug in used or slug_taken(slug):
        slug = free_slug(slug)
    used.add(slug)
    d = Draft(profile=profile, name=persian_text(profile), slug=slug, exists=profile in names)
    state = Path(str((values or {}).get("state") or "")).stem
    d.adoptable = next((s for s in dict.fromkeys([base, state]) if SLUG_RE.match(s) and adoptable(s)), "")
    if values is None:
        d.problems.append("not_a_table")
        return d

    template = str(values.get("template") or "").strip()
    if not template:
        d.problems.append("no_template")
    elif not TEMPLATE_RE.match(template):
        d.problems.append("bad_template")
    tokens = {}
    for name in TOKENS:
        if values.get(name) in (None, ""):
            continue
        value = persian_text(str(values[name]))
        if token_issue(name, value):
            d.problems.append("bad_token")
        tokens[name] = value
    columns, maps = _columns(values, d)
    if set(tokens) & set(columns):
        d.problems.append("token_twice")
    links = _links(values, set(tokens) | set(columns), d)

    d.settings = {"template": template, "tokens": tokens, "token_columns": columns, "value_maps": maps,
                  **_run_settings(values, d)}
    if links:
        d.settings["links"] = links
    d.segment = _segment(values, set(columns.values()))
    if d.segment is not None:
        d.settings.update(segment=d.segment.slug, input=str(d.segment.path),
                          user_id_column=d.segment.user_id_column or None)
    d.left_out = sorted(key for key in values if key not in USED)
    d.problems = list(dict.fromkeys(d.problems))
    return d


def drafts(profiles: dict[str, dict | None]) -> list[Draft]:
    used: set[str] = set()
    names = set(Campaign.objects.values_list("name", flat=True))
    return [draft(name, values, used=used, names=names) for name, values in profiles.items()]
