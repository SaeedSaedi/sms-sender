"""Plan 05, P2: before and at sending. The ready step reads the segment
(the CLI's dry-run and preview): each recipient's message, one number's,
and what the short-link service will be asked for. The send starts now or
at a set time (Solar Hijri, Tehran), optionally to one recipient first
(the CLI's --smoke-test). A send waiting for its time locks the settings,
and one whose settings changed after approval sends nothing."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender.window import TEHRAN  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns.forms import parse_when  # noqa: E402
from sms_sender_web.campaigns.lifecycle import lifecycle  # noqa: E402
from sms_sender_web.dashboard.templatetags.fa import jalali  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Job  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402

from .world import TEST_PHONE, build_world  # noqa: E402

pytestmark = pytest.mark.django_db

# 1405/07/13, 09:00 in Tehran.
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=TEHRAN)


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def signed_in(user, verified) -> Client:
    client = Client()
    verified(client, user)
    return client


def send_job(slug: str) -> Job | None:
    return Job.objects.filter(campaign__slug=slug, kind=Job.Kind.SEND).order_by("-id").first()


def counts(campaign) -> dict:
    return StateStore(campaign_db(campaign)).counts()


# ---------- the date and time a person types ----------

def test_a_solar_hijri_date_and_a_tehran_time():
    at, problem = parse_when("۱۴۰۵/۰۷/۱۴", "۰۹:۳۰", now=NOW)
    assert problem is None and at == datetime(2026, 10, 6, 9, 30, tzinfo=TEHRAN)
    assert parse_when("1405-7-14", "9:30", now=NOW) == (at, None)  # Latin digits, dashes, no zeros


@pytest.mark.parametrize("date, time, problem", [
    ("", "09:30", "bad_date"),
    ("1405/07", "09:30", "bad_date"),
    ("1405/13/01", "09:30", "bad_date"),   # there's no 13th month
    ("2026/10/06", "09:30", "gregorian"),  # today's date, in the wrong calendar
    ("1405/07/14", "", "bad_time"),
    ("1405/07/14", "24:00", "bad_time"),
    ("1405/07/14", "9.30", "bad_time"),
    ("1405/07/13", "08:00", "past"),
    ("1405/07/13", "09:00", "past"),       # now: less than a minute ahead
    ("1405/08/20", "09:00", "too_far"),    # 37 days ahead
])
def test_what_a_schedule_refuses(date, time, problem):
    assert parse_when(date, time, now=NOW) == (None, problem)


# ---------- now, or at a set time ----------

def test_a_send_set_for_later_waits_for_its_time(world, verified):
    client = signed_in(world.users["operator"], verified)
    when = timezone.localtime(timezone.now() + timedelta(days=2))
    response = client.post("/campaigns/approved/send/", {
        "when": "later", "date": jalali(when, "%Y/%m/%d"), "time": "۱۰:۱۵", "smoke_test": "on",
    })
    assert response.status_code == 302
    job = send_job("approved")
    assert job.state == Job.State.QUEUED and job.params["smoke_test"] is True
    assert timezone.localtime(job.not_before).strftime("%Y-%m-%d %H:%M") == f"{when:%Y-%m-%d} 10:15"
    assert lifecycle(job.campaign).stage == "scheduled"
    assert AuditEvent.objects.filter(action="send_scheduled", campaign="approved").exists()

    # The worker leaves it until then (the world's other scheduled send too).
    assert Worker(worker_id="w1").claim() is None
    html = client.get("/campaigns/approved/").content.decode()
    assert "زمان آغاز ارسال" in html and str(jalali(job.not_before)) in html
    assert "نخست به یک گیرنده" in html
    assert f"زمان آغاز: {jalali(job.not_before)}" in html  # the history says it too
    Job.objects.filter(pk=job.pk).update(not_before=timezone.now() - timedelta(seconds=1))
    assert Worker(worker_id="w1").claim().pk == job.pk


def test_a_wrong_date_comes_back_with_what_was_typed(world, verified):
    client = signed_in(world.users["operator"], verified)
    response = client.post("/campaigns/approved/send/", {
        "when": "later", "date": "2026/10/20", "time": "09:30", "smoke_test": "on",
    })
    html = response.content.decode()
    assert response.status_code == 200 and send_job("approved") is None
    assert "میلادی" in html  # the reason, under the fields
    assert '<details class="subsection schedule" open>' in html
    assert 'value="2026/10/20" aria-describedby="id_schedule_error" aria-invalid="true"' in html
    assert 'value="09:30" aria-describedby="id_schedule_error">' in html  # the time was fine
    assert 'name="smoke_test" checked' in html


def test_start_now_sends_without_a_schedule(world, verified):
    client = signed_in(world.users["operator"], verified)
    client.post("/campaigns/approved/send/", {"when": "now"})
    job = send_job("approved")
    assert (job.state, job.not_before, job.params["smoke_test"]) == (Job.State.QUEUED, None, False)
    assert lifecycle(job.campaign).stage == "sending"


def test_a_scheduled_send_can_start_at_once(world, verified):
    client = signed_in(world.users["operator"], verified)
    job = send_job("scheduled")
    client.post("/campaigns/scheduled/start_now/", {"job": job.pk})
    job.refresh_from_db()
    assert job.not_before is None and lifecycle(job.campaign).stage == "sending"
    event = AuditEvent.objects.filter(action="send_started", campaign="scheduled").get()
    assert event.detail == {"smoke_test": True, "was_scheduled": True}
    assert Worker(worker_id="w1").claim().pk == job.pk


def test_cancelling_the_schedule_cancels_no_one(world, verified):
    client = signed_in(world.users["operator"], verified)
    campaign = world.campaigns["scheduled"]
    before = counts(campaign)
    job = send_job("scheduled")
    client.post("/campaigns/scheduled/unschedule/", {"job": job.pk})
    job.refresh_from_db()
    assert job.state == Job.State.CANCELLED and job.result == {"withdrawn": True}
    assert counts(campaign) == before
    # Approved still: it can be sent now, or set for another time.
    assert lifecycle(campaign).stage == "approved" and services.settings_locked(campaign) == ""
    html = client.get("/campaigns/scheduled/").content.decode()
    assert 'id="send-form"' in html and "پیش از آغاز پس گرفته شد" in html
    assert AuditEvent.objects.filter(action="send_unscheduled", campaign="scheduled").exists()
    # A second click finds nothing waiting.
    client.post("/campaigns/scheduled/unschedule/", {"job": job.pk})
    assert "دیگر در انتظار زمان تعیین‌شده نیست" in client.get("/campaigns/scheduled/").content.decode()


def test_the_settings_wait_while_a_send_is_scheduled(world, verified):
    client = signed_in(world.users["operator"], verified)
    campaign = world.campaigns["scheduled"]
    assert services.settings_locked(campaign) == "scheduled"
    assert client.get("/campaigns/scheduled/settings/").status_code == 302
    client.post("/campaigns/scheduled/settings/", {"segment": "vip", "template": "other"})
    campaign.refresh_from_db()
    assert campaign.settings["template"] == "coin-price"
    html = client.get("/campaigns/scheduled/").content.decode()
    assert "نخست زمان‌بندی را لغو کنید" in html
    assert 'href="/campaigns/scheduled/settings/"' not in html


class NoRuns(Engine):
    def runner(self, *args, **kwargs):
        raise AssertionError("nothing may run")


def test_a_send_whose_settings_changed_after_approval_sends_nothing(world, verified):
    campaign = world.campaigns["scheduled"]
    job = send_job("scheduled")
    Job.objects.filter(pk=job.pk).update(not_before=None)
    # Past the locked page (a race, say): the test SMS approved another message.
    campaign.settings["template"] = "other-template"
    campaign.save()
    ran = Worker(NoRuns(), worker_id="w1").run_once()
    assert ran.state == Job.State.CANCELLED and ran.started_at is None
    assert ran.result == {"withdrawn": True, "stop_reason": "not_approved"}
    # Back to the test SMS, with nothing locked and the reason in the history.
    assert lifecycle(campaign).stage == "ready" and services.settings_locked(campaign) == ""
    html = signed_in(world.users["operator"], verified).get("/campaigns/scheduled/").content.decode()
    assert "پس هیچ پیامکی ارسال نشد" in html


def test_the_engine_passes_the_smoke_test_on(world):
    runner = Engine().runner(world.campaigns["approved"], reporter=None, smoke_test=True)
    assert runner.smoke_test is True
    assert Engine().runner(world.campaigns["approved"], reporter=None).smoke_test is False


def test_the_send_step_says_what_happens(world, verified):
    html = signed_in(world.users["operator"], verified).get("/campaigns/approved/").content.decode()
    # All six wait in the world's approved campaign.
    assert 'data-confirm="۶ گیرنده این پیامک را دریافت می‌کنند.' in html
    assert "ساخت ۶ لینک تا حدود ۱ دقیقه طول می‌کشد" in html
    assert 'name="when" value="later"' in html and 'placeholder="۱۴' in html  # today, Solar Hijri


# ---------- the ready step: each recipient's message ----------

def test_each_recipients_message_before_the_test(world, verified):
    html = signed_in(world.users["operator"], verified).get("/campaigns/fresh/").content.decode()
    assert html.count('class="row-text"') == 5  # the first five of six
    assert "۰۹۱۲*****۰۱" in html and "09120000001" not in html
    assert "Ali عزیز، قیمت نفت امروز اعلام شد: " in html and "/aB3dE" in html
    assert "گیرندگان معتبر" in html and '<span class="num">۶</span>' in html
    # What the short-link service will be asked for: nothing personal in it.
    assert "درخواست ساخت لینک کوتاه" in html and "utm_campaign=fresh" in html and "&amp;r=" in html
    assert "campaign-fresh" in html and "مقدار r تصادفی است" in html
    # The test SMS makes its own link only; the recipients' wait for the send.
    assert "لینک کوتاه مخصوص خودش را دارد" in html
    # The number search is a POST: no number in a URL or an access log.
    assert 'method="post" action="/campaigns/fresh/preview/#preview-heading"' in html


def test_one_numbers_message_and_more_rows(world, verified):
    client = signed_in(world.users["operator"], verified)
    one = client.post("/campaigns/fresh/preview/", {"preview": "۰۹۱۲۰۰۰۰۰۰۳", "rows": "5"},
                      HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in one and one.count('class="row-text"') == 1 and "۰۹۱۲*****۰۳" in one
    assert 'value="۰۹۱۲۰۰۰۰۰۰۳"' in one  # the search box keeps it
    none = client.post("/campaigns/fresh/preview/", {"preview": "09129999999"}, HTTP_HX_REQUEST="true")
    assert "این شماره در گروه مخاطبان این کمپین نیست" in none.content.decode()
    twenty = client.post("/campaigns/fresh/preview/", {"rows": "20"}, HTTP_HX_REQUEST="true").content.decode()
    assert twenty.count('class="row-text"') == 6 and '<option value="20" selected>' in twenty
    odd = client.post("/campaigns/fresh/preview/", {"rows": "1000"}, HTTP_HX_REQUEST="true").content.decode()
    assert odd.count('class="row-text"') == 5
    # Without JavaScript, the whole page comes back with the answer.
    page = client.post("/campaigns/fresh/preview/", {"preview": "09120000003"})
    assert page.status_code == 200 and page.content.decode().count('class="row-text"') == 1


def test_a_viewer_sees_the_message_but_not_each_recipient(world, verified):
    client = signed_in(world.users["viewer"], verified)
    html = client.get("/campaigns/fresh/").content.decode()
    assert 'class="sms-bubble"' in html
    assert 'id="preview-search"' not in html and 'class="row-text"' not in html
    assert client.post("/campaigns/fresh/preview/", {"preview": "09120000001"}).status_code == 403


def test_the_live_part_keeps_the_check_after_a_failed_test(world, verified):
    campaign = world.campaigns["fresh"]
    now = timezone.now()
    Job.objects.create(
        campaign=campaign, kind=Job.Kind.TEST, state=Job.State.FAILED, requested_by=world.users["operator"],
        params={"test_number": TEST_PHONE}, settings_hash=services.settings_hash(campaign),
        result={"stop_reason": "provider_halt", "stop_fields": {"code": 418}}, started_at=now, finished_at=now,
    )
    html = signed_in(world.users["operator"], verified).get("/campaigns/fresh/live/").content.decode()
    assert 'id="check-form"' in html and 'class="sms-bubble"' in html and 'class="row-text"' in html


# ---------- the check, as a checklist ----------

def test_the_check_reads_as_a_checklist(world, verified):
    html = signed_in(world.users["operator"], verified).get("/campaigns/fresh/").content.decode()
    checks = html.split('id="checklist"')[1].split("</ul>")[0]
    assert checks.count('class="check-ok"') == 6 and "check-fail" not in checks
    assert "مشتریان ویژه" in checks and "۶ شماره معتبر." in checks
    assert '<bdi dir="ltr">coin-price</bdi>' in checks  # an identifier keeps its characters
    assert '<bdi dir="ltr">https://kifpool.me/wallet</bdi>' in checks
    assert "۶ گیرنده برای ارسال." in checks
    assert '<span class="visually-hidden">انجام شد:</span>' in checks  # not by colour alone


def test_each_finding_has_its_line():
    from sms_sender_web.campaigns.checks import CheckResult
    from sms_sender_web.campaigns.preview import Preview
    from sms_sender_web.campaigns.present import checklist

    settings = {"template": "coin-price", "links": {"token": "token20", "destination": "https://evil.example/x"}}
    check = CheckResult(problems=["bad_destination", "nobody_to_send"], valid=3, window_open=False)
    message = Preview(template="coin-price", known=True, problems=["The text uses %token2, but nothing fills it."])
    states = {item.label: item.state for item in checklist(check, message, settings, None)}
    assert states == {
        "گروه مخاطبان": "ok", "قالب پیامک": "ok", "متغیرهای قالب": "fail", "لینک کوتاه": "fail",
        "بازه مجاز ارسال (به وقت تهران)": "warn", "گیرندگان": "fail",
    }
    unknown = checklist(CheckResult(), Preview(template="other"), {"template": "other"}, None)
    assert [(i.label, i.state) for i in unknown][1] == ("قالب پیامک", "warn")  # can't be previewed
    assert "متغیرهای قالب" not in {i.label for i in unknown}  # nothing to tell without the text
    missing = checklist(CheckResult(problems=["segment_missing"]), None, {}, None)
    assert [(i.label, i.state) for i in missing][0] == ("گروه مخاطبان", "fail")
    assert "گیرندگان" not in {i.label for i in missing}


# ---------- what it will cost, before the test SMS ----------

def test_the_cost_is_estimated_from_the_latest_test_sms(world, verified):
    from sms_sender_web.dashboard.templatetags.fa import fa_number

    client = signed_in(world.users["operator"], verified)
    # The world's tests cost 3,020 rials for a two-part message: 1,510 a part.
    html = client.get("/campaigns/fresh/").content.decode()
    each, total = fa_number(3020), fa_number(6 * 3020)
    assert f"حدود {each} ریال برای هر گیرنده و {total} ریال برای {fa_number(6)} گیرنده" in html
    assert f"حدود {each} ریال برای هر گیرنده" in client.get("/campaigns/fresh/settings/").content.decode()

    # A test SMS records its message's parts, for the next estimates.
    client.post("/campaigns/fresh/test/")
    assert send_job("fresh") is None and Job.objects.get(campaign__slug="fresh").params["parts"] == 2

    # Before any test whose parts are known, there's no guess.
    Job.objects.filter(kind=Job.Kind.TEST).delete()
    assert "ریال برای هر گیرنده" not in client.get("/campaigns/fresh/").content.decode()
