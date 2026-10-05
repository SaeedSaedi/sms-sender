"""One number across every campaign (R5 of the 2026-10-05 review): who to
answer when someone asks "did I get it, and how many times?". Every
campaign DB in the folder is read, the CLI's too, read only. The number is
searched with a POST, never in a URL, and each lookup is in the activity log."""
from __future__ import annotations

import time
from pathlib import Path

from django.conf import settings as django_settings
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from sms_sender.phone import InvalidPhoneError, normalize
from sms_sender.state import folder_sends_since, number_history

from ..accounts.decorators import requires
from ..audit.record import record
from ..jobs.models import Campaign
from ..suppression.models import Suppression
from ..system.models import SystemSettings


@never_cache
@requires("reveal_phone")
@require_http_methods(["GET", "POST"])
def number_lookup(request):
    if request.method != "POST":
        return render(request, "reports/numbers.html", {})
    typed = request.POST.get("number", "").strip()[:32]
    try:
        phone = normalize(typed)
    except InvalidPhoneError:
        return render(request, "reports/numbers.html", {"typed": typed, "invalid": True})
    folder = Path(django_settings.SMS_SENDER_DB_DIR)
    found = number_history(folder, phone)
    names = dict(Campaign.objects.filter(slug__in=[s.campaign for s in found]).values_list("slug", "name"))
    blocked = list(Suppression.objects.filter(phone=phone).select_related("campaign"))
    cap = SystemSettings.load().frequency_cap
    record("number_looked_up", request=request, phone=phone, campaigns=len(found))
    return render(request, "reports/numbers.html", {
        "typed": typed, "phone": phone,
        "rows": [{"s": s, "name": names.get(s.campaign), "twice": s.sms >= 2} for s in found],
        "sms": sum(s.sms for s in found),
        "tests": sum(s.tests for s in found),
        "campaigns": sum(1 for s in found if s.sms),
        "twice": [s.campaign for s in found if s.sms >= 2],
        "suppressed": blocked,
        "cap": cap,
        "cap_count": folder_sends_since(folder, time.time() - cap.seconds).get(phone, 0) if cap else None,
    })
