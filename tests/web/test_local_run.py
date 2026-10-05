"""Plan 06, L1 in the dashboard: which data a page is on (the badge), a
sign-in remembered on the Mac for 30 days (D5), restricted sending (D7),
the worker's daily backup, and one worker per data folder."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("django")

from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone as dj_timezone  # noqa: E402
from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: E402

from sms_sender.allowlist import ENV_ALLOWED_NUMBERS  # noqa: E402
from sms_sender.sharing import HEARTBEAT  # noqa: E402
from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.accounts.views import REMEMBER_FOR  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns.checks import check_campaign  # noqa: E402
from sms_sender_web.campaigns.present import checklist  # noqa: E402
from sms_sender_web.campaigns.terms import stop_reason  # noqa: E402
from sms_sender_web.jobs import worker as worker_module  # noqa: E402
from sms_sender_web.jobs.models import Campaign, WorkerBeat  # noqa: E402
from sms_sender_web.jobs.worker import Worker, other_worker  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402
from sms_sender_web.system import operations  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

from .conftest import PASSWORD  # noqa: E402
from .test_accounts import code  # noqa: E402

pytestmark = pytest.mark.django_db

ME = "09151097710"
ROWS = ["09151097710", "09120000001", "09120000002"]
TEHRAN = timezone(timedelta(hours=3, minutes=30))


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    settings.BACKUP_DIR = tmp_path / "backups"
    (tmp_path / "db").mkdir()


# ---------- the badge ----------

def test_the_badge_says_which_data_the_mac_runs_on(settings, signed_in):
    settings.LOCAL, settings.SANDBOX = True, False
    html = signed_in.get("/").content.decode()
    assert 'data-env="real"' in html and "محلی · واقعی" in html
    settings.SANDBOX = True
    html = signed_in.get("/").content.decode()
    assert 'data-env="sandbox"' in html and "محلی · شبیه‌سازی" in html
    assert 'data-env="sandbox"' in Client().get("/login/").content.decode()  # before signing in too
    settings.LOCAL = False  # on the server
    assert "env-badge" not in signed_in.get("/").content.decode()


# ---------- remembered on this Mac (D5) ----------

def test_on_the_mac_a_sign_in_may_last_30_days(settings, client, viewer):
    settings.LOCAL = True
    page = client.get("/login/").content.decode()  # the test client asks from 127.0.0.1
    assert 'name="remember"' in page and "تا ۳۰ روز در همین مک وارد بمانم" in page
    client.post("/login/", {"username": "viewer1", "password": PASSWORD, "remember": "1"})
    age = client.session.get_expiry_age()
    assert REMEMBER_FOR.total_seconds() - 60 < age <= REMEMBER_FOR.total_seconds()
    event = AuditEvent.objects.get(action="login")
    assert event.detail == {"remembered": True}


def test_without_asking_a_sign_in_lasts_8_hours(settings, client, viewer):
    settings.LOCAL = True
    client.post("/login/", {"username": "viewer1", "password": PASSWORD})
    assert client.session.get_expiry_age() == settings.SESSION_COOKIE_AGE
    assert AuditEvent.objects.get(action="login").detail == {}


def test_elsewhere_it_is_never_offered_nor_taken(settings, viewer):
    settings.LOCAL = False  # the server
    server = Client()
    assert 'name="remember"' not in server.get("/login/").content.decode()
    server.post("/login/", {"username": "viewer1", "password": PASSWORD, "remember": "1"})
    assert server.session.get_expiry_age() == settings.SESSION_COOKIE_AGE
    settings.LOCAL = True  # the Mac, but asked from another machine (e.g. over NetBird)
    colleague = Client(REMOTE_ADDR="100.64.0.7")
    assert 'name="remember"' not in colleague.get("/login/").content.decode()
    colleague.post("/login/", {"username": "viewer1", "password": PASSWORD, "remember": "1"})
    assert colleague.session.get_expiry_age() == settings.SESSION_COOKIE_AGE
    assert not any(e.detail.get("remembered") for e in AuditEvent.objects.filter(action="login"))


def test_an_operator_still_gives_the_code_and_then_stays(settings, client, make_user):
    settings.LOCAL = True
    operator = make_user("operator1", "operator")
    device = TOTPDevice.objects.create(user=operator, name="authenticator", confirmed=True)
    client.post("/login/", {"username": "operator1", "password": PASSWORD, "remember": "1"})
    assert client.get("/")["Location"] == "/2fa/?next=/"  # the second step still comes
    client.post("/2fa/?next=/", {"code": code(device), "next": "/"})
    assert client.get("/").status_code == 200
    assert client.session.get_expiry_age() > (REMEMBER_FOR - timedelta(minutes=1)).total_seconds()


# ---------- restricted sending (D7) ----------

def test_restricted_sending_shows_on_every_page(settings, signed_in, monkeypatch):
    settings.SANDBOX = False
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    html = signed_in.get("/").content.decode()
    assert 'id="restricted-banner"' in html and "فقط به شماره‌های مجاز" in html
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, "0915-typo")
    assert "ارسال محدود درست تنظیم نشده است" in signed_in.get("/").content.decode()
    settings.SANDBOX = True  # the sandbox sends nothing: not bound by it
    assert 'id="restricted-banner"' not in signed_in.get("/").content.decode()
    settings.SANDBOX = False
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, "")
    assert 'id="restricted-banner"' not in signed_in.get("/").content.decode()


@pytest.fixture
def campaign(tmp_path):
    folder = tmp_path / "segments"
    folder.mkdir()
    (folder / "vip.csv").write_text("phone\n" + "".join(f"{p}\n" for p in ROWS), encoding="utf-8")
    segment = Segment.objects.create(slug="vip", name="VIP", status=Segment.Status.READY, columns=["phone"])
    return Campaign.objects.create(slug="coin-7", name="قیمت سکه", settings={
        "segment": "vip", "input": str(folder / "vip.csv"), "template": "coin-price", "send_window": "off",
    }), segment


def test_the_check_says_who_restricted_sending_holds_back(settings, monkeypatch, campaign):
    campaign, segment = campaign
    settings.SANDBOX = False
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    result = check_campaign(campaign)
    assert (result.not_allowed, result.ok) == (2, True)  # a test SMS to ME still works
    line = next(item for item in checklist(result, None, campaign.settings, segment) if item.label == "ارسال محدود")
    assert line.state == "warn" and "۲ گیرنده" in line.note
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ",".join(ROWS))
    result = check_campaign(campaign)
    line = next(item for item in checklist(result, None, campaign.settings, segment) if item.label == "ارسال محدود")
    assert (result.not_allowed, line.state) == (0, "ok")
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, "typo")
    result = check_campaign(campaign)
    assert "allowlist_invalid" in result.problems and not result.ok  # not even a test SMS
    settings.SANDBOX = True
    assert check_campaign(campaign).not_allowed is None


def test_a_run_restricted_sending_stopped_says_why_in_persian():
    assert "۲ گیرنده جزو شماره‌های مجاز نیستند" in stop_reason("recipients_not_allowed", {"count": 2})
    assert "پیامک آزمایشی فرستاده نشد" in stop_reason("test_number_not_allowed", {})
    assert "درست تنظیم نشده است" in stop_reason("allowlist_invalid", {"count": 1})


def test_the_status_page_names_the_allowed_numbers_masked(settings, signed_in, monkeypatch):
    from .test_reports import FakeShlink

    settings.SANDBOX = False
    monkeypatch.setenv(ENV_ALLOWED_NUMBERS, ME)
    monkeypatch.setattr("sms_sender_web.jobs.engine.Engine.link_client", lambda self: FakeShlink())
    html = signed_in.get("/status/").content.decode()
    assert 'id="restricted-sending"' in html and "۰۹۱۵*****۱۰" in html and ME not in html


# ---------- the daily backup ----------

def _at(hour: int, minute: int = 0, day: int = 6) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TEHRAN)


def test_the_daily_backup_is_due_once_its_hour_has_passed():
    assert operations.scheduled_moment(_at(10), 9) == _at(9)
    assert operations.scheduled_moment(_at(8), 9) == _at(9, day=5)  # today's is still ahead
    assert operations.backup_due(_at(9, 1), 9, None)
    assert operations.backup_due(_at(9, 1), 9, _at(8, 59))
    assert not operations.backup_due(_at(9, 1), 9, _at(9))
    assert not operations.backup_due(_at(8), 9, _at(9, day=5))
    assert operations.backup_due(_at(7), 9, _at(9, day=4))  # a day missed while the Mac slept
    assert not operations.backup_due(_at(23), None, None)  # off


def test_it_is_overdue_two_hours_on():
    newest = _at(9, day=5)
    assert not operations.backup_overdue(_at(10, 59), 9, newest)
    assert operations.backup_overdue(_at(11, 1), 9, newest)
    assert not operations.backup_overdue(_at(11, 1), None, newest)


def test_a_backups_name_is_its_moment():
    assert operations.stamp("20261006T053000Z") == datetime(2026, 10, 6, 5, 30, tzinfo=timezone.utc)
    assert operations.stamp("20261006T053000Z-2") == operations.stamp("20261006T053000Z")
    assert operations.stamp("not-a-backup") is None


def _worker() -> Worker:
    return Worker(worker_id=f"{socket.gethostname()}:{os.getpid()}")


def test_the_worker_makes_the_daily_backup_when_due(settings, tmp_path):
    StateStore(tmp_path / "db" / "coin-7.db")  # a campaign DB to copy
    current = SystemSettings.load()
    current.backup_hour, current.backup_keep = 9, 2
    current.save()
    worker = _worker()
    worker.back_up_if_due(_at(8, day=1)).join()  # nothing yet: due since yesterday's 09:00
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert worker.back_up_if_due(dj_timezone.now() - timedelta(minutes=1)) is None  # done for now
    current.backup_hour = None
    current.save()
    assert worker.back_up_if_due(dj_timezone.now() + timedelta(days=2)) is None  # off


def test_a_failed_daily_backup_is_announced_once_and_tried_again_later(settings, monkeypatch):
    current = SystemSettings.load()
    current.notify_targets = ["https://hooks.example.invalid/x"]
    current.save()
    said = []
    monkeypatch.setattr(worker_module, "notify_text", lambda target, text: said.append(text) or True)
    monkeypatch.setattr("sms_sender_web.backup.make_backup", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    worker, now = _worker(), dj_timezone.now()
    worker.back_up_if_due(now).join()
    assert len(said) == 1 and "daily backup failed (OSError)" in said[0]
    assert worker.back_up_if_due(now + timedelta(minutes=30)) is None  # waits an hour
    worker.back_up_if_due(now + timedelta(hours=1, minutes=1)).join()
    assert len(said) == 1  # still failing: said once
    monkeypatch.undo()
    worker.back_up_if_due(now + timedelta(hours=3)).join()
    assert worker._backup_retry_at is None and worker._backup_failing is False


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def test_an_admin_sets_the_daily_backup(admin_client):
    page = admin_client.get("/system/backups/").content.decode()
    assert "پشتیبان‌گیری روزانه" in page and '<option value="9" selected>۰۹:۰۰</option>' in page
    admin_client.post("/system/backups/", {"action": "schedule", "backup_hour": "7", "backup_keep": "۱۰"})
    current = SystemSettings.load()
    assert (current.backup_hour, current.backup_keep) == (7, 10)
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert event.detail == {"backups": {"before": "09:00/14", "after": "07:00/10"}}
    admin_client.post("/system/backups/", {"action": "schedule", "backup_hour": "off", "backup_keep": "10"})
    assert SystemSettings.load().backup_hour is None
    response = admin_client.post("/system/backups/", {"action": "schedule", "backup_hour": "7", "backup_keep": "0"},
                                 follow=True)
    assert "بین ۱ تا ۹۰ نسخه" in response.content.decode()
    assert SystemSettings.load().backup_hour is None  # unchanged


def test_an_overdue_daily_backup_is_called_out(admin_client, monkeypatch):
    monkeypatch.setattr(operations, "newest_backup_at", lambda: dj_timezone.now() - timedelta(days=3))
    assert "عقب افتاده است" in admin_client.get("/system/backups/").content.decode()
    current = SystemSettings.load()
    current.backup_hour = None
    current.save()
    assert "عقب افتاده است" not in admin_client.get("/system/backups/").content.decode()


# ---------- one worker per data folder ----------

def test_another_worker_alive_on_the_folder_is_named(settings, tmp_path):
    me = f"{socket.gethostname()}:{os.getpid()}"
    assert other_worker(me) is None
    WorkerBeat.objects.create(worker_id=me, seen_at=dj_timezone.now())  # itself
    assert other_worker(me) is None
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    WorkerBeat.objects.create(worker_id=f"{socket.gethostname()}:{ended.pid}", seen_at=dj_timezone.now())
    assert other_worker(me) is None  # stopped without signing off
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        WorkerBeat.objects.create(worker_id=f"{socket.gethostname()}:{alive.pid}", seen_at=dj_timezone.now())
        assert "running on this machine" in other_worker(me)
    finally:
        alive.kill()
        alive.wait()
    WorkerBeat.objects.exclude(worker_id=me).delete()
    WorkerBeat.objects.create(worker_id="a1b2c3d4e5f6:1", seen_at=dj_timezone.now() - timedelta(seconds=20))
    assert "another machine or container" in other_worker(me)  # e.g. the old Docker worker
    WorkerBeat.objects.filter(worker_id="a1b2c3d4e5f6:1").update(seen_at=dj_timezone.now() - timedelta(minutes=5))
    assert other_worker(me) is None
    (tmp_path / "db" / HEARTBEAT).write_text(json.dumps(
        {"kernel": "linux:docker-desktop", "worker": "vm-worker:1", "at": time.time()}), encoding="utf-8")
    assert "another VM" in other_worker(me)


def test_run_worker_waits_for_another_worker_then_gives_up(monkeypatch):
    from sms_sender_web.jobs.management.commands import run_worker as command

    monkeypatch.setattr(command, "WAIT_SEC", 0.3)
    monkeypatch.setattr(command, "LOOK_EVERY_SEC", 0.1)
    WorkerBeat.objects.create(worker_id="a1b2c3d4e5f6:1", seen_at=dj_timezone.now())
    with pytest.raises(CommandError, match="Run exactly one worker") as refused:
        call_command("run_worker", "--once")
    assert refused.value.returncode == 2
    WorkerBeat.objects.all().delete()
    call_command("run_worker", "--once")  # nobody else: it runs


def test_a_clean_stop_signs_off(tmp_path):
    worker = _worker()
    worker.beat()
    assert WorkerBeat.objects.filter(worker_id=worker.id).exists() and (tmp_path / "db" / HEARTBEAT).exists()
    worker.stop.set()
    worker.run_forever(poll_sec=0.01)
    assert not WorkerBeat.objects.filter(worker_id=worker.id).exists()
    assert not (tmp_path / "db" / HEARTBEAT).exists()
