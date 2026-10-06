"""Kavenegar's credit kept current (R5 of the 2026-10-05 review): the worker
asks every 15 minutes, the campaign list warns under the level an admin
set, and the notification targets hear once each time it drops under. And
a send that stops before it can run tells the targets why."""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("django")

from django.utils import timezone  # noqa: E402

from sms_sender.sender import AccountInfo, HaltError  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.models import Job  # noqa: E402
from sms_sender_web.system import credit  # noqa: E402
from sms_sender_web.system.models import ProviderCheck, SystemSettings  # noqa: E402

from .test_jobs import FakeEngine, FakeKavenegar, campaign, make_worker, queue_send  # noqa: E402,F401
from .test_rounds import signed_in  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

TARGET = "slack:https://hooks.slack.com/services/x"


@pytest.fixture
def told(monkeypatch):
    """What the notification targets were told, instead of telling them."""
    sent = []
    fake = lambda target, text: sent.append(text) or True  # noqa: E731
    monkeypatch.setattr("sms_sender_web.system.credit.notify_text", fake)
    monkeypatch.setattr("sms_sender.notify.notify_text", fake)
    SystemSettings.objects.update_or_create(pk=1, defaults={"notify_targets": [TARGET], "credit_floor": 5_000_000})
    return sent


class Account(FakeKavenegar):
    def __init__(self, credit=None, error=None):
        super().__init__()
        self.credit, self.error = credit, error

    def account_info(self):
        if self.error:
            raise self.error
        return AccountInfo(remaining_credit=self.credit, expire_date=None, type="master")


def test_the_targets_hear_once_each_time_it_drops_under(told):
    credit.check_now(Account(9_000_000))
    assert told == [] and credit.warning() is None
    credit.check_now(Account(4_000_000))
    assert told == ["sms-sender: Kavenegar credit is 4,000,000 rials, under the warning level of 5,000,000. "
                    "Top up the account before the next send."]
    credit.check_now(Account(3_000_000))
    assert len(told) == 1  # still under: not again
    assert credit.warning()["credit"] == 3_000_000
    credit.check_now(Account(8_000_000))  # topped up
    assert ProviderCheck.load().below_since is None and credit.warning() is None
    credit.check_now(Account(1_000))
    assert len(told) == 2


def test_a_refused_account_is_a_warning_of_its_own(told):
    credit.check_now(Account(error=HaltError(403, "forbidden")))
    check = ProviderCheck.load()
    assert (check.problem, check.credit) == ("refused", None)
    assert credit.warning()["refused"] is True
    assert credit.check_now(FakeKavenegar()) is None  # can't ask: records nothing


def test_the_worker_asks_every_15_minutes(campaign, told):
    engine = FakeEngine(Account(4_000_000))
    worker = make_worker(engine)
    worker.schedule(force=True)
    first = ProviderCheck.load().checked_at
    assert first is not None and ProviderCheck.load().credit == 4_000_000
    worker.schedule(force=True)
    assert ProviderCheck.load().checked_at == first  # not due yet
    ProviderCheck.objects.filter(pk=1).update(checked_at=timezone.now() - timedelta(minutes=16))
    worker.schedule(force=True)
    assert ProviderCheck.load().checked_at > first


def test_the_campaign_list_warns(told, make_user, verified):
    credit.record(4_000_000)
    viewer = signed_in(make_user("viewer1", "viewer"), verified)
    html = viewer.get("/campaigns/").content.decode()
    assert 'id="credit-warning"' in html and "۴٬۰۰۰٬۰۰۰" in html
    room = viewer.get("/").content.decode()  # the control room's credit card says it too
    assert 'credit-card is-low' in room and "۴٫۰ <span>میلیون ریال</span>" in room  # as people say it
    credit.record(None, "refused")
    assert "کاوه‌نگار بررسی حساب را نپذیرفت" in viewer.get("/campaigns/").content.decode()
    assert "کاوه‌نگار بررسی حساب را نپذیرفت" in viewer.get("/").content.decode()


def test_an_admin_sets_the_warning_level(make_user, verified):
    admin = signed_in(make_user("admin1", "admin"), verified)
    admin.post("/system/", {"section": "credit", "credit_floor": "۲٬۵۰۰٬۰۰۰"})
    assert SystemSettings.load().credit_floor == 2_500_000
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert event.detail == {"credit_floor": {"before": "-", "after": 2_500_000}}
    html = admin.post("/system/", {"section": "credit", "credit_floor": "lots"}).content.decode()
    assert 'id="credit-error"' in html and SystemSettings.load().credit_floor == 2_500_000
    admin.post("/system/", {"section": "credit", "credit_floor": ""})
    assert SystemSettings.load().credit_floor is None


def test_a_send_that_cant_start_tells_the_targets(campaign, told):
    campaign.settings["input"] = "/nowhere/list.csv"
    campaign.save()
    queue_send(campaign)
    job = make_worker(FakeEngine()).run_once()
    assert job.state == Job.State.FAILED and job.result["stop_reason"] == "input_unreadable"
    assert len(told) == 1 and told[0].startswith("sms-sender: coin-7: the send stopped before sending anything:")


def test_a_test_sms_that_went_out_waits_for_approval_and_the_targets_hear_it(campaign, told):
    job = Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, params={"test_number": "09120000099"})
    ran = make_worker(FakeEngine()).run_once()
    assert ran.pk == job.pk and ran.state == Job.State.DONE
    assert told == ["sms-sender: coin-7: the test SMS went to 0912*****99. "
                    "It waits for someone to approve or reject it on the dashboard."]
