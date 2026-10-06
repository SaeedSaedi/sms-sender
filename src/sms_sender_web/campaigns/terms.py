"""The engine's keys in Persian (spec 4.11): why a run stopped, why an
action can't be done, why a token or link setting is refused. The engine
itself writes English for the CLI and the logs; none of it is shown here."""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..dashboard.templatetags.fa import fa_digits, fa_number

STOP_REASONS = {
    "outside_window": _("It's outside the sending window ({start} to {end}, Tehran time); it's {now} now. Try again inside it."),
    "window_closed": _("The sending window ({start} to {end}) ended. Sending continues by itself when it opens again, from where it stopped."),
    "not_enough_credit": _("Not enough credit: about {estimate} rials are needed for {recipients} SMS, and {credit} rials are left."),
    "no_credit": _("The Kavenegar account has no credit left ({credit} rials). Top it up before sending."),
    "debug_mode": _("The Kavenegar account is in debug mode, so no SMS would be delivered. Turn it off in the Kavenegar panel."),
    "account_refused": _("Kavenegar returned error {code} on the account check: {meaning}"),
    "provider_halt": _("Kavenegar returned error {code}: {meaning} Sending stopped."),
    "links_refused": _("The short-link service refused to make the links (code {status})."),
    "links_failed": _("Not every short link could be made. Try again."),
    "test_needs_recipient": _("The test SMS takes its token values from a recipient, and there's none to send to."),
    "test_refused": _("Kavenegar returned error {code} for the test SMS: {meaning}"),
    "test_failed": _("Kavenegar returned error {code} for the test SMS: {meaning}"),
    "provider_unreachable": _("Kavenegar didn't answer. Try again in a moment."),
    "busy": _("Another process is sending this campaign right now."),
    "settings_mismatch": _("This campaign's settings don't match what it sent before."),
    "not_approved": _("The settings changed after the test SMS was approved, so nothing was sent. Send a new test SMS."),
    "input_unreadable": _("The segment's file couldn't be read."),
    "crashed": _("The run stopped on an unexpected error. The details are in the system log."),
    "given_up": _("The worker stopped during this job {attempts} times, so it was given up."),
    "recipients_not_allowed": _("Restricted sending: {count} recipients aren't allowed numbers, so nothing was sent. Until it's lifted, only the numbers in SMS_SENDER_ALLOWED_NUMBERS get SMS."),
    "test_number_not_allowed": _("Restricted sending: your number for test SMS isn't an allowed number, so the test SMS wasn't sent."),
    "allowlist_invalid": _("Restricted sending is set wrongly: SMS_SENDER_ALLOWED_NUMBERS holds something that isn't a phone number, so nothing is sent."),
}
_GENERIC_STOP = _("A check before sending failed.")

# What Kavenegar's codes mean (kavenegar.com/rest.html, checked 2026-10-03):
# the account problems that stop a run, and the request problems a test SMS
# can hit. Shown after the code, so the operator knows what to fix.
KAVENEGAR_CODES = {
    401: _("The Kavenegar account is disabled."),
    403: _("The Kavenegar API key isn't valid."),
    407: _("This account can't use this Kavenegar service (for example, the server's IP isn't on the allowed list)."),
    410: _("The server's IP isn't allowed for this account."),
    416: _("The server's IP isn't allowed for this account."),
    429: _("The server's IP isn't allowed for this account."),
    418: _("The account's credit isn't enough."),
    420: _("Links in the SMS text are blocked for this account."),
    426: _("This needs Kavenegar's advanced service."),
    427: _("The sender line needs a higher access level."),
    501: _("This account may only send test SMS to its owner's number."),
    411: _("The recipient's number isn't valid."),
    413: _("The text is empty or too long."),
    422: _("The text has characters Kavenegar doesn't accept."),
    424: _("The template wasn't found, or isn't approved yet."),
    431: _("A token has a space, “_” or a line break that Kavenegar doesn't accept."),
    432: _("The template's text has no code."),
}
_UNKNOWN_CODE = _("Kavenegar didn't say more.")

