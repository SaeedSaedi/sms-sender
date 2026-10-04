"""Persian display rules (spec 4.11, decided 2026-10-04).

- Numbers people read — counts, money, dates, phone numbers, message IDs,
  error codes — in Persian digits, with the Persian thousands separator.
- Dates and times in the Solar Hijri calendar, Tehran time.
- Values that must be copied exactly (links, short codes, mixed IDs) keep
  their characters; anything with separators or Latin letters is isolated
  left-to-right so a Persian sentence can't reorder it.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import jdatetime
from django import template
from django.utils import timezone
from django.utils.html import format_html

from ...privacy import mask_phone as _mask_phone
from ..terms import DELIVERY_STATUS, SUBMISSION_STATUS

register = template.Library()

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
THOUSANDS = "٬"  # U+066C ARABIC THOUSANDS SEPARATOR, the Persian one


@register.filter
def fa_digits(value) -> str:
    """'0912 4' → '۰۹۱۲ ۴'. Only for numbers: never for links or codes."""
    return str(value).translate(_PERSIAN_DIGITS)


@register.filter
def fa_number(value) -> str:
    """12345 → '۱۲٬۳۴۵'."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fa_digits(value)
    return f"{number:,}".replace(",", THOUSANDS).translate(_PERSIAN_DIGITS)


@register.filter
def jalali(value, fmt: str = "%Y/%m/%d %H:%M") -> str:
    """A moment (datetime or unix seconds) as a Solar Hijri date in Tehran
    time, with Persian digits: '۱۴۰۵/۰۷/۱۲ ۱۳:۵۲'."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=dt_timezone.utc)
    if timezone.is_aware(value):
        value = timezone.localtime(value)  # TIME_ZONE is Asia/Tehran
    return jdatetime.datetime.fromgregorian(datetime=value).strftime(fmt).translate(_PERSIAN_DIGITS)


@register.filter
def ltr(value):
    """Keep a left-to-right value whole inside Persian text."""
    return format_html('<bdi dir="ltr">{}</bdi>', value)


@register.filter
def mask_phone(value) -> str:
    """'09120001234' → '0912*****34' (spec 4.12: pages show numbers masked)."""
    return _mask_phone(str(value))


@register.filter
def status_label(status: str) -> str:
    return SUBMISSION_STATUS.get(status, status)


@register.filter
def delivery_label(code) -> str:
    return DELIVERY_STATUS.get(code, fa_digits(code))


@register.filter
def get(mapping: dict, key):
    """`counts|get:status` in templates."""
    return mapping.get(key, 0) if mapping else 0
