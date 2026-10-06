"""Plan 06, L4: the control room. How sending went today and in the last 7
days, read from every campaign DB without changing one; the sends on their
way with their pace and time left; the credit and how far it goes; this
week; and the latest campaigns with their numbers."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta

import pytest

pytest.importorskip("django")

from django.utils import timezone  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender.window import TEHRAN  # noqa: E402
from sms_sender_web.dashboard import activity, control  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.system.models import ProviderCheck  # noqa: E402

pytestmark = pytest.mark.django_db

NOW = time.time()
DAY = 24 * 3600


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


def _db(tmp_path, slug, rows):
    """rows: (phone, sent_at, cost, delivery_status, own link clicks or None)."""
    store = StateStore(tmp_path / "db" / f"{slug}.db")
    store.bind_campaign(slug, {"template": "t"})
    store.upsert_pending([(phone, phone) for phone, *_ in rows])
    conn = store._conn()
    conn.execute("BEGIN")
    for phone, sent_at, cost, delivery, clicks in rows:
        link = phone if clicks is not None else None
        conn.execute("UPDATE recipients SET status='sent', sent_at=?, cost=?, delivery_status=?, link_key=? "
                     "WHERE phone=?", (sent_at, cost, delivery, link, phone))
        if clicks is not None:
            conn.execute("INSERT INTO links (key, ref, plan, long_url, title, tags, valid_until, status, created_at, "
                         "clicks) VALUES (?, ?, 'p', 'u', 't', '[]', 'x', 'ready', ?, ?)",
                         (phone, f"r{phone[-4:]}", sent_at, clicks))
    conn.execute("COMMIT")
    return store


@pytest.fixture
def sent(tmp_path):
    today, three_days_ago, month_ago = NOW - 60, NOW - 3 * DAY, NOW - 40 * DAY
    coin = _db(tmp_path, "coin", [
        ("09120000001", today, 3020, 10, 2),
        ("09120000002", today, 3020, 11, 0),
        ("09120000003", three_days_ago, 3020, None, None),
        ("09120000004", month_ago, 3020, 10, None),
    ])
    coin.replace_click_hours(0, {int(today) // 3600 * 3600: 5, int(three_days_ago) // 3600 * 3600: 2})
    # A test SMS's call record, with its cost.
    coin._conn().execute("INSERT INTO attempts (phone, kind, outcome, started_at, cost) "
                         "VALUES ('09120000099', 'test', 'accepted', ?, 3020)", (today,))
    coin._conn().commit()
    _db(tmp_path, "oil", [("09120000005", today, 6040, 10, None)])
    return coin


def test_activity_adds_up_today_the_week_and_all_time(sent):
    totals = activity.overall(activity.folder_activity())
    today, week, everything = totals["today"], totals["week"], totals["all"]
    assert (today.accepted, today.delivered, today.delivery_known) == (3, 2, 3)
    assert (today.own_links, today.clicked, today.clicks) == (2, 1, 5)
    assert today.cost == 3020 * 2 + 6040 + 3020  # two SMS, the other campaign's, and the test SMS
    assert (week.accepted, week.clicks) == (4, 7)
    assert everything.accepted == 5
    assert today.click_rate == 0.5 and round(today.delivered_rate, 3) == 0.667


def test_reading_changes_no_campaign_db(sent, tmp_path):
    path = tmp_path / "db" / "coin.db"
    before = path.read_bytes()
    activity.folder_activity()
    assert path.read_bytes() == before


def test_an_old_or_broken_db_is_read_as_far_as_it_goes(tmp_path):
    old = sqlite3.connect(tmp_path / "db" / "old.db")  # schema 1: no cost, links or click hours
    old.execute("CREATE TABLE recipients (phone TEXT PRIMARY KEY, raw TEXT, status TEXT, sent_at REAL)")
    old.execute("INSERT INTO recipients VALUES ('09120000001', 'x', 'sent', ?)", (NOW,))
    old.commit()
    old.close()
    (tmp_path / "db" / "broken.db").write_bytes(b"not a database at all, just bytes" * 100)
    found = activity.folder_activity()
    assert found["old"]["today"].accepted == 1 and "broken" not in found


def test_the_control_room_shows_the_figures(sent, signed_in):
    html = signed_in.get("/").content.decode()
    assert "اتاق کنترل" in html
    strip = html.split('class="figures control-figures"')[1].split("</ul>")[0]
    assert "پیامک پذیرفته‌شده · امروز" in strip and '<span class="figure-value num">۳</span>' in strip
    assert "۷ روز: ۴" in strip  # accepted in the last 7 days
    assert "۵۰٫۰٪" in strip  # click rate: one of the two with their own link
    assert "۱۵٬۱۰۰ ریال" in strip  # spent today, the test SMS included


def test_a_send_on_its_way_shows_its_pace_and_time_left(segment_campaign, signed_in):
    started = timezone.now() - timedelta(seconds=100)
    job = Job.objects.create(campaign=segment_campaign, kind=Job.Kind.SEND, state=Job.State.RUNNING,
                             started_at=started, progress={"total": 1000, "processed": 200, "sent": 190,
                                                           "failed_retriable": 4, "failed_permanent": 5,
                                                           "unknown": 1})
    sends = control.active_sends()
    assert len(sends) == 1 and round(sends[0].per_second) == 2
    assert sends[0].left == "حدود ۷ دقیقه مانده"  # 800 more at 2 a second
    html = signed_in.get("/").content.decode()
    assert 'id="active-sends" hx-get="/home/sends/"' in html and "۲۰۰</strong> / ۱٬۰۰۰" in html
    assert signed_in.get("/home/sends/").status_code == 200
    Job.objects.filter(pk=job.pk).update(state=Job.State.DONE)
    assert signed_in.get("/home/sends/").status_code == 286  # nothing on its way: the polling stops


@pytest.fixture
def segment_campaign():
    return Campaign.objects.create(slug="coin", name="قیمت سکه", settings={"segment": "vip"})


def test_time_left_in_words():
    assert control.time_left(None) == ""
    assert control.time_left(20) == "کمتر از یک دقیقه مانده"
    assert control.time_left(3 * 3600 + 25 * 60) == "حدود ۳ ساعت و ۲۵ دقیقه مانده"


def test_the_credit_says_how_far_it_goes(segment_campaign, sent):
    check = ProviderCheck.load()
    check.credit, check.checked_at = 1_000_000, timezone.now()
    check.save()
    for cost in (100_000, 300_000):
        Job.objects.create(campaign=segment_campaign, kind=Job.Kind.SEND, state=Job.State.DONE,
                           finished_at=timezone.now() - timedelta(days=2), result={"cost": cost})
    month = activity.overall(activity.folder_activity())["month"]
    panel = control.credit(month)
    # Sends cost 200,000 on average; the last 30 days spent 18,120 (604 a day).
    assert panel["runway"] == "با روند اخیر، برای حدود ۵ ارسال دیگر یا ۱٬۶۵۵ روز کافی است."


def test_the_week_shows_what_went_and_what_is_set(segment_campaign):
    now = datetime(2026, 10, 6, 12, 0, tzinfo=TEHRAN)  # a Tuesday
    Job.objects.create(campaign=segment_campaign, kind=Job.Kind.SEND, state=Job.State.DONE,
                       started_at=now - timedelta(hours=3))
    Job.objects.create(campaign=segment_campaign, kind=Job.Kind.SEND, state=Job.State.QUEUED,
                       not_before=now + timedelta(days=1))
    week = control.week(now)
    assert [d["name"] for d in week["days"]][0] == "ش"  # Saturday first
    tuesday, wednesday = week["days"][3], week["days"][4]
    assert tuesday["today"] and (tuesday["sent"], tuesday["scheduled"]) == (1, 0)
    assert (wednesday["sent"], wednesday["scheduled"]) == (0, 1)
    assert wednesday["summary"] == "چهارشنبه ۱۵: ۰ ارسال‌شده، ۱ زمان‌بندی‌شده"
    assert [s["campaign"] for s in week["scheduled"]] == [segment_campaign]


def test_the_latest_campaigns_have_their_numbers(sent, signed_in):
    html = signed_in.get("/").content.decode()
    table = html.split('id="recent-title"')[1].split("</table>")[0]
    assert "coin" in table and "oil" in table
    assert "۵۰٫۰٪" in table  # coin's click rate
    assert 'href="/campaigns/"' in html  # every campaign, one click away


def test_the_menu_has_the_control_room_and_the_campaign_list(signed_in):
    html = signed_in.get("/").content.decode()
    assert '<a href="/" aria-current="true">' in html
    html = signed_in.get("/campaigns/").content.decode()
    assert '<a href="/campaigns/" aria-current="true">' in html