CONFLICTS = {
    "held": _("All sending is held by an admin. It can start again once the hold is lifted."),
    "busy": _("Another process is using this campaign right now. Try again in a moment."),
    "no_test_number": _("First set your own mobile number on “My account”: test SMS go only there."),
    "send_active": _("This campaign is sending; a test SMS can't run at the same time."),
    "test_active": _("A test SMS is still running. Wait until it finishes."),
    "not_approved": _("Send a test SMS and approve it first."),
    "settings_changed": _("The settings changed after the test SMS. Send a new test SMS and approve it."),
    "not_decidable": _("This test SMS can't be approved or rejected now."),
    "not_scheduled": _("This send isn't waiting for a set time any more."),
    "send_on_its_way": _("A send is on its way. Change the list after it ends."),
    "not_needed": _("Nothing has gone out yet, so the settings can change as they are."),
    "requeue_while_sending": _("A send is on its way. Queue recipients again after it ends."),
}

# Queueing a status again (views.campaign_requeue): what it means, said
# with the number before anyone confirms.
REQUEUE_CONFIRM = {
    "failed_permanent": _("{n} recipients Kavenegar rejected go back in the queue. Fix the cause first (the template, a token), or they'll be rejected again."),
    "unknown": _("{n} recipients may already have the SMS. Queued without checking, they can get a second one; “Reconcile with Kavenegar” checks first."),
    "needs_review": _("{n} recipients may already have the SMS: Kavenegar listed more than one message. Queued again, they can get a second one."),
    "suppressed": _("{n} recipients are on the suppression list. The next send leaves them out again unless they're taken off it first."),
    "invalid": _("{n} numbers came with two different user IDs. Queue them again only after fixing the IDs at the source."),
    "cancelled": _("{n} cancelled recipients go back in the queue for the next send."),
    "sent": _("{n} recipients already got this SMS. Each gets a second one with the next send. Everything is backed up first."),
}
REQUEUED = _("{n} recipients are back in the queue. They go out with the next send.")

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
    if name == "meaning":
        return str(value)
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


def code_meaning(code) -> str:
    return str(KAVENEGAR_CODES.get(code, _UNKNOWN_CODE))


def stop_reason(key: str | None, fields: dict | None) -> str:
    """'' for no reason (a run that ended normally, or the operator stopped it)."""
    if not key:
        return ""
    fields = dict(fields or {})
    if "code" in fields:
        if fields["code"] is None:  # no answer at all: there's no code to explain
            key = "provider_unreachable"
        else:
            fields["meaning"] = code_meaning(fields["code"])
    return fill(STOP_REASONS.get(key, _GENERIC_STOP), fields)

CHECK_PROBLEMS = {
    "segment_missing": _("The campaign's segment isn't ready or its file is missing."),
    "more_segment_missing": _("One of the more segments isn't ready, or its file is missing."),
    "no_template": _("The campaign has no template."),
    "columns_missing": _("A column the tokens use isn't in the segment's file."),
    "nobody_to_send": _("Nobody is left to send to."),
    "bad_destination": _("The short link's address isn't allowed."),
    "allowlist_invalid": _("Restricted sending is set wrongly: SMS_SENDER_ALLOWED_NUMBERS holds something that isn't a phone number, so nothing is sent."),
}

# The check's lines (present.checklist).
CHECKLIST = {
    "list": _("Segment"),
    "lists": _("Segments"),
    "valid": _("{n} valid numbers."),
    "template": _("Template"),
    "text_known": _("Its text is in the library."),
    "text_unknown": _("Its text isn't in the library, so the message can't be shown here."),
    "tokens": _("Tokens"),
    "tokens_filled": _("Every token the text uses is filled."),
    "link": _("Short link"),
    "window": _("Sending window (Tehran time)"),
    "window_open": _("Open now."),
    "window_closed": _("Closed now: a test SMS or a send waits for it to open."),
    "recipients": _("Recipients"),
    "to_send": _("{n} to send."),
    "restricted": _("Restricted sending"),
    "restricted_ok": _("Every recipient is an allowed number."),
    "restricted_blocks": _("{n} recipients aren't allowed numbers: the test SMS works, but the send would be refused."),
}
CHECK_STATES = {"ok": _("Done:"), "warn": _("Warning:"), "fail": _("Problem:")}

