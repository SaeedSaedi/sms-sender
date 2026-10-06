"""Plan 06, L4: notifications inside the panel. What happened in the last 7
days that someone may need (a test SMS to approve, a send that ended,
stopped or was cancelled, low credit, an overdue backup), read from what
the panel already records; the menu counts the ones you haven't seen."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("django")

from django.utils import timezone  # noqa: E402

from sms_sender.window import TEHRAN  # noqa: E402
from sms_sender_web.dashboard import notices  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.system.models import ProviderCheck, SystemSettings  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    settings.BACKUP_DIR = tmp_path / "backups"
    (tmp_path / "db").mkdir()
    SystemSettings.objects.update_or_create(pk=1, defaults={"backup_hour": None})


@pytest.fixture
def campaign():
    return Campaign.objects.create(slug="coin", name="قیمت سکه", settings={"segment": "vip", "template": "t"})


@pytest.fixture
def operator(make_user):
    return make_user("op", "operator")


def _job(campaign, kind, state, ago=timedelta(hours=1), **fields):
    return Job.objects.create(campaign=campaign, kind=kind, state=state,
                              finished_at=timezone.now() - ago, **fields)


def _kinds(user):
    return [n.kind for n in notices.notices(user)]


def test_a_test_waiting_for_approval_is_for_those_who_run_campaigns(campaign, operator, viewer):
    _job(campaign, Job.Kind.TEST, Job.State.DONE, settings_hash=services.settings_hash(campaign))
    [notice] = notices.notices(operator)
    assert (notice.kind, notice.tone, notice.url) == ("test", "warning", "/campaigns/coin/")
    assert notice.text == "پیامک آزمایشی «قیمت سکه» در انتظار تأیید است."
    assert _kinds(viewer) == []  # a viewer can't approve it


def test_a_decided_or_outdated_test_is_not_waiting(campaign, operator):
    _job(campaign, Job.Kind.TEST, Job.State.DONE, settings_hash="another message")
    assert _kinds(operator) == []
    newer = _job(campaign, Job.Kind.TEST, Job.State.DONE, ago=timedelta(minutes=5),
                 settings_hash=services.settings_hash(campaign))
    assert _kinds(operator) == ["test"]
    newer.decision = Job.Decision.APPROVED
    newer.save()
    assert _kinds(operator) == []


def test_a_failed_test_says_why(campaign, operator):
    _job(campaign, Job.Kind.TEST, Job.State.FAILED, result={"stop_reason": "window_closed"})
    [notice] = notices.notices(operator)
    assert notice.kind == "test_failed" and notice.text.startswith("پیامک آزمایشی «قیمت سکه» ارسال نشد.")
    assert len(notice.text) > len("پیامک آزمایشی «قیمت سکه» ارسال نشد.")  # and the reason after it


def test_sends_that_ended_stopped_or_were_cancelled(campaign, viewer):
    _job(campaign, Job.Kind.SEND, Job.State.DONE, ago=timedelta(hours=3), result={"sent": 1200})
    _job(campaign, Job.Kind.SEND, Job.State.FAILED, ago=timedelta(hours=2),
         result={"stop_reason": "provider_halt", "stop_fields": {"code": 418}})
    _job(campaign, Job.Kind.SEND, Job.State.CANCELLED, ago=timedelta(hours=1))
    _job(campaign, Job.Kind.SEND, Job.State.CANCELLED, result={"withdrawn": True})  # nobody cancelled it
    _job(campaign, Job.Kind.SEND, Job.State.DONE, ago=timedelta(days=8))  # too long ago
    cancelled, stopped, sent = notices.notices(viewer)  # the newest first
    assert sent.text == "ارسال «قیمت سکه» تمام شد: ۱٬۲۰۰ پیامک پذیرفته شد."
    assert (sent.tone, stopped.tone, cancelled.tone) == ("success", "danger", "neutral")
    assert stopped.text.startswith("ارسال «قیمت سکه» متوقف شد. ") and "۴۱۸" in stopped.text
    assert cancelled.text == "ارسال «قیمت سکه» لغو شد."


def test_an_alert_opens_in_the_composer_for_who_can_send_it(viewer, operator):
    from sms_sender_web.campaigns.models import Preset

    preset = Preset.objects.create(slug="price", name="قیمت", settings={"template": "t"})
    alert = Campaign.objects.create(slug="price-1", name="قیمت ۱", preset=preset, settings={"template": "t"})
    _job(alert, Job.Kind.SEND, Job.State.DONE, result={"sent": 3})
    [notice] = notices.notices(operator)
    assert notice.url == "/compose/c/price-1/"
    # The composer needs run_campaigns: a viewer goes to the campaign's page.
    [notice] = notices.notices(viewer)
    assert notice.url == "/campaigns/price-1/"


def test_low_credit_since_it_dropped(viewer):
    check = ProviderCheck.load()
    check.credit, check.below_since = 120_000, timezone.now() - timedelta(hours=5)
    check.save()
    [notice] = notices.notices(viewer)
    assert notice.at == check.below_since  # not when it was last asked: it isn't new every 15 minutes
    assert notice.text == "اعتبار کاوه‌نگار کمتر از سطح هشدار است: ۱۲۰٬۰۰۰ ریال."
    assert notice.url == "/#credit"


def test_an_overdue_backup_is_for_admins_and_keeps_its_moment(make_user, viewer):
    SystemSettings.objects.filter(pk=1).update(backup_hour=9)
    admin = make_user("boss", "admin")
    now = datetime(2026, 10, 6, 14, 0, tzinfo=TEHRAN)
    [notice] = notices.notices(admin, now)
    assert notice.kind == "backup" and notice.url == "/system/backups/"
    assert notice.at == datetime(2026, 10, 6, 11, 0, tzinfo=TEHRAN)  # due at 9, overdue from 11
    assert notices.notices(admin, now + timedelta(hours=1))[0].at == notice.at
    assert _kinds(viewer) == []


def test_the_count_is_of_those_newer_than_your_last_look(campaign, viewer):
    _job(campaign, Job.Kind.SEND, Job.State.DONE, ago=timedelta(hours=2), result={"sent": 1})
    assert notices.unseen(viewer) == 1
    notices.mark_seen(viewer)
    assert notices.unseen(viewer) == 0
    _job(campaign, Job.Kind.SEND, Job.State.CANCELLED, ago=timedelta(seconds=-1))  # after the look
    assert notices.unseen(viewer) == 1


def test_the_menu_counts_them_and_the_page_marks_them_seen(campaign, signed_in):
    _job(campaign, Job.Kind.SEND, Job.State.DONE, result={"sent": 5})
    _job(campaign, Job.Kind.SEND, Job.State.CANCELLED)
    html = signed_in.get("/campaigns/").content.decode()
    assert '<span class="nav-count" aria-hidden="true">۲</span>' in html
    assert "(۲ تازه)" in html  # what a screen reader hears
    page = signed_in.get("/notifications/").content.decode()
    assert page.count('class="is-new"') == 2 and "ارسال «قیمت سکه» تمام شد" in page
    assert 'href="/notifications/" aria-current="true"' in page
    assert 'class="nav-count"' not in page  # you're looking at them
    page = signed_in.get("/notifications/").content.decode()
    assert 'class="is-new"' not in page and "ارسال «قیمت سکه» لغو شد" in page


def test_nothing_to_hear_about(signed_in):
    html = signed_in.get("/notifications/").content.decode()
    assert "empty-state" in html and "در ۷ روز گذشته خبری نبوده است" in html


def test_the_live_parts_that_poll_never_count_them(signed_in, monkeypatch):
    def counted(*args, **kwargs):
        raise AssertionError("counted on a poll")

    monkeypatch.setattr(notices, "unseen", counted)
    assert signed_in.get("/home/sends/").status_code == 286
