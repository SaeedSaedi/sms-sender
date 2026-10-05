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
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from sms_sender.allowlist import allowlist
from sms_sender.conversions import ConversionFileError, import_conversions, read_rows
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
from ..dashboard.templatetags.fa import fa_number, status_label
from ..jobs.engine import Engine
from ..jobs.models import Campaign, Job
from ..jobs.sandbox import read_outbox
from ..jobs.worker import last_seen, worker_alive
from ..segments import files as segment_files
from ..system import credit, operations
from ..system.models import SystemSettings
from ..segments.audience import make_audience, suggest_slug
from ..segments.forms import UPLOAD_ERRORS, clean_slug
from ..segments.models import Segment
from ..text import persian_text
from .charts import clicks_chart, funnel
from .terms import (
    AUDIENCE_DONE, AUDIENCE_HINT, AUDIENCE_NAME, AUDIENCES, CHART_BY_DAY, CHART_BY_HOUR, CONVERSIONS_IMPORTED,
)

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


def _conversions(store: StateStore, counts: dict) -> dict | None:
    people, conversions, value = store.conversion_totals()
    if not conversions:
        return None
    sent = counts.get(SENT, 0)
    return {"people": people, "conversions": conversions, "value": round(value),
            "share": round(100 * people / sent) if sent else None}


def _audiences(store: StateStore) -> list[tuple[str, str, int]]:
    """The ready-made audiences that have anyone in them: (label, query, how many)."""
    personal = store.has_personal_links()
    out = []
    for _key, label, params in AUDIENCES:
        if "clicked" in params and not personal:
            continue
        n = store.recipient_total(where=_filters(params)[0])
        if n:
            out.append((label, urlencode(params), n))
    return out


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
    chart = clicks_chart(store.click_hours())
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
        # Ready-made audiences (each a filter), and making a segment from the list.
        "audiences": _audiences(store),
        "audience_name": str(AUDIENCE_NAME) % {"campaign": campaign.name if campaign else slug},
        "audience_slug": suggest_slug(slug),
        "audience_hint": say(AUDIENCE_HINT, {"n": total}),
        "funnel": funnel(store, counts),
        "conversions": _conversions(store, counts),
        "chart": chart,
        "chart_summary": say(CHART_BY_HOUR if chart.by_hour else CHART_BY_DAY,
                             {"total": chart.total, "peak": chart.peak, "when": chart.peak_label}) if chart else "",
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
    store, _campaign = _store(slug)
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
    store, _campaign = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="summary")
    return _csv(f"{slug}-summary.csv", ["section", "name", "value"], _summary_rows(store))


@requires("export_people")
def attribution_csv(request, slug: str):
    store, _campaign = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="attribution")
    return _csv(f"{slug}-attribution.csv", ATTRIBUTION_HEADER, attribution_rows(store))


@requires("export_people")
def clickers_csv(request, slug: str):
    store, _campaign = _store(slug)
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


@requires("edit_campaigns")
@require_POST
def audience(request, slug: str):
    """A new segment from the recipients the list's filters match."""
    store, campaign = _store(slug)
    where, filters = _filters(request.POST)
    back = f"{request.path.rsplit('audience/', 1)[0]}?{urlencode(filters)}#recipients"
    new_slug = request.POST.get("segment_slug", "").strip()
    try:
        clean_slug(new_slug)
    except ValidationError as e:
        messages.error(request, e.messages[0])
        return redirect(back)
    if Segment.objects.filter(slug=new_slug).exists():
        messages.error(request, _("A segment with this short name already exists. Choose another."))
        return redirect(back)
    if not store.recipient_total(where=where):
        messages.error(request, _("No recipient matches these filters."))
        return redirect(back)
    name = persian_text(request.POST.get("name", "").strip())[:200] or new_slug
    made = make_audience(store, where, slug=new_slug, name=name, user=request.user)
    record("audience_created", request=request, campaign=slug, segment=new_slug, filters=filters,
           count=made.rows, columns=made.columns)
    messages.success(request, say(AUDIENCE_DONE, {"n": made.rows}))
    return redirect("segment_detail", slug=new_slug)


@requires("update_campaigns")
@require_POST
def conversions(request, slug: str):
    """Conversions from the business's own records, by CSV: matched to the
    recipients who were sent the SMS, by the link's r or by user ID."""
    store, _campaign = _store(slug)
    back = redirect(f"{request.path.rsplit('conversions/', 1)[0]}#conversions")
    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, _("Choose a CSV file."))
        return back
    if upload.size > segment_files.MAX_BYTES:
        messages.error(request, UPLOAD_ERRORS["too_big"])
        return back
    try:
        table = segment_files.parse(upload.read())
        found, invalid = read_rows(table.rows[0], table.rows[1:])
    except segment_files.UploadError as e:
        messages.error(request, UPLOAD_ERRORS[e.code])
        return back
    except ConversionFileError:
        messages.error(request, _("The file needs a column named r (the link's reference) or user_id."))
        return back
    result = import_conversions(store, found, batch=f"{upload.name} {dj_timezone.now().isoformat()}", invalid=invalid)
    record("conversions_imported", request=request, campaign=slug, file=upload.name, rows=result.rows,
           by_ref=result.by_ref, by_user_id=result.by_user_id, unmatched=result.unmatched,
           duplicates=result.duplicates, invalid=result.invalid)
    messages.success(request, say(CONVERSIONS_IMPORTED, {
        "added": result.added, "by_ref": result.by_ref, "by_user_id": result.by_user_id,
        "unmatched": result.unmatched, "duplicates": result.duplicates, "invalid": result.invalid,
    }))
    return back


