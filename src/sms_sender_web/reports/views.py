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
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

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
from sms_sender.sendcheck import check_sends
from sms_sender.sender import HaltError, SendError
from sms_sender.shortlink import ShlinkError
from sms_sender.state import DELIVERY_GROUPS, SENT, UNCHECKED, RecipientFilter, StateStore

from ..accounts.decorators import requires
from ..audit.record import record
from ..campaigns.present import say
from ..campaigns.terms import stop_reason
from ..dashboard.terms import CLICK_FILTERS, DELIVERY_FILTERS, DELIVERY_STATUS, STATUS_ORDER
from ..dashboard.templatetags.fa import status_label
from ..jobs.engine import Engine
from ..jobs.models import Campaign, Job
from ..jobs.sandbox import read_outbox
from ..jobs.worker import last_seen, worker_alive

PAGE = 100
MATCHING = _("{n} recipients match these filters.")
SENDS_OK = _("Nobody got it twice: {sms} SMS to {recipients} recipients, test SMS apart ({tests}).")
SENDS_TWICE = _("{n} recipients got it more than once.")
SENDS_MAYBE = _("{n} recipients may have got it twice: a call isn't settled yet. Reconciling with Kavenegar settles it.")
SENDS_UNRECORDED = _("{n} SMS from before calls were recorded can't be checked.")
TWICE_SHOWN = 20
EVERYONE = _("{n} recipients in this campaign.")


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


def _filters(data) -> tuple[RecipientFilter, dict]:
    """The list's filters, from the address (nothing personal is in it), and
    their values for the form and the links."""
    status = data.get("status", "")
    delivery = data.get("delivery", "")
    clicked = data.get("clicked", "")
    values = {
        "status": status if status in STATUS_ORDER else "",
        "segment": data.get("segment", "").strip()[:64],
        "delivery": delivery if delivery in (*DELIVERY_GROUPS, UNCHECKED) else "",
        "clicked": clicked if clicked in ("yes", "no") else "",
        "missing": "1" if data.get("missing") == "1" else "",
    }
    where = RecipientFilter(
        status=values["status"] or None, segment=values["segment"] or None,
        delivery=values["delivery"] or None,
        clicked={"yes": True, "no": False}.get(values["clicked"]), missing_user_id=bool(values["missing"]),
    )
    return where, {k: v for k, v in values.items() if v}


@requires("view_campaigns")
@require_http_methods(["GET", "POST"])
def report(request, slug: str):
    """The campaign's report. The recipients list filters by the address; a
    number is looked up with a POST, so it never appears in a URL or a log."""
    store, campaign = _store(slug)
    where, filters = _filters(request.GET)
    query = request.POST.get("q", "").strip()[:32] if request.method == "POST" else ""
    phone, query_invalid = None, False
    if query:
        try:
            phone = normalize(query)
        except InvalidPhoneError:
            query_invalid = True
        where, filters = RecipientFilter(phone=phone), {}
    total = 0 if query_invalid else store.recipient_total(where=where)
    pages = max(1, -(-total // PAGE))
    try:
        number = min(max(1, int(request.GET.get("page", 1))), pages)
    except ValueError:
        number = 1
    rows = [] if query_invalid else store.recipients_page(limit=PAGE, offset=(number - 1) * PAGE, where=where)
    counts = store.display_counts()
    sends = check_sends(store)
    segments, campaign_clicks = click_report(store)
    stored = store.get_meta("settings")
    last_run = store.get_meta("last_run")
    return render(request, "reports/report.html", {
        "slug": slug,
        "campaign": campaign,
        "name": campaign.name if campaign else (store.get_meta("campaign") or slug),
        "template": json.loads(stored).get("template") if stored else None,
        "last_run_at": json.loads(last_run).get("at") if last_run else None,
        "counts": counts,
        "status_order": STATUS_ORDER,
        "status_choices": [(key, status_label(key), counts[key]) for key in STATUS_ORDER if counts.get(key)],
        "segment_choices": store.segments_in_use(),
        "delivery_choices": DELIVERY_FILTERS,
        "click_choices": CLICK_FILTERS if store.has_personal_links() else (),
        # "Missing user ID" means something only where user IDs are used.
        "has_user_ids": store.has_user_ids() or bool(campaign and (campaign.settings or {}).get("user_id_column")),
        "filters": filters,
        "filter_query": urlencode(filters),
        "matching": say(MATCHING if filters else EVERYONE, {"n": total}),
        "sends": sends,
        "sends_line": (
            say(SENDS_TWICE, {"n": len(sends.twice)}) if sends.twice
            else say(SENDS_MAYBE, {"n": len(sends.maybe_twice)}) if sends.maybe_twice
            else say(SENDS_OK, {"sms": sends.sms, "recipients": sends.recipients, "tests": sends.test_sms})
        ),
        "sends_maybe_line": say(SENDS_MAYBE, {"n": len(sends.maybe_twice)}) if sends.twice and sends.maybe_twice else "",
        "sends_unrecorded": say(SENDS_UNRECORDED, {"n": sends.unrecorded}) if sends.unrecorded else "",
        "twice": [(p.phone, len(p.message_ids)) for p in sends.twice[:TWICE_SHOWN]],
        "twice_more": max(0, len(sends.twice) - TWICE_SHOWN),
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


RECIPIENTS_HEADER = ["phone", "status", "delivery", "segment", "user_id", "clicks", "accepted_at", "cost_rials"]


def _iso(moment: float | None) -> str:
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat(timespec="seconds") if moment else ""


def _recipient_rows(store: StateStore, where: RecipientFilter):
    """The list's rows as they're shown, with the full number: for the
    systems an operator feeds them to."""
    for r in store.iter_recipients(where):
        invalid = r["phone"].startswith("INVALID:")
        delivery = ""
        if r["status"] == SENT:
            delivery = STATUS_NAMES.get(r["delivery_status"], "") if r["delivery_status"] is not None else "not checked"
        yield [
            r["raw"] if invalid else r["phone"], "invalid" if invalid else r["status"], delivery,
            r["segment"] or "", r["user_id"] or "", "" if r["clicks"] is None else r["clicks"],
            _iso(r["sent_at"]), r["cost"] or "",
        ]


@requires("export_people")
def recipients_csv(request, slug: str):
    """The recipients list with its filters, numbers in full."""
    store, _ = _store(slug)
    where, filters = _filters(request.GET)
    record("report_downloaded", request=request, campaign=slug, kind="recipients", filters=filters)
    return _csv(f"{slug}-recipients.csv", RECIPIENTS_HEADER, _recipient_rows(store, where))


@requires("export_people")
def failed_csv(request, slug: str):
    """The CLI's `export-failed`: the rows Kavenegar rejected and the input
    rows that weren't valid, to fix and feed again."""
    store, _ = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="failed")
    rows = ([r["phone"], r["raw"], r["status_code"], r["attempts"], r["last_error"]]
            for r in store.iter_failed_permanent())
    return _csv(f"{slug}-failed.csv", list(StateStore.FAILED_HEADER), rows)


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

    outbox = read_outbox() if django_settings.SANDBOX else []
    return render(request, "reports/status.html", {
        "outbox_count": len(outbox),
        "outbox_latest": outbox[-5:][::-1],
        "kavenegar": kavenegar,
        "shlink": shlink,
        "worker_alive": worker_alive(),
        "worker_seen": last_seen(),
        "queued": Job.objects.filter(state=Job.State.QUEUED).count(),
        "running": Job.objects.filter(state=Job.State.RUNNING).count(),
        "checked_at": dj_timezone.now(),
    })
