"""Scrub secrets out of arbitrary strings: Kavenegar API keys, Telegram bot
tokens and Slack webhook paths.

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

# A Telegram bot token is `<bot id>:<secret>`, in its API's path
# (`/bot<token>/sendMessage`) and in a notification target
# (`telegram:<token>:<chat id>`). A Slack webhook's path is its secret.
_TELEGRAM_TOKEN_RE = re.compile(r"(/bot|telegram:)\d+:[A-Za-z0-9_-]+")
_SLACK_HOOK_RE = re.compile(r"(hooks\.slack\.com/services/)[A-Za-z0-9/_-]+")

REDACTED = "***"


def redact_secrets(text: str) -> str:
    """Return `text` with every Kavenegar API key in URL form, Telegram bot
    token and Slack webhook path replaced by `***`."""
    if not text:
        return text
    text = _KAVENEGAR_KEY_RE.sub(rf"\1{REDACTED}\2", text)
    text = _TELEGRAM_TOKEN_RE.sub(rf"\1{REDACTED}", text)
    return _SLACK_HOOK_RE.sub(rf"\1{REDACTED}", text)
