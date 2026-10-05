"""A campaign's message before its test SMS (plan 05, P2): the template's
text from the library, filled with the first recipient's values, its
length and SMS parts, and the token mismatches — a placeholder nothing
fills, or a token the text doesn't use. Nothing here sends or saves."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sms_sender import input_loader
from sms_sender.input_loader import InputError, TokenColumns
from sms_sender.links import token_value
from sms_sender.shortlink import shlink_base_url

from ..privacy import mask_phone
from .message import check, fill, length, shows_left_to_right
from .models import MessageTemplate
from .present import say
from .terms import LENGTH, PREVIEW_LTR, PREVIEW_MISSING, PREVIEW_UNUSED

# Shlink makes 5-character codes by default; the preview shows one like it.
SAMPLE_CODE = "aB3dE"


@dataclass(frozen=True)
class Preview:
    template: str                   # the template's name ("" when none is set)
    known: bool = False             # its text is in the library
    text: str = ""                  # the message, filled in
    length: str = ""                # "{chars} characters · {parts} SMS"
    parts: int = 0
    sample: str = ""                # whose values (masked), when a column is used
    has_link: bool = False
    problems: list[str] = field(default_factory=list)   # placeholders nothing fills
    warnings: list[str] = field(default_factory=list)   # tokens the text doesn't use


def _first_row(settings: dict, segment) -> tuple[dict[str, str], str]:
    """The first valid recipient's token values (translated), and its phone."""
    columns = settings.get("token_columns") or {}
    if not columns or segment is None or not segment.path.exists():
        return {}, ""
    token_columns = TokenColumns(columns=dict(columns), value_maps=dict(settings.get("value_maps") or {}))
    try:
        loaded = input_loader.load(segment.path, token_columns, segment.user_id_column or None)
    except (InputError, OSError):
        return {}, ""
    if not loaded.valid:
        return {}, ""
    row = loaded.valid[0]
    return dict(row.tokens), mask_phone(row.phone)


def preview_of(settings: dict | None, segment) -> Preview:
    settings = settings or {}
    name = (settings.get("template") or "").strip()
    template = MessageTemplate.objects.filter(name=name).first() if name else None
    if template is None:
        return Preview(template=name)

    values = dict(settings.get("tokens") or {})
    row, sample = _first_row(settings, segment)
    values.update(row)
    links = settings.get("links") or {}
    if links.get("token"):
        url = f"{shlink_base_url().rstrip('/')}/{SAMPLE_CODE}"
        values[links["token"]] = token_value(links.get("format") or "url", url, SAMPLE_CODE)
    filled = set(values) | set(settings.get("token_columns") or {})

    text = fill(template.text, values)
    size = length(text)
    found = check(template.text, filled)
    return Preview(
        template=name, known=True, text=text, parts=size.parts,
        length=say(LENGTH, {"chars": size.chars, "parts": size.parts}),
        sample=sample, has_link=bool(links.get("token")),
        problems=[say(PREVIEW_MISSING, {"token": f"%{t}"}) for t in found.missing],
        warnings=[say(PREVIEW_UNUSED, {"token": t}) for t in found.unused]
        + ([str(PREVIEW_LTR)] if shows_left_to_right(text) else []),
    )


# ---------- each recipient's message (the CLI's dry-run and preview) ----------

ROW_CHOICES = (5, 10, 20)


@dataclass(frozen=True)
class RecipientPreview:
    phone: str                          # masked
    values: list[tuple[str, str]]       # (token, value), in the template's token order
    text: str                           # the message ("" when the template's text isn't known)
    length: str


@dataclass(frozen=True)
class LinkPreview:
    long_url: str
    title: str
    tags: str
    valid_until: datetime               # Shlink is asked for it in ISO, UTC
    personal: bool                      # a fresh random `r` for every recipient


@dataclass(frozen=True)
class RecipientsPreview:
    rows: list[RecipientPreview]
    total: int                          # valid recipients in the segment
    asked: str = ""                     # the number searched for, masked
    found: bool = True
    link: LinkPreview | None = None


def recipients(settings: dict | None, segment, campaign: str, *, limit: int = 5,
               phone: str | None = None) -> RecipientsPreview | None:
    """The first `limit` recipients' messages, or one number's, with the
    link's request. Reads the segment's file; nothing is sent or made."""
    from sms_sender.links import LinkSettings, plan_link
    from sms_sender.phone import InvalidPhoneError, normalize

    settings = settings or {}
    if segment is None or not segment.path.exists():
        return None
    columns = settings.get("token_columns") or {}
    token_columns = (
        TokenColumns(columns=dict(columns), value_maps=dict(settings.get("value_maps") or {}))
        if columns else None
    )
    try:
        loaded = input_loader.load(segment.path, token_columns, segment.user_id_column or None)
    except (InputError, OSError):
        return None
    rows = loaded.valid
    asked, found = "", True
    if phone:
        try:
            wanted = normalize(phone)
        except InvalidPhoneError:
            wanted = None
        asked = mask_phone(wanted or phone)
        rows = [r for r in rows if r.phone == wanted]
        found = bool(rows)
    else:
        rows = rows[:limit]

    name = (settings.get("template") or "").strip()
    template = MessageTemplate.objects.filter(name=name).first() if name else None
    links = settings.get("links") or {}
    sample_link = None
    if links.get("token"):
        sample_link = token_value(links.get("format") or "url",
                                  f"{shlink_base_url().rstrip('/')}/{SAMPLE_CODE}", SAMPLE_CODE)
    order = ("token", "token2", "token3", "token10", "token20")
    out = []
    for row in rows:
        values = {**(settings.get("tokens") or {}), **row.tokens}
        if sample_link:
            values[links["token"]] = sample_link
        text = fill(template.text, values) if template else ""
        size = length(text) if text else None
        out.append(RecipientPreview(
            phone=mask_phone(row.phone),
            values=[(t, values[t]) for t in order if t in values],
            text=text,
            length=say(LENGTH, {"chars": size.chars, "parts": size.parts}) if size else "",
        ))

    link = None
    if links.get("token") and loaded.valid:
        first = (rows or loaded.valid)[0]
        planned = plan_link(LinkSettings(**links), campaign=campaign, key=first.phone, segment=segment.slug)
        link = LinkPreview(planned.long_url, planned.title, ", ".join(planned.tags),
                           datetime.fromisoformat(planned.valid_until), personal=planned.ref is not None)
    return RecipientsPreview(rows=out, total=len(loaded.valid), asked=asked, found=found, link=link)
