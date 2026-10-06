"""Plan 06, L6: the polish. The control room's numbers with context (the
last 7 days against the last 30, what an SMS costs, the credit as people
say it), its most pressing item with what to do about it, links that open
where each person can act, and the page's loading states."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.utils import timezone  # noqa: E402

from sms_sender_web.dashboard import control  # noqa: E402
from sms_sender_web.dashboard.activity import Totals  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.system.models import ProviderCheck  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


def _by_label(figures):
    return {str(f["label"]): f for f in figures}


def test_the_rates_against_the_last_30_days():
    week = Totals(accepted=100, delivered=95, delivery_known=100, own_links=100, clicked=5, clicks=8, cost=302_000)
    month = Totals(accepted=400, delivered=360, delivery_known=400, own_links=400, clicked=12, clicks=20,
                   cost=1_208_000)
    f = _by_label(control.figures(Totals(accepted=10, cost=30_200), week, month))
    delivered, clicked = f["تحویل‌شده · ۷ روز"], f["نرخ کلیک · ۷ روز"]
    assert (delivered["sub"], delivered["tone"]) == ("۵٫۰ واحد درصد بیشتر از میانگین ۳۰ روز", "success")
    assert (clicked["sub"], clicked["tone"]) == ("۲٫۰ واحد درصد بیشتر از میانگین ۳۰ روز", "success")
    assert f["هزینه · امروز"]["sub"] == "هر پیامک ۳٬۰۲۰ ریال"


def test_no_comparison_before_there_is_an_older_day():
    week = Totals(accepted=100, delivered=95, delivery_known=100)
    f = _by_label(control.figures(Totals(), week, week))  # the 30 days are these 7
    assert f["تحویل‌شده · ۷ روز"]["sub"] == "از ۱۰۰ پیامک با گزارش تحویل" and "tone" not in f["تحویل‌شده · ۷ روز"]


def test_the_credit_as_people_say_it():
    check = ProviderCheck.load()
    check.credit, check.checked_at = 264_731_842, timezone.now()
    check.save()
    panel = control.credit(Totals())
    assert (panel["amount"], panel["unit"]) == ("۲۶۴٫۷", "میلیون ریال")
    check.credit = 950_000
    check.save()
    panel = control.credit(Totals())
    assert (panel["amount"], panel["unit"]) == ("۹۵۰٬۰۰۰", "ریال")


@pytest.fixture
def awaiting_alert():
    from sms_sender_web.campaigns.models import Preset

    preset = Preset.objects.create(slug="price", name="قیمت", settings={"template": "t"})
    alert = Campaign.objects.create(slug="price-1", name="قیمت ۱", preset=preset,
                                    settings={"segment": "vip", "template": "t", "tokens": {"token": "x"}})
    Job.objects.create(campaign=alert, kind=Job.Kind.TEST, state=Job.State.DONE, finished_at=timezone.now(),
                       settings_hash=services.settings_hash(alert))
    return alert


def test_the_most_pressing_item_says_what_to_do(awaiting_alert, client, make_user, verified):
    verified(client, make_user("op", "operator"))
    html = client.get("/").content.decode()
    needs = html.split('id="needs-you"')[1].split("</section>")[0]
    assert 'class="is-first tone-warning"' in needs
    assert '<a class="button small" href="/compose/c/price-1/">بررسی و تأیید</a>' in needs


def test_a_viewer_is_sent_where_they_can_look(awaiting_alert, signed_in):
    html = signed_in.get("/").content.decode()
    needs = html.split('id="needs-you"')[1].split("</section>")[0]
    assert 'href="/campaigns/price-1/"' in needs and "/compose/" not in needs  # the composer would refuse them
    assert ">باز کردن</a>" in needs and "بررسی و تأیید" not in needs  # a viewer can't approve


def test_the_page_shows_it_is_loading(signed_in):
    html = signed_in.get("/").content.decode()
    assert '<div class="page-progress" aria-hidden="true"></div>' in html


def test_downloads_are_marked_as_downloads(signed_in, tmp_path):
    from sms_sender.state import StateStore

    StateStore(tmp_path / "db" / "oil.db").upsert_pending([("09120000001", "09120000001")])
    html = signed_in.get("/reports/oil/").content.decode()
    assert 'href="/reports/oil/summary.csv"' in html
    for tag in html.split("<a ")[1:]:
        if ".csv" in tag.split(">")[0]:
            assert " download" in tag.split(">")[0], tag.split(">")[0]


def test_the_parts_that_refresh_as_you_type_can_shimmer():
    from pathlib import Path

    import sms_sender_web

    root = Path(sms_sender_web.__file__).parent / "campaigns" / "templates" / "campaigns"
    for path, marker in (("compose/page.html", 'id="compose-preview" data-skeleton'),
                         ("compose/page.html", 'id="compose-counts" data-skeleton'),
                         ("settings.html", 'id="preview" data-skeleton'),
                         ("stage/_recipients_preview.html", 'id="recipients-preview" data-skeleton')):
        assert marker in (root / path).read_text(encoding="utf-8"), path
