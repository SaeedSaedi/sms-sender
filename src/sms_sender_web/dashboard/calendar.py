"""A month of sends (plan 06, L4): what went out each day and what's set to
go, by the Solar Hijri calendar, Saturday first, in Tehran time. The
control room's week, for a whole month and further back or ahead.

A send is on the day it started; one waiting for its time, on the day it's
set for. Withdrawn sends never started, so they aren't on it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import jdatetime
from django.utils import timezone

from sms_sender.window import TEHRAN

from ..campaigns import lifecycle
from ..campaigns.terms import STAGES
from ..jobs.models import Campaign, Job
from .control import WEEKDAYS, _url
from .templatetags.fa import fa_digits, jalali

# What a send's state reads as on the calendar: the campaign stage's words.
_STAGE = {
    Job.State.QUEUED: lifecycle.SENDING, Job.State.RUNNING: lifecycle.SENDING,
    Job.State.PAUSED: lifecycle.PAUSED, Job.State.FAILED: lifecycle.STOPPED,
    Job.State.DONE: lifecycle.COMPLETED, Job.State.CANCELLED: lifecycle.CANCELLED,
}
_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")


@dataclass(frozen=True)
class Entry:
    campaign: Campaign
    url: str
    at: datetime     # started, or set for
    time: str        # «۱۳:۳۰»
    stage: str       # lifecycle's
    label: str
    tone: str


def month_of(text: str | None, now: datetime | None = None) -> tuple[int, int]:
    """?month=1405-07 → (1405, 7); anything else, this month."""
    match = _MONTH.match(text or "")
    if match:
        year, month = int(match[1]), int(match[2])
        if 1300 <= year <= 1500 and 1 <= month <= 12:
            return year, month
    today = jdatetime.date.fromgregorian(date=(now or timezone.now()).astimezone(TEHRAN).date())
    return today.year, today.month


def _next(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _previous(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _entry(job: Job, at: datetime, stage: str) -> Entry:
    return Entry(campaign=job.campaign, url=_url(job.campaign), at=at, time=jalali(at, "%H:%M"),
                 stage=stage, label=str(STAGES[stage]), tone=lifecycle.TONE[stage])


def month(year: int, month: int, now: datetime | None = None) -> dict:
    """The month as weeks of seven cells (None outside it), each day with
    its sends in time order; and the days that have any, for phones."""
    local = (now or timezone.now()).astimezone(TEHRAN)
    first = jdatetime.date(year, month, 1)
    start = datetime.combine(first.togregorian(), datetime.min.time(), tzinfo=TEHRAN)
    end = datetime.combine(jdatetime.date(*_next(year, month), 1).togregorian(), datetime.min.time(),
                           tzinfo=TEHRAN)
    sends = Job.objects.filter(kind=Job.Kind.SEND).select_related("campaign")
    entries = [_entry(job, job.started_at, _STAGE[job.state])
               for job in sends.filter(started_at__gte=start, started_at__lt=end)]
    entries += [_entry(job, job.not_before, lifecycle.SCHEDULED)
                for job in sends.filter(state=Job.State.QUEUED, started_at__isnull=True,
                                        not_before__gte=start, not_before__lt=end)]
    entries.sort(key=lambda e: e.at)
    by_day: dict[int, list[Entry]] = {}
    for e in entries:
        by_day.setdefault(jdatetime.date.fromgregorian(date=e.at.astimezone(TEHRAN).date()).day, []).append(e)

    cells: list[dict | None] = [None] * first.weekday()
    day = first
    while day.month == month:
        date = day.togregorian()
        cells.append({
            "day": day.day, "number": fa_digits(day.day), "today": date == local.date(),
            "past": date < local.date(), "entries": by_day.get(day.day, []),
            "name": f"{day.j_weekdays_fa[day.weekday()]} {fa_digits(day.day)} {day.j_months_fa[month - 1]}",
        })
        day += timedelta(days=1)
    cells += [None] * (-len(cells) % 7)
    previous, following = _previous(year, month), _next(year, month)
    return {
        "title": f"{first.j_months_fa[month - 1]} {fa_digits(year)}",
        "weekdays": [{"name": jdatetime.date.j_weekdays_fa[i], "short": WEEKDAYS[i]} for i in range(7)],
        # The legend: the stages this month has, each once.
        "stages": list({e.stage: e for e in entries}.values()),
        "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)],
        "days": [c for c in cells if c and c["entries"]],
        "count": len(entries),
        "previous": f"{previous[0]}-{previous[1]:02d}",
        "next": f"{following[0]}-{following[1]:02d}",
        "this_month": (year, month) == month_of(None, now),
    }
