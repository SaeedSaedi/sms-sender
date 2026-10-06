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


# ---------- validation as you type (live.py) ----------

LIVE = {"HTTP_X_VALIDATE": "1"}


@pytest.fixture
def operator_client(client, make_user, verified):
    verified(client, make_user("op", "operator"))
    return client


def test_a_new_campaign_checks_itself_and_saves_nothing(operator_client):
    response = operator_client.post("/campaigns/new/", {"name": "قیمت", "slug": "Bad Slug!", "template": ""}, **LIVE)
    assert response["Content-Type"] == "application/json"
    errors = response.json()["errors"]
    assert set(errors) >= {"slug", "segment", "template"} and errors["slug"]
    assert not Campaign.objects.exists()


def test_the_settings_check_a_token_as_you_type(operator_client):
    campaign = Campaign.objects.create(slug="oil", name="نفت", settings={"segment": "vip", "template": "t"})
    response = operator_client.post(f"/campaigns/{campaign.slug}/settings/",
                                     {"template": "t", "token_source": "value", "token_value": "a_b"},
                                     **LIVE)  # Kavenegar refuses "_"
    errors = response.json()["errors"]
    assert "token_value" in errors and "_" in errors["token_value"][0]
    campaign.refresh_from_db()
    assert campaign.settings == {"segment": "vip", "template": "t"}  # nothing saved


def test_a_presets_two_forms_answer_together(operator_client):
    from sms_sender_web.campaigns.models import Preset

    response = operator_client.post("/presets/new/", {"template": "t", "slug": "Not ok"}, **LIVE)
    errors = response.json()["errors"]
    assert "slug" in errors and "name" in errors  # the preset's own fields, beside the settings'
    assert not Preset.objects.exists()


def test_an_upload_checks_its_names_without_the_file(operator_client):
    from sms_sender_web.segments.models import Segment

    response = operator_client.post("/segments/upload/", {"name": "", "slug": "upload"}, **LIVE)
    errors = response.json()["errors"]
    assert "slug" in errors  # "upload" is reserved
    assert not Segment.objects.exists()


def test_my_test_number_checks_itself(operator_client):
    response = operator_client.post("/account/", {"test_phone": "123"}, **LIVE)
    assert "test_phone" in response.json()["errors"]


def test_errors_stay_where_a_submit_shows_them(operator_client):
    """Every field's error box is on the page, hidden while empty, so the
    errors found as you type land where a submit would put them."""
    html = operator_client.get("/campaigns/new/").content.decode()
    assert '<form method="post" data-validate>' in html
    assert '<div class="field-errors" id="id_slug_error" hidden>' in html


# ---------- undo, where an action is safe to take back (undo.py) ----------

def test_putting_a_preset_away_can_be_undone(operator_client):
    from sms_sender_web.campaigns.models import Preset

    Preset.objects.create(slug="price", name="قیمت", settings={"template": "t"})
    html = operator_client.post("/presets/price/archive/", follow=True).content.decode()
    toast = html.split('data-undo>')[1].split("</div>")[0]
    assert 'action="/presets/price/archive/"' in toast and "واگرد" in toast
    assert Preset.objects.get().archived_at is not None
    operator_client.post("/presets/price/archive/", follow=True)  # the toast's button
    assert Preset.objects.get().archived_at is None


@pytest.fixture
def scheduled(make_user):
    from datetime import timedelta

    user = make_user("planner", "operator")
    campaign = Campaign.objects.create(slug="coin", name="سکه", settings={"segment": "vip", "template": "t",
                                                                          "tokens": {"token": "x"}})
    Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, state=Job.State.DONE, finished_at=timezone.now(),
                       settings_hash=services.settings_hash(campaign), decision=Job.Decision.APPROVED,
                       decided_by=user, result={"cost_per_sms": 3020})
    at = (timezone.now() + timedelta(days=1)).replace(second=0, microsecond=0)
    services.start_send(campaign, user, at=at, smoke_test=True)
    return campaign, at


def test_taking_a_send_off_its_schedule_can_be_undone(scheduled, operator_client):
    campaign, at = scheduled
    job = Job.objects.get(kind=Job.Kind.SEND, state=Job.State.QUEUED)
    html = operator_client.post("/campaigns/coin/unschedule/", {"job": job.pk}, follow=True).content.decode()
    toast = html.split('data-undo>')[1].split("</div>")[0]
    assert 'action="/campaigns/coin/reschedule/"' in toast
    assert not Job.objects.filter(kind=Job.Kind.SEND, state=Job.State.QUEUED).exists()
    operator_client.post("/campaigns/coin/reschedule/", follow=True)  # the toast's button
    again = Job.objects.get(kind=Job.Kind.SEND, state=Job.State.QUEUED)
    assert again.not_before == at and again.params["smoke_test"] is True  # the same time, one recipient first


def test_an_undo_only_soon_after_and_before_the_time(scheduled, make_user):
    from datetime import timedelta

    campaign, at = scheduled
    user = make_user("other", "operator")
    services.unschedule(Job.objects.get(kind=Job.Kind.SEND, state=Job.State.QUEUED))
    with pytest.raises(services.JobConflict) as late:
        services.reschedule(campaign, user, now=timezone.now() + timedelta(minutes=11))
    assert late.value.code == "too_late"
    with pytest.raises(services.JobConflict):
        services.reschedule(campaign, user, now=at + timedelta(minutes=1))  # its time has passed


def test_the_activity_log_says_when_filters_match_nothing(client, make_user, verified):
    verified(client, make_user("boss", "admin"))
    html = client.get("/activity/?action=api_called").content.decode()
    assert "چیزی با این فیلترها پیدا نشد" in html and 'href="/activity/"' in html
