"""What the activity page calls each recorded action, and how it
describes the details — in Persian, never the raw key=value."""
from datetime import datetime

from django.utils.html import format_html
from django.utils.translation import gettext as gettext_now
from django.utils.translation import gettext_lazy as _

from ..accounts.terms import ROLE_LABELS
from ..campaigns.terms import SETTING_NAMES
from ..dashboard.terms import SUBMISSION_STATUS
from ..jobs.models import Job
from ..dashboard.templatetags.fa import fa_digits, fa_number, jalali
from ..privacy import mask_phone

ACTION_LABELS = {
    "login": _("Signed in"),
    "logout": _("Signed out"),
    "login_failed": _("Failed sign-in"),
    "2fa_enrolled": _("Two-step verification set up"),
    "2fa_verified": _("Two-step code confirmed"),
    "2fa_failed": _("Wrong two-step code"),
    "2fa_reset": _("Two-step verification reset"),
    "user_created": _("User created"),
    "role_changed": _("Role changed"),
    "user_deactivated": _("Account deactivated"),
    "user_activated": _("Account activated"),
    "password_changed": _("Password changed"),
    "password_reset": _("Password reset by an admin"),
    "audit_exported": _("Activity log downloaded"),
    "segment_uploaded": _("Segment uploaded"),
    "segment_mapped": _("Segment columns chosen"),
    "segment_deleted": _("Segment deleted"),
    "segment_downloaded": _("Segment downloaded"),
    "segment_replaced": _("Segment's file replaced"),
    "suppression_added": _("Added to the suppression list"),
    "suppression_removed": _("Removed from the suppression list"),
    "test_number_changed": _("Test SMS number changed"),
    "campaign_created": _("Campaign created"),
    "campaign_changed": _("Campaign settings changed"),
    "template_saved": _("Template saved"),
    "template_deleted": _("Template removed from the library"),
    "test_requested": _("Test SMS requested"),
    "test_approved": _("Test SMS approved"),
    "test_rejected": _("Test SMS rejected"),
    "send_started": _("Sending started"),
    "send_scheduled": _("Sending scheduled"),
    "send_unscheduled": _("Schedule cancelled"),
    "campaign_duplicated": _("Campaign duplicated"),
    "segment_switched": _("Campaign moved to another segment"),
    "message_unlocked": _("Message opened for a change, after a backup"),
    "recipients_requeued": _("Recipients queued again"),
    "audience_created": _("Segment made from a report"),
    "conversions_imported": _("Conversions imported"),
    "api_token_created": _("API token issued"),
    "api_token_revoked": _("API token revoked"),
    "api_called": _("Attribution API called"),
    "system_settings_changed": _("System settings changed"),
    "backup_made": _("Backup made"),
    "numbers_removed": _("Old phone numbers removed"),
    "backup_verified": _("Backup checked"),
    "campaign_purged": _("Campaign and its records deleted"),
    "campaign_adopted": _("CLI campaign brought to the dashboard"),
    "campaign_imported": _("Campaign made from a CLI profile"),
    "sending_held": _("All sending held"),
    "sending_released": _("Hold on sending lifted"),
    "preset_created": _("Preset made"),
    "preset_changed": _("Preset changed"),
    "preset_archived": _("Preset put away"),
    "preset_restored": _("Preset brought back"),
    "job_requested": _("Update requested"),
    "job_paused": _("Paused"),
    "job_resumed": _("Resumed"),
    "job_cancelled": _("Cancelled"),
    "phone_revealed": _("Phone number shown"),
    "number_looked_up": _("Number looked up across campaigns"),
    "report_downloaded": _("Report downloaded"),
}
# Which download, for "report_downloaded".
DOWNLOADS = {
    "summary": _("summary"),
    "attribution": _("attribution, no phone numbers"),
    "clickers": _("people who clicked, with phone numbers"),
    "recipients": _("recipients, filtered, with phone numbers"),
    "failed": _("rejected rows, with phone numbers"),
}


def _who(name: str):
    return format_html('<bdi dir="ltr">{}</bdi>', name)


