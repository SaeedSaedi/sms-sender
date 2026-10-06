"""Plan 06, D1 and D3: an admin keeps up to five team numbers that get
every test SMS too, and can ask for a second person to approve it."""
import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.audit.terms import describe  # noqa: E402
from sms_sender_web.dashboard import notices  # noqa: E402
from sms_sender_web.dashboard.views import campaign_rows  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Job  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .test_campaigns import MY_NUMBER, campaign, operator, operator_client, places, segment  # noqa: E402,F401
from .test_jobs import FakeEngine, make_worker  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

TEAM_A, TEAM_B = "09150000088", "09150000099"
OWN_TEST = "چون این پیامک آزمایشی را شما درخواست کرده‌اید، شخص دیگری آن را تأیید می‌کند."


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def team(*numbers):
    SystemSettings.objects.update_or_create(pk=1, defaults={"team_test_numbers": list(numbers)})


def second_approver(on=True):
    SystemSettings.objects.update_or_create(pk=1, defaults={"second_approver": on})


def add(client, phone):
    return client.post("/system/", {"section": "team_add", "team_phone": phone}, follow=True)


def changes():
    return [e.detail for e in AuditEvent.objects.filter(action="system_settings_changed").order_by("id")]


def sent_test(client, engine=None):
    """Ask for a test SMS and let the worker send it."""
    client.post("/campaigns/coin-7/test/")
    make_worker(engine or FakeEngine()).run_once()
    return Job.objects.get(kind=Job.Kind.TEST)


# ---------- the team's numbers (D1) ----------

def test_an_admin_adds_team_numbers_and_they_show_masked(admin_client):
    html = add(admin_client, "۰۹۱۵ ۰۰۰ ۰۰۸۸").content.decode()  # Persian digits, spaces
    assert SystemSettings.load().team_test_numbers == [TEAM_A]
    assert "۰۹۱۵*****۸۸" in html and TEAM_A not in html
    assert "ذخیره شد. از پیامک آزمایشی بعدی اعمال می‌شود." in html
    assert changes() == [{"team_number_added": "0915*****88"}]
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert "شماره تیم برای پیامک آزمایشی افزوده شد" in str(describe(event))


def test_at_most_five_numbers_each_once(admin_client):
    for n in range(5):
        add(admin_client, f"0915000001{n}")
    add(admin_client, "09150000010")  # already there: nothing changes
    html = add(admin_client, "09150000020").content.decode()
    assert "اکنون ۵ شماره ثبت شده است؛ ابتدا یکی را حذف کنید." in html
    assert len(SystemSettings.load().team_test_numbers) == 5
    assert 'id="team-form"' not in admin_client.get("/system/").content.decode()  # full: no add form


def test_a_number_that_isnt_one_is_refused(admin_client):
    html = add(admin_client, "12345").content.decode()
    assert "این شماره موبایل معتبر نیست." in html
    assert SystemSettings.load().team_test_numbers == []


def test_an_admin_removes_a_team_number(admin_client):
    team(TEAM_A, TEAM_B)
    admin_client.post("/system/", {"section": "team_remove", "index": "0"})
    assert SystemSettings.load().team_test_numbers == [TEAM_B]
    assert changes() == [{"team_number_removed": "0915*****88"}]
    html = admin_client.post("/system/", {"section": "team_remove", "index": "7"}).content.decode()
    assert "یک شماره از فهرست انتخاب کنید." in html


def test_restricted_sending_marks_the_team_numbers_it_skips(admin_client, monkeypatch):
    monkeypatch.setenv("SMS_SENDER_ALLOWED_NUMBERS", TEAM_B)
    team(TEAM_A, TEAM_B)
    html = admin_client.get("/system/").content.decode()
    assert html.count("تا وقتی ارسال محدود است، ارسال نمی‌شود") == 1


def test_only_admins_keep_the_team_numbers(operator_client):
    response = operator_client.post("/system/", {"section": "team_add", "team_phone": TEAM_A})
    assert response.status_code == 403
    assert SystemSettings.load().team_test_numbers == []


