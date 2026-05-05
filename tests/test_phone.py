import pytest

from sms_sender.phone import InvalidPhoneError, normalize


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("09123456789", "09123456789"),
        ("9123456789", "09123456789"),
        ("+989123456789", "09123456789"),
        ("00989123456789", "09123456789"),
        ("989123456789", "09123456789"),
        ("0912 345 6789", "09123456789"),
        ("0912-345-6789", "09123456789"),
        ("(0912) 345 6789", "09123456789"),
        ("  09123456789  ", "09123456789"),
        ("۰۹۱۲۳۴۵۶۷۸۹", "09123456789"),       # Persian digits
        ("٠٩١٢٣٤٥٦٧٨٩", "09123456789"),       # Arabic-Indic digits
    ],
)
def test_normalize_accepts(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abcdef",
        "0812345678",       # not 9-prefix
        "0912345",          # too short
        "091234567890",     # too long
        "+1234567890",      # wrong country
        "98123456789",      # 11 digits, not valid form (would be country code without leading 0)
    ],
)
def test_normalize_rejects(raw):
    with pytest.raises(InvalidPhoneError):
        normalize(raw)
