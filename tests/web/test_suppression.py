"""Step 3.5: the suppression list (spec 4.10, 4.12). Operators add numbers,
admins remove them, every change is recorded, and every send run leaves
the listed numbers out."""
import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import IntegrityError, transaction  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.engine import Engine  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402
from sms_sender_web.suppression import service  # noqa: E402
from sms_sender_web.suppression.models import Suppression  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator_client(make_user, verified):
    client = Client()
    verified(client, make_user("operator1", "operator"))
    return client


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def listed() -> list[str]:
    return sorted(Suppression.objects.values_list("phone", flat=True))


def test_operators_add_numbers_in_any_form(operator_client):
    Suppression.objects.create(phone="09120000003")
    response = operator_client.post("/suppression/", {
        "action": "add",
        "numbers": "09120000001\n+98 912 000 0002، ۰۹۱۲۰۰۰۰۰۰۳\n\nnot-a-number\n0912000",
        "note": "asked to stop",
    }, follow=True)
    html = response.content.decode()
    assert "۲ شماره به فهرست عدم ارسال افزوده شد؛ ۱ شماره از پیش در فهرست بود" in html
    assert "۲ سطر شماره موبایل معتبر نبود" in html
    assert listed() == ["09120000001", "09120000002", "09120000003"]
    assert Suppression.objects.get(phone="09120000001").note == "asked to stop"
    (event,) = AuditEvent.objects.filter(action="suppression_added")
    assert (event.username, event.detail) == ("operator1", {"count": 2})


def test_a_file_of_numbers_can_be_added(operator_client):
    content = "name,mobile\nAli,09120000001\nSara,09120000002\nnobody,\n".encode()
    operator_client.post("/suppression/", {
        "action": "add", "file": SimpleUploadedFile("optout.csv", content),
    })
    assert listed() == ["09120000001", "09120000002"]


def test_nothing_to_add_is_explained(operator_client):
    html = operator_client.post("/suppression/", {"action": "add", "numbers": " "}).content.decode()
    assert "دست‌کم یک شماره وارد کنید" in html


def test_the_list_shows_numbers_masked(operator_client):
    Suppression.objects.create(phone="09120000001", note="by phone")
    html = operator_client.get("/suppression/").content.decode()
    assert "۰۹۱۲*****۰۱" in html and "همه کمپین‌ها" in html
    assert "09120000001" not in html
    assert 'name="action" value="remove"' not in html  # operators can't remove


def test_finding_a_number(operator_client):
    Suppression.objects.create(phone="09120000001")
    Suppression.objects.create(phone="09120000002")
    html = operator_client.get("/suppression/", {"q": "+98 912 000 0001"}).content.decode()
    assert "۰۹۱۲*****۰۱" in html and "۰۹۱۲*****۰۲" not in html
    html = operator_client.get("/suppression/", {"q": "09120009999"}).content.decode()
    assert "این شماره در فهرست عدم ارسال نیست" in html
    html = operator_client.get("/suppression/", {"q": "hello"}).content.decode()
    assert "این شماره موبایل معتبر نیست" in html


def test_only_admins_remove_numbers(operator_client, admin_client):
    entry = Suppression.objects.create(phone="09120000001")
    assert operator_client.post("/suppression/", {"action": "remove", "entry": entry.pk}).status_code == 403
    assert listed() == ["09120000001"]

    html = admin_client.get("/suppression/").content.decode()
    assert 'name="action" value="remove"' in html
    response = admin_client.post("/suppression/", {"action": "remove", "entry": entry.pk}, follow=True)
    assert "شماره از فهرست عدم ارسال حذف شد" in response.content.decode()
    assert listed() == []
    (event,) = AuditEvent.objects.filter(action="suppression_removed")
    assert event.detail == {"phone": "09120000001"}
    assert admin_client.post("/suppression/", {"action": "remove", "entry": "x"}).status_code == 404


def test_the_activity_log_shows_removed_numbers_masked(admin_client):
    entry = Suppression.objects.create(phone="09120000001")
    admin_client.post("/suppression/", {"action": "remove", "entry": entry.pk})
    html = admin_client.get("/activity/").content.decode()
    assert "حذف از فهرست عدم ارسال" in html and "۰۹۱۲*****۰۱" in html
    assert "09120000001" not in html


def test_viewers_do_not_see_the_list(signed_in):
    assert signed_in.get("/suppression/").status_code == 403
    assert 'href="/suppression/"' not in signed_in.get("/").content.decode()


def test_a_number_is_listed_once_globally_and_once_per_campaign():
    campaign = Campaign.objects.create(slug="coin-7", name="Coin 7")
    Suppression.objects.create(phone="09120000001")
    with pytest.raises(IntegrityError), transaction.atomic():
        Suppression.objects.create(phone="09120000001")
    Suppression.objects.create(phone="09120000001", campaign=campaign)
    with pytest.raises(IntegrityError), transaction.atomic():
        Suppression.objects.create(phone="09120000001", campaign=campaign)
    assert service.add(["09120000001", "09120000002"]) == 1
    assert service.add(["09120000002"], campaign=campaign) == 1


def test_each_campaign_skips_the_global_list_and_its_own():
    coin, other = (Campaign.objects.create(slug=s, name=s) for s in ("coin-7", "other"))
    service.add(["09120000001"])
    service.add(["09120000002"], campaign=coin)
    service.add(["09120000003"], campaign=other)
    assert service.phones_for(coin) == {"09120000001", "09120000002"}
    assert service.phones_for() == {"09120000001"}


def test_a_send_run_leaves_the_listed_numbers_out(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("KAVENEGAR_API_KEY", "test-key-not-real")
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    segment = tmp_path / "vip.csv"
    segment.write_text("phone\n09120000001\n09120000002\n", encoding="utf-8")
    campaign = Campaign.objects.create(
        slug="coin-7", name="Coin 7", settings={"input": str(segment), "template": "coin-price"},
    )
    service.add(["09120000002"])
    service.add(["09120000003"], campaign=campaign)
    runner = Engine().runner(campaign, reporter=None)
    assert runner.opt_out == {"09120000002", "09120000003"}
