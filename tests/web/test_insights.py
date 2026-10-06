"""Plan 06, L5: insights. Numbers with something to compare them with: an
alert against its series, a series over time, each segment, how fast SMS
were delivered, how often people get an SMS, the best hour to send, and
how far segments overlap. Read only, and no phone number on any of it."""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("django")

from sms_sender.state import StateStore  # noqa: E402
from sms_sender.window import TEHRAN  # noqa: E402
from sms_sender_web.campaigns.models import Preset  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402
from sms_sender_web.reports import insights  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

pytestmark = pytest.mark.django_db

AT_13 = datetime(2026, 10, 5, 13, 10, tzinfo=TEHRAN).timestamp()
AT_9 = datetime(2026, 10, 5, 9, 5, tzinfo=TEHRAN).timestamp()


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


def _sent(tmp_path, slug, rows, *, sent_at=AT_13):
    """A campaign DB whose rows were all accepted. rows: (phone, segment,
    delivery status or None, own link's clicks or None for no own link)."""
    store = StateStore(tmp_path / "db" / f"{slug}.db")
    store.bind_campaign(slug, {"template": "t"})
    for segment in dict.fromkeys(r[1] for r in rows):
        store.upsert_pending([(r[0], r[0]) for r in rows if r[1] == segment], segment=segment)
    conn = store._conn()
    conn.execute("BEGIN")
    for i, (phone, _segment, delivery, clicks) in enumerate(rows):
        conn.execute("UPDATE recipients SET status='sent', sent_at=?, cost=3000, delivery_status=?, "
                     "delivery_checked_at=?, link_key=? WHERE phone=?",
                     (sent_at, delivery, sent_at + 1800 if delivery else None,
                      phone if clicks is not None else None, phone))
        if clicks is not None:
            conn.execute("INSERT INTO links (key, ref, plan, long_url, title, tags, valid_until, status, "
                         "created_at, clicks) VALUES (?, ?, 'p', 'u', 't', '[]', 'x', 'ready', ?, ?)",
                         (phone, f"r{i:09d}", sent_at, clicks))
        conn.execute("INSERT INTO attempts (phone, kind, outcome, started_at, message_id) "
                     "VALUES (?, 'send', 'accepted', ?, ?)", (phone, sent_at, 1000 + i))
    conn.execute("COMMIT")
    return store


def _phones(n, start=0):
    return [f"0912{i:07d}" for i in range(start, start + n)]


def _alert(tmp_path, preset, slug, *, clicked, of=10, delivered=None, sent_at=AT_13):
    """An alert of `preset`: `of` people sent their own link, `clicked` of
    them clicked; all delivered unless `delivered` says how many."""
    delivered = of if delivered is None else delivered
    rows = [(p, "vip", 10 if i < delivered else 11, 1 if i < clicked else 0) for i, p in enumerate(_phones(of))]
    hour = int(sent_at) // 3600 * 3600
    _sent(tmp_path, slug, rows, sent_at=sent_at).replace_click_hours(hour, {hour: clicked})
    return Campaign.objects.create(slug=slug, name=slug, preset=preset, settings={"segment": "vip"})


@pytest.fixture
def preset():
    return Preset.objects.create(slug="coin", name="قیمت سکه", settings={"template": "t"})


def test_an_alert_against_the_rest_of_its_series(tmp_path, preset):
    _alert(tmp_path, preset, "coin-1", clicked=4)
    _alert(tmp_path, preset, "coin-2", clicked=2, delivered=8)
    third = _alert(tmp_path, preset, "coin-3", clicked=1)
    versus = insights.against_preset(third)
    assert versus["n"] == 2 and versus["url"] == "/analytics/series/coin/"
    rows = {v.key: v for v in versus["rows"]}
    # The others: 18 of 20 delivered (90%), 6 of 20 clicked (30%).
    assert (rows["delivered_rate"].value, rows["delivered_rate"].average) == ("۱۰۰٫۰٪", "۹۰٫۰٪")
    assert rows["delivered_rate"].change.text == "۱۰٫۰ واحد درصد بیشتر"
    assert rows["delivered_rate"].change.tone == "success"
    assert rows["click_rate"].change.text == "۲۰٫۰ واحد درصد کمتر" and rows["click_rate"].change.tone == "danger"
    assert rows["cost_per_sms"].change.text == "تقریباً برابر"
    # Dearer per click: 30,000 rials for 1 click, against 60,000 for 6.
    assert rows["cost_per_click"].change.text == "۲۰۰٪ گران‌تر"
    assert rows["cost_per_click"].change.tone == "danger"


