import pytest
from kavenegar import APIException, HTTPException

from sms_sender.sender import (
    HaltError,
    PermanentSendError,
    SendError,
    Sender,
    SenderConfig,
)


class FakeSDK:
    def __init__(self, script, account_script=None):
        # script is a list; each element is either a return value or an exception to raise.
        self.script = list(script)
        self.account_script = list(account_script or [])
        self.calls = []
        self.account_calls = 0

    def verify_lookup(self, params):
        self.calls.append(params)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def account_info(self):
        self.account_calls += 1
        item = self.account_script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def cfg(**kw):
    base = dict(api_key="k", template="t", token="123", max_attempts=3, backoff_max=0.01, timeout=1)
    base.update(kw)
    return SenderConfig(**base)


def test_success_first_try():
    sdk = FakeSDK([[{"messageid": 7, "status": 200}]])
    s = Sender(cfg(), sdk=sdk)
    r = s.send("09123456789")
    assert r.message_id == 7
    assert r.status_code == 200
    assert sdk.calls == [{"receptor": "09123456789", "template": "t", "token": "123"}]


def test_retries_then_succeeds_on_http_exception():
    sdk = FakeSDK([HTTPException("timeout"), HTTPException("timeout"), [{"messageid": 9, "status": 200}]])
    s = Sender(cfg(max_attempts=3), sdk=sdk)
    r = s.send("09123456789")
    assert r.message_id == 9
    assert len(sdk.calls) == 3


def test_retries_exhausted_raises_send_error():
    sdk = FakeSDK([HTTPException("t"), HTTPException("t"), HTTPException("t")])
    s = Sender(cfg(max_attempts=3), sdk=sdk)
    with pytest.raises(SendError) as exc:
        s.send("09123456789")
    # not Halt or Permanent, but the parent SendError class
    assert not isinstance(exc.value, (HaltError, PermanentSendError))


def test_halt_on_account_error():
    sdk = FakeSDK([APIException("APIException[418 insufficient credit]")])
    s = Sender(cfg(), sdk=sdk)
    with pytest.raises(HaltError) as exc:
        s.send("09123456789")
    assert exc.value.status_code == 418


def test_permanent_on_invalid_template():
    sdk = FakeSDK([APIException("APIException[424 template not found]")])
    s = Sender(cfg(), sdk=sdk)
    with pytest.raises(PermanentSendError) as exc:
        s.send("09123456789")
    assert exc.value.status_code == 424


def test_retries_on_409_then_succeeds():
    sdk = FakeSDK([
        APIException("APIException[409 server busy]"),
        [{"messageid": 1, "status": 200}],
    ])
    s = Sender(cfg(max_attempts=3), sdk=sdk)
    r = s.send("09123456789")
    assert r.status_code == 200
    assert len(sdk.calls) == 2


def test_optional_tokens_omitted_when_none():
    sdk = FakeSDK([[{"messageid": 1, "status": 200}]])
    s = Sender(cfg(token=None, token2=None, token3=None), sdk=sdk)
    s.send("09123456789")
    assert sdk.calls[0] == {"receptor": "09123456789", "template": "t"}


def test_token2_token3_passed_through():
    sdk = FakeSDK([[{"messageid": 1, "status": 200}]])
    s = Sender(cfg(token="a", token2="b", token3="c"), sdk=sdk)
    s.send("09123456789")
    assert sdk.calls[0] == {
        "receptor": "09123456789", "template": "t",
        "token": "a", "token2": "b", "token3": "c",
    }


def test_account_info_parses_fields():
    sdk = FakeSDK([], account_script=[
        {"remaincredit": "12345", "expiredate": "2027-01-01", "type": "Master"}
    ])
    s = Sender(cfg(), sdk=sdk)
    info = s.account_info()
    assert info.remaining_credit == 12345
    assert info.expire_date == "2027-01-01"
    assert info.type == "Master"
    assert sdk.account_calls == 1


def test_account_info_halts_on_auth_error():
    sdk = FakeSDK([], account_script=[APIException("APIException[401 invalid api key]")])
    s = Sender(cfg(), sdk=sdk)
    with pytest.raises(HaltError) as exc:
        s.account_info()
    assert exc.value.status_code == 401


def test_account_info_network_error_is_send_error_not_halt():
    sdk = FakeSDK([], account_script=[HTTPException("connection refused")])
    s = Sender(cfg(), sdk=sdk)
    with pytest.raises(SendError) as exc:
        s.account_info()
    assert not isinstance(exc.value, HaltError)


def test_build_params_is_pure_no_io():
    sdk = FakeSDK([])
    s = Sender(cfg(token="x", token2="y"), sdk=sdk)
    p = s.build_params("09120000000")
    assert p == {"receptor": "09120000000", "template": "t", "token": "x", "token2": "y"}
    assert sdk.calls == []  # no API calls


# ---------- _KavenegarHTTP redaction (regression for API key leak) ----------


