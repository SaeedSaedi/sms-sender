import pytest

from sms_sender.classifier import (
    HALT_CODES,
    PERMANENT_CODES,
    RETRY_CODES,
    Action,
    classify,
)


def test_success():
    assert classify(200) is Action.SUCCESS


@pytest.mark.parametrize("code", sorted(HALT_CODES))
def test_halt_codes(code):
    assert classify(code) is Action.HALT


@pytest.mark.parametrize("code", sorted(RETRY_CODES))
def test_retry_codes(code):
    assert classify(code) is Action.RETRY


@pytest.mark.parametrize("code", sorted(PERMANENT_CODES))
def test_permanent_codes(code):
    assert classify(code) is Action.PERMANENT


def test_unknown_defaults_to_permanent():
    assert classify(999) is Action.PERMANENT


def test_none_is_retry():
    # No code parsed → assume transient (network/timeout shape).
    assert classify(None) is Action.RETRY


def test_no_overlap_between_classes():
    assert HALT_CODES & RETRY_CODES == set()
    assert HALT_CODES & PERMANENT_CODES == set()
    assert RETRY_CODES & PERMANENT_CODES == set()


@pytest.mark.parametrize("code", [420, 427, 429, 501])
def test_account_level_codes_halt(code):
    # Every message would fail the same way — stop at the first one.
    assert classify(code) is Action.HALT


def test_ip_rate_limit_retries():
    assert classify(451) is Action.RETRY


@pytest.mark.parametrize("code", [414, 419])
def test_request_shape_codes_are_permanent(code):
    # Retrying the identical request can't succeed.
    assert classify(code) is Action.PERMANENT