def test_no_comparison_alone_or_without_a_preset(tmp_path, preset):
    first = _alert(tmp_path, preset, "coin-1", clicked=4)
    assert insights.against_preset(first) is None  # nothing else in its series yet
    _sent(tmp_path, "oil", [("09120000001", "vip", 10, None)])
    assert insights.against_preset(Campaign.objects.create(slug="oil", name="oil")) is None


def test_a_series_and_which_way_it_goes(tmp_path, preset):
    for day, clicked in enumerate((5, 5, 3, 2, 1)):  # the click rate falls
        _alert(tmp_path, preset, f"coin-{day}", clicked=clicked, sent_at=AT_13 + day * 86400)
    Campaign.objects.create(slug="coin-9", name="coin-9", preset=preset)  # never sent: not in it
    alerts = insights.alerts_of(preset)
    assert [a.campaign.slug for a in alerts] == ["coin-0", "coin-1", "coin-2", "coin-3", "coin-4"]
    trends = {t["key"]: t for t in insights.trends(alerts)}
    click = trends["click_rate"]
    assert click["tone"] == "danger"
    assert click["text"] == "نرخ کلیک: ۳ هشدار آخر ۲۰٫۰٪، هشدارهای پیش از آن‌ها ۵۰٫۰٪ (۳۰٫۰ واحد درصد کمتر)."
    assert insights.trends(alerts[:3]) == []  # too few to tell


def test_the_series_pages(tmp_path, preset, signed_in):
    for day, clicked in enumerate((5, 5, 3, 2)):
        _alert(tmp_path, preset, f"coin-{day}", clicked=clicked, sent_at=AT_13 + day * 86400)
    Preset.objects.create(slug="quiet", name="بی‌صدا")  # no alerts: not listed
    html = signed_in.get("/analytics/series/").content.decode()
    assert 'href="/analytics/series/coin/"' in html and "quiet" not in html
    assert "نرخ کلیک: ۱۶٫۷ واحد درصد کمتر" in html  # the latest 3 (10 of 30) against the first (5 of 10)
    assert 'href="/analytics/series/" aria-current="page"' in html
    html = signed_in.get("/analytics/series/coin/").content.decode()
    assert html.count("<rect ") == 4 and "۵۰٫۰٪" in html  # a bar per alert, its rate in its title
    assert html.index("coin-3") < html.index("coin-0")  # the latest first in the table
    assert "۴ هشدار" in html


def test_an_empty_series(preset, signed_in):
    assert "هنوز سری‌ای نیست" in signed_in.get("/analytics/series/").content.decode()
    html = signed_in.get("/analytics/series/coin/").content.decode()
    assert "هنوز هیچ‌یک از هشدارهای آن فرستاده نشده است." in html
    assert signed_in.get("/analytics/series/nope/").status_code == 404


def test_the_report_has_each_segment_its_delivery_speed_and_its_series(tmp_path, preset, signed_in):
    _alert(tmp_path, preset, "coin-1", clicked=4)
    _sent(tmp_path, "coin-2", [("09120000001", "vip", 10, 1), ("09120000002", "new", 11, 0)])
    Campaign.objects.create(slug="coin-2", name="coin-2", preset=preset, settings={"segment": "vip"})
    html = signed_in.get("/reports/coin-2/").content.decode()
    segments = html.split('id="by-segment"')[1].split("</section>")[0]
    assert "vip" in segments and "new" in segments and "۱۰۰٫۰٪" in segments
    speed = html.split('id="delivery-speed"')[1].split("</section>")[0]
    assert "تا ۱ ساعت" in speed and "۱ (۵۰٪)" in speed  # one of the two seen delivered within the hour
    versus = html.split('id="versus"')[1].split("</section>")[0]
    assert "«قیمت سکه»" in versus and 'href="/analytics/series/coin/"' in versus


def test_how_often_people_get_an_sms(tmp_path, signed_in):
    _sent(tmp_path, "a", [(p, "vip", 10, None) for p in _phones(3)])
    _sent(tmp_path, "b", [(p, "vip", 10, None) for p in _phones(2)])
    _sent(tmp_path, "c", [(p, "vip", 10, None) for p in _phones(1)])
    now = datetime.fromtimestamp(AT_13 + 3600, tz=TEHRAN)
    fatigue = insights.fatigue(None, now)
    assert fatigue["people"] == {7: 3, 30: 3} and fatigue["sms"] == {7: 6, 30: 6}
    assert [(r["d7"], r["s7"]) for r in fatigue["rows"]] == [(1, 33), (1, 33), (1, 33), (0, 0), (0, 0)]
    from sms_sender.frequency import FrequencyCap

    assert insights.fatigue(FrequencyCap(2, 7), now)["at_cap"] == 2  # two people already got 2 or more


