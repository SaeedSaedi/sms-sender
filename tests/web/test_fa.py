"""Persian display rules (spec 4.11): digits, separators, the Solar Hijri
calendar in Tehran time, and left-to-right isolation."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("django")

from sms_sender_web.dashboard.templatetags.fa import (  # noqa: E402
    delivery_label,
    fa_digits,
    fa_number,
    jalali,
    ltr,
    status_label,
)


def test_numbers_get_persian_digits_and_separators():
    assert fa_digits("0912 345") == "۰۹۱۲ ۳۴۵"
    assert fa_number(1234567) == "۱٬۲۳۴٬۵۶۷"
    assert fa_number("12") == "۱۲"
    assert fa_number("n/a") == "n/a"


@pytest.mark.parametrize("utc, expected", [
    # Nowruz 1405: the equinox fell after noon in Tehran on 20 March 2026.
    (datetime(2026, 3, 20, 20, 30, tzinfo=timezone.utc), "۱۴۰۵/۰۱/۰۱ ۰۰:۰۰"),
    # 30 Esfand 1403 exists: 1403 is a leap year (some arithmetic calendars
    # get this one wrong).
    (datetime(2025, 3, 20, 8, 0, tzinfo=timezone.utc), "۱۴۰۳/۱۲/۳۰ ۱۱:۳۰"),
    (datetime(2025, 3, 20, 20, 30, tzinfo=timezone.utc), "۱۴۰۴/۰۱/۰۱ ۰۰:۰۰"),
    # Mehr 1405, during this project.
    (datetime(2026, 10, 4, 10, 22, tzinfo=timezone.utc), "۱۴۰۵/۰۷/۱۲ ۱۳:۵۲"),
])
def test_dates_are_solar_hijri_in_tehran_time(utc, expected):
    assert jalali(utc) == expected


def test_unix_seconds_and_missing_values():
    assert jalali(datetime(2026, 10, 4, 10, 22, tzinfo=timezone.utc).timestamp(), "%Y/%m/%d") \
        == "۱۴۰۵/۰۷/۱۲"
    assert jalali(None) == ""


def test_left_to_right_values_are_isolated_and_escaped():
    assert str(ltr("coin-7")) == '<bdi dir="ltr">coin-7</bdi>'
    assert str(ltr("<b>")) == '<bdi dir="ltr">&lt;b&gt;</bdi>'


def test_status_names_come_from_the_glossary():
    assert str(status_label("sent")) == "پذیرفته‌شده"
    assert str(status_label("failed_retriable")) == "ارسال‌نشده"
    assert str(delivery_label(10)) == "تحویل‌شده"
    assert str(delivery_label(4)) == "ارسال‌شده به مخابرات"
    assert str(delivery_label(None)) == "استعلام‌نشده"
