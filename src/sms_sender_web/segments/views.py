"""Segment pages (spec 4.9): upload a list, choose its columns, see what
the engine will make of it."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings as django_settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from ..accounts.decorators import requires
from ..audit.record import record
from sms_sender.input_loader import SLUG_RE
from sms_sender.state import StateStore

from ..jobs.models import Campaign, Job
from ..jobs.services import ACTIVE
from ..suppression.service import phones_for
from . import files
from .forms import MAPPING_ERRORS, MappingForm, ReplaceForm, UploadForm
from .models import Segment

# Upload → columns → summary: the steps every segment page shows.
STEPS = ((1, gettext_lazy("File")), (2, gettext_lazy("Columns")), (3, gettext_lazy("Summary")))


def segment_steps(request) -> dict:
    """The segment pages' stepper labels (a context processor would be
    global; each view passes `step`)."""
    return {"segment_steps": [(n, str(label)) for n, label in STEPS]}


# Header names that usually hold a user ID: the mapping page preselects one.
_USER_ID_NAMES = {"user_id", "userid", "user id", "user", "id", "uid", "شناسه", "شناسه کاربر"}


@requires("view_campaigns")
def segment_list(request):
    return render(request, "segments/list.html", {"segments": Segment.objects.all()})


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def segment_upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        segment = Segment(
            slug=d["slug"], name=d["name"], original_name=d["file"].name[:255],
            has_header=d["table"].first_row_is_header(), uploaded_by=request.user,
        )
        try:
            with transaction.atomic():
                segment.save()
        except IntegrityError:
            form.add_error("slug", _("A segment with this short name already exists. Choose another."))
        else:
            Segment.folder().mkdir(parents=True, exist_ok=True)
            segment.upload_path.write_bytes(d["raw"])
            record("segment_uploaded", request=request, segment=segment.slug,
                   file=segment.original_name)
            return redirect("segment_map", slug=segment.slug)
    return render(request, "segments/upload.html", {"form": form, "step": 1, **segment_steps(request)})


def _columns(table: files.Table, has_header: bool) -> list[dict]:
    samples = files.preview(table, has_header)
    return [
        {
            "value": str(i),  # compared with the posted choice, a string
            "number": i + 1,
            "header": table.rows[0][i] if has_header else "",
            "samples": [row[i] for row in samples if row[i][0]],
        }
        for i in range(table.width)
    ]


def _guess(table: files.Table, has_header: bool) -> tuple[int, int | None]:
    """The likely phone and user-ID columns, to preselect."""
    data = table.data(has_header)
    first = data[0] if data else []
    phone = next((i for i, cell in enumerate(first) if files.is_phone(cell)), 0)
    user_id = None
    if has_header:
        user_id = next(
            (i for i, name in enumerate(table.rows[0])
             if i != phone and name.strip().casefold() in _USER_ID_NAMES),
            None,
        )
    return phone, user_id


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def segment_map(request, slug: str):
    segment = get_object_or_404(Segment, slug=slug)
    if segment.status == Segment.Status.READY:
        return redirect("segment_detail", slug=slug)
    if not segment.upload_path.exists():
        raise Http404
    table = files.parse(segment.upload_path.read_bytes())
    form = MappingForm(request.POST or None, width=table.width)
    if request.method == "POST" and form.is_valid():
        mapping = form.mapping()
        try:
            header = files.write_prepared(table, mapping, segment.path)
        except files.MappingError as e:
            form.add_error(None, MAPPING_ERRORS[e.code])
        else:
            has_user_id = mapping.user_id is not None
            segment.has_header = mapping.has_header
            segment.columns = header
            segment.user_id_column = header[1] if has_user_id else ""
            segment.token_columns = header[2 if has_user_id else 1:]
            segment.summary = files.summarize(segment.path, segment.user_id_column, phones_for())
            segment.status = Segment.Status.READY
            segment.save()
            segment.upload_path.unlink(missing_ok=True)
            record("segment_mapped", request=request, segment=segment.slug,
                   valid=segment.summary["valid"], invalid=segment.summary["invalid"])
            return redirect("segment_detail", slug=slug)
    has_header = form["has_header"].value() if form.is_bound else segment.has_header
    phone, user_id = _guess(table, has_header)
    if form.is_bound:
        phone, user_id = form["phone"].value(), form["user_id"].value()
    return render(request, "segments/map.html", {
        "segment": segment,
        "form": form,
        "has_header": has_header,
        "columns": _columns(table, has_header),
        "phone": "" if phone is None else str(phone),
        "user_id": "" if user_id is None else str(user_id),
        "tokens": [str(t) for t in (form["tokens"].value() or [])] if form.is_bound else [],
        "rows": len(table.data(has_header)),
        "step": 2,
        **segment_steps(request),
    })


@requires("view_campaigns")
def segment_detail(request, slug: str):
    segment = get_object_or_404(Segment, slug=slug)
    if segment.status == Segment.Status.DRAFT:
        return render(request, "segments/detail.html", {"segment": segment, "draft": True, "step": 2,
                                                        **segment_steps(request)})
    return render(request, "segments/detail.html", {
        "segment": segment,
        "summary": segment.summary,
        "used_by": list(Campaign.objects.filter(settings__segment=segment.slug)),
        "replace_blocked": _replace_blocked(segment),
        "step": 4,  # every step done: the summary is the page itself
        **segment_steps(request),
    })


def _sent_from(segment: Segment) -> list[str]:
    """The campaigns (the CLI's too) that may have sent to someone from it."""
    found = []
    for path in sorted(Path(django_settings.SMS_SENDER_DB_DIR).glob("*.db")):
        if SLUG_RE.match(path.stem) and StateStore(path).segment_may_have_sent(segment.slug):
            found.append(path.stem)
    return found


def _replace_blocked(segment: Segment) -> str:
    """Why the file can't be replaced now ("" when it can): a campaign has
    sent from it (what went out and what follows would be two lists), or a
    send that uses it is on its way."""
    sent = _sent_from(segment)
    if sent:
        return _("Campaign %(campaigns)s has sent from this segment, so its file stays as it is. For a new list, upload a new segment.") % {"campaigns": "، ".join(sent)}
    if Job.objects.filter(kind=Job.Kind.SEND, state__in=ACTIVE, campaign__settings__segment=segment.slug).exists():
        return _("A send that uses this segment is on its way. Replace the file after it ends.")
    return ""


@requires("export_people")
def segment_download(request, slug: str):
    """The prepared copy, as the engine reads it (numbers in full)."""
    segment = get_object_or_404(Segment, slug=slug, status=Segment.Status.READY)
    if not segment.path.exists():
        raise Http404
    record("segment_downloaded", request=request, segment=segment.slug)
    response = HttpResponse("\ufeff" + segment.path.read_text(encoding="utf-8"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{segment.slug}.csv"'
    return response


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def segment_replace(request, slug: str):
    """A new file for a segment nobody has been sent from yet. It goes
    through the columns step again, and campaigns using the segment need a
    new test SMS (the version is part of what their approval covered)."""
    segment = get_object_or_404(Segment, slug=slug, status=Segment.Status.READY)
    blocked = _replace_blocked(segment)
    if blocked:
        messages.error(request, blocked)
        return redirect("segment_detail", slug=slug)
    form = ReplaceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        segment.upload_path.write_bytes(d["raw"])
        segment.original_name = d["file"].name[:255]
        segment.has_header = d["table"].first_row_is_header()
        segment.status = Segment.Status.DRAFT
        segment.version += 1
        segment.save()
        record("segment_replaced", request=request, segment=segment.slug, file=segment.original_name)
        return redirect("segment_map", slug=slug)
    return render(request, "segments/replace.html", {
        "segment": segment, "form": form,
        "used_by": list(Campaign.objects.filter(settings__segment=segment.slug)),
    })


@requires("edit_campaigns")
@require_POST
def segment_delete(request, slug: str):
    segment = get_object_or_404(Segment, slug=slug)
    used_by = list(Campaign.objects.filter(settings__segment=segment.slug).values_list("slug", flat=True))
    if used_by:
        messages.error(request, _("A campaign uses this segment, so it can't be deleted."))
        return redirect("segment_detail", slug=slug)
    segment.delete_files()
    segment.delete()
    record("segment_deleted", request=request, segment=slug)
    messages.success(request, _("The segment and its file were deleted."))
    return redirect("segment_list")
