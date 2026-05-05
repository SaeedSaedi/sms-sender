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
    def __init__(self, script):
        # script is a list; each element is either a return value or an exception to raise.
        self.script = list(script)
        self.calls = []

    def verify_lookup(self, params):
        self.calls.append(params)
        item = self.script.pop(0)
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
