"""End-of-run notifications. Best-effort: failures here never abort the run.

Supported targets, picked by URL scheme:

- ``slack:https://hooks.slack.com/...`` — POST `{"text": "..."}` to a Slack webhook.
- ``telegram:<bot_token>:<chat_id>`` — POST to ``https://api.telegram.org``.
- ``https://...`` / ``http://...`` — POST the JSON-encoded summary as-is.

The Slack and Telegram forms get a human-readable message body. The plain
HTTP form posts a structured JSON payload so it works as a generic webhook.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from .runner import RunSummary, format_report

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


def _summary_to_dict(s: RunSummary) -> dict[str, Any]:
    """Serialize a RunSummary to a webhook-friendly JSON shape."""
    return {
        "total_input": s.total_input,
        "new_recipients": s.new_recipients,
        "duplicates_collapsed": s.duplicates_collapsed,
        "invalid": s.invalid,
        "sent": s.sent,
        "failed_permanent": s.failed_permanent,
        "failed_retriable": s.failed_retriable,
        "already_done": s.already_done,
        "unknown": s.unknown,
        "needs_review": s.needs_review,
        "halted": s.halted,
        "elapsed_sec": s.elapsed_sec,
        "sends_per_sec": s.sends_per_sec,
        "top_errors": [{"message": m, "count": c} for m, c in s.top_errors],
    }


def _build_text(s: RunSummary, error: str | None) -> str:
    header = "SMS run halted" if (s.halted or error) else "SMS run finished"
    if error:
        return f"{header}: {error}\n\n{format_report(s)}"
    return f"{header}\n\n{format_report(s)}"


def _post_slack(webhook_url: str, text: str, timeout: float) -> None:
    requests.post(webhook_url, json={"text": text}, timeout=timeout).raise_for_status()


def _post_telegram(spec: str, text: str, timeout: float) -> None:
    bot_token, _, chat_id = spec.partition(":")
    if not bot_token or not chat_id:
        raise ValueError(
            f"telegram target must be 'telegram:<bot_token>:<chat_id>', got telegram:{spec!r}"
        )
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(
        url, json={"chat_id": chat_id, "text": text}, timeout=timeout
    ).raise_for_status()


def _post_generic(url: str, summary: RunSummary, error: str | None, timeout: float) -> None:
    payload = {"summary": _summary_to_dict(summary), "error": error}
    requests.post(url, json=payload, timeout=timeout).raise_for_status()


def notify(
    target: str | None, summary: RunSummary, *, error: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Send a notification. Returns True on success, False on any failure.

    Never raises — a notification is a side-channel signal, not part of the
    run contract. Errors are logged at WARNING level.
    """
    if not target:
        return False
    try:
        if target.startswith("slack:"):
            _post_slack(target[len("slack:"):], _build_text(summary, error), timeout)
        elif target.startswith("telegram:"):
            _post_telegram(target[len("telegram:"):], _build_text(summary, error), timeout)
        elif target.startswith(("http://", "https://")):
            _post_generic(target, summary, error, timeout)
        else:
            logger.warning("notify_unknown_scheme", extra={"target": target})
            return False
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning(
            "notify_failed",
            extra={"target": _redact(target), "detail": str(e)},
        )
        return False
    logger.info("notify_sent", extra={"target": _redact(target)})
    return True


def _redact(target: str) -> str:
    """Strip credentials before logging the target.

    Slack webhooks and Telegram bot tokens are secrets; we keep just the
    scheme so an operator can still see the channel was hit.
    """
    if target.startswith("slack:"):
        return "slack:<redacted>"
    if target.startswith("telegram:"):
        return "telegram:<redacted>"
    # Generic webhook — keep the host but drop the path/query.
    if "://" in target:
        scheme, rest = target.split("://", 1)
        host = rest.split("/", 1)[0].split("?", 1)[0]
        return f"{scheme}://{host}/<redacted>"
    return "<redacted>"
