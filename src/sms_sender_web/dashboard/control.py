"""The control room (plan 06, L4): the home page answers "how is it going?"
and "what do I do next?" at a glance.

- today and the last 7 days: SMS accepted, delivered, clicks, click rate, spend;
- sends on their way, each with its bar, pace and time left;
- Kavenegar's credit, with how far it goes at the recent pace;
- this week: what went out each day, and what's set to go;
- the latest campaigns, each with its key numbers.

Everything is read: from the campaign DBs (activity.py) and the jobs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import jdatetime
from django.urls import reverse
from django.utils import timezone

from sms_sender.window import TEHRAN

from ..accounts.roles import can
from ..campaigns.present import say
from ..reports.insights import change
from ..jobs.models import Campaign, Job
from ..system.models import ProviderCheck, SystemSettings
from .activity import Totals
from .templatetags.fa import fa_digits, fa_number, fa_percent, jalali, jalali_long
from .terms import FIGURES, PACE, RIAL_UNITS, RUNWAY, TIME_LEFT, WEEK_DAY

ACTIVE = (Job.State.RUNNING, Job.State.PAUSED)
PACE_DAYS = 30


def _against_month(week_rate: float | None, month_rate: float | None, week_n: int, month_n: int) -> dict:
    """The last 7 days' rate against the last 30's, once the 30 hold more
    than the 7 (else they're the same SMS): the line under a figure, and its
    tone."""
    if week_rate is None or month_rate is None or month_n <= week_n:
        return {}
    moved = change(week_rate, month_rate, "rate", True)
    if moved.tone == "neutral":
        return {"sub": str(FIGURES["same_as_month"]), "tone": "neutral"}
    return {"sub": say(FIGURES["vs_month"], {"change": moved.text}), "tone": moved.tone}


def figures(today: Totals, week: Totals, month: Totals) -> list[dict]:
    """The strip at the top: what went out today and in the last 7 days,
    the rates against the last 30 days', and what an SMS costs."""
    def rials(n: int) -> str:
        return say(FIGURES["rials"], {"n": n})

    delivered = {"sub": say(FIGURES["reports"], {"n": week.delivery_known}) if week.delivery_known
                 else str(FIGURES["no_reports"])}
    delivered.update(_against_month(week.delivered_rate, month.delivered_rate, week.delivery_known,
                                    month.delivery_known))
    clicked = {"sub": say(FIGURES["clicked"], {"n": week.clicked, "of": week.own_links}) if week.own_links
               else str(FIGURES["no_links"])}
    clicked.update(_against_month(week.click_rate, month.click_rate, week.own_links, month.own_links))
    per_sms = week.cost_per_sms
    return [
        {"label": FIGURES["accepted"], "value": say("{n}", {"n": today.accepted}),
         "sub": say(FIGURES["week"], {"n": week.accepted})},
        {"label": FIGURES["delivered"], "value": fa_percent(week.delivered_rate), **delivered},
        {"label": FIGURES["clicks"], "value": say("{n}", {"n": today.clicks}),
         "sub": say(FIGURES["week"], {"n": week.clicks})},
        {"label": FIGURES["click_rate"], "value": fa_percent(week.click_rate), **clicked},
        {"label": FIGURES["spend"], "value": rials(today.cost),
         "sub": say(FIGURES["per_sms"], {"n": round(per_sms)}) if per_sms
         else say(FIGURES["week_rials"], {"n": week.cost})},
    ]


def campaign_url(campaign: Campaign, user=None) -> str:
    """An alert opens in the composer for someone who can send it (the
    composer needs run_campaigns); any other campaign, and an alert for
    anyone else or when nobody is named, on its page."""
    composer = campaign.preset_id is not None and user is not None and can(user, "run_campaigns")
    return reverse("compose_campaign" if composer else "campaign_detail", args=[campaign.slug])


@dataclass(frozen=True)
class ActiveSend:
    job: Job
    campaign: Campaign
    url: str
    stage: str              # "links" while the short links are made, else "sms"
    done: int
    total: int
    per_second: float | None
    left: str               # time left, in words ("" when it can't be told)
    counts: dict

    @property
    def pace(self) -> str:
        """«۱٫۷ در ثانیه»: one decimal, the Persian decimal mark."""
        if not self.per_second:
            return ""
        return say(PACE, {"n": f"{self.per_second:.1f}".replace(".", "٫")})


def time_left(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes = round(seconds / 60)
    if minutes < 1:
        return str(TIME_LEFT["under_a_minute"])
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return say(TIME_LEFT["minutes"], {"m": minutes})
    return say(TIME_LEFT["hours"], {"h": hours, "m": minutes})


def active_send(job: Job, now: datetime | None = None, user=None) -> ActiveSend:
    """How far a send has got, its pace and the time left. The pace is the
    average since it started, so it's shown only while it runs."""
    now = now or timezone.now()
    p = job.progress or {}
    links = p.get("stage") == "links"
    total, done = p.get("total") or 0, p.get("processed") or 0
    elapsed = (now - job.started_at).total_seconds() if job.started_at else 0
    pace = done / elapsed if done and elapsed > 0 and job.state == Job.State.RUNNING else None
    if links and p.get("eta_sec") is not None:
        left = time_left(p["eta_sec"])
    else:
        left = time_left((total - done) / pace) if pace and total > done else ""
    return ActiveSend(
        job=job, campaign=job.campaign, url=campaign_url(job.campaign, user), stage="links" if links else "sms",
        done=done, total=total, per_second=pace, left=left,
        counts={k: p.get(k, 0) for k in ("sent", "failed_retriable", "failed_permanent", "unknown")},
    )


def active_sends(now: datetime | None = None, user=None) -> list[ActiveSend]:
    """Sends running or paused, the newest first, with how far they've got."""
    now = now or timezone.now()
    out = [active_send(job, now, user) for job in
           Job.objects.filter(kind=Job.Kind.SEND, state__in=ACTIVE).select_related("campaign")]
    out.sort(key=lambda a: a.job.started_at or now, reverse=True)
    return out


def credit(month: Totals, now: datetime | None = None) -> dict:
    """The credit as last asked, and how far it goes: in sends like the
    recent ones (their average cost), and in days at the last 30 days'
    spending."""
    now = now or timezone.now()
    check = ProviderCheck.load()
    floor = SystemSettings.load().credit_floor
    out = {"check": check, "low": floor is not None and check.credit is not None and check.credit < floor,
           "floor": floor, "runway": ""}
    if check.credit is None:
        return out
    # As people say it: 264,731,842 → «۲۶۴٫۷ میلیون ریال»; under a million, in full.
    if check.credit >= 1_000_000:
        out["amount"] = fa_digits(f"{check.credit / 1_000_000:.1f}".replace(".", "٫"))
        out["unit"] = str(RIAL_UNITS["million"])
    else:
        out["amount"], out["unit"] = fa_number(check.credit), str(RIAL_UNITS["rials"])
    costs = [
        (job.result or {}).get("cost") or 0
        for job in Job.objects.filter(kind=Job.Kind.SEND, finished_at__gte=now - timedelta(days=PACE_DAYS))
    ]
    costs = [c for c in costs if c > 0]
    sends = check.credit // (sum(costs) // len(costs)) if costs and sum(costs) >= len(costs) else None
    per_day = month.cost / PACE_DAYS if month.cost else 0
    days = int(check.credit // per_day) if per_day else None
    if sends is not None and days is not None:
        out["runway"] = say(RUNWAY["both"], {"sends": sends, "days": days})
    elif sends is not None:
        out["runway"] = say(RUNWAY["sends"], {"sends": sends})
    elif days is not None:
        out["runway"] = say(RUNWAY["days"], {"days": days})
    return out


WEEKDAYS = ("ش", "ی", "د", "س", "چ", "پ", "ج")


def week(now: datetime | None = None, user=None) -> dict:
    """This week, Saturday to Friday (Tehran): the sends that went out each
    day, and the ones set for later."""
    local = (now or timezone.now()).astimezone(TEHRAN)
    today = jdatetime.date.fromgregorian(date=local.date())
    saturday = local.date() - timedelta(days=today.weekday())
    start = datetime.combine(saturday, datetime.min.time(), tzinfo=TEHRAN)
    end = start + timedelta(days=7)
    sends = Job.objects.filter(kind=Job.Kind.SEND).select_related("campaign")
    went = list(sends.filter(started_at__gte=start, started_at__lt=end))  # a withdrawn send never started
    scheduled = list(sends.filter(state=Job.State.QUEUED, not_before__gte=local).order_by("not_before"))
    days = []
    for i in range(7):
        day_start = start + timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        j_day = jdatetime.date.fromgregorian(date=day_start.date())
        sent = sum(1 for job in went if day_start <= job.started_at.astimezone(TEHRAN) < day_end)
        later = sum(1 for job in scheduled if day_start <= job.not_before.astimezone(TEHRAN) < day_end)
        days.append({
            "name": WEEKDAYS[i], "number": fa_digits(j_day.day), "today": day_start.date() == local.date(),
            "past": day_end <= local, "sent": sent, "scheduled": later,
            # What a screen reader hears for the day (the dots are hidden).
            "summary": say(WEEK_DAY, {"day": f"{j_day.j_weekdays_fa[i]} {j_day.day}", "sent": sent,
                                      "later": later}),
        })
    return {"days": days, "scheduled": [
        {"job": job, "campaign": job.campaign, "url": campaign_url(job.campaign, user),
         "when": f"{jalali_long(job.not_before)} · {jalali(job.not_before, '%H:%M')}"}
        for job in scheduled[:5]
    ]}


@dataclass(frozen=True)
class RecentRow:
    campaign: Campaign | None
    slug: str
    name: str
    url: str
    when: datetime | None
    recipients: int
    totals: Totals
    stage: str
    tone: str
    segments: int


def recent(rows: list[dict], activity: dict[str, dict[str, Totals]], limit: int = 8, user=None) -> list[RecentRow]:
    """The latest campaigns (the campaign list's rows, newest first), each
    with its key numbers over all time."""
    out = []
    for row in rows[:limit]:
        summary, campaign = row["summary"], row["campaign"]
        settings = (campaign.settings or {}) if campaign else {}
        out.append(RecentRow(
            campaign=campaign, slug=summary.slug, name=campaign.name if campaign else summary.name,
            url=campaign_url(campaign, user) if campaign else reverse("report", args=[summary.slug]),
            when=summary.last_run_at or (campaign.created_at if campaign else None),
            recipients=summary.recipients,
            totals=activity.get(summary.slug, {}).get("all") or Totals(),
            stage=row["stage"], tone=row["tone"],
            segments=len([s for s in [settings.get("segment"), *(settings.get("more_segments") or [])] if s]),
        ))
    return out