@requires("export_people")
def recipients_csv(request, slug: str):
    """The recipients list with its filters, numbers in full."""
    store, _campaign = _store(slug)
    where, filters = _filters(request.GET)
    record("report_downloaded", request=request, campaign=slug, kind="recipients", filters=filters)
    return _csv(f"{slug}-recipients.csv", RECIPIENTS_HEADER, _recipient_rows(store, where))


@requires("export_people")
def failed_csv(request, slug: str):
    """The CLI's `export-failed`: the rows Kavenegar rejected and the input
    rows that weren't valid, to fix and feed again."""
    store, _campaign = _store(slug)
    record("report_downloaded", request=request, campaign=slug, kind="failed")
    rows = ([r["phone"], r["raw"], r["status_code"], r["attempts"], r["last_error"]]
            for r in store.iter_failed_permanent())
    return _csv(f"{slug}-failed.csv", list(StateStore.FAILED_HEADER), rows)


@requires("view_campaigns")
def analytics(request):
    """Every campaign side by side (the CLI's and the dashboard's): accepted,
    delivered, clicked, cost. Read only; numbers only, no people."""
    rows = []
    names = dict(Campaign.objects.values_list("slug", "name"))
    for path in sorted(Path(django_settings.SMS_SENDER_DB_DIR).glob("*.db")):
        if not SLUG_RE.match(path.stem):
            continue
        store = StateStore(path)
        counts = store.display_counts()
        sent = counts.get("sent", 0)
        segments, campaign_clicks = click_report(store)
        personal = store.has_personal_links()
        clicked = sum(s.clicked or 0 for s in segments) if personal else None
        clicks = sum(s.clicks for s in segments) + campaign_clicks
        delivered = store.delivery_counts().get(10, 0)
        cost = store.total_cost()
        rows.append({
            "slug": path.stem, "name": names.get(path.stem) or store.get_meta("campaign") or path.stem,
            "sent": sent, "delivered": delivered, "delivered_share": round(100 * delivered / sent) if sent else None,
            "clicked": clicked, "clicked_share": round(100 * clicked / sent) if sent and clicked is not None else None,
            "clicks": clicks, "cost": cost, "per_click": round(cost / clicks) if clicks else None,
            "last": store.last_sent_at(),
        })
    rows.sort(key=lambda r: r["last"] or 0, reverse=True)
    sent = sum(r["sent"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    return render(request, "reports/analytics.html", {
        "rows": rows,
        "totals": {"sent": sent, "clicks": clicks, "cost": cost, "per_click": round(cost / clicks) if clicks else None,
                   "delivered": sum(r["delivered"] for r in rows)},
    })


# Kavenegar reports an account that never expires as 9999-12-31, Tehran time
# (expiredate 253402201800). No Solar Hijri date reaches that far.
NEVER_EXPIRES_YEAR = 9000


def _expiry(value) -> tuple[datetime | None, bool]:
    """Kavenegar's expiry, a unix time (as int or text): (the date, False);
    (None, True) for an account that never expires; (None, False) when
    Kavenegar says nothing."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            when = datetime.fromtimestamp(int(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None, True
        return (None, True) if when.year >= NEVER_EXPIRES_YEAR else (when, False)
    return None, False


@never_cache
@requires("view_campaigns")
def status(request):
    """The provider account, Shlink and the worker, asked now (read only)."""
    engine = Engine()
    kavenegar: dict = {}
    try:
        sender = engine.sender()
        info = sender.account_info()
        credit.record(info.remaining_credit)  # what the campaign list's warning reads
        expires, never_expires = _expiry(info.expire_date)
        kavenegar = {"ok": True, "credit": info.remaining_credit, "expires": expires,
                     "never_expires": never_expires, "type": info.type}
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
    if kavenegar.get("problem"):
        credit.record(None, kavenegar["problem"])

    shlink: dict
    try:
        version = engine.link_client().health()
        shlink = {"ok": version is not None, "version": version}
    except RuntimeError:
        shlink = {"problem": "no_key"}
    except ShlinkError as e:
        shlink = {"problem": "unreachable", "code": e.status}

    outbox = read_outbox() if django_settings.SANDBOX else []
    system = SystemSettings.load()
    allowed = None if django_settings.SANDBOX else allowlist()
    sends = Job.objects.filter(kind=Job.Kind.SEND, state__in=(Job.State.QUEUED, Job.State.RUNNING)).count()
    return render(request, "reports/status.html", {
        "hold_confirm": _(
            "Hold all sending? %(n)s sends on their way or waiting stop, and no SMS goes out until the hold is lifted."
        ) % {"n": fa_number(sends)},
        "outbox_count": len(outbox),
        "outbox_latest": outbox[-5:][::-1],
        "kavenegar": kavenegar,
        "shlink": shlink,
        "worker_alive": worker_alive(),
        "worker_seen": last_seen(),
        "queue": operations.queue(),
        "version": operations.version(),
        "last_backup": operations.last_backup(),
        "backup_at": None if system.backup_hour is None else f"{system.backup_hour:02d}:00",
        "backup_keep": say(_("keeps the newest {n}"), {"n": system.backup_keep}),
        "backup_overdue": operations.backup_overdue(dj_timezone.now(), system.backup_hour,
                                                    operations.newest_backup_at()),
        "allowlist": allowed,
        "allowed_numbers": sorted(allowed.numbers) if allowed is not None else [],
        "checked_at": dj_timezone.now(),
    })
