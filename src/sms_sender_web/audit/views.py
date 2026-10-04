from django.core.paginator import Paginator
from django.shortcuts import render

from ..accounts.decorators import requires
from .models import AuditEvent
from .terms import ACTION_LABELS, describe


@requires("view_audit_log")
def audit_log(request):
    """Every recorded action, newest first, 100 to a page."""
    page = Paginator(AuditEvent.objects.all(), 100).get_page(request.GET.get("page"))
    rows = [(event, ACTION_LABELS.get(event.action, event.action), describe(event)) for event in page]
    return render(request, "audit/log.html", {"page": page, "rows": rows})
