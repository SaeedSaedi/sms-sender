"""The report's charts (plan 05, P3): the funnel from accepted to delivered
to clicked, and clicks over time. Drawn on the server, with no script: the
numbers, and the SVG's geometry as text (so no locale formats a coordinate).
Time runs right to left, earliest on the right, as the page reads."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone

from sms_sender.state import SENT, StateStore
from sms_sender.window import TEHRAN

from ..dashboard.templatetags.fa import fa_percent, jalali
from .terms import FUNNEL_LABELS

DELIVERED = 10  # Kavenegar's "delivered"
HOURLY_UP_TO = 72  # hours shown one by one; a longer span goes by day
WIDTH, HEIGHT = 600, 160
MAX_BAR = 48  # a rate chart's widest bar, in the same units


@dataclass(frozen=True)
class FunnelStep:
    key: str
    label: str
    n: int
    share: int  # percent of the accepted


def funnel(store: StateStore, counts: dict[str, int]) -> list[FunnelStep]:
    """Accepted → delivered → clicked. Clicked only where people had links
    of their own; delivery only as far as it has been checked."""
    sent = counts.get(SENT, 0)
    if not sent:
        return []
    steps = [("accepted", sent), ("delivered", store.delivery_counts().get(DELIVERED, 0))]
    if store.has_personal_links():
        steps.append(("clicked", sum(row["clicked"] for row in store.clicks_by_segment())))
    people, conversions, _value = store.conversion_totals()
    if conversions:
        steps.append(("converted", people))
    return [FunnelStep(key, str(FUNNEL_LABELS[key]), n, round(100 * n / sent)) for key, n in steps]


@dataclass(frozen=True)
class Bar:
    x: str
    y: str
    w: str
    h: str
    label: str
    n: int


@dataclass(frozen=True)
class Chart:
    bars: list[Bar]
    rows: list[tuple[str, int]]  # the same, as a table: when, clicks (oldest first)
    by_hour: bool
    total: int
    peak: int
    peak_label: str


def _points(hours: list[tuple[int, int]]) -> tuple[list[tuple[str, int]], bool]:
    first, last = hours[0][0], hours[-1][0]
    if last - first <= HOURLY_UP_TO * 3600:
        counts = dict(hours)
        return [(jalali(h, "%m/%d %H:00"), counts.get(h, 0)) for h in range(first, last + 3600, 3600)], True
    days: Counter = Counter()
    for h, n in hours:
        days[timezone.localtime(datetime.fromtimestamp(h, tz=dt_timezone.utc)).date()] += n
    start, end = min(days), max(days)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [(jalali(datetime.combine(d, time(12), tzinfo=TEHRAN), "%Y/%m/%d"), days.get(d, 0)) for d in dates], False


def clicks_chart(hours: list[tuple[int, int]]) -> Chart | None:
    """Bars of clicks over time: per hour (Tehran) up to three days, else
    per day. None before the first click."""
    if not hours:
        return None
    points, by_hour = _points(hours)
    peak = max(n for _, n in points)
    step = WIDTH / len(points)
    bars = []
    for i, (label, n) in enumerate(points):
        height = HEIGHT * n / peak if peak else 0
        x = WIDTH - (i + 1) * step + step * 0.1
        bars.append(Bar(f"{x:.2f}", f"{HEIGHT - height:.2f}", f"{step * 0.8:.2f}", f"{height:.2f}", label, n))
    peak_label = next(label for label, n in points if n == peak)
    return Chart(bars, points, by_hour, sum(n for _, n in points), peak, peak_label)


@dataclass(frozen=True)
class RateChart:
    bars: list[Bar]               # label: the point's and its rate, for the bar's title
    rows: list[tuple[str, float | None]]  # the same, as a table: label, rate (oldest first)
    peak: float


def rate_chart(points: list[tuple[str, float | None]]) -> RateChart | None:
    """One bar per point (a series' alerts), its height the rate; an alert
    without one leaves a gap. The earliest on the right, as the page reads."""
    if not any(rate for _label, rate in points):
        return None
    peak = max(rate or 0 for _label, rate in points)
    step = WIDTH / len(points)
    width = min(step * 0.7, MAX_BAR)  # a series of one or two alerts keeps slim bars
    bars = []
    for i, (label, rate) in enumerate(points):
        height = HEIGHT * (rate or 0) / peak
        x = WIDTH - (i + 1) * step + (step - width) / 2
        bars.append(Bar(f"{x:.2f}", f"{HEIGHT - height:.2f}", f"{width:.2f}", f"{height:.2f}",
                        f"{label}: {fa_percent(rate)}", round((rate or 0) * 1000)))
    return RateChart(bars, points, peak)