def test_kavenegar_http_redacts_key_in_request_exception(monkeypatch):
    """A requests.ConnectionError text contains the API key URL — must be scrubbed."""
    import requests as _requests
    from sms_sender.sender import _KavenegarHTTP

    SECRET = "SECRET_API_KEY_DO_NOT_LEAK"
    http = _KavenegarHTTP(SECRET, timeout=1)

    def boom(*_args, **_kwargs):
        # Mimics the shape of a real ConnectionError message from urllib3.
        raise _requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='api.kavenegar.com', port=443): Max retries "
            f"exceeded with url: /v1/{SECRET}/verify/lookup.json (Caused by …)"
        )

    monkeypatch.setattr(http._session, "post", boom)
    s = Sender(cfg(api_key=SECRET, max_attempts=1), sdk=http)
    with pytest.raises(SendError) as exc:
        s.send("09123456789")
    # The retries-exhausted message wraps the inner _RetriableSendError text;
    # the key must not appear anywhere in the surfaced message.
    assert SECRET not in exc.value.message
    assert "***" in exc.value.message


def test_kavenegar_http_redacts_key_on_non_json_response(monkeypatch):
    """A 5xx with HTML body produces a `non-json response …` exception that
    today included the URL via the json-parser error message."""
    from sms_sender.sender import _KavenegarHTTP

    SECRET = "SECRET_API_KEY_DO_NOT_LEAK"
    http = _KavenegarHTTP(SECRET, timeout=1)

    class _FakeResp:
        status_code = 502

        def json(self):
            # Simulate json.JSONDecodeError shape that may include the URL.
            raise ValueError(
                f"Expecting value at https://api.kavenegar.com/v1/{SECRET}/verify/lookup.json"
            )

    monkeypatch.setattr(http._session, "post", lambda *a, **kw: _FakeResp())
    s = Sender(cfg(api_key=SECRET, max_attempts=1), sdk=http)
    with pytest.raises(SendError) as exc:
        s.send("09123456789")
    assert SECRET not in exc.value.message


class _FakeJSONResp:
    """Minimal fake of `requests.Response` with a controllable JSON body."""

    def __init__(self, body, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_kavenegar_http_malformed_no_return_key_is_permanent(monkeypatch):
    """A 200 with a JSON body missing the `return` key should be a permanent
    failure, not retried indefinitely."""
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    monkeypatch.setattr(
        http._session, "post",
        lambda *a, **kw: _FakeJSONResp({"unexpected": "shape"}),
    )
    s = Sender(cfg(api_key="k", max_attempts=5), sdk=http)
    with pytest.raises(PermanentSendError):
        s.send("09123456789")


def test_kavenegar_http_malformed_status_not_int_is_permanent(monkeypatch):
    """A `return.status` that isn't an int (e.g., string) means the response
    can't be classified — fail permanent rather than spin retries."""
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    monkeypatch.setattr(
        http._session, "post",
        lambda *a, **kw: _FakeJSONResp({"return": {"status": "weird", "message": "?"}}),
    )
    s = Sender(cfg(api_key="k", max_attempts=5), sdk=http)
    with pytest.raises(PermanentSendError):
        s.send("09123456789")


def test_kavenegar_http_non_json_body_is_permanent(monkeypatch):
    """An HTML 5xx page (json() raises ValueError) should fail permanent."""
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)

    class _HTMLResp:
        status_code = 502
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(http._session, "post", lambda *a, **kw: _HTMLResp())
    s = Sender(cfg(api_key="k", max_attempts=5), sdk=http)
    with pytest.raises(PermanentSendError):
        s.send("09123456789")


def test_send_falls_back_when_inner_is_unexpected(monkeypatch):
    """If tenacity ever raises RetryError with a non-_RetriableSendError inner
    (shouldn't happen, but defensive), Sender.send must surface a SendError
    rather than crash on AttributeError (the old `assert` path)."""
    from tenacity import RetryError
    from sms_sender import sender as sender_mod

    s = Sender(cfg(max_attempts=2), sdk=FakeSDK([]))

    class _FakeAttempt:
        def exception(self):
            return RuntimeError("not the expected type")

    def boom(self):
        raise RetryError(_FakeAttempt())

    monkeypatch.setattr(sender_mod.Retrying, "__iter__", boom)
    with pytest.raises(SendError) as exc:
        s.send("09123456789")
    # Best-effort message — exact wording not contracted, only the type.
    assert "retries exhausted" in exc.value.message


def test_kavenegar_http_account_info_redacts_on_network_error(monkeypatch):
    """The preflight account_info path must also strip the key on RequestException."""
    import requests as _requests
    from sms_sender.sender import _KavenegarHTTP

    SECRET = "SECRET_API_KEY_DO_NOT_LEAK"
    http = _KavenegarHTTP(SECRET, timeout=1)

    def boom(*_a, **_kw):
        raise _requests.exceptions.ConnectionError(
            f"refused: https://api.kavenegar.com/v1/{SECRET}/account/info.json"
        )

    monkeypatch.setattr(http._session, "post", boom)
    s = Sender(cfg(api_key=SECRET), sdk=http)
    with pytest.raises(SendError) as exc:
        s.account_info()
    assert SECRET not in exc.value.message
