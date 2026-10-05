"""Step 3.5: campaign pages (spec 3, 4.6). Settings, the check, a test SMS
to the operator's own number and its approval, then sending with pause,
resume and cancel — all through the worker, with a fake Kavenegar."""
import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import CANCELLED, PENDING, SENT, StateStore  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402
from sms_sender_web.suppression.models import Suppression  # noqa: E402

from .test_jobs import FakeEngine, FakeKavenegar, make_worker  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

MY_NUMBER = "09120000099"
ROWS = ["09120000001", "09120000002", "09120000003"]


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


@pytest.fixture
def segment(tmp_path):
    folder = tmp_path / "segments"
    folder.mkdir()
    (folder / "vip.csv").write_text(
        "phone,user_id,first_name\n" + "".join(f"{p},u{i},Ali\n" for i, p in enumerate(ROWS)),
        encoding="utf-8",
    )
    return Segment.objects.create(
        slug="vip", name="VIP", status=Segment.Status.READY,
        columns=["phone", "user_id", "first_name"], user_id_column="user_id", token_columns=["first_name"],
    )


@pytest.fixture
def operator(make_user):
    user = make_user("operator1", "operator")
    Profile.objects.create(user=user, test_phone=MY_NUMBER)
    return user


@pytest.fixture
def operator_client(operator, verified):
    client = Client()
    verified(client, operator)
    return client


def create(client, segment, slug="coin-7", **extra):
    return client.post("/campaigns/new/", {
        "name": "قیمت سکه", "slug": slug, "segment": segment.slug, "template": "coin-price", **extra,
    })


def settings_post(client, slug="coin-7", **fields):
    data = {
        "segment": "vip", "template": "coin-price", "token_source": "column", "token_column": "first_name",
        "link_strategy": "recipient", "link_expiry_days": "7", "link_format": "url",
        "send_window": "08:00-21:00", "workers": "2", **fields,
    }
    return client.post(f"/campaigns/{slug}/settings/", data)


@pytest.fixture
def campaign(operator_client, segment):
    create(operator_client, segment)
    settings_post(operator_client)
    return Campaign.objects.get(slug="coin-7")


def run_worker(engine):
    return make_worker(engine).run_once()


def actions():
    return [e.action for e in AuditEvent.objects.order_by("id") if e.campaign]


# --- Creating and setting up -------------------------------------------------------


def test_an_operator_creates_a_campaign(operator_client, segment):
    response = create(operator_client, segment)
    assert response["Location"] == "/campaigns/coin-7/settings/"
    campaign = Campaign.objects.get(slug="coin-7")
    assert campaign.name == "قیمت سکه"
    assert campaign.settings["segment"] == "vip" and campaign.settings["input"].endswith("segments/vip.csv")
    assert campaign.settings["user_id_column"] == "user_id"
    assert actions() == ["campaign_created"]


@pytest.mark.parametrize("slug", ["Coin 7", "new"])
def test_campaign_short_names_follow_the_rules(operator_client, segment, slug):
    html = create(operator_client, segment, slug=slug).content.decode()
    assert "فقط حروف کوچک انگلیسی" in html or "کمپینی با این نام کوتاه وجود دارد" in html
    assert not Campaign.objects.exists()


def test_a_cli_campaign_db_is_never_taken_over(operator_client, segment, tmp_path):
    StateStore(tmp_path / "db" / "coin-7.db")
    html = create(operator_client, segment).content.decode()
    assert "کمپینی با این نام کوتاه وجود دارد" in html


def test_a_draft_segment_cannot_be_chosen(operator_client, segment):
    Segment.objects.filter(pk=segment.pk).update(status=Segment.Status.DRAFT)
    html = create(operator_client, segment).content.decode()
    assert "گروه مخاطبانی را انتخاب کنید که ستون‌هایش تعیین شده است" in html


def test_viewers_cannot_create_or_change_campaigns(signed_in, segment, campaign):
    assert create(signed_in, segment, slug="other").status_code == 403
    assert settings_post(signed_in).status_code == 403
    assert signed_in.post("/campaigns/coin-7/test/").status_code == 403
    assert signed_in.post("/campaigns/coin-7/send/").status_code == 403
    assert signed_in.get("/campaigns/coin-7/").status_code == 200


