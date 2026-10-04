import socket

import pytest
from kavenegar import APIException, HTTPException

from sms_sender.sender import (
    HaltError,
    PermanentSendError,
    ProviderMessage,
    SendError,
    Sender,
    SenderConfig,
    UncertainSendError,
    _NotSent,
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


def test_success_carries_the_cost():
    sdk = FakeSDK([[{"messageid": 7, "status": 200, "cost": 1100}]])
    assert Sender(cfg(), sdk=sdk).send("09123456789").cost == 1100


def test_retries_when_the_request_never_left():
    sdk = FakeSDK([_NotSent("refused"), _NotSent("refused"), [{"messageid": 9, "status": 200}]])
    s = Sender(cfg(max_attempts=3), sdk=sdk)
    r = s.send("09123456789")
    assert r.message_id == 9
    assert len(sdk.calls) == 3


def test_retries_exhausted_raises_send_error():
    sdk = FakeSDK([_NotSent("t"), _NotSent("t"), _NotSent("t")])
    s = Sender(cfg(max_attempts=3), sdk=sdk)
    with pytest.raises(SendError) as exc:
        s.send("09123456789")
    # not Halt, Permanent or Uncertain: the parent SendError class
    assert not isinstance(exc.value, (HaltError, PermanentSendError, UncertainSendError))
    assert "retries exhausted" in exc.value.message
    assert len(sdk.calls) == 3


def test_network_failure_after_sending_is_uncertain_and_never_retried():
    """A read timeout may mean Kavenegar accepted the SMS — retrying could
    deliver it twice, so the send stops after one call."""
    sdk = FakeSDK([HTTPException("read timed out"), [{"messageid": 9, "status": 200}]])
    with pytest.raises(UncertainSendError):
        Sender(cfg(max_attempts=3), sdk=sdk).send("09123456789")
    assert len(sdk.calls) == 1


def test_status_200_without_entries_is_uncertain():
    with pytest.raises(UncertainSendError):
        Sender(cfg(), sdk=FakeSDK([[]])).send("09123456789")


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


def test_retries_are_logged_per_phone(caplog):
    """Every retry leaves a per-phone trace in the log. Only calls that never
    left are retried, so these lines are not possible double sends."""
    sdk = FakeSDK([_NotSent("connection refused"), [{"messageid": 7, "status": 200}]])
    s = Sender(cfg(), sdk=sdk)
    with caplog.at_level("WARNING", logger="sms_sender.sender"):
        s.send("09123456789")
    retries = [r for r in caplog.records if r.getMessage() == "send_retry"]
    assert [(r.phone, r.attempt, r.status) for r in retries] == [("09123456789", 1, None)]
    assert "connection refused" in retries[0].detail


def test_per_recipient_tokens_layer_over_static_ones():
    sdk = FakeSDK([[{"messageid": 7, "status": 200}]])
    s = Sender(cfg(token="static", token2="y"), sdk=sdk)
    s.send("09123456789", tokens={"token": "خرید", "token10": "علی"})
    assert sdk.calls == [{
        "receptor": "09123456789", "template": "t",
        "token": "خرید", "token2": "y", "token10": "علی",
    }]


# ---------- delivery reports (sms/status) ----------


def test_message_status_posts_comma_joined_ids_and_parses(monkeypatch):
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    seen = {}

    def post(url, data=None, timeout=None, **_kw):
        seen.update(url=url, data=data)
        return _FakeJSONResp({
            "return": {"status": 200, "message": "ok"},
            "entries": [{"messageid": 11, "status": 10, "statustext": "…"},
                        {"messageid": 12, "status": 4}],
        })

    monkeypatch.setattr(http._session, "post", post)
    assert Sender(cfg(), sdk=http).delivery_statuses([11, 12]) == {11: 10, 12: 4}
    assert seen["url"].endswith("/sms/status.json")
    assert seen["data"] == {"messageid": "11,12"}


def test_delivery_statuses_449_means_none():
    sdk = FakeSDK([])

    def no_record(_ids):
        raise APIException("APIException[449] رکوردی با مشخصات مورد نظر پیدا نشد")

    sdk.message_status = no_record
    assert Sender(cfg(), sdk=sdk).delivery_statuses([1]) == {}


# ---------- account settings (read-only) ----------


def test_account_config_is_read_with_a_plain_get(monkeypatch):
    """GET without parameters only reads the settings; any parameter would
    change that setting on the account, so none may ever be sent."""
    from sms_sender.sender import AccountConfig, _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    seen = {}

    def get(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return _FakeJSONResp({
            "return": {"status": 200, "message": "ok"},
            "entries": {"apilogs": "justfaults", "debugmode": "disabled", "resendfailed": "enabled"},
        })

    monkeypatch.setattr(http._session, "get", get)
    monkeypatch.setattr(
        http._session, "post", lambda *a, **kw: pytest.fail("account/config must never be POSTed"),
    )
    assert Sender(cfg(), sdk=http).account_config() == AccountConfig(
        debug_mode=False, resend_failed=True,
    )
    assert seen["url"].endswith("/account/config.json")
    assert set(seen["kwargs"]) == {"timeout"}  # no data, no params


def test_account_config_unrecognized_values_are_unknown():
    sdk = FakeSDK([])
    sdk.account_config = lambda: {"debugmode": "maybe"}
    config = Sender(cfg(), sdk=sdk).account_config()
    assert (config.debug_mode, config.resend_failed) == (None, None)


# ---------- Kavenegar's token rules (error 431) ----------


@pytest.mark.parametrize("name, value, fragment", [
    ("token", "x" * 101, "at most 100"),
    ("token20", "two\nlines", "line break"),
    ("token3", "has_underscore", "'_'"),
    ("token", "no spaces allowed", "space"),
    ("token10", "a b c d e f g", "space"),  # 6 spaces > 5
])
def test_token_problem_names_the_rule(name, value, fragment):
    from sms_sender.sender import token_problem

    problem = token_problem(name, value)
    assert problem is not None and fragment in problem and name in problem


@pytest.mark.parametrize("name, value", [
    ("token", "x" * 100),
    ("token3", "190,400"),                   # comma: sent fine in production
    ("token20", "wallet/usoon-oil(usoon)"),  # slashes, parens: sent fine
    ("token10", "نفت خام OIL(USOON)"),        # 2 spaces ≤ 5
])
def test_token_problem_accepts_real_production_values(name, value):
    from sms_sender.sender import token_problem

    assert token_problem(name, value) is None


# ---------- looking up what Kavenegar sent (reconciliation) ----------


class _LookupSDK(FakeSDK):
    def __init__(self, entries=None, error=None):
        super().__init__([])
        self.entries = entries or []
        self.error = error

    def status_by_receptor(self, receptor, startdate, enddate):
        self.calls.append((receptor, startdate, enddate))
        if self.error:
            raise self.error
        return self.entries


def test_find_messages_parses_kavenegar_entries():
    sdk = _LookupSDK([
        {"messageid": 85463238, "receptor": "09123456789", "status": 10, "statustext": "…"},
        {"messageid": None},  # unusable entry is skipped
    ])
    found = Sender(cfg(), sdk=sdk).find_messages("09123456789", 1000.7, 2000.2)
    assert found == [ProviderMessage(85463238, 10)]
    assert sdk.calls == [("09123456789", 1000, 2000)]


def test_find_messages_treats_no_record_449_as_nothing_found():
    """Seen live (2026-10-04): an empty lookup is error 449, not []."""
    sdk = _LookupSDK(error=APIException("APIException[449] رکوردی با مشخصات مورد نظر پیدا نشد"))
    assert Sender(cfg(), sdk=sdk).find_messages("09123456789", 1, 2) == []


@pytest.mark.parametrize("failure, error_type", [
    (HTTPException("read timed out"), SendError),
    (APIException("APIException[403 invalid api key]"), HaltError),
    (APIException("APIException[417 invalid date]"), SendError),
])
def test_find_messages_errors(failure, error_type):
    with pytest.raises(error_type) as exc:
        Sender(cfg(), sdk=_LookupSDK(error=failure)).find_messages("09123456789", 1, 2)
    if error_type is SendError:
        assert not isinstance(exc.value, HaltError)


def test_kavenegar_http_status_by_receptor_posts_the_window(monkeypatch):
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    seen = {}

    def post(url, data=None, timeout=None, **_kw):
        seen.update(url=url, data=data)
        return _FakeJSONResp({
            "return": {"status": 200, "message": "ok"},
            "entries": [{"messageid": 1, "status": 10}],
        })

    monkeypatch.setattr(http._session, "post", post)
    assert http.status_by_receptor("09123456789", 100, 200) == [{"messageid": 1, "status": 10}]
    assert seen["url"].endswith("/sms/statusbyreceptor.json")
    assert seen["data"] == {"receptor": "09123456789", "startdate": 100, "enddate": 200}


# ---------- every call is reported (audit trail) ----------


def test_every_call_is_reported_with_its_outcome():
    attempts = []
    sdk = FakeSDK([_NotSent("refused"), [{"messageid": 9, "status": 200, "cost": 1100}]])
    Sender(cfg(), sdk=sdk, on_attempt=attempts.append).send("09123456789")
    assert [a.outcome for a in attempts] == ["retry", "accepted"]
    assert "refused" in attempts[0].detail
    accepted = attempts[1]
    assert (accepted.phone, accepted.message_id, accepted.cost) == ("09123456789", 9, 1100)
    assert accepted.started_at <= accepted.finished_at


@pytest.mark.parametrize("failure, outcome", [
    (HTTPException("read timed out"), "unknown"),
    (APIException("APIException[424 template not found]"), "rejected"),
    (APIException("APIException[418 insufficient credit]"), "halt"),
])
def test_failed_calls_are_reported(failure, outcome):
    attempts = []
    with pytest.raises(SendError):
        Sender(cfg(), sdk=FakeSDK([failure]), on_attempt=attempts.append).send("09123456789")
    assert [a.outcome for a in attempts] == [outcome]


def test_a_failing_attempt_hook_never_changes_the_send_result():
    def broken_hook(_attempt):
        raise RuntimeError("audit DB is down")

    sdk = FakeSDK([[{"messageid": 7, "status": 200}]])
    r = Sender(cfg(), sdk=sdk, on_attempt=broken_hook).send("09123456789")
    assert r.message_id == 7


# ---------- which network errors may have reached Kavenegar ----------


def test_never_sent_only_when_the_connection_itself_failed():
    import requests as r
    import urllib3

    from sms_sender.sender import _never_sent

    refused = urllib3.exceptions.NewConnectionError(None, "Failed to establish a new connection")
    via_proxy = urllib3.exceptions.ProxyError("Unable to connect to proxy", refused)
    url = "/v1/k/verify/lookup.json"

    assert _never_sent(r.exceptions.ConnectTimeout("connect timed out"))
    assert _never_sent(r.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(None, url, reason=refused)))
    assert _never_sent(r.exceptions.ProxyError(
        urllib3.exceptions.MaxRetryError(None, url, reason=via_proxy)))

    assert not _never_sent(r.exceptions.ReadTimeout("read timed out"))
    assert not _never_sent(r.exceptions.ConnectionError(
        urllib3.exceptions.ProtocolError("Connection aborted.", ConnectionResetError())))
    assert not _never_sent(r.exceptions.ConnectionError("no structured cause"))


def _http_to_localhost(monkeypatch, port: int, timeout: float):
    from sms_sender.sender import _KavenegarHTTP

    monkeypatch.setattr(
        _KavenegarHTTP, "BASE", f"http://127.0.0.1:{port}/v1/{{key}}/{{path}}.json",
    )
    http = _KavenegarHTTP("k", timeout=timeout)
    http._session.trust_env = False  # ignore any HTTP(S)_PROXY in the environment
    return http


def test_refused_connection_is_retried_as_never_sent(monkeypatch):
    """Real sockets: nothing listens on the port, so the request never left."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    attempts = []
    http = _http_to_localhost(monkeypatch, port, timeout=1)
    with pytest.raises(SendError) as exc:
        Sender(cfg(max_attempts=2), sdk=http, on_attempt=attempts.append).send("09123456789")
    assert not isinstance(exc.value, UncertainSendError)
    assert [a.outcome for a in attempts] == ["retry", "retry"]


def test_request_without_an_answer_is_uncertain_and_sent_once(monkeypatch):
    """Real sockets: the server accepts the connection (listen backlog) and
    receives the request, but never answers — a genuine read timeout."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        attempts = []
        http = _http_to_localhost(monkeypatch, server.getsockname()[1], timeout=0.3)
        with pytest.raises(UncertainSendError):
            Sender(cfg(max_attempts=3), sdk=http, on_attempt=attempts.append).send("09123456789")
        assert [a.outcome for a in attempts] == ["unknown"]
    finally:
        server.close()


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
    # The key must not appear anywhere in the surfaced message.
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


class _HTMLResp:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        raise ValueError("not json")


def _send_through_http(monkeypatch, response):
    """Send once through the real _KavenegarHTTP with a canned response;
    return (raised exception, number of HTTP posts)."""
    from sms_sender.sender import _KavenegarHTTP

    http = _KavenegarHTTP("k", timeout=1)
    posts = []
    monkeypatch.setattr(http._session, "post", lambda *a, **kw: posts.append(1) or response)
    with pytest.raises(SendError) as exc:
        Sender(cfg(api_key="k", max_attempts=5), sdk=http).send("09123456789")
    return exc.value, len(posts)


@pytest.mark.parametrize("response", [
    _FakeJSONResp({"unexpected": "shape"}),                        # no `return` key
    _FakeJSONResp({"return": {"status": "weird", "message": "?"}}),  # status not int
    _HTMLResp(502),                                                # gateway page
], ids=["no-return-key", "status-not-int", "html-5xx"])
def test_garbled_200_or_5xx_reply_is_uncertain_and_sent_once(monkeypatch, response):
    """The API (200) or a gateway that forwarded the call (5xx) may have
    processed it: never retry, park it as unknown."""
    error, posts = _send_through_http(monkeypatch, response)
    assert isinstance(error, UncertainSendError)
    assert posts == 1


@pytest.mark.parametrize("response", [
    _FakeJSONResp({"unexpected": "shape"}, status_code=403),
    _HTMLResp(404),
], ids=["malformed-4xx", "html-404"])
def test_garbled_4xx_reply_is_permanent(monkeypatch, response):
    """A 4xx page never reached the API (bad URL, proxy refusal): permanent."""
    error, posts = _send_through_http(monkeypatch, response)
    assert isinstance(error, PermanentSendError)
    assert posts == 1


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


@pytest.mark.parametrize("name, value, issue", [
    ("token", "x" * 101, ("too_long", {"length": 101, "max": 100})),
    ("token", "a\nb", ("line_break", {})),
    ("token", "a_b", ("underscore", {})),
    ("token", "a b", ("too_many_spaces", {"max": 0, "spaces": 1})),
    ("token10", "a b c d e f g", ("too_many_spaces", {"max": 5, "spaces": 6})),
    ("token10", "a b", None),
])
def test_token_issue_is_a_key_for_the_dashboard(name, value, issue):
    from sms_sender.sender import token_issue

    assert token_issue(name, value) == issue
