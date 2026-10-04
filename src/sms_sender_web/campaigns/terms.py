"""The engine's keys in Persian (spec 4.11): why a run stopped, why an
action can't be done, why a token or link setting is refused. The engine
itself writes English for the CLI and the logs; none of it is shown here."""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..dashboard.templatetags.fa import fa_digits, fa_number

STOP_REASONS = {
    "outside_window": _("It's outside the sending window ({start} to {end}, Tehran time); it's {now} now. Try again inside it."),
    "window_closed": _("The sending window ({start} to {end}) ended. Resume inside the next one: it carries on from where it stopped."),
    "not_enough_credit": _("Not enough credit: about {estimate} rials are needed for {recipients} SMS, and {credit} rials are left."),
    "no_credit": _("The Kavenegar account has no credit left ({credit} rials). Top it up before sending."),
    "debug_mode": _("The Kavenegar account is in debug mode, so no SMS would be delivered. Turn it off in the Kavenegar panel."),
    "account_refused": _("Kavenegar refused the account check (code {code})."),
    "provider_halt": _("Kavenegar stopped the sending because of an account problem (code {code})."),
    "links_refused": _("The short-link service refused to make the links (code {status})."),
    "links_failed": _("Not every short link could be made. Try again."),
    "test_needs_recipient": _("The test SMS takes its token values from a recipient, and there's none to send to."),
    "test_refused": _("Kavenegar refused the test SMS because of an account problem (code {code})."),
    "test_failed": _("The test SMS wasn't sent (code {code}). Check the template and its tokens."),
    "busy": _("Another process is sending this campaign right now."),
    "settings_mismatch": _("This campaign's settings don't match what it sent before."),
    "input_unreadable": _("The segment's file couldn't be read."),
    "crashed": _("The run stopped on an unexpected error. The details are in the system log."),
    "given_up": _("The worker stopped during this job {attempts} times, so it was given up."),
}
_GENERIC_STOP = _("A check before sending failed.")

CONFLICTS = {
    "busy": _("Another process is using this campaign right now. Try again in a moment."),
    "no_test_number": _("First set your own mobile number on “My account”: test SMS go only there."),
    "send_active": _("This campaign is sending; a test SMS can't run at the same time."),
    "test_active": _("A test SMS is still running. Wait until it finishes."),
    "not_approved": _("Send a test SMS and approve it first."),
    "settings_changed": _("The settings changed after the test SMS. Send a new test SMS and approve it."),
    "not_decidable": _("This test SMS can't be approved or rejected now."),
}

TOKEN_ISSUES = {
    "too_long": _("At most {max} characters; this is {length}."),
    "line_break": _("A line break or tab isn't allowed."),
    "underscore": _("The “_” character isn't allowed."),
    "too_many_spaces": _("This token allows at most {max} spaces; this has {spaces}."),
}

DESTINATION_ISSUES = {
    "not_https": _("The address must start with https://"),
    "no_domain": _("The address has no domain."),
    "credentials": _("The address must not contain a user name or password."),
    "domain_not_allowed": _("{host} isn't an allowed destination. Allowed: {domains}."),
    "short_link": _("The address is a short link itself."),
    "has_added_params": _("The address already has {params}; the link stage adds those itself."),
}

INVALID_ROWS = {
    "invalid_phone": _("Not a valid mobile number"),
    "conflicting_user_ids": _("Conflicting user IDs"),
    "empty_value": _("A token's column is empty"),
    "unmapped_value": _("A value has no translation"),
    "token_rule": _("A value breaks Kavenegar's token rules"),
}


def _show(name: str, value) -> str:
    """Numbers people read, in Persian digits; names (hosts, params) as they are."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return fa_number(value) if name in ("estimate", "credit", "recipients", "length") else fa_digits(value)
    if name in ("start", "end", "now"):
        return fa_digits(value)
    return str(value)


def fill(template, fields: dict | None) -> str:
    try:
        return str(template).format(**{k: _show(k, v) for k, v in (fields or {}).items()})
    except (KeyError, IndexError):
        return str(_GENERIC_STOP)


def stop_reason(key: str | None, fields: dict | None) -> str:
    """'' for no reason (a run that ended normally, or the operator stopped it)."""
    if not key:
        return ""
    return fill(STOP_REASONS.get(key, _GENERIC_STOP), fields)

CHECK_PROBLEMS = {
    "segment_missing": _("The campaign's segment isn't ready or its file is missing."),
    "no_template": _("The campaign has no template."),
    "columns_missing": _("A column the tokens use isn't in the segment's file."),
    "nobody_to_send": _("Nobody is left to send to."),
    "bad_destination": _("The short link's address isn't allowed."),
}

# Setting names, for the activity log's "settings changed" line.
SETTING_NAMES = {
    "segment": _("segment"),
    "template": _("template"),
    "tokens": _("tokens"),
    "token_columns": _("tokens"),
    "value_maps": _("value translations"),
    "links": _("short link"),
    "send_window": _("sending window"),
    "rate": _("sending rate"),
    "workers": _("parallel sends"),
}