def test_settings_are_stored_in_the_clis_terms(operator_client, segment):
    create(operator_client, segment)
    response = settings_post(
        operator_client, token2_source="value", token2_value="سکه", token10_source="link",
        link_destination="https://kifpool.me/wallet", value_maps="first_name:Ali=علی", rate="۱۰/s",
    )
    assert response["Location"] == "/campaigns/coin-7/"
    s = Campaign.objects.get(slug="coin-7").settings
    assert (s["tokens"], s["token_columns"]) == ({"token2": "سکه"}, {"token": "first_name"})
    assert s["value_maps"] == {"first_name": {"Ali": "علی"}}
    assert s["links"]["token"] == "token10" and s["links"]["destination"] == "https://kifpool.me/wallet"
    assert (s["send_window"], s["rate"], s["workers"]) == ("08:00-21:00", "10/s", 2)
    (changed,) = AuditEvent.objects.filter(action="campaign_changed")
    assert {"tokens", "token_columns", "links", "value_maps"} <= set(changed.detail["changed"])


@pytest.mark.parametrize("fields, message", [
    ({"token_source": ""}, "کاوه‌نگار در هر پیامک به متغیر اول (token) نیاز دارد"),
    ({"token_source": "value", "token_value": "a_b"}, "نویسه «_» مجاز نیست"),
    ({"token_source": "value", "token_value": "a b"}, "این متغیر حداکثر ۰ فاصله می‌پذیرد"),
    ({"token_column": "city"}, "یکی از ستون‌های گروه مخاطبان را انتخاب کنید"),
    ({"token2_source": "link", "token3_source": "link", "link_destination": "https://kifpool.me/x"},
     "فقط یک متغیر می‌تواند لینک کوتاه را در خود داشته باشد"),
    ({"token2_source": "link", "link_destination": "https://evil.example/x"}, "جزو مقصدهای مجاز نیست"),
    ({"token2_source": "link", "link_destination": "http://kifpool.me/x"}, "نشانی باید با https:// آغاز شود"),
    ({"send_window": "off"}, "بازه روزانه را به شکل"),
    ({"rate": "fast"}, "سرعت را مانند"),
    ({"value_maps": "nonsense"}, "هر ترجمه را به شکل ستون:مقدار=ترجمه بنویسید"),
])
def test_settings_mistakes_are_explained_in_persian(operator_client, segment, fields, message):
    create(operator_client, segment)
    response = settings_post(operator_client, **fields)
    assert response.status_code == 200
    assert message in response.content.decode()


# --- The check -----------------------------------------------------------------------


def test_the_check_counts_without_sending(operator_client, campaign):
    Suppression.objects.create(phone="09120000002")
    html = operator_client.post("/campaigns/coin-7/check/").content.decode()
    assert "آماده پیامک آزمایشی است" in html
    assert '<dt>در انتظار ارسال</dt><dd class="num">۲</dd>' in html
    assert '<dt>در فهرست عدم ارسال</dt><dd class="num">۱</dd>' in html
    assert not campaign_db(campaign).exists()  # read only


def test_the_check_says_when_nobody_is_left(operator_client, campaign):
    Suppression.objects.bulk_create([Suppression(phone=p) for p in ROWS])
    html = operator_client.post("/campaigns/coin-7/check/").content.decode()
    assert "گیرنده‌ای برای ارسال باقی نمانده است" in html


# --- Test SMS, approval, sending -------------------------------------------------------


def test_the_whole_flow_test_approve_send(operator_client, campaign):
    engine = FakeEngine()
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "ارسال پیامک آزمایشی" in html and "۰۹۱۲*****۹۹" in html
    assert "آغاز ارسال" not in html  # nothing approved yet

    operator_client.post("/campaigns/coin-7/test/")
    test = Job.objects.get(kind=Job.Kind.TEST)
    assert test.params == {"test_number": MY_NUMBER}
    run_worker(engine)
    test.refresh_from_db()
    assert test.state == Job.State.DONE and test.result["test_only"] is True
    assert engine.fake.calls == [MY_NUMBER]  # only the operator got an SMS
    assert StateStore(campaign_db(campaign)).counts() == {PENDING: 3}

    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "پیامک رسید و متن و لینک آن درست است؟" in html
    assert "ارسال پیامک آزمایشی تازه" not in html  # decide on this one first
    response = operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk}, follow=True)
    assert "پیامک آزمایشی تأیید شد" in response.content.decode()
    assert "آغاز ارسال" in response.content.decode()

    operator_client.post("/campaigns/coin-7/send/")
    send = Job.objects.get(kind=Job.Kind.SEND)
    assert send.settings_hash == test.settings_hash
    run_worker(engine)
    send.refresh_from_db()
    assert send.state == Job.State.DONE and send.result["sent"] == 3
    assert StateStore(campaign_db(campaign)).counts() == {SENT: 3}
    assert sorted(engine.fake.calls) == sorted([MY_NUMBER, *ROWS])
    assert actions() == [
        "campaign_created", "campaign_changed", "test_requested", "test_approved", "send_started",
    ]