# Importing the CLI's profiles (profiles.py): why a profile can't be one,
# and why the file can't be read.
IMPORT_PROBLEMS = {
    "not_a_table": _("It isn't a table of settings."),
    "no_template": _("It has no template."),
    "bad_template": _("Its template's name isn't one Kavenegar takes."),
    "bad_token": _("A fixed token value breaks Kavenegar's rules."),
    "bad_token_column": _("A token column isn't written as TOKEN=COLUMN."),
    "token_twice": _("A token is filled both with a fixed value and with a column."),
    "bad_value_map": _("A value translation isn't written as COLUMN:FROM=TO, or no token uses its column."),
    "bad_link": _("Its short link's settings can't be used."),
}
IMPORT_FILE_ERRORS = {
    "too_big": _("The file is too big for a profiles file."),
    "not_utf8": _("The file isn't UTF-8 text."),
    "not_toml": _("The file isn't valid TOML."),
    "no_profiles": _("The file has no [profile.…] tables."),
    "missing": _("Choose the profiles file first."),
    "expired": _("The file read earlier is gone. Choose it again."),
}

# Setting names, for the activity log's "settings changed" line.
SETTING_NAMES = {
    "segment": _("segment"),
    "more_segments": _("more segments"),
    "template": _("template"),
    "tokens": _("tokens"),
    "token_columns": _("tokens"),
    "value_maps": _("value translations"),
    "links": _("short link"),
    "send_window": _("sending window"),
    "rate": _("sending rate"),
    "workers": _("parallel sends"),
    "max_attempts": _("advanced settings"),
    "timeout": _("advanced settings"),
    "backoff_max": _("advanced settings"),
    "link_rate": _("advanced settings"),
}

# ---------- the campaign's stage (lifecycle.py) ----------

STAGES = {
    "draft": _("Draft"),
    "ready": _("Ready for a test"),
    "testing": _("Testing"),
    "awaiting": _("Awaiting approval"),
    "approved": _("Ready to send"),
    "scheduled": _("Scheduled"),
    "sending": _("Sending"),
    "paused": _("Paused"),
    "stopped": _("Stopped with an error"),
    "completed": _("Completed"),
    "cancelled": _("Cancelled"),
}
# Campaigns the CLI made: the dashboard only has their records.
CLI_CAMPAIGN = _("Made with the command line")

STEPS = (
    _("Message and list"),
    _("Check and preview"),
    _("Test SMS"),
    _("Send"),
    _("Results"),
)

NEXT_STEP = {
    "draft": _("Choose the segment, the template and what fills its tokens."),
    "ready": _("Check the list, then send a test SMS to yourself."),
    "testing": _("The test SMS is on its way to your number."),
    "awaiting": _("Check the test SMS on your phone, then approve it or reject it."),
    "approved": _("Everything is ready. Start sending when you want."),
    "scheduled": _("Sending starts at the set time."),
    "sending": _("Sending. You can pause or cancel at any time."),
    "paused": _("Paused. Resume when you're ready."),
    "paused_by_window": _("Paused: the sending window closed. Sending continues by itself when it opens again."),
    "stopped": _("Sending stopped on an error. Fix it, then continue: nobody gets the SMS twice."),
    "completed": _("Sending finished. The results and what's left to do are below."),
    "cancelled": _("Cancelled. Recipients who hadn't got the SMS won't get it."),
}

# ---------- what the engine noted during a job (JobEvent keys) ----------

