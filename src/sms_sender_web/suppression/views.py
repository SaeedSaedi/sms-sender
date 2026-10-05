"""The suppression list page (spec 4.10, 4.12): operators add numbers,
admins remove them, and every change is in the activity log."""
from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from sms_sender.phone import InvalidPhoneError, normalize

from ..accounts.decorators import forbidden, requires
from ..accounts.roles import can
from ..audit.record import record
from ..dashboard.templatetags.fa import fa_number
from ..text import persian_text
from . import service
from .forms import AddForm
from ..jobs.models import Campaign
from .models import Suppression


@requires("add_suppression")
@require_http_methods(["GET", "POST"])
def suppression_list(request):
    form = AddForm(
        request.POST if request.POST.get("action") == "add" else None,
        request.FILES if request.POST.get("action") == "add" else None,
    )
    if request.method == "POST":
        if request.POST.get("action") == "remove":
            if not can(request.user, "remove_suppression"):
                return forbidden(request)
            pk = request.POST.get("entry", "")
            if not pk.isdigit():
                raise Http404
            entry = get_object_or_404(Suppression, pk=pk)
            campaign = entry.campaign.slug if entry.campaign else ""
            entry.delete()
            record("suppression_removed", request=request, campaign=campaign, phone=entry.phone)
            messages.success(request, _("The number was removed from the suppression list."))
            return redirect("suppression")
        if form.is_valid():
            phones, invalid = form.cleaned_data["phones"], form.cleaned_data["invalid"]
            note = persian_text(form.cleaned_data["note"].strip())
            campaign = form.cleaned_data["scope"]
            added = service.add(phones, campaign=campaign, note=note, user=request.user)
            if added:
                record("suppression_added", request=request, campaign=campaign.slug if campaign else "",
                       count=added)
            already = len(phones) - added
            if already:
                messages.success(request, _(
                    "%(added)s numbers were added to the suppression list; %(listed)s were on it already."
                ) % {"added": fa_number(added), "listed": fa_number(already)})
            else:
                messages.success(request, _(
                    "%(added)s numbers were added to the suppression list."
                ) % {"added": fa_number(added)})
            if invalid:
                messages.warning(request, _(
                    "%(invalid)s lines weren't mobile numbers and were skipped."
                ) % {"invalid": fa_number(invalid)})
            return redirect("suppression")

    entries = Suppression.objects.select_related("campaign", "added_by")
    # A number is looked up with a POST, so it never lands in a URL or a log.
    query = request.POST.get("q", "").strip()[:32] if request.POST.get("action") == "find" else ""
    query_invalid = False
    if query:
        try:
            entries = entries.filter(phone=normalize(query))
        except InvalidPhoneError:
            query_invalid = True
            entries = entries.none()
    page = Paginator(entries, 100).get_page(request.GET.get("page"))
    return render(request, "suppression/list.html", {
        "form": form,
        "page": page,
        "query": query,
        "query_invalid": query_invalid,
        "total": Suppression.objects.count(),
        "campaigns": Campaign.objects.order_by("name"),
    })
