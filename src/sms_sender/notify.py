"""End-of-run notifications. Best-effort: failures here never abort the run.

Supported targets, picked by URL scheme:

- ``slack:https://hooks.slack.com/...`` — POST `{"text": "..."}` to a Slack webhook.
- ``telegram:<bot_token>:<chat_id>`` — POST to ``https://api.telegram.org``.
- ``https://...`` / ``http://...`` — POST the JSON-encoded summary as-is.

The Slack and Telegram forms get a human-readable message body. The plain
HTTP form posts a structured JSON payload so it works as a generic webhook.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlsplit

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
        "suppressed": s.suppressed,
        "capped": s.capped,
        "cost": s.cost,
        "halted": s.halted,
        "stopped": s.stopped,
        "elapsed_sec": s.elapsed_sec,
        "sends_per_sec": s.sends_per_sec,
        "top_errors": [{"message": m, "count": c} for m, c in s.top_errors],
    }


def _build_text(s: RunSummary, error: str | None, heading: str | None = None) -> str:
    header = "SMS run halted" if (s.halted or error) else "SMS run finished"
    if heading:
        header = f"{header}: {heading}"
    if error:
        return f"{header}: {error}\n\n{format_report(s)}"
    return f"{header}\n\n{format_report(s)}"


def _post(url: str, payload: dict, timeout: float) -> None:
    """A target answers for itself: a redirect, which could lead anywhere
    (an address inside the network too), isn't followed and counts as a
    failure."""
    response = requests.post(url, json=payload, timeout=timeout, allow_redirects=False)
    response.raise_for_status()
    if response.status_code >= 300:
        raise requests.HTTPError(f"HTTP {response.status_code}: a redirect, not followed")


def _post_slack(webhook_url: str, text: str, timeout: float) -> None:
    _post(webhook_url, {"text": text}, timeout)


def _telegram(spec: str) -> tuple[str, str]:
    """`<bot_token>:<chat_id>`. A bot's token has a colon of its own
    (`123456:ABC…`), so the chat is what follows the last one."""
    bot_token, _, chat_id = spec.rpartition(":")
    if not bot_token or not chat_id:
        raise ValueError("telegram target must be 'telegram:<bot_token>:<chat_id>'")
    return bot_token, chat_id


def _post_telegram(spec: str, text: str, timeout: float) -> None:
    bot_token, chat_id = _telegram(spec)
    _post(f"https://api.telegram.org/bot{bot_token}/sendMessage", {"chat_id": chat_id, "text": text}, timeout)


def _post_generic(url: str, summary: RunSummary, error: str | None, timeout: float) -> None:
    _post(url, {"summary": _summary_to_dict(summary), "error": error}, timeout)


# A bot's token (its number, a colon, its secret), then a chat: a number
# (a group's is negative) or a public @name.
_TELEGRAM = re.compile(r"\d+:[A-Za-z0-9_-]+:(-?\d+|@[A-Za-z0-9_]{5,32})")


def valid_target(target: str) -> bool:
    """A target the dashboard takes: a Slack webhook on Slack's own host, a
    Telegram bot and chat, or an https:// address that isn't this machine's
    or a private network's by IP, so an admin can't point the server at its
    own insides. The CLI's --notify takes any target, as before."""
    if target.startswith("slack:"):
        return target[len("slack:"):].startswith("https://hooks.slack.com/")
    if target.startswith("telegram:"):
        return _TELEGRAM.fullmatch(target[len("telegram:"):]) is not None
    parts = urlsplit(target)
    host = parts.hostname or ""
    if parts.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True  # a name


def notify_text(target: str, text: str, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """A plain message (a test, say): never raises, like notify()."""
    try:
        if target.startswith("slack:"):
            _post_slack(target[len("slack:"):], text, timeout)
        elif target.startswith("telegram:"):
            _post_telegram(target[len("telegram:"):], text, timeout)
        elif target.startswith(("http://", "https://")):
            requests.post(target, json={"text": text, "test": True}, timeout=timeout).raise_for_status()
        else:
            return False
    except Exception as e:  # noqa: BLE001 — best-effort
        # Only the kind of failure: its text would carry the URL, and a
        # webhook's or a bot's URL holds its secret.
        logger.warning("notify_failed", extra={"target": redact_target(target), "detail": _failure(e)})
        return False
    logger.info("notify_sent", extra={"target": redact_target(target)})
    return True


def notify(
    target: str | None, summary: RunSummary, *, error: str | None = None,
    timeout: float = DEFAULT_TIMEOUT, heading: str | None = None,
) -> bool:
    """Send a notification. Returns True on success, False on any failure.

    Never raises — a notification is a side-channel signal, not part of the
    run contract. Errors are logged at WARNING level.
    """
    if not target:
        return False
    try:
        if target.startswith("slack:"):
            _post_slack(target[len("slack:"):], _build_text(summary, error, heading), timeout)
        elif target.startswith("telegram:"):
            _post_telegram(target[len("telegram:"):], _build_text(summary, error, heading), timeout)
        elif target.startswith(("http://", "https://")):
            _post_generic(target, summary, error, timeout)
        else:
            logger.warning("notify_unknown_scheme", extra={"target": redact_target(target)})
            return False
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.warning("notify_failed", extra={"target": redact_target(target), "detail": _failure(e)})
        return False
    logger.info("notify_sent", extra={"target": redact_target(target)})
    return True


def _failure(error: Exception) -> str:
    """`HTTPError 404`, `ConnectTimeout`: what went wrong, without the URL."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return f"{type(error).__name__} {status}" if status else type(error).__name__


def redact_target(target: str) -> str:
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