def test_each_test_sms_goes_to_the_team_after_the_requester(operator_client, campaign, monkeypatch):
    told = []
    monkeypatch.setattr("sms_sender.notify.notify_text", lambda target, text: told.append(text) or True)
    SystemSettings.objects.update_or_create(pk=1, defaults={
        "team_test_numbers": [TEAM_A, MY_NUMBER, TEAM_B], "notify_targets": ["slack:https://hooks.slack.com/x"],
    })
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "به شماره خود شما و شماره‌های تیم ارسال می‌شود" in html
    assert "و شماره‌های تیم (۲)" in html  # the requester's own number isn't counted twice

    engine = FakeEngine()
    test = sent_test(operator_client, engine)
    assert test.params["team_numbers"] == [TEAM_A, TEAM_B]
    assert engine.fake.calls == [MY_NUMBER, TEAM_A, TEAM_B]
    assert test.state == Job.State.DONE and test.result["test_team_sent"] == 2
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert "و شماره‌های تیم (۲)" in html and "به درخواست" in html
    assert told == ["sms-sender: coin-7: the test SMS went to 0912*****99 and 2 of the team's numbers. "
                    "It waits for someone to approve or reject it on the dashboard."]


def test_the_composer_says_the_team_gets_it_too(operator_client, campaign):
    from sms_sender_web.campaigns.composer import _test_context
    from django.test import RequestFactory

    team(TEAM_A)
    request = RequestFactory().get("/")
    request.user = Profile.objects.get(test_phone=MY_NUMBER).user
    hint = _test_context(request, campaign.settings)["test_hint"]
    assert "و سپس به شماره‌های تیم (۱)" in hint


# ---------- a second approver (D3) ----------

def test_an_admin_asks_for_a_second_approver_and_hears_when_nobody_could(admin_client, make_user):
    html = admin_client.post("/system/", {"section": "approval", "second_approver": "on"}, follow=True).content.decode()
    assert SystemSettings.load().second_approver is True
    assert changes() == [{"second_approver": True}]
    # The admin is the only one who can run campaigns: nobody could approve their test.
    assert "اکنون فقط یک نفر می‌تواند کمپین اجرا کند" in html
    make_user("operator9", "operator")
    assert "اکنون فقط یک نفر می‌تواند کمپین اجرا کند" not in admin_client.get("/system/").content.decode()
    admin_client.post("/system/", {"section": "approval"})
    assert SystemSettings.load().second_approver is False
    event = AuditEvent.objects.filter(action="system_settings_changed").order_by("-id").first()
    assert str(describe(event)) == "درخواست‌کننده پیامک آزمایشی می‌تواند خودش آن را تأیید کند"


def test_with_a_second_approver_whoever_asked_can_only_reject(operator_client, campaign, operator):
    second_approver()
    test = sent_test(operator_client)
    html = operator_client.get("/campaigns/coin-7/").content.decode()
    assert OWN_TEST in html
    assert 'id="approve-form"' not in html and 'id="reject-form"' in html

    html = operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk}, follow=True).content.decode()
    assert OWN_TEST in html
    test.refresh_from_db()
    assert test.decision == ""
    with pytest.raises(services.JobConflict) as e:
        services.decide_test(test, operator, approve=True)
    assert e.value.code == "own_test"

    operator_client.post("/campaigns/coin-7/reject/", {"job": test.pk})
    test.refresh_from_db()
    assert test.decision == Job.Decision.REJECTED


def test_with_a_second_approver_someone_else_approves(operator_client, campaign, make_user, verified):
    second_approver()
    test = sent_test(operator_client)
    colleague = make_user("operator2", "operator")
    other = Client()
    verified(other, colleague)
    html = other.get("/campaigns/coin-7/").content.decode()
    assert 'id="approve-form"' in html and OWN_TEST not in html
    other.post("/campaigns/coin-7/approve/", {"job": test.pk})
    test.refresh_from_db()
    assert test.decision == Job.Decision.APPROVED and test.decided_by == colleague
    assert services.approval(campaign) == test


def test_notices_and_the_overview_ask_the_right_person(operator_client, campaign, operator, make_user):
    second_approver()
    sent_test(operator_client)
    colleague = make_user("operator2", "operator")
    assert [n.text for n in notices.notices(operator)] == ["پیامک آزمایشی «قیمت سکه» در انتظار تأیید شخص دیگری است."]
    assert [n.text for n in notices.notices(colleague)] == ["پیامک آزمایشی «قیمت سکه» در انتظار تأیید است."]
    _, mine, _ = campaign_rows(operator)
    _, theirs, _ = campaign_rows(colleague)
    assert str(mine[0]["action"]) == "باز کردن" and "شخص دیگری آن را تأیید می‌کند" in mine[0]["lines"][0]
    assert str(theirs[0]["action"]) == "بررسی و تأیید"


def test_without_a_second_approver_whoever_asked_approves_as_before(operator_client, campaign, operator):
    test = sent_test(operator_client)
    assert not services.needs_another_approver(test, operator)
    operator_client.post("/campaigns/coin-7/approve/", {"job": test.pk})
    test.refresh_from_db()
    assert test.decision == Job.Decision.APPROVED
