"""Plan 05, P3: the report's funnel (accepted → delivered → clicked) and
clicks over time, drawn on the server; and every campaign side by side."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("django")

from sms_sender.clicks import sync_clicks  # noqa: E402
from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.reports.charts import HOURLY_UP_TO, WIDTH, clicks_chart, funnel  # noqa: E402

from ..test_clicks import Visits  # noqa: E402
from ..test_clicks import campaign_db as clicks_campaign  # noqa: E402

pytestmark = pytest.mark.django_db

T0 = int(datetime(2026, 10, 5, 6, tzinfo=timezone.utc).timestamp())  # 09:30 in Tehran


@pytest.fixture
def store(settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    store = clicks_campaign(tmp_path / "coin-7.db")  # A, B, C accepted, A delivered
    sync_clicks(store, Visits({"c1": 2, "c2": 1, "c3": 0}), "coin-7")
    return store


def test_the_funnel_goes_from_accepted_to_clicked(store):
    steps = funnel(store, store.display_counts())
    assert [(s.key, s.n, s.share) for s in steps] == [("accepted", 3, 100), ("delivered", 1, 33), ("clicked", 2, 67)]
    plain = StateStore(Path(store.db_path).parent / "plain.db")
    assert funnel(plain, plain.display_counts()) == []  # nothing accepted, nothing to show


def test_clicks_over_time_go_by_hour_then_by_day():
    chart = clicks_chart([(T0, 2), (T0 + 2 * 3600, 5)])
    assert chart.by_hour and [n for _, n in chart.rows] == [2, 0, 5]  # the quiet hour is drawn too
    assert (chart.total, chart.peak) == (7, 5)
    first, last = chart.bars[0], chart.bars[-1]
    assert float(first.x) > float(last.x)  # the earliest on the right
    assert float(last.h) == 160 and float(first.h) == 64 and float(chart.bars[1].h) == 0
    assert all("." in bar.w and "٫" not in bar.x for bar in chart.bars)  # SVG wants plain numbers
    assert float(first.x) + float(first.w) <= WIDTH
    days = clicks_chart([(T0, 1), (T0 + (HOURLY_UP_TO + 30) * 3600, 3)])
    assert not days.by_hour and len(days.rows) == 5 and days.rows[0][0] == "۱۴۰۵/۰۷/۱۳"
    assert clicks_chart([]) is None


def test_the_report_draws_both(signed_in, store):
    store.replace_click_hours(0, {T0: 2, T0 + 3600: 1})
    html = signed_in.get("/reports/coin-7/").content.decode()
    assert 'id="funnel"' in html and "تحویل‌شده" in html and "(۶۷٪)" in html
    assert 'id="clicks-chart"' in html and html.count("<rect ") == 2
    assert "در مجموع ۳ کلیک؛ بیشترین در یک ساعت: ۲" in html and "قدیمی‌ترین در سمت راست است" in html
    assert "به صورت جدول" in html  # the same numbers, for screen readers and copying


def test_every_campaign_side_by_side(signed_in, store, tmp_path):
    other = StateStore(tmp_path / "vip-9.db")  # a CLI campaign: no row in the dashboard
    other.upsert_pending([("09120000099", "09120000099")])
    html = signed_in.get("/analytics/").content.decode()
    assert 'href="/reports/coin-7/"' in html and 'href="/reports/vip-9/"' in html
    assert "(۶۷٪)" in html  # clicked, of accepted
    assert "هزینه هر کلیک" in html
    for phone in ("09120000001", "09120000099"):
        assert phone not in html
