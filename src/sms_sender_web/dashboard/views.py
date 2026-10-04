from django.shortcuts import render

from ..accounts.decorators import requires
from ..jobs.models import Campaign
from .campaigns import CampaignSummary, list_campaigns
from .terms import STATUS_ORDER


@requires("view_campaigns")
def home(request):
    """Every campaign with its recipients by status: the ones in data/db
    (the CLI's and the dashboard's), and dashboard campaigns that haven't
    sent yet. A dashboard campaign links to its page."""
    managed = {c.slug: c for c in Campaign.objects.all()}
    summaries = list_campaigns()
    on_disk = {s.slug for s in summaries}
    fresh = [
        CampaignSummary(slug=c.slug, name=c.name, template=c.settings.get("template"),
                        counts={}, last_run_at=None)
        for c in sorted(managed.values(), key=lambda c: c.created_at, reverse=True)
        if c.slug not in on_disk
    ]
    rows = [{"summary": s, "campaign": managed.get(s.slug)} for s in fresh + summaries]
    return render(request, "dashboard/home.html", {
        "campaigns": rows,
        "status_order": STATUS_ORDER,
    })
