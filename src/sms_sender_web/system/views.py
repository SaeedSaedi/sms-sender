"""The admins' system settings page (`manage_settings`) and backups."""
from __future__ import annotations

import logging

from django.conf import settings as django_settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from sms_sender.allowlist import allowlist
from sms_sender.notify import notify_text, redact_target, valid_target
from sms_sender.phone import InvalidPhoneError, normalize
from sms_sender.rate import parse_rate
from sms_sender.window import parse_window

from ..accounts.decorators import requires
from ..accounts.roles import count_who_can
from ..audit.record import record
from ..backup import BackupError
from ..dashboard.templatetags.fa import fa_number
from ..jobs import services
from ..privacy import mask_phone
from . import operations
from .models import TEAM_TEST_MAX, SystemSettings

logger = logging.getLogger(__name__)

CAP_SMS, CAP_DAYS = range(1, 21), range(1, 91)  # what the form accepts
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
TEST_MESSAGE = "sms-sender: a test notification from the dashboard. Sends that end or stop are announced here."


def _number(data, name: str, allowed: range) -> int | None:
    value = data.get(name, "").strip().translate(_DIGITS)
    return int(value) if value.isdigit() and int(value) in allowed else None


def _cap(request, current: SystemSettings) -> str:
    """The frequency cap section; an error, or ""."""
    before = str(current.frequency_cap or "off")
    if request.POST.get("cap") == "on":
        sms, days = _number(request.POST, "cap_sms", CAP_SMS), _number(request.POST, "cap_days", CAP_DAYS)
        if sms is None or days is None:
            return _("Write at most 1 to 20 SMS, in 1 to 90 days.")
        current.frequency_cap_sms, current.frequency_cap_days = sms, days
    else:
        current.frequency_cap_sms = current.frequency_cap_days = None
    current.updated_by = request.user
    current.save()
    after = str(current.frequency_cap or "off")
    if after != before:
        record("system_settings_changed", request=request, frequency_cap={"before": before, "after": after})
    return ""


def _notifications(request, current: SystemSettings) -> str:
    """Add, test or remove a notification target. Targets hold secrets: the
    activity log and the page only ever see them masked."""
    action, targets = request.POST.get("section"), list(current.notify_targets or [])
    if action == "notify_add":
        target = request.POST.get("target", "").strip()
        if not valid_target(target):
            return _("Write slack:https://hooks.slack.com/…, telegram:<bot token>:<chat id>, or an https:// address.")
        if target not in targets:
            targets.append(target)
            current.notify_targets, current.updated_by = targets, request.user
            current.save()
            record("system_settings_changed", request=request, notify_added=redact_target(target))
        return ""
    index = request.POST.get("index", "")
    if not index.isdigit() or int(index) >= len(targets):
        return _("Choose a target from the list.")
    target = targets[int(index)]
    if action == "notify_test":
        if notify_text(target, TEST_MESSAGE):
            messages.success(request, _("Sent. Check that it arrived."))
        else:
            messages.error(request, _("It didn't go through; the details are in the system log."))
        return ""
    targets.pop(int(index))
    current.notify_targets, current.updated_by = targets, request.user
    current.save()
    record("system_settings_changed", request=request, notify_removed=redact_target(target))
    return ""


def _defaults(request, current: SystemSettings) -> str:
    window = request.POST.get("default_window", "").strip().translate(_DIGITS)
    rate = request.POST.get("default_rate", "").strip().translate(_DIGITS)
    try:
        if window:
            parse_window(window)
        if rate:
            parse_rate(rate)
    except ValueError:
        return _("Write the window as HH:MM-HH:MM, and the rate like 10/s, 60/m or 3600/h.")
    before = {"window": current.default_send_window, "rate": current.default_rate}
    current.default_send_window, current.default_rate, current.updated_by = window, rate, request.user
    current.save()
    if before != {"window": window, "rate": rate}:
        record("system_settings_changed", request=request, defaults={"window": window or "-", "rate": rate or "-"})
    return ""


def _credit(request, current: SystemSettings) -> str:
    """The credit warning level, in rials (empty: no warning)."""
    value = request.POST.get("credit_floor", "").strip().translate(_DIGITS).replace(",", "").replace("٬", "")
    if value and not value.isdigit():
        return _("Write the credit in rials, digits only, or leave it empty for no warning.")
    floor = int(value) if value else None
    if floor != current.credit_floor:
        before = current.credit_floor
        current.credit_floor, current.updated_by = floor, request.user
        current.save()
        record("system_settings_changed", request=request,
               credit_floor={"before": before if before is not None else "-", "after": floor if floor is not None else "-"})
    return ""


def _team(request, current: SystemSettings) -> str:
    """The team's numbers for test SMS (plan 06, D1): add one, or remove
    one. Like every number on a page, they're shown and recorded masked."""
    numbers = list(current.team_test_numbers or [])
    if request.POST.get("section") == "team_add":
        try:
            phone = normalize(request.POST.get("team_phone", "").strip())
        except InvalidPhoneError:
            return _("That isn't a valid mobile number.")
        if phone in numbers:
            return ""
        if len(numbers) >= TEAM_TEST_MAX:
            return _("There are already %(n)s numbers: remove one first.") % {"n": fa_number(TEAM_TEST_MAX)}
        numbers.append(phone)
        change = {"team_number_added": mask_phone(phone)}
    else:
        index = request.POST.get("index", "")
        if not index.isdigit() or int(index) >= len(numbers):
            return _("Choose a number from the list.")
        change = {"team_number_removed": mask_phone(numbers.pop(int(index)))}
    current.team_test_numbers, current.updated_by = numbers, request.user
    current.save()
    record("system_settings_changed", request=request, **change)
    return ""


