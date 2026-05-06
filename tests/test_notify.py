"""Tests for end-of-run notifications. Best-effort: failures must not raise."""
from __future__ import annotations

import pytest

from sms_sender import notify as notify_module
from sms_sender.notify import _redact, notify
from sms_sender.runner import RunSummary


def _summary(halted: bool = False) -> RunSummary:
    return RunSummary(
        total_input=10, new_recipients=10, duplicates_collapsed=0, invalid=0,
        sent=8, failed_permanent=1, failed_retriable=1, halted=halted,
        elapsed_sec=2.5, sends_per_sec=3.2,
        top_errors=(("[424] template not found", 1),),
    )


class FakePost:
    """Captures requests.post calls so we can assert on payload + URL."""

    def __init__(self, status_code: int = 200, raise_on_post: Exception | None = None):
        self.calls: list[dict] = []
        self.status_code = status_code
        self.raise_on_post = raise_on_post

    def __call__(self, url, *, json=None, timeout=None, **kwargs):
        if self.raise_on_post:
            raise self.raise_on_post
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(self.status_code)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ---------- target dispatch ----------


def test_no_target_returns_false():
    assert notify(None, _summary()) is False
    assert notify("", _summary()) is False


def test_unknown_scheme_logs_and_returns_false(caplog):
    with caplog.at_level("WARNING"):
        ok = notify("ftp://example.com/x", _summary())
    assert ok is False
    assert any("unknown_scheme" in r.message for r in caplog.records)


def test_slack_post_shape(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(notify_module.requests, "post", fp)
    ok = notify("slack:https://hooks.slack.com/services/AAA/BBB/CCC", _summary())
    assert ok is True
    assert len(fp.calls) == 1
    call = fp.calls[0]
    assert call["url"] == "https://hooks.slack.com/services/AAA/BBB/CCC"
    assert "text" in call["json"]
    assert "SMS run finished" in call["json"]["text"]
    assert "[424] template not found" in call["json"]["text"]


def test_slack_halted_summary_says_halted(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(notify_module.requests, "post", fp)
    notify("slack:https://hooks.slack.com/x", _summary(halted=True))
    assert "halted" in fp.calls[0]["json"]["text"].lower()


def test_telegram_post_shape(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(notify_module.requests, "post", fp)
    ok = notify("telegram:123456:abcdef:99999", _summary())
    # bot_token = "123456", chat_id = "abcdef:99999" (after first colon)
    assert ok is True
    call = fp.calls[0]
    assert call["url"] == "https://api.telegram.org/bot123456/sendMessage"
    assert call["json"]["chat_id"] == "abcdef:99999"
    assert "text" in call["json"]


def test_telegram_malformed_target_returns_false(monkeypatch):
    monkeypatch.setattr(notify_module.requests, "post", FakePost())
    # No colon between bot_token and chat_id.
    assert notify("telegram:no-chat-id", _summary()) is False


def test_generic_https_posts_structured_payload(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(notify_module.requests, "post", fp)
    ok = notify("https://example.com/hook", _summary())
    assert ok is True
    payload = fp.calls[0]["json"]
    assert "summary" in payload
    s = payload["summary"]
    assert s["sent"] == 8
    assert s["failed_permanent"] == 1
    assert s["top_errors"] == [{"message": "[424] template not found", "count": 1}]
    assert payload["error"] is None


def test_generic_http_with_explicit_error(monkeypatch):
    fp = FakePost()
    monkeypatch.setattr(notify_module.requests, "post", fp)
    notify("http://example.com/hook", _summary(), error="boom")
    assert fp.calls[0]["json"]["error"] == "boom"


# ---------- failure modes (must never raise) ----------


def test_network_failure_does_not_raise(monkeypatch, caplog):
    fp = FakePost(raise_on_post=ConnectionError("dns failed"))
    monkeypatch.setattr(notify_module.requests, "post", fp)
    with caplog.at_level("WARNING"):
        ok = notify("slack:https://hooks.slack.com/x", _summary())
    assert ok is False
    assert any("notify_failed" in r.message for r in caplog.records)


def test_http_400_does_not_raise(monkeypatch):
    fp = FakePost(status_code=500)
    monkeypatch.setattr(notify_module.requests, "post", fp)
    assert notify("https://example.com/hook", _summary()) is False


# ---------- redaction ----------


@pytest.mark.parametrize("target, expected_prefix", [
    ("slack:https://hooks.slack.com/SECRET", "slack:<redacted>"),
    ("telegram:BOT_TOKEN:CHAT", "telegram:<redacted>"),
    ("https://example.com/secret/path?key=v", "https://example.com/<redacted>"),
])
def test_redact(target, expected_prefix):
    assert _redact(target) == expected_prefix
