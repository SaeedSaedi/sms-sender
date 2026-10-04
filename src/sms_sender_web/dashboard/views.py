from django.shortcuts import render

from .campaigns import list_campaigns
from .terms import STATUS_ORDER


def home(request):
    """Every campaign with its recipients by status (read-only)."""
    return render(request, "dashboard/home.html", {
        "campaigns": list_campaigns(),
        "status_order": STATUS_ORDER,
    })
