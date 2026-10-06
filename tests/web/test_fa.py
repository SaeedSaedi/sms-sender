"""Persian display rules (spec 4.11): digits, separators, the Solar Hijri
calendar in Tehran time, and left-to-right isolation."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("django")

from sms_sender_web.dashboard.templatetags.fa import (  # noqa: E402
    ago,
    delivery_label,
    fa_digits,
    fa_number,
    jalali,
    jalali_when,
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


def test_a_date_beyond_the_solar_hijri_calendar_shows_instead_of_failing():
    """Kavenegar's "never expires" is 9999-12-31: no Solar Hijri year reaches
    it, and a page must still render."""
    assert jalali(datetime(9999, 12, 31, 20, 30, tzinfo=timezone.utc)) == "9999-12-31"


# Tuesday 14 Mehr 1405, 15:00 in Tehran.
NOW = datetime(2026, 10, 6, 11, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("utc, expected", [
    (datetime(2026, 10, 6, 9, 32, tzinfo=timezone.utc), "امروز ۱۳:۰۲"),
    (datetime(2026, 10, 5, 7, 15, tzinfo=timezone.utc), "دیروز ۱۰:۴۵"),
    (datetime(2026, 10, 3, 5, 45, tzinfo=timezone.utc), "شنبه ۰۹:۱۵"),  # within the week: its day
    (datetime(2026, 9, 28, 9, 30, tzinfo=timezone.utc), "۱۴۰۵/۰۷/۰۶ ۱۳:۰۰"),  # older: the date
    # 23:00 UTC on the 5th is already the 6th in Tehran: today.
    (datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc), "امروز ۰۰:۳۰"),
])
def test_a_moment_in_a_few_words(utc, expected):
    assert jalali_when(utc, NOW) == expected
    assert jalali_when(utc.timestamp(), NOW) == expected  # unix seconds too


def test_how_long_ago():
    assert ago(NOW, NOW) == "همین حالا"
    assert ago(datetime(2026, 10, 6, 11, 25, tzinfo=timezone.utc), NOW) == "۵ دقیقه پیش"
    assert ago(datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc), NOW) == "۳ ساعت پیش"
    assert ago(datetime(2026, 10, 5, 7, 15, tzinfo=timezone.utc), NOW) == "دیروز ۱۰:۴۵"
    assert ago(None) == "" and jalali_when(None) == ""
