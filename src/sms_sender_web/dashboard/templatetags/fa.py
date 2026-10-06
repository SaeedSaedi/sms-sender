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
from ..terms import DELIVERY_STATUS, STATUS_TONE, SUBMISSION_STATUS, WHEN

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
def jalali_long(value) -> str:
    """A moment as its Solar Hijri day in words, Tehran time: «سه‌شنبه ۱۴ مهر ۱۴۰۵»."""
    if value is None or value == "":
        return ""
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    day = jdatetime.date.fromgregorian(date=local.date())
    return f"{day.j_weekdays_fa[day.weekday()]} {fa_digits(day.day)} {day.j_months_fa[day.month - 1]} {fa_digits(day.year)}"


@register.filter
def fa_percent(rate) -> str:
    """0.0432 → '۴٫۳٪' (one decimal, the Persian decimal mark); None → '—'."""
    if rate is None or rate == "":
        return "—"
    text = f"{float(rate) * 100:.1f}".replace(".", "٫").translate(_PERSIAN_DIGITS)
    return f"{text}٪"


@register.filter
def jalali(value, fmt: str = "%Y/%m/%d %H:%M") -> str:
    """A moment (datetime or unix seconds) as a Solar Hijri date in Tehran
    time, with Persian digits: '۱۴۰۵/۰۷/۱۲ ۱۳:۵۲'."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=dt_timezone.utc)
    try:
        local = timezone.localtime(value) if timezone.is_aware(value) else value  # TIME_ZONE is Asia/Tehran
        return jdatetime.datetime.fromgregorian(datetime=local).strftime(fmt).translate(_PERSIAN_DIGITS)
    except (OverflowError, ValueError):
        # Beyond the Solar Hijri calendar (e.g. a year-9999 "never" date):
        # the Gregorian date, rather than a page that fails.
        return value.strftime("%Y-%m-%d")


def _moment(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=dt_timezone.utc)
    return value


@register.filter
def jalali_when(value, now: datetime | None = None) -> str:
    """A moment in a few words, Tehran time (plan 06, L6): «امروز ۱۳:۰۲»,
    «دیروز ۱۰:۴۵», the weekday within the last week («دوشنبه ۰۹:۱۵»), else
    the date and time."""
    moment = _moment(value)
    if moment is None:
        return ""
    local = timezone.localtime(moment) if timezone.is_aware(moment) else moment
    today = timezone.localtime(now or timezone.now()).date()
    days = (today - local.date()).days
    time = jalali(local, "%H:%M")
    if days == 0:
        return str(WHEN["today"]).format(time=time)
    if days == 1:
        return str(WHEN["yesterday"]).format(time=time)
    if 1 < days < 7:
        day = jdatetime.date.fromgregorian(date=local.date())
        return f"{day.j_weekdays_fa[day.weekday()]} {time}"
    return jalali(local)


@register.filter
def ago(value, now: datetime | None = None) -> str:
    """How long ago, in a few words (plan 06, L6): «همین حالا», «۵ دقیقه
    پیش», «۳ ساعت پیش»; longer than a day, as jalali_when."""
    moment = _moment(value)
    if moment is None:
        return ""
    seconds = ((now or timezone.now()) - moment).total_seconds()
    if seconds < 60:
        return str(WHEN["just_now"])
    if seconds < 3600:
        return str(WHEN["minutes_ago"]).format(n=fa_digits(int(seconds // 60)))
    if seconds < 86400:
        return str(WHEN["hours_ago"]).format(n=fa_digits(int(seconds // 3600)))
    return jalali_when(moment, now)


@register.filter
def ltr(value):
    """Keep a left-to-right value whole inside Persian text."""
    return format_html('<bdi dir="ltr">{}</bdi>', value)


@register.filter
def copyable(value):
    """An identifier with a copy button (spec 4.11). A purely numeric one is
    a number, shown in Persian digits; anything else keeps its characters.
    The button always copies the stored value exactly."""
    from django.utils.translation import gettext

    text = str(value)
    shown = fa_digits(text) if text.isdigit() else text
    return format_html(
        '<bdi dir="ltr">{}</bdi><button type="button" class="copy" data-copy="{}" '
        'data-copied="{}" title="{}" aria-label="{}: {}">⧉</button>',
        shown, text, gettext("Copied"), gettext("Copy"), gettext("Copy"), text,
    )


@register.filter
def mask_phone(value) -> str:
    """'09120001234' → '0912*****34' (spec 4.12: pages show numbers masked)."""
    return _mask_phone(str(value))


@register.filter
def status_label(status: str) -> str:
    return SUBMISSION_STATUS.get(status, status)


@register.filter
def status_tone(status: str) -> str:
    """success / info / warning / danger / neutral, for bars and legends."""
    return STATUS_TONE.get(status, "neutral")


@register.filter
def delivery_label(code) -> str:
    return DELIVERY_STATUS.get(code, fa_digits(code))


@register.filter
def get(mapping: dict, key):
    """`counts|get:status` in templates."""
    return mapping.get(key, 0) if mapping else 0