def _approval(request, current: SystemSettings) -> str:
    """Whether someone other than whoever asked for a test SMS approves it
    (plan 06, D3)."""
    second = request.POST.get("second_approver") == "on"
    if second != current.second_approver:
        current.second_approver, current.updated_by = second, request.user
        current.save()
        record("system_settings_changed", request=request, second_approver=second)
    return ""


_HANDLERS = {"cap": _cap, "defaults": _defaults, "credit": _credit, "approval": _approval,
             "team_add": _team, "team_remove": _team}
# The box an error shows in: each form's own.
_ERROR_BOX = {"team_add": "team", "team_remove": "team"}


def _saved(section: str) -> str:
    if section in ("cap", "defaults", "credit", "notify_add", "notify_remove"):
        return _("Saved. It applies to the next check and the next send of every campaign.")
    if section in ("team_add", "team_remove"):
        return _("Saved. It applies to the next test SMS.")
    if section == "approval":
        return _("Saved. It applies to test SMS waiting for a decision too.")
    return ""  # a test notification says how it went itself


@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def system_settings(request):
    current = SystemSettings.load()
    errors: dict[str, str] = {}
    if request.method == "POST":
        section = request.POST.get("section", "cap")
        error = _HANDLERS.get(section, _notifications)(request, current)
        if error:
            errors[_ERROR_BOX.get(section, section if section in _HANDLERS else "notify")] = error
        else:
            if _saved(section):
                messages.success(request, _saved(section))
            return redirect("system_settings")
    # Restricted sending skips the team numbers it doesn't allow (the sandbox isn't bound).
    allowed = None if django_settings.SANDBOX else allowlist()
    return render(request, "system/settings.html", {
        "settings": current, "errors": errors, "post": request.POST,
        "targets": [redact_target(t) for t in current.notify_targets or []],
        "team": [{"phone": phone, "skipped": allowed is not None and not allowed.allows(phone)}
                 for phone in current.team_test_numbers or []],
        "team_max": TEAM_TEST_MAX,
        # With a second approver, one person alone could approve nothing.
        "runners": count_who_can("run_campaigns"),
    })


HELD_MESSAGE = "sms-sender: {who} held all sending on the dashboard. Nothing is sent until an admin lifts the hold."
RELEASED_MESSAGE = "sms-sender: {who} lifted the hold on all sending. {n} paused send(s) continue."


@requires("manage_settings")
@require_POST
def sending_hold(request):
    """An admin's emergency stop (status page): hold all sending, or lift
    the hold. The team's notification targets hear of it either way."""
    who = request.user.get_username()
    if request.POST.get("action") == "release":
        resumed = services.release_sending(request.user)
        record("sending_released", request=request, resumed=resumed)
        messages.success(request, _("The hold is lifted. Sends it paused continue: %(n)s.") % {"n": fa_number(resumed)})
        text = RELEASED_MESSAGE.format(who=who, n=resumed)
    else:
        try:
            sends = services.hold_sending(request.user)
        except OSError:
            logger.exception("hold_marker_failed")
            sends = None
            messages.error(request, _(
                "Sending is held on the dashboard, but the CLI's folder couldn't be marked: "
                "tell whoever sends from the command line. The details are in the system log."
            ))
        record("sending_held", request=request, sends=sends)
        if sends is not None:
            messages.success(request, _(
                "All sending is held. Sends on their way pause within seconds: %(n)s."
            ) % {"n": fa_number(sends)})
        text = HELD_MESSAGE.format(who=who)
    for target in SystemSettings.load().notify_targets or []:
        notify_text(target, text)  # best-effort: never raises
    return redirect("status")


BACKUP_HOURS, BACKUP_KEEP = range(24), range(1, 91)  # what the schedule form accepts


def _backup_schedule(request, current: SystemSettings) -> str:
    """The worker's daily backup: its hour (Tehran time, or off) and how
    many to keep. An error, or ""."""
    hour = request.POST.get("backup_hour", "").strip()
    keep = _number(request.POST, "backup_keep", BACKUP_KEEP)
    if (hour != "off" and _number(request.POST, "backup_hour", BACKUP_HOURS) is None) or keep is None:
        return _("Choose an hour, or off, and keep 1 to 90 backups.")
    before = _schedule_text(current)
    current.backup_hour = None if hour == "off" else _number(request.POST, "backup_hour", BACKUP_HOURS)
    current.backup_keep, current.updated_by = keep, request.user
    current.save()
    after = _schedule_text(current)
    if after != before:
        record("system_settings_changed", request=request, backups={"before": before, "after": after})
    return ""


def _schedule_text(current: SystemSettings) -> str:
    if current.backup_hour is None:
        return "off"
    return f"{current.backup_hour:02d}:00/{current.backup_keep}"


@requires("manage_settings")
@require_http_methods(["GET", "POST"])
def backups(request):
    """Back up now, and check a backup (the server's `manage.py backup` and
    `verify_backup`); and the worker's daily backup. Restoring stays an
    operations procedure: it needs both services stopped (docs/deploy.md)."""
    current = SystemSettings.load()
    if request.method == "POST" and request.POST.get("action") == "schedule":
        error = _backup_schedule(request, current)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, _("Saved. The worker backs up at that hour every day."))
        return redirect("backups")
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
    newest = operations.newest_backup_at()
    return render(request, "system/backups.html", {
        "backups": operations.backups(),
        "settings": current,
        "hours": [(h, f"{h:02d}:00") for h in BACKUP_HOURS],
        "overdue": operations.backup_overdue(timezone.now(), current.backup_hour, newest),
    })