def test_the_best_hour_to_send(tmp_path):
    # At 13:00, 120 people, 12 clicked (10%); at 09:00, 150 people, 30 clicked (20%);
    # at 21:00, 20 people, all clicked: too few to compare.
    _sent(tmp_path, "noon", [(p, "vip", 10, 1 if i < 12 else 0) for i, p in enumerate(_phones(120))])
    _sent(tmp_path, "morning", [(p, "vip", 10, 1 if i < 30 else 0) for i, p in enumerate(_phones(150, 200))],
          sent_at=AT_9)
    _sent(tmp_path, "night", [(p, "vip", 10, 1) for p in _phones(20, 400)],
          sent_at=datetime(2026, 10, 5, 21, 0, tzinfo=TEHRAN).timestamp())
    StateStore(tmp_path / "db" / "noon.db").replace_click_hours(
        int(datetime(2026, 10, 5, 14, 0, tzinfo=TEHRAN).timestamp()), {
            int(datetime(2026, 10, 5, 14, 0, tzinfo=TEHRAN).timestamp()): 40,
            int(datetime(2026, 10, 5, 15, 0, tzinfo=TEHRAN).timestamp()): 7,
        })
    hours = insights.send_hours(datetime.fromtimestamp(AT_13, tz=TEHRAN))
    by_hour = {r["hour"]: r for r in hours["rows"]}
    assert (by_hour[13]["sent"], by_hour[13]["clicked"]) == (120, 12)
    assert by_hour[21]["rate"] is None  # too few
    assert hours["best"]["hour"] == 9 and hours["busiest"]["hour"] == 14
    assert by_hour[14]["clicks"] == 40 and by_hour[14]["sent"] == 0


def _segment(tmp_path, slug, lines):
    segment = Segment.objects.create(slug=slug, name=slug, status=Segment.Status.READY, summary={"valid": len(lines)})
    segment.path.parent.mkdir(parents=True, exist_ok=True)
    segment.path.write_text("phone,user_id\n" + "".join(f"{p},u\n" for p in lines), encoding="utf-8")
    return segment


def test_how_far_segments_overlap(tmp_path):
    a = _segment(tmp_path, "a", _phones(10))
    b = _segment(tmp_path, "b", ["+98" + p[1:] for p in _phones(6, 6)] + ["not a phone"])  # 4 shared with a
    result = insights.overlap([a, b])
    assert (result["once"], result["several"]) == (12, 4)
    row_a, row_b = result["rows"]
    assert (row_a["size"], row_a["cells"][1]["n"], row_a["cells"][1]["share"]) == (10, 4, 40)
    assert (row_b["size"], row_b["cells"][0]["n"], row_b["cells"][0]["share"]) == (6, 4, 67)
    assert row_a["cells"][0]["same"]


def test_a_changed_file_is_read_again(tmp_path):
    a = _segment(tmp_path, "a", _phones(3))
    assert len(insights.phones_of(a)) == 3
    a.path.write_text("phone\n" + "".join(f"{p}\n" for p in _phones(5)), encoding="utf-8")
    assert len(insights.phones_of(a)) == 5


def test_the_audience_page(tmp_path, signed_in):
    _sent(tmp_path, "noon", [(p, "vip", 10, 1 if i < 12 else 0) for i, p in enumerate(_phones(120))])
    for slug in "abcdefg":
        _segment(tmp_path, slug, _phones(10))
    SystemSettings.objects.update_or_create(pk=1, defaults={"frequency_cap_sms": 2, "frequency_cap_days": 7})
    html = signed_in.get("/analytics/audience/?s=a&s=b").content.decode()
    assert 'href="/analytics/audience/" aria-current="page"' in html
    assert "سقف ارسال: حداکثر ۲ پیامک در ۷ روز." in html
    assert "۱۳:۰۰" in html and "۱۰٫۰٪" in html
    overlap = html.split('id="overlap"')[1].split("</section>")[0]
    assert "۱۰ شماره دارند" in overlap and '۱۰ <span class="muted">(۱۰۰٪)</span>' in overlap
    assert "0912" not in html  # numbers only, nobody's number
    html = signed_in.get("/analytics/audience/?" + "&".join(f"s={s}" for s in "abcdefg")).content.decode()
    assert "حداکثر ۶ گروه مخاطبان انتخاب کنید" in html


def test_an_empty_audience(signed_in):
    html = signed_in.get("/analytics/audience/").content.decode()
    assert "در ۳۰ روز گذشته برای کسی پیامکی فرستاده نشده است." in html
    assert "سقف ارسال تعیین نشده است" in html


def test_a_preset_links_to_its_series(tmp_path, preset, signed_in):
    _alert(tmp_path, preset, "coin-1", clicked=1)
    html = signed_in.get("/presets/").content.decode()
    assert 'href="/analytics/series/coin/"' in html
