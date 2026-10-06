"""Undo (plan 06, L6): a success message that offers to take its action
back, for the few actions that are safe to reverse (putting a preset away;
taking a send off its schedule). The toast holds a «واگرد» button: a POST to
the URL given here (ui/messages.html, the `undo_url` filter), and it stays a
little longer than other toasts (app.js). The URL travels in the message's
extra tags, so nothing else has to be stored."""
from __future__ import annotations

from django.contrib import messages


def offer(request, text, url: str) -> None:
    messages.success(request, text, extra_tags=f"undo {url}")


def url_of(message) -> str:
    """The URL a message offers to undo with, or ''."""
    parts = (getattr(message, "extra_tags", "") or "").split()
    return parts[1] if len(parts) >= 2 and parts[0] == "undo" else ""
