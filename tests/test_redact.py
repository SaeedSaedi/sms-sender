"""Tests for the API-key redaction helper."""
from __future__ import annotations

import pytest

from sms_sender.redact import REDACTED, redact_secrets


KEY = "SECRET_API_KEY_DO_NOT_LEAK"


@pytest.mark.parametrize("text", [
    f"https://api.kavenegar.com/v1/{KEY}/verify/lookup.json",
    f"http://api.kavenegar.com/v1/{KEY}/account/info.json",
    f"https://api.kavenegar.com/v1/{KEY}/sms/send.json",
    (
        f"HTTPSConnectionPool(host='api.kavenegar.com', port=443): Max retries "
        f"exceeded with url: /v1/{KEY}/verify/lookup.json (Caused by ConnectError…)"
    ),
    (
        f"non-json response (http 502): Expecting value: line 1 column 1 "
        f"(at https://api.kavenegar.com/v1/{KEY}/verify/lookup.json)"
    ),
])
def test_redact_strips_key_in_url_paths(text):
    out = redact_secrets(text)
    assert KEY not in out
    assert REDACTED in out


def test_redact_keeps_surrounding_text():
    text = (
        f"prefix https://api.kavenegar.com/v1/{KEY}/verify/lookup.json suffix"
    )
    out = redact_secrets(text)
    assert "prefix" in out
    assert "suffix" in out
    # Endpoint segment is preserved.
    assert "/verify/lookup.json" in out


def test_redact_idempotent_on_already_redacted():
    text = f"api.kavenegar.com/v1/{REDACTED}/verify/lookup.json"
    assert redact_secrets(text) == text


def test_redact_unrelated_v1_url_untouched():
    """Other APIs that happen to use /v1/<token>/ shouldn't be scrubbed —
    we only match Kavenegar's known endpoint shape."""
    text = "https://example.com/v1/some-other-key/users"
    assert redact_secrets(text) == text


def test_redact_empty_string():
    assert redact_secrets("") == ""


def test_redact_handles_uppercase_host():
    text = f"API.KAVENEGAR.COM/v1/{KEY}/verify/lookup.json"
    out = redact_secrets(text)
    assert KEY not in out
