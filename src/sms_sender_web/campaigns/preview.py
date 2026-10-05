"""A campaign's message before its test SMS (plan 05, P2): the template's
text from the library, filled with the first recipient's values, its
length and SMS parts, and the token mismatches — a placeholder nothing
fills, or a token the text doesn't use. Nothing here sends or saves."""
from __future__ import annotations

from dataclasses import dataclass, field

from sms_sender import input_loader
from sms_sender.input_loader import InputError, TokenColumns
from sms_sender.links import token_value
from sms_sender.shortlink import shlink_base_url

from ..privacy import mask_phone
from .message import check, fill, length
from .models import MessageTemplate
from .present import say
from .terms import LENGTH, PREVIEW_MISSING, PREVIEW_UNUSED

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
        warnings=[say(PREVIEW_UNUSED, {"token": t}) for t in found.unused],
    )
