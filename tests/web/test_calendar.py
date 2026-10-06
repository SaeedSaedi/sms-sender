"""Plan 06, L4: the calendar. A Solar Hijri month of sends, Saturday first
in Tehran time: each send on the day it started, one waiting for its time on
the day it's set for, and the months before and after one click away."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("django")

from sms_sender.window import TEHRAN  # noqa: E402
from sms_sender_web.dashboard import calendar as months  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=TEHRAN)  # Tuesday 14 Mehr 1405


@pytest.fixture
def campaign():
    return Campaign.objects.create(slug="coin", name="قیمت سکه", settings={"segment": "vip"})


def test_the_month_asked_for_or_this_one():
    assert months.month_of("1405-07", NOW) == (1405, 7)
    assert months.month_of("1405-7", NOW) == (1405, 7)
    for wrong in ("", None, "2026-10", "1405-13", "x", "1405/07"):
        assert months.month_of(wrong, NOW) == (1405, 7)


def test_mehr_starts_on_a_wednesday_and_has_thirty_days():
    month = months.month(1405, 7, NOW)
    assert month["title"] == "مهر ۱۴۰۵"
    first_week = month["weeks"][0]
    assert first_week[:4] == [None] * 4 and first_week[4]["number"] == "۱"  # Sat Sun Mon Tue, then Wed
    days = [c for week in month["weeks"] for c in week if c]
    assert len(days) == 30 and all(len(week) == 7 for week in month["weeks"])
    today = [c for c in days if c["today"]]
    assert [c["day"] for c in today] == [14] and all(c["past"] for c in days[:13])
    assert (month["previous"], month["next"]) == ("1405-06", "1405-08")
    assert months.month(1405, 12, NOW)["next"] == "1406-01"
    assert months.month(1405, 1, NOW)["previous"] == "1404-12"


def test_each_send_on_its_day(campaign):
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.DONE,
                       started_at=NOW - timedelta(hours=2), finished_at=NOW)
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.FAILED,
                       started_at=NOW - timedelta(days=4))
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.QUEUED,
                       not_before=NOW + timedelta(days=6, hours=1))
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.CANCELLED,
                       result={"withdrawn": True})  # never started: not on it
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.DONE,
                       started_at=NOW - timedelta(days=40))  # another month
    month = months.month(1405, 7, NOW)
    by_day = {c["day"]: [(e.stage, e.time) for e in c["entries"]] for c in month["days"]}
    assert by_day == {10: [("stopped", "۱۲:۰۰")], 14: [("completed", "۱۰:۰۰")], 20: [("scheduled", "۱۳:۰۰")]}
    assert [e.stage for e in month["stages"]] == ["stopped", "completed", "scheduled"]
    assert month["count"] == 3


def test_the_page(campaign, signed_in):
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.QUEUED,
                       not_before=datetime(2026, 10, 12, 9, 30, tzinfo=TEHRAN))  # 20 Mehr
    html = signed_in.get("/calendar/?month=1405-07").content.decode()
    assert "<h2 id=\"month-title\">" in html and "مهر ۱۴۰۵" in html
    assert 'href="?month=1405-06" rel="prev"' in html and 'href="?month=1405-08" rel="next"' in html
    assert html.count('href="/campaigns/coin/"') == 2  # in the month, and in the phone's list
    assert "<h3>دوشنبه ۲۰ مهر</h3>" in html
    assert 'href="/calendar/" aria-current="true"' in html


def test_an_empty_month_says_so(signed_in):
    html = signed_in.get("/calendar/?month=1404-01").content.decode()
    assert "این ماه ارسالی نیست." in html and "فروردین ۱۴۰۴" in html
    assert 'href="/calendar/">این ماه</a>' in html  # back to this month


def test_the_control_room_links_to_it(signed_in):
    assert 'href="/calendar/">کل ماه</a>' in signed_in.get("/").content.decode()
