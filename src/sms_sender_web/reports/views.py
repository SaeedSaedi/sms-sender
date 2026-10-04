"""Reports per campaign, segment and recipient, downloads, and the status
page (spec 4.9, 4.12). Read only: nothing here sends an SMS.

Phone numbers are masked; an operator may reveal one at a time, and each
reveal is recorded. Downloads: the aggregate report for everyone (no
personal data), attribution (no phone numbers) and clickers (with phone
numbers) for operators. Every download is recorded."""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings as django_settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone as dj_timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from sms_sender.clicks import (
    ATTRIBUTION_HEADER,
    CLICKERS_HEADER,
    attribution_rows,
    click_report,
    clicker_rows,
)
from sms_sender.delivery import STATUS_NAMES
from sms_sender.input_loader import SLUG_RE
from sms_sender.phone import InvalidPhoneError, normalize
from sms_sender.sender import HaltError, SendError
from sms_sender.shortlink import ShlinkError
from sms_sender.state import StateStore

from ..accounts.decorators import requires
from ..audit.record import record
from ..campaigns.terms import stop_reason
from ..dashboard.terms import DELIVERY_STATUS, STATUS_ORDER
from ..jobs.engine import Engine
from ..jobs.models import Campaign, Job
from ..jobs.worker import last_seen, worker_alive

PAGE = 100


def _store(slug: str) -> tuple[StateStore, Campaign | None]:
    """The campaign DB in data/db (the CLI's or the dashboard's), or 404.
    Never creates one."""
    if not SLUG_RE.match(slug):
        raise Http404
    path = Path(django_settings.SMS_SENDER_DB_DIR) / f"{slug}.db"
    if not path.exists():
        raise Http404
    return StateStore(path), Campaign.objects.filter(slug=slug).first()


def _delivery_rows(counts: dict) -> list[tuple[str, int]]:
    """Accepted SMS by delivery status, codes with the same meaning merged."""
    merged: Counter[str] = Counter()
    for code, n in counts.items():
        merged[str(DELIVERY_STATUS.get(code, code))] += n
    return merged.most_common()


@requires("view_campaigns")
def report(request, slug: str):
    store, campaign = _store(slug)
    query = request.GET.get("q", "").strip()
    phone, query_invalid = None, False
    if query:
        try:
            phone = normalize(query)
        except InvalidPhoneError:
            query_invalid = True
    total = 0 if query_invalid else store.recipient_total(phone)
    pages = max(1, -(-total // PAGE))
    try:
        number = min(max(1, int(request.GET.get("page", 1))), pages)
    except ValueError:
        number = 1
    rows = [] if query_invalid else store.recipients_page(limit=PAGE, offset=(number - 1) * PAGE, phone=phone)
    segments, campaign_clicks = click_report(store)
    stored = store.get_meta("settings")
    last_run = store.get_meta("last_run")
    return render(request, "reports/report.html", {
        "slug": slug,
        "campaign": campaign,
        "name": campaign.name if campaign else (store.get_meta("campaign") or slug),
        "template": json.loads(stored).get("template") if stored else None,
        "last_run_at": json.loads(last_run).get("at") if last_run else None,
        "counts": store.display_counts(),
        "status_order": STATUS_ORDER,
        "delivery": _delivery_rows(store.delivery_counts()),
        "total_cost": store.total_cost(),
        "average_cost": store.average_cost(),
        "links": store.link_counts(),
        "segments": segments,
        "campaign_clicks": campaign_clicks,
        "last_click_sync": store.last_click_sync(),
        "rows": rows,
        "total": total,
        "page": number,
        "pages": pages,
        "query": query,
        "query_invalid": query_invalid,
    })


@never_cache
@requires("reveal_phone")
@require_POST
def reveal(request, slug: str):
    """One full number, for an operator who needs it; recorded."""
    store, _ = _store(slug)
    row = request.POST.get("row", "")
    phone = store.phone_of_row(int(row)) if row.isdigit() else None
    if phone is None or phone.startswith("INVALID:"):
        raise Http404
    record("phone_revealed", request=request, campaign=slug, phone=phone)
    return render(request, "reports/_phone.html", {"phone": phone})


def _csv(filename: str, header: list[str], rows) -> HttpResponse:
    """UTF-8 with a BOM, so Excel shows Persian text; Latin digits and
    English column names — machine input, like the CLI's exports."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


def _summary_rows(store: StateStore):
    for status, n in store.display_counts().items():
        yield ["status", status, n]
    for code, n in sorted(store.delivery_counts().items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
        yield ["delivery", "not checked" if code is None else STATUS_NAMES.get(code, str(code)), n]
    yield ["cost", "total_rials", store.total_cost()]
    yield ["cost", "per_sms_rials", store.average_cost() or ""]
    for status, n in store.link_counts().items():
        yield ["links", status, n]
    segments, campaign_clicks = click_report(store)
    for s in segments:
        yield ["segment:" + s.segment, "sent", s.sent]
        yield ["segment:" + s.segment, "clicks", s.clicks]
        if s.clicked is not None:
            yield ["segment:" + s.segment, "clicked", s.clicked]
        yield ["segment:" + s.segment, "missing_user_id", s.missing_user_id]
    if campaign_clicks:
        yield ["campaign_link", "clicks", campaign_clicks]


@requires("download_aggregates")
def summary_csv(request, slug: str):
    store, _ = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="summary")
    return _csv(f"{slug}-summary.csv", ["section", "name", "value"], _summary_rows(store))


@requires("export_people")
def attribution_csv(request, slug: str):
    store, _ = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="attribution")
    return _csv(f"{slug}-attribution.csv", ATTRIBUTION_HEADER, attribution_rows(store))


@requires("export_people")
def clickers_csv(request, slug: str):
    store, _ = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="clickers")
    return _csv(f"{slug}-clickers.csv", CLICKERS_HEADER, clicker_rows(store))


def _expiry(value):
    """Kavenegar's expiry: a unix time (as int or text), shown as a date."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), timezone.utc)
    return None


@never_cache
@requires("view_campaigns")
def status(request):
    """The provider account, Shlink and the worker, asked now (read only)."""
    engine = Engine()
    kavenegar: dict = {}
    try:
        sender = engine.sender()
        info = sender.account_info()
        kavenegar = {"ok": True, "credit": info.remaining_credit, "expires": _expiry(info.expire_date),
                     "type": info.type}
        try:
            config = sender.account_config()
            kavenegar.update(debug_mode=config.debug_mode, resend_failed=config.resend_failed)
        except SendError:
            pass  # settings can't be read: the page says nothing about them
    except RuntimeError:
        kavenegar = {"problem": "no_key"}
    except HaltError as e:
        kavenegar = {"problem": "refused", "text": stop_reason("account_refused", {"code": e.status_code})}
    except SendError as e:
        kavenegar = {"problem": "unreachable", "code": e.status_code}

    shlink: dict
    try:
        version = engine.link_client().health()
        shlink = {"ok": version is not None, "version": version}
    except RuntimeError:
        shlink = {"problem": "no_key"}
    except ShlinkError as e:
        shlink = {"problem": "unreachable", "code": e.status}

    return render(request, "reports/status.html", {
        "kavenegar": kavenegar,
        "shlink": shlink,
        "worker_alive": worker_alive(),
        "worker_seen": last_seen(),
        "queued": Job.objects.filter(state=Job.State.QUEUED).count(),
        "running": Job.objects.filter(state=Job.State.RUNNING).count(),
        "checked_at": dj_timezone.now(),
    })
