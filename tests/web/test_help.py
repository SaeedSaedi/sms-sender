"""Plan 05, P6: the short in-app help. Every status and delivery status a
page can show is explained, under the very label the pages show it with;
the menu, the report and the campaign page lead there; the sending window
it names is the one new campaigns get."""
import pytest

pytest.importorskip("django")

from sms_sender_web.dashboard import help as guide  # noqa: E402
from sms_sender_web.dashboard.terms import DELIVERY_STATUS, SUBMISSION_STATUS  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .test_reports import store  # noqa: E402,F401

pytestmark = pytest.mark.django_db

SECTIONS = ("steps", "rules", "statuses", "delivery", "actions", "stops", "roles")


def test_every_status_and_delivery_status_is_explained():
    assert set(guide.STATUS_HELP) == set(SUBMISSION_STATUS)
    assert {str(label) for label, _text in guide.deliveries()} == {str(label) for label in DELIVERY_STATUS.values()}
    assert len(guide.steps()) == 5


def test_each_status_is_explained_under_its_own_label(signed_in):
    html = signed_in.get("/help/").content.decode()
    assert '<a href="/help/" aria-current="true">' in html  # its menu entry
    assert '<span class="badge status-unknown">نامعلوم</span>' in html
    assert "هرگز به‌طور خودکار دوباره ارسال نمی‌شود" in html
    assert "<dt>تحویل‌نشده</dt>" in html and "<dt>ادامه ارسال</dt>" in html and "<dt>مشاهده‌گر</dt>" in html
    for section in SECTIONS:  # the contents lead to every section
        assert f'id="{section}"' in html and f'href="#{section}"' in html


def test_the_window_it_names_is_the_one_new_campaigns_get(signed_in):
    assert "از ۰۸:۰۰ تا ۲۱:۰۰" in signed_in.get("/help/").content.decode()
    defaults = SystemSettings.load()
    defaults.default_send_window = "09:30-20:00"
    defaults.save()
    assert "از ۰۹:۳۰ تا ۲۰:۰۰" in signed_in.get("/help/").content.decode()


def test_someone_without_a_role_sees_no_help(client, make_user):
    client.force_login(make_user("newcomer", None))
    assert client.get("/help/").status_code == 403


def test_the_report_leads_to_the_statuses_and_delivery(signed_in, store):  # noqa: F811
    html = signed_in.get("/reports/coin-7/").content.decode()
    assert 'href="/help/#statuses"' in html and 'href="/help/#delivery"' in html
