"""Analytics' series and audience tabs (plan 06, L5). Read only, numbers
only: nothing here names a person."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, render

from ..accounts.decorators import requires
from ..campaigns.models import Preset
from ..campaigns.present import say
from ..segments.models import Segment
from ..system.models import SystemSettings
from . import insights
from .charts import rate_chart
from ..dashboard.templatetags.fa import fa_percent
from .terms import (
    ALERT_COUNT, BEST_LINE, BUSIEST_LINE, CAP_LINE, CHOOSE_SEGMENTS, FATIGUE_LINE, HOURS_HINT, METRIC_LABELS, OVERLAP_LINE,
    TOO_MANY_SEGMENTS,
)


@requires("view_campaigns")
def series_list(request):
    """Every preset that has sent, with its alerts pooled: how many, the
    latest, delivery, click rate and cost per click, and which way the click
    rate (else delivery) has been going."""
    rows = []
    for preset in Preset.objects.all():
        alerts = insights.alerts_of(preset)
        if not alerts:
            continue
        trends = {t["key"]: t for t in insights.trends(alerts)}
        rows.append({
            "preset": preset, "alerts": len(alerts), "latest": alerts[-1].when,
            "figures": insights.figures(insights.pooled(alerts)),
            "trend": trends.get("click_rate") or trends.get("delivered_rate"),
        })
    rows.sort(key=lambda r: r["latest"] or 0, reverse=True)
    return render(request, "reports/series_list.html", {"rows": rows, "tab": "series"})


@requires("view_campaigns")
def series_detail(request, slug: str):
    """One preset's alerts over time: a bar per alert, a sentence per figure
    on which way it's going, and the table."""
    preset = get_object_or_404(Preset, slug=slug)
    alerts = insights.alerts_of(preset)
    key, points = insights.series_points(alerts) if alerts else ("click_rate", [])
    return render(request, "reports/series_detail.html", {
        "preset": preset,
        "alerts": [{"alert": a, "figures": insights.figures(a.totals)} for a in reversed(alerts)],  # latest first
        "first": alerts[0].when if alerts else None, "last": alerts[-1].when if alerts else None,
        "figures": insights.figures(insights.pooled(alerts)), "trends": insights.trends(alerts),
        "recent": insights.RECENT, "count_line": say(ALERT_COUNT, {"n": len(alerts)}),
        "chart": rate_chart(points), "chart_label": METRIC_LABELS[key], "tab": "series",
    })


@requires("view_campaigns")
def audience(request):
    """How often people get an SMS, the best hour to send, and how far the
    chosen segments overlap (their short names in the address: nothing
    personal)."""
    ready = [s for s in Segment.objects.filter(status=Segment.Status.READY).order_by("name", "slug") if s.path.exists()]
    wanted = request.GET.getlist("s")
    chosen = [s for s in ready if s.slug in wanted]
    if len(chosen) > insights.MAX_CHOSEN:
        messages.warning(request, say(TOO_MANY_SEGMENTS, {"n": insights.MAX_CHOSEN}))
        chosen = chosen[: insights.MAX_CHOSEN]
    fatigue = insights.fatigue(SystemSettings.load().frequency_cap)
    hours = insights.send_hours()
    overlap = insights.overlap(chosen) if len(chosen) >= 2 else None
    best, busiest = (hours or {}).get("best"), (hours or {}).get("busiest")
    cap = fatigue["cap"]
    return render(request, "reports/audience.html", {
        "fatigue": fatigue,
        "fatigue_line": say(FATIGUE_LINE, {"p7": fatigue["people"][7], "s7": fatigue["sms"][7],
                                           "p30": fatigue["people"][30], "s30": fatigue["sms"][30]}),
        "cap_line": say(CAP_LINE, {"sms": cap.sms, "days": cap.days, "n": fatigue["at_cap"]}) if cap else "",
        "hours": hours,
        "best_line": say(BEST_LINE, {"hour": best["label"], "rate": fa_percent(best["rate"]), "n": best["sent"]})
        if best else "",
        "busiest_line": say(BUSIEST_LINE, {"hour": busiest["label"], "n": busiest["clicks"]}) if busiest else "",
        "hours_hint": say(HOURS_HINT, {"n": insights.MIN_SENT}),
        "segments": ready, "chosen": {s.slug for s in chosen},
        "choose_line": say(CHOOSE_SEGMENTS, {"n": insights.MAX_CHOSEN}),
        "overlap": overlap,
        "overlap_line": say(OVERLAP_LINE, {"once": overlap["once"], "several": overlap["several"]}) if overlap else "",
        "tab": "audience",
    })
