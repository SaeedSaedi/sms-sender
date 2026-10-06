"""The send time picker (plan 06, L3): the days a send may be set for, as a
Solar Hijri calendar, and the times of day. Radio buttons and a select, so
it works without a script and with the keyboard; it posts the same values
`campaigns.forms.parse_when` reads ("1405/07/15", "13:30")."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import jdatetime
from django import template
from django.utils import timezone

from sms_sender.window import TEHRAN

from .fa import fa_digits

register = template.Library()

DAYS = 30            # campaigns.forms.SCHEDULE_MAX_DAYS: never further ahead
STEP_MINUTES = 30    # the times offered
WEEKDAYS = ("ش", "ی", "د", "س", "چ", "پ", "ج")  # Saturday first
_ASCII = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _now() -> datetime:
    return timezone.now().astimezone(TEHRAN)


def default_moment(now: datetime | None = None) -> datetime:
    """The next whole hour: a send set "for later" is rarely set for now."""
    now = now or _now()
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


@register.simple_tag
def schedule_days(selected: str = "", now: datetime | None = None) -> list[dict]:
    """The next DAYS days by month, each month as weeks of seven cells (None
    before its first day). A cell: its value, number, full name and whether
    it's chosen (`selected`, else the default moment's day)."""
    now = now or _now()
    chosen = _normalize(selected) or _date(default_moment(now))
    months: list[dict] = []
    for offset in range(DAYS + 1):
        day = jdatetime.date.fromgregorian(date=(now + timedelta(days=offset)).date())
        if not months or months[-1]["month"] != (day.year, day.month):
            months.append({"month": (day.year, day.month), "cells": [None] * day.weekday(),
                           "title": f"{day.j_months_fa[day.month - 1]} {fa_digits(day.year)}"})
        value = _date(day)
        months[-1]["cells"].append({
            "value": value, "day": fa_digits(day.day), "today": offset == 0, "checked": value == chosen,
            "label": f"{day.j_weekdays_fa[day.weekday()]} {fa_digits(day.day)} "
                     f"{day.j_months_fa[day.month - 1]} {fa_digits(day.year)}",
        })
    for month in months:
        cells = month.pop("cells")
        month["weeks"] = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    return months


@register.simple_tag
def schedule_times(selected: str = "", now: datetime | None = None) -> list[dict]:
    """Every half hour of the day; a time typed before (13:15) is kept too."""
    chosen = selected or default_moment(now).strftime("%H:%M")
    values = [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, STEP_MINUTES)]
    if chosen not in values:
        values = sorted([*values, chosen])
    return [{"value": v, "label": fa_digits(v), "selected": v == chosen} for v in values]


@register.simple_tag
def weekdays() -> tuple[str, ...]:
    return WEEKDAYS


def _normalize(text: str) -> str:
    """A date typed or posted before ("۱۴۰۵/۷/۱۵") in the cells' form."""
    parts = re.split(r"[/\-.]", (text or "").strip().translate(_ASCII))
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return ""
    year, month, day = (int(p) for p in parts)
    return f"{year}/{month:02d}/{day:02d}"


def _date(day) -> str:
    if isinstance(day, datetime):
        day = jdatetime.date.fromgregorian(date=day.date())
    return f"{day.year}/{day.month:02d}/{day.day:02d}"