NOTES = {
    "links_ready": _("Short links ready: {needed} ({created} made now)."),
    "account": _("Kavenegar account credit: {credit} rials."),
    "resend_failed_on": _("Kavenegar resends undelivered SMS once by itself (resend failed is on)."),
    "cost_estimate": _("Estimated cost: {count} SMS × {per_sms} rials = {estimate} rials; credit {credit} rials."),
    "test_tokens_from": _("The test SMS uses the tokens of {phone}."),
    "test_sending": _("Sending the test SMS to {phone}."),
    "test_sent": _("The test SMS was sent."),
    "reconciled": _("Checked {checked} unknown outcomes with Kavenegar: {sent} had been sent, {requeued} had not, {needs_review} need review."),
    "smoke_sending": _("Sending to one recipient first: {phone}."),
    "smoke_passed": _("The first recipient's SMS was accepted; sending to the rest."),
    "window_closed": _("The sending window closed."),
    "window_resumed": _("The sending window opened; sending continues."),
    "settings_changed": _("The campaign's settings changed: {changed}."),
    "orphans": _("{n} recipients were mid-send when the last run stopped: they're marked unknown and not sent again."),
    "suppressed": _("{n} recipients are on the suppression list and won't be sent."),
    "capped": _("{n} recipients already got {sms} SMS in the last {days} days (the frequency cap) and won't be sent this time."),
    "not_in_input": _("{n} queued recipients aren't in the segment's file, so they were skipped."),
    "missing_user_id": _("{n} recipients have no user ID: they're sent, and reported as missing user ID."),
    "user_id_conflicts": _("{n} numbers came with two different user IDs and won't be sent."),
}

# ---------- what a job did (the history) ----------

JOB_RESULTS = {
    "reconcile": _("Found sent {sent} · Safe to send again {requeued} · Needs review {needs_review} · Not checked yet {deferred}"),
    "delivery": _("Checked {checked} · New delivery statuses {updated}"),
    "clicks": _("Links {links} · Clicks {clicks}"),
}
TOP_ERROR = _("Error {code}: {meaning}")
TOP_ERROR_NO_CODE = _("An error Kavenegar didn't explain")

# What's left to do after a send (the results step).
FOLLOWUPS = {
    "unknown": _("{n} recipients' outcome is unknown: they're checked with Kavenegar at the next run, or you can check now."),
    "not_sent": _("{n} recipients weren't sent (for example, Kavenegar was busy). You can send to them again."),
    "needs_review": _("{n} recipients need review: Kavenegar's records weren't clear, so an admin decides."),
    "rejected": _("Kavenegar rejected {n} recipients; the most common reasons are above."),
}

# A message's length (campaigns/message.py), as people read it.
# Who an alert reaches, in the composer (plan 06, L3).
COUNTS = {
    "to_send": _("{n} will get the SMS"),
    "lists": _("{lists} segments · {n} numbers, each counted once"),
    "repeated": _("{n} repeated numbers counted once"),
    "suppressed": _("{n} on the suppression list"),
    "capped": _("{n} over the frequency cap ({cap})"),
    "already": _("{n} already got this alert"),
    "not_allowed": _("{n} aren't allowed numbers (restricted sending)"),
    "cost": _("About {total} rials in all"),
    "invalid": _("{n} rows aren't valid numbers and are skipped"),
}
TEST_HINT = _("It goes to your own number, {phone}. Ctrl+Enter does the same.")
SAMPLE_LINE = _("Recipient {n} of {count}")
SAMPLE_MISSING = _("{phone} isn't among these segments' recipients.")
SERIES = _("{n} alerts")
SERIES_LAST = _("{n} alerts · the last on {date}")
LACKS_COLUMNS = _("It lacks the column {columns}")
LENGTH = _("{chars} characters · {parts} SMS")
COST_EACH = _("About {cost} rials per recipient, from the price of the latest test SMS.")
COST_TOTAL = _("About {cost} rials per recipient, {total} rials for {n}, from the price of the latest test SMS.")
PREVIEW_MISSING = _("The text uses {token}, but nothing fills it: Kavenegar would refuse the SMS.")
PREVIEW_UNUSED = _("{token} is filled, but the template's text doesn't use it.")
PREVIEW_LTR = _("The message starts with a Latin word, so phones show it left to right, with its punctuation out of place. A Persian value (a translation) or a Persian word first keeps it right to left.")

# Before sending: the recipients' links are made first (plan 05 decision 4).
LINKS_AT_SEND = _("When sending starts, the recipients' short links are made first, before any SMS: up to about {minutes} minutes for {n} links.")

# The history's result line for a send that waits for its time, or was
# withdrawn before it began.
SCHEDULED_FOR = _("Starts at {when}.")
WITHDRAWN = _("Taken back before it started: nothing was sent, and no one was cancelled.")