def test_sending_needs_an_approved_test(operator_client, campaign):
    response = operator_client.post("/campaigns/coin-7/send/", follow=True)
    assert "ابتدا یک پیامک آزمایشی بفرستید و آن را تأیید کنید" in response.content.decode()
    assert not Job.objects.filter(kind=Job.Kind.SEND).exists()


def test_a_rejected_test_does_not_allow_sending(operator_client, campaign):
    operator_client.post("/campaigns/coin-7/test/")
    run_worker(FakeEngine())
    test = Job.objects.get(kind=Job.Kind.TEST)
    operator_client.post("/campaigns/coin-7/reject/", {"job": test.pk})
    test.refresh_from_db()
    assert test.decision == Job.Decision.REJECTED
    with pytest.raises(services.JobConflict) as e:
        services.start_send(campaign, None)
    assert e.value.code == "not_approved"


def test_changing_the_message_after_approval_needs_a_new_test(operator_client, campaign):
    operator_client.post("/campaigns/coin-7/test/")
    run_worker(FakeEngine())
    test = Job.objects.get(kind=Job.Kind.TEST)
    operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk})
    settings_post(operator_client, workers="3")  # throughput: still approved
    assert services.approval(Campaign.objects.get(slug="coin-7")) is not None
    settings_post(operator_client, template="coin-price-2")
    response = operator_client.post("/campaigns/coin-7/send/", follow=True)
    assert "تنظیمات پس از پیامک آزمایشی تغییر کرده است" in response.content.decode()


def test_a_test_cannot_run_while_sending(operator_client, campaign):
    operator_client.post("/campaigns/coin-7/test/")
    run_worker(FakeEngine())
    test = Job.objects.get(kind=Job.Kind.TEST)
    operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk})
    operator_client.post("/campaigns/coin-7/send/")
    response = operator_client.post("/campaigns/coin-7/test/", follow=True)
    assert "این کمپین در حال ارسال است" in response.content.decode()


def test_no_test_number_no_test(operator_client, campaign, operator):
    Profile.objects.filter(user=operator).update(test_phone="")
    response = operator_client.post("/campaigns/coin-7/test/", follow=True)
    assert "ابتدا شماره موبایل خود را در «حساب من» ثبت کنید" in response.content.decode()
    assert not Job.objects.exists()


def test_pause_resume_and_cancel_a_waiting_send(operator_client, campaign):
    operator_client.post("/campaigns/coin-7/test/")
    engine = FakeEngine()
    run_worker(engine)
    test = Job.objects.get(kind=Job.Kind.TEST)
    operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk})
    operator_client.post("/campaigns/coin-7/send/")
    send = Job.objects.get(kind=Job.Kind.SEND)

    operator_client.post("/campaigns/coin-7/pause/", {"job": send.pk})
    send.refresh_from_db()
    assert send.state == Job.State.PAUSED
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "ادامه ارسال" in html and "لغو کمپین" in html
    operator_client.post("/campaigns/coin-7/resume/", {"job": send.pk})
    send.refresh_from_db()
    assert send.state == Job.State.QUEUED

    operator_client.post("/campaigns/coin-7/cancel/", {"job": send.pk})
    send.refresh_from_db()
    assert send.state == Job.State.CANCELLED
    assert StateStore(campaign_db(campaign)).counts() == {CANCELLED: 3}
    assert actions()[-3:] == ["job_paused", "job_resumed", "job_cancelled"]


