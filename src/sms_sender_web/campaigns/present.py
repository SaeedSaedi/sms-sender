"""What the campaign pages show about jobs, in Persian (plan 05, P2): each
job's result in a line, the notes the engine wrote while it ran, the most
common errors, and the summary of a finished send. The engine's English
(JobEvent.text, Job.last_error) never reaches a page."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..dashboard.templatetags.fa import fa_digits, fa_number, jalali
from ..jobs.models import Job
from ..privacy import mask_phone
from .terms import (
    CHECKLIST, CHECK_PROBLEMS, JOB_RESULTS, NOTES, SCHEDULED_FOR, SETTING_NAMES, TOP_ERROR,
    TOP_ERROR_NO_CODE, WITHDRAWN, code_meaning, stop_reason,
)

_CODE = re.compile(r"^\[(\d+)\]")


def _shown(name: str, value) -> str:
    if name == "token":  # an identifier: %token2 keeps its Latin digit
        return str(value)
    if name == "phone":
        return fa_digits(mask_phone(str(value)))
    if name == "changed":
        names = dict.fromkeys(str(SETTING_NAMES.get(v, v)) for v in (value or []))
        return "، ".join(names)
    if isinstance(value, int) and not isinstance(value, bool):
        return fa_number(value)
    if value is None:
        return "—"
    return fa_digits(value)


def say(template, fields: dict | None) -> str:
    try:
        return str(template).format(**{k: _shown(k, v) for k, v in (fields or {}).items()})
    except (KeyError, IndexError, ValueError):
        return ""


def notes(job: Job) -> list[str]:
    """The job's keyed notes, in Persian, oldest first. Notes without a key
    (the engine's English only) aren't shown."""
    out = []
    for event in job.events.all():
        template = NOTES.get(event.key)
        if template is not None:
            line = say(template, event.data)
            if line:
                out.append(line)
    return out


def top_errors(result: dict | None) -> list[tuple[str, int]]:
    """[("Error 411: the recipient's number isn't valid.", 12), …]: what went
    wrong most often, with Kavenegar's code explained."""
    lines = []
    for message, count in (result or {}).get("top_errors") or []:
        match = _CODE.match(str(message))
        if match:
            code = int(match.group(1))
            text = str(TOP_ERROR).format(code=fa_digits(code), meaning=code_meaning(code))
        else:
            text = str(TOP_ERROR_NO_CODE)
        lines.append((text, count))
    return lines


def result_line(job: Job) -> str:
    """One line for the history: why it stopped, or what it did."""
    result = job.result or {}
    reason = stop_reason(result.get("stop_reason"), result.get("stop_fields"))
    if reason:
        return reason
    if job.state == Job.State.QUEUED and getattr(job, "not_before", None):
        return say(SCHEDULED_FOR, {"when": jalali(job.not_before)})
    if result.get("withdrawn"):
        return str(WITHDRAWN)
    if job.kind in JOB_RESULTS and result:
        fields = {k: result.get(k, 0) for k in ("sent", "requeued", "needs_review", "deferred",
                                                 "checked", "updated", "links", "clicks")}
        return say(JOB_RESULTS[job.kind], fields)
    return ""


@dataclass(frozen=True)
class SendSummary:
    """A finished (or stopped, or cancelled) send, for the results card."""
    sent: int
    failed_permanent: int
    failed_retriable: int
    unknown: int
    needs_review: int
    suppressed: int
    cancelled: int
    cost: int
    elapsed: str
    errors: list[tuple[str, int]] = field(default_factory=list)


def _elapsed(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    return fa_digits(text)


def send_summary(job: Job | None) -> SendSummary | None:
    if job is None or job.kind != Job.Kind.SEND or not job.result:
        return None
    r = job.result
    return SendSummary(
        sent=r.get("sent", 0), failed_permanent=r.get("failed_permanent", 0),
        failed_retriable=r.get("failed_retriable", 0), unknown=r.get("unknown", 0),
        needs_review=r.get("needs_review", 0), suppressed=r.get("suppressed", 0),
        cancelled=r.get("cancelled", 0), cost=r.get("cost", 0),
        elapsed=_elapsed(r.get("elapsed_sec")), errors=top_errors(r),
    )


# ---------- the check, as a checklist (plan 05, P2) ----------

@dataclass(frozen=True)
class ChecklistItem:
    state: str          # "ok", "warn" (doesn't stop a test SMS) or "fail"
    label: str
    note: str = ""
    value: str = ""     # what the line is about: a name, an address
    ltr: bool = False   # the value is an identifier or an address


def checklist(check, message, settings: dict | None, segment) -> list[ChecklistItem]:
    """The check's findings, one line each: the list, the template, its
    tokens, the link, the window and who's left to send to."""
    s = settings or {}
    problems = set(check.problems)
    items = []
    if "segment_missing" in problems:
        items.append(ChecklistItem("fail", str(CHECKLIST["list"]), str(CHECK_PROBLEMS["segment_missing"])))
    elif "columns_missing" in problems:
        items.append(ChecklistItem("fail", str(CHECKLIST["list"]), str(CHECK_PROBLEMS["columns_missing"]),
                                   value=segment.name if segment else ""))
    else:
        items.append(ChecklistItem("ok", str(CHECKLIST["list"]), say(CHECKLIST["valid"], {"n": check.valid}),
                                   value=segment.name if segment else ""))
    if "no_template" in problems:
        items.append(ChecklistItem("fail", str(CHECKLIST["template"]), str(CHECK_PROBLEMS["no_template"])))
    else:
        known = message is not None and message.known
        items.append(ChecklistItem(
            "ok" if known else "warn", str(CHECKLIST["template"]),
            str(CHECKLIST["text_known" if known else "text_unknown"]), value=s.get("template", ""), ltr=True,
        ))
        if known and message.problems:
            items.append(ChecklistItem("fail", str(CHECKLIST["tokens"]), " ".join(message.problems)))
        elif known:
            items.append(ChecklistItem("ok", str(CHECKLIST["tokens"]), str(CHECKLIST["tokens_filled"])))
    links = s.get("links") or {}
    if links.get("token"):
        bad = "bad_destination" in problems
        items.append(ChecklistItem(
            "fail" if bad else "ok", str(CHECKLIST["link"]),
            str(CHECK_PROBLEMS["bad_destination"]) if bad else "", value=links.get("destination", ""), ltr=True,
        ))
    items.append(ChecklistItem(
        "ok" if check.window_open else "warn", str(CHECKLIST["window"]),
        str(CHECKLIST["window_open" if check.window_open else "window_closed"]),
    ))
    if "nobody_to_send" in problems:
        items.append(ChecklistItem("fail", str(CHECKLIST["recipients"]), str(CHECK_PROBLEMS["nobody_to_send"])))
    elif "segment_missing" not in problems and "columns_missing" not in problems:
        items.append(ChecklistItem("ok", str(CHECKLIST["recipients"]), say(CHECKLIST["to_send"], {"n": check.to_send})))
    return items
