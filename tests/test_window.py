from datetime import datetime, time, timezone

import pytest

from sms_sender.window import TEHRAN, SendWindow, parse_window


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 4, hour, minute, tzinfo=TEHRAN)


def test_parse_window():
    assert parse_window("08:00-21:00") == SendWindow(time(8, 0), time(21, 0))
    assert parse_window(" 8:30 - 21:15 ") == SendWindow(time(8, 30), time(21, 15))
    for off in ("off", "OFF", "", None, "none"):
        assert parse_window(off) is None


@pytest.mark.parametrize("spec", ["8-21", "25:00-21:00", "08:00-08:00", "08:60-21:00", "always"])
def test_parse_window_rejects_nonsense(spec):
    with pytest.raises(ValueError):
        parse_window(spec)


def test_window_includes_its_start_but_not_its_end():
    w = parse_window("08:00-21:00")
    assert not w.contains(at(7, 59))
    assert w.contains(at(8, 0))
    assert w.contains(at(20, 59))
    assert not w.contains(at(21, 0))


def test_window_is_in_tehran_time_whatever_the_input_zone():
    w = parse_window("08:00-21:00")
    # 16:30 UTC is 20:00 in Tehran (UTC+03:30); 17:30 UTC is 21:00.
    assert w.contains(datetime(2026, 10, 4, 16, 30, tzinfo=timezone.utc))
    assert not w.contains(datetime(2026, 10, 4, 17, 30, tzinfo=timezone.utc))


def test_window_across_midnight():
    w = parse_window("22:00-02:00")
    assert w.contains(at(23, 0)) and w.contains(at(1, 59))
    assert not w.contains(at(2, 0)) and not w.contains(at(12, 0))
