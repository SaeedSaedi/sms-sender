"""The activity log (spec 4.12): every recorded action, newest first, 100
to a page; filtered by action, person, campaign and period, and
downloadable as it's filtered. The page and the file mask phone numbers."""
import csv
import json
from datetime import timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from sms_sender import csvsafe

from ..accounts.decorators import requires
from ..privacy import mask_phone
from .models import AuditEvent
from .record import record
from .terms import ACTION_LABELS, describe

PERIODS = {"1": 1, "7": 7, "30": 30}  # days


def _filtered(data):
    """The events the filters pick, and the filters (for the form and links)."""
    events = AuditEvent.objects.all()
    filters = {}
    action = data.get("action", "")
    if action in ACTION_LABELS:
        events, filters["action"] = events.filter(action=action), action
    who = data.get("user", "").strip()[:150]
    if who:
        events, filters["user"] = events.filter(username=who), who
    campaign = data.get("campaign", "").strip()[:64]
    if campaign:
        events, filters["campaign"] = events.filter(campaign=campaign), campaign
    period = data.get("period", "")
    if period in PERIODS:
        events = events.filter(at__gte=timezone.now() - timedelta(days=PERIODS[period]))
        filters["period"] = period
    return events, filters


@requires("view_audit_log")
def audit_log(request):
    events, filters = _filtered(request.GET)
    page = Paginator(events, 100).get_page(request.GET.get("page"))
    rows = [(event, ACTION_LABELS.get(event.action, event.action), describe(event)) for event in page]
    return render(request, "audit/log.html", {
        "page": page, "rows": rows, "filters": filters, "filter_query": urlencode(filters),
        "actions": sorted(((key, str(label)) for key, label in ACTION_LABELS.items()), key=lambda kv: kv[1]),
        "people": AuditEvent.objects.exclude(username="").values_list("username", flat=True).distinct().order_by("username"),
    })


def _masked(detail: dict) -> dict:
    """A number in the details is masked, as the page shows it."""
    return {k: (mask_phone(str(v)) if k == "phone" and v else v) for k, v in (detail or {}).items()}


@requires("view_audit_log")
def audit_csv(request):
    events, filters = _filtered(request.GET)
    record("audit_exported", request=request, filters=filters)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="activity.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["at", "username", "action", "campaign", "ip", "detail"])
    for e in events.iterator():
        writer.writerow(csvsafe.row([
            e.at.astimezone(dt_timezone.utc).isoformat(timespec="seconds"), e.username, e.action,
            e.campaign, e.ip or "", json.dumps(_masked(e.detail), ensure_ascii=False, sort_keys=True),
        ]))
    return response
