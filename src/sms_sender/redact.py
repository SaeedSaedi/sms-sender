"""Scrub Kavenegar API keys out of arbitrary strings.

A `requests.ConnectionError` text (or anything constructed from `str(e)`)
typically embeds the full request URL, including the API key in the path
segment after `/v1/`. We must strip it before that string lands in the
SQLite `last_error` column, the `top_errors` counter, the human-printed
report, the structured log, or a webhook payload.

The redaction is generic — it does not need to know the configured key.
"""
from __future__ import annotations

import re

# Kavenegar URLs look like:
#   https://api.kavenegar.com/v1/<KEY>/verify/lookup.json
# But error text from `requests` / `urllib3` often surfaces just the path:
#   "with url: /v1/<KEY>/verify/lookup.json"
# So we match by the `/v1/<KEY>/<endpoint>` shape, where <endpoint> is
# any of Kavenegar's known top-level endpoint names. That keeps the regex
# narrow enough to avoid eating unrelated `/v1/...` paths from other APIs.
_KAVENEGAR_ENDPOINTS = "verify|account|sms|call|template"
_KAVENEGAR_KEY_RE = re.compile(
    rf"(/v1/)[^/\s'\"]+(/(?:{_KAVENEGAR_ENDPOINTS})\b)",
    re.IGNORECASE,
)

REDACTED = "***"


def redact_secrets(text: str) -> str:
    """Return `text` with any Kavenegar API key in URL form replaced by `***`."""
    if not text:
        return text
    return _KAVENEGAR_KEY_RE.sub(rf"\1{REDACTED}\2", text)