def test_cancelling_a_test_leaves_the_recipients_alone(operator_client, campaign):
    operator_client.post("/campaigns/coin-7/test/")
    test = Job.objects.get(kind=Job.Kind.TEST)
    operator_client.post("/campaigns/coin-7/cancel/", {"job": test.pk})
    test.refresh_from_db()
    assert test.state == Job.State.CANCELLED
    assert not campaign_db(campaign).exists()


def test_follow_up_jobs_never_send(operator_client, campaign):
    StateStore(campaign_db(campaign))  # the campaign has sent before
    for action in ("reconcile", "delivery", "clicks"):
        operator_client.post(f"/campaigns/coin-7/{action}/")
    assert sorted(Job.objects.values_list("kind", flat=True)) == ["clicks", "delivery", "reconcile"]
    assert operator_client.post("/campaigns/coin-7/explode/").status_code == 404


def test_a_failed_run_says_why_in_persian(operator_client, campaign):
    Job.objects.create(
        campaign=campaign, kind=Job.Kind.TEST, state=Job.State.FAILED,
        result={"stop_reason": "not_enough_credit",
                "stop_fields": {"estimate": 3600, "recipients": 3, "credit": 3000}},
        last_error="not enough credit: about 3600 rials needed for 3 SMS, 3000 left",
    )
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "اعتبار حساب کافی نیست: برای ۳ پیامک حدود ۳٬۶۰۰ ریال لازم است و ۳٬۰۰۰ ریال باقی مانده است" in html
    assert "not enough credit" not in html  # the engine's English never shows


def test_the_live_part_polls_only_while_a_job_is_active(operator_client, campaign):
    response = operator_client.get("/campaigns/coin-7/live/")
    # 286: HTMX swaps the final state in, then stops polling.
    assert response.status_code == 286 and "hx-get" not in response.content.decode()
    operator_client.post("/campaigns/coin-7/test/")
    response = operator_client.get("/campaigns/coin-7/live/")
    html = response.content.decode()
    assert response.status_code == 200
    assert 'hx-get="/campaigns/coin-7/live/"' in html and 'hx-trigger="every 3s"' in html
    assert 'hx-swap="morph:outerHTML"' in html  # in place, so focus stays on its button


def test_settings_are_fixed_once_an_sms_may_have_gone_out(operator_client, campaign):
    # A send that failed before sending anything fixes nothing: a wrong
    # template can still be corrected.
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.FAILED,
                       started_at=campaign.created_at)
    assert operator_client.get("/campaigns/coin-7/settings/").status_code == 200
    store = StateStore(campaign_db(campaign))
    store.upsert_pending([("09120000001", "09120000001")])
    store.claim("09120000001")
    store.mark_sent("09120000001", 1001, 200, 3020)
    response = operator_client.get("/campaigns/coin-7/settings/", follow=True)
    assert "ارسال این کمپین آغاز شده است و تنظیماتش دیگر تغییر نمی‌کند" in response.content.decode()
    assert services.settings_locked(campaign) == "started"


def test_the_engine_builds_a_test_run_for_the_operators_number(settings, monkeypatch, campaign):
    monkeypatch.setenv("KAVENEGAR_API_KEY", "test-key-not-real")
    runner = Engine().runner(campaign, reporter=None, test_number=MY_NUMBER)
    assert (runner.test_only, runner.approval_test_number) == (True, MY_NUMBER)
    runner = Engine().runner(campaign, reporter=None, cost_per_sms=1200)
    assert (runner.test_only, runner._approval_cost) == (False, 1200)


# --- My account, the home page -----------------------------------------------------------


def test_my_account_keeps_my_number_for_test_sms(operator_client, operator):
    response = operator_client.post("/account/", {"test_phone": "۰۹۱۲ ۰۰۰ ۰۰۱۲"}, follow=True)
    assert "ذخیره شد" in response.content.decode()
    assert Profile.objects.get(user=operator).test_phone == "09120000012"
    html = operator_client.post("/account/", {"test_phone": "123"}).content.decode()
    assert "این شماره موبایل معتبر نیست" in html
    assert AuditEvent.objects.filter(action="test_number_changed").count() == 1


def test_the_home_page_lists_new_campaigns(operator_client, campaign, signed_in):
    html = operator_client.get("/").content.decode()
    assert 'href="/campaigns/coin-7/"' in html and "قیمت سکه" in html
    assert 'href="/campaigns/new/"' in html
    assert 'href="/campaigns/new/"' not in signed_in.get("/").content.decode()  # viewers