def describe(event) -> str:
    """One short Persian line about an event's details ("" when none)."""
    d = event.detail or {}
    target = d.get("target")
    if event.action == "login" and d.get("remembered"):
        return gettext_now("kept signed in on this Mac for 30 days")
    if event.action == "role_changed":
        return format_html(
            gettext_now("{who}: from {before} to {after}"), who=_who(target),
            before=ROLE_LABELS.get(d.get("before"), ROLE_LABELS[None]),
            after=ROLE_LABELS.get(d.get("after"), ROLE_LABELS[None]),
        )
    if event.action == "user_created":
        return format_html(
            gettext_now("{who}, role: {role}"), who=_who(target),
            role=ROLE_LABELS.get(d.get("role"), ROLE_LABELS[None]),
        )
    if event.action in ("2fa_reset", "user_activated", "user_deactivated", "password_reset"):
        return _who(target)
    if event.action in ("segment_uploaded", "segment_deleted", "segment_downloaded", "segment_replaced"):
        return _who(d.get("segment", ""))
    if event.action in ("template_saved", "template_deleted"):
        return _who(d.get("name", ""))
    if event.action in ("api_token_created", "api_token_revoked"):
        return format_html("{} · {}", d.get("name", ""), _who(f"{d.get('prefix', '')}…"))
    if event.action in ("backup_made", "backup_verified", "campaign_purged") and d.get("backup"):
        return _who(d["backup"])
    if event.action == "system_settings_changed" and d.get("frequency_cap"):
        cap = d["frequency_cap"]
        return format_html(gettext_now("frequency cap: from {before} to {after}"),
                           before=_who(cap.get("before", "")), after=_who(cap.get("after", "")))
    if event.action == "system_settings_changed" and d.get("backups"):
        change = d["backups"]
        return format_html(gettext_now("daily backup: from {before} to {after}"),
                           before=_who(change.get("before", "")), after=_who(change.get("after", "")))
    if event.action == "system_settings_changed" and d.get("credit_floor"):
        floor = d["credit_floor"]
        shown = {k: fa_number(v) if isinstance(v, int) else "—" for k, v in floor.items()}
        return format_html(gettext_now("credit warning: from {before} to {after}"),
                           before=shown.get("before", "—"), after=shown.get("after", "—"))
    if event.action == "system_settings_changed" and (d.get("team_number_added") or d.get("team_number_removed")):
        added = d.get("team_number_added")
        line = gettext_now("team number for test SMS added: {phone}") if added \
            else gettext_now("team number for test SMS removed: {phone}")
        return format_html(line, phone=_who(fa_digits(added or d["team_number_removed"])))
    if event.action == "system_settings_changed" and "second_approver" in d:
        return gettext_now("a second person approves test SMS") if d["second_approver"] \
            else gettext_now("whoever asks for a test SMS may approve it")
    if event.action == "system_settings_changed" and d.get("retention_months"):
        change = d["retention_months"]
        return format_html(gettext_now("phone numbers kept: from {before} to {after} months"),
                           before=fa_number(change.get("before", 0)), after=fa_number(change.get("after", 0)))
    if event.action == "numbers_removed":
        return format_html(
            gettext_now("older than {months} months: {campaigns} campaigns, {segments} segment files, {backups} backups"),
            months=fa_number(d.get("months", 0)), campaigns=fa_number(d.get("campaigns", 0)),
            segments=fa_number(d.get("segments", 0)), backups=fa_number(d.get("backups", 0)),
        )
    if event.action == "api_called":
        return format_html("{} · {}", _who(d.get("path", "")), fa_number(d.get("rows", 0)))
    if event.action == "conversions_imported":
        return format_html(gettext_now("{file}: {count} matched"), file=_who(d.get("file", "")),
                           count=fa_number(d.get("by_ref", 0) + d.get("by_user_id", 0)))
    if event.action == "audience_created":
        return format_html(gettext_now("{segment}: {count}"), segment=_who(d.get("segment", "")),
                           count=fa_number(d.get("count", 0)))
    if event.action == "recipients_requeued":
        return format_html(gettext_now("{status}: {count}"), status=SUBMISSION_STATUS.get(d.get("status"), d.get("status", "")),
                           count=fa_number(d.get("count", 0)))
    if event.action == "message_unlocked":
        return format_html(gettext_now("backup {name}"), name=_who(d.get("backup", "")))
    if event.action == "campaign_duplicated":
        return format_html(gettext_now("from {source}"), source=_who(d.get("source", "")))
    if event.action == "sending_held" and d.get("sends") is not None:
        return format_html(gettext_now("{count} sends stopped"), count=fa_number(d["sends"]))
    if event.action.startswith("preset_") and d.get("preset"):
        return _who(d["preset"])
    if event.action == "sending_released":
        return format_html(gettext_now("{count} sends continued"), count=fa_number(d.get("resumed", 0)))
    if event.action == "campaign_imported":
        return format_html(gettext_now("from profile {profile}"), profile=_who(d.get("profile", "")))
    if event.action == "segment_switched":
        after = " + ".join([d.get("after", ""), *d.get("more", [])])
        return format_html(gettext_now("from {before} to {after}"), before=_who(d.get("before") or "—"),
                           after=_who(after))
    if event.action in ("send_started", "send_scheduled"):
        parts = [str(jalali(datetime.fromisoformat(d["at"])))] if d.get("at") else []
        if d.get("was_scheduled"):
            parts.append(gettext_now("before its scheduled time"))
        if d.get("smoke_test"):
            parts.append(gettext_now("to one recipient first"))
        return "، ".join(parts)
    if event.action == "segment_mapped":
        return format_html(
            gettext_now("{segment}: {valid} valid numbers, {invalid} invalid rows"),
            segment=_who(d.get("segment", "")), valid=fa_number(d.get("valid", 0)),
            invalid=fa_number(d.get("invalid", 0)),
        )
    if event.action == "suppression_added":
        return format_html(gettext_now("{count} numbers"), count=fa_number(d.get("count", 0)))
    if event.action == "report_downloaded":
        return str(DOWNLOADS.get(d.get("kind"), d.get("kind", "")))
    if event.action in ("suppression_removed", "test_number_changed", "phone_revealed", "number_looked_up"):
        return _who(fa_digits(mask_phone(d.get("phone", "")))) if d.get("phone") else ""
    if event.action in ("job_requested", "job_paused", "job_resumed", "job_cancelled"):
        return str(Job.Kind(d["kind"]).label) if d.get("kind") in Job.Kind.values else ""
    if event.action == "campaign_changed":
        names = dict.fromkeys(str(SETTING_NAMES[k]) for k in d.get("changed", []) if k in SETTING_NAMES)
        return "، ".join(names)
    if event.action == "2fa_failed":
        when = gettext_now("while setting up") if d.get("during") == "setup" else gettext_now("while signing in")
        if d.get("throttled"):
            return format_html("{}، {}", when, gettext_now("blocked after repeated wrong codes"))
        return when
    return ""
