"""Step 3.8 fixes against spec 4.11: Kavenegar's codes with their meaning,
copy buttons for identifiers, Persian «ی»/«ک» in typed text, and
confirmations that state the consequence with real numbers."""
import pytest

pytest.importorskip("django")

from django.utils import translation  # noqa: E402

from sms_sender_web.campaigns.terms import stop_reason  # noqa: E402
from sms_sender_web.dashboard.templatetags.fa import copyable  # noqa: E402
from sms_sender_web.text import persian_text  # noqa: E402


@pytest.fixture(autouse=True)
def persian():
    with translation.override("fa"):
        yield


def test_a_halt_names_the_code_and_what_it_means():
    assert stop_reason("provider_halt", {"code": 418}) == (
        "کاوه‌نگار خطای ۴۱۸ برگرداند: اعتبار حساب کافی نیست. ارسال متوقف شد."
    )
    assert stop_reason("test_refused", {"code": 420}) == (
        "کاوه‌نگار برای پیامک آزمایشی خطای ۴۲۰ برگرداند: ارسال لینک در متن پیامک برای این حساب مسدود است."
    )


def test_an_unknown_code_and_no_answer_are_said_plainly():
    assert stop_reason("test_failed", {"code": 999}).endswith("کاوه‌نگار توضیح بیشتری نداد.")
    assert stop_reason("test_failed", {"code": None}) == "کاوه‌نگار پاسخ نداد. کمی بعد دوباره بررسی کنید."
    assert stop_reason(None, {}) == ""


def test_identifiers_have_a_copy_button_with_the_exact_value():
    numeric = str(copyable("12345"))
    assert '<bdi dir="ltr">۱۲۳۴۵</bdi>' in numeric  # a purely numeric ID is a number
    assert 'data-copy="12345"' in numeric and 'aria-label="کپی: 12345"' in numeric
    mixed = str(copyable("u-12"))
    assert '<bdi dir="ltr">u-12</bdi>' in mixed and 'data-copy="u-12"' in mixed  # kept exactly
    assert "<script>" not in str(copyable("<script>"))


def test_typed_text_gets_the_persian_letters():
    assert persian_text("علي كاظمى") == "علی کاظمی"
    assert persian_text("vip-1") == "vip-1"


@pytest.mark.django_db
def test_names_and_token_values_are_stored_in_persian_letters(make_user, verified, settings, tmp_path):
    from django.test import Client

    from sms_sender_web.jobs.models import Campaign
    from sms_sender_web.segments.models import Segment

    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "segments").mkdir()
    (tmp_path / "segments" / "vip.csv").write_text("phone,first_name\n09120000001,Ali\n", encoding="utf-8")
    Segment.objects.create(slug="vip", name="VIP", status=Segment.Status.READY,
                           columns=["phone", "first_name"], token_columns=["first_name"])
    client = Client()
    verified(client, make_user("operator1", "operator"))
    client.post("/campaigns/new/", {"name": "كمپين علي", "slug": "c-1", "segment": "vip", "template": "t"})
    client.post("/campaigns/c-1/settings/", {
        "segment": "vip", "template": "t", "token_source": "value", "token_value": "ياقوت",
        "token2_source": "column", "token2_column": "first_name", "value_maps": "first_name:Ali=علي",
        "link_strategy": "recipient", "link_expiry_days": "7", "send_window": "08:00-21:00", "workers": "1",
    })
    campaign = Campaign.objects.get(slug="c-1")
    assert campaign.name == "کمپین علی"
    assert campaign.settings["tokens"] == {"token": "یاقوت"}
    assert campaign.settings["value_maps"] == {"first_name": {"Ali": "علی"}}


@pytest.mark.django_db
def test_cancel_and_send_confirmations_give_the_number(make_user, verified, settings, tmp_path):
    from django.test import Client

    from sms_sender.state import StateStore
    from sms_sender_web.jobs.models import Campaign, Job

    settings.SMS_SENDER_DB_DIR = tmp_path
    campaign = Campaign.objects.create(slug="c-1", name="C", settings={"template": "t"})
    StateStore(tmp_path / "c-1.db").upsert_pending([(f"0912000{i:04d}", "x") for i in range(1234)])
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, state=Job.State.RUNNING)
    client = Client()
    verified(client, make_user("operator1", "operator"))
    html = client.get("/campaigns/c-1/").content.decode()
    assert "کمپین لغو شود؟ ارسال به ۱٬۲۳۴ گیرنده‌ای که هنوز پیامک نگرفته‌اند لغو می‌شود." in html
