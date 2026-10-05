"""The admins' system settings page (`manage_settings`)."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

import logging

from ..accounts.decorators import requires
from ..audit.record import record
from ..backup import BackupError
from . import operations
from .models import SystemSettings

logger = logging.getLogger(__name__)

CAP_SMS, CAP_DAYS = range(1, 21), range(1, 91)  # what the form accepts
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _number(data, name: str, allowed: range) -> int | None:
    value = data.get(name, "").strip().translate(_DIGITS)
    return int(value) if value.isdigit() and int(value) in allowed else None


@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def system_settings(request):
    current = SystemSettings.load()
    error = ""
    if request.method == "POST":
        before = str(current.frequency_cap or "off")
        if request.POST.get("cap") == "on":
            sms, days = _number(request.POST, "cap_sms", CAP_SMS), _number(request.POST, "cap_days", CAP_DAYS)
            if sms is None or days is None:
                error = _("Write at most 1 to 20 SMS, in 1 to 90 days.")
            else:
                current.frequency_cap_sms, current.frequency_cap_days = sms, days
        else:
            current.frequency_cap_sms = current.frequency_cap_days = None
        if not error:
            current.updated_by = request.user
            current.save()
            after = str(current.frequency_cap or "off")
            if after != before:
                record("system_settings_changed", request=request, frequency_cap={"before": before, "after": after})
            messages.success(request, _("Saved. It applies to the next check and the next send of every campaign."))
            return redirect("system_settings")
    return render(request, "system/settings.html", {"settings": current, "error": error, "post": request.POST})


@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def backups(request):
    """Back up now, and check a backup (the server's `manage.py backup` and
    `verify_backup`). Restoring stays an operations procedure: it needs
    both services stopped (docs/deploy.md)."""
    if request.method == "POST":
        if request.POST.get("action") == "create":
            try:
                name = operations.back_up_now()
            except (BackupError, OSError):
                logger.exception("backup_failed")
                messages.error(request, _("The backup failed, so nothing was kept. The details are in the system log."))
            else:
                record("backup_made", request=request, backup=name)
                messages.success(request, _("Backed up: %(name)s.") % {"name": name})
        elif request.POST.get("action") == "verify":
            name = request.POST.get("name", "")
            problems = operations.check_backup(name)
            if problems is None:
                messages.error(request, _("There's no such backup."))
            else:
                record("backup_verified", request=request, backup=name, ok=not problems)
                if problems:
                    messages.error(request, _("%(name)s has problems: %(problems)s") % {"name": name, "problems": "; ".join(problems)})
                else:
                    messages.success(request, _("%(name)s is whole: every file matches its fingerprint.") % {"name": name})
        return redirect("backups")
    return render(request, "system/backups.html", {"backups": operations.backups()})
