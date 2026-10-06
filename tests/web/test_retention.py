"""Plan 06, D2 in the dashboard: phone numbers older than the period an
admin set (12 months by default) are removed once a day. The counts stay,
nothing on its way is touched, and nothing can send from a campaign whose
numbers went."""
import io
import json
import os
import sqlite3
import time
from datetime import timedelta

import pytest

pytest.importorskip("django")

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from sms_sender.locking import RunLock  # noqa: E402
from sms_sender.state import SENT, StateStore  # noqa: E402
from sms_sender_web import retention  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.audit.terms import describe  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job, JobEvent  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402
from sms_sender_web.system.models import SystemSettings  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

A, B = "09120000001", "09120000002"
DAY = 86_400


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    settings.BACKUP_DIR = tmp_path / "backups"
    for name in ("db", "segments", "exports", "backups"):
        (tmp_path / name).mkdir()
    return tmp_path


def sent_long_ago(places, slug: str, days: int, segment: str = "vip") -> StateStore:
    """A campaign DB that sent to A and B `days` ago."""
    db = places / "db" / f"{slug}.db"
    store = StateStore(db)
    store.upsert_pending([(A, A), (B, B)], segment=segment)
    for n, phone in enumerate((A, B)):
        store.claim(phone)
        store.mark_sent(phone, 1000 + n, 200, 3020)
        store.record_attempt(phone=phone, kind="send", outcome="accepted", started_at=time.time(),
                             message_id=1000 + n)
    then = time.time() - days * DAY
    conn = sqlite3.connect(db)
    conn.execute("UPDATE recipients SET first_seen_at=?, last_attempt_at=?, sent_at=?", (then, then, then))
    conn.execute("UPDATE attempts SET started_at=?, finished_at=?", (then, then))
    conn.commit()
    conn.close()
    return store


def a_campaign(slug: str, segment: str = "vip", days: int = 400) -> Campaign:
    campaign = Campaign.objects.create(slug=slug, name=slug, settings={
        "segment": segment, "input": "", "template": "coin-price", "tokens": {"token": "x"},
    })
    Campaign.objects.filter(pk=campaign.pk).update(created_at=timezone.now() - timedelta(days=days))
    return campaign


def a_segment(places, slug: str, days: int) -> Segment:
    segment = Segment.objects.create(slug=slug, name=slug, status=Segment.Status.READY, columns=["phone"],
                                     summary={"rows": 2, "valid": 2, "invalid": 0, "duplicates": 0})
    segment.path.write_text(f"phone\n{A}\n{B}\n", encoding="utf-8")
    then = time.time() - days * DAY
    os.utime(segment.path, (then, then))
    Segment.objects.filter(pk=segment.pk).update(uploaded_at=timezone.now() - timedelta(days=days))
    segment.refresh_from_db()
    return segment


# ---------- what goes, and what stays ----------

def test_an_old_campaign_loses_its_numbers_and_keeps_its_counts(places):
    sent_long_ago(places, "old", 400)
    sent_long_ago(places, "recent", 30)
    removed = retention.remove_old_numbers()
    assert (removed.campaigns, removed.numbers, removed.kept) == (["old"], 2, [])
    old, recent = StateStore(places / "db" / "old.db"), StateStore(places / "db" / "recent.db")
    assert old.numbers_removed_at() is not None and old.counts() == {SENT: 2}
    assert recent.numbers_removed_at() is None and recent.status_for_phones([A]) == {A: SENT}


def test_the_period_is_the_admins(places):
    sent_long_ago(places, "old", 200)
    assert retention.remove_old_numbers().campaigns == []  # 12 months: not yet
    SystemSettings.objects.update_or_create(pk=1, defaults={"retention_months": 6})
    assert retention.remove_old_numbers().campaigns == ["old"]


def test_nothing_on_its_way_is_touched(places):
    sent_long_ago(places, "paused", 400)
    sent_long_ago(places, "cli", 400)
    Job.objects.create(campaign=a_campaign("paused"), kind=Job.Kind.SEND, state=Job.State.PAUSED)
    with RunLock(places / "db" / "cli.db"):  # the CLI is using it
        removed = retention.remove_old_numbers()
    assert removed.campaigns == [] and sorted(removed.kept) == ["cli", "paused"]
    assert StateStore(places / "db" / "paused.db").numbers_removed_at() is None


def test_a_dry_run_only_says(places):
    sent_long_ago(places, "old", 400)
    removed = retention.remove_old_numbers(dry_run=True)
    assert removed.campaigns == ["old"]
    assert StateStore(places / "db" / "old.db").numbers_removed_at() is None
    out = io.StringIO()
    call_command("remove_old_numbers", "--dry-run", stdout=out)
    assert "campaigns  1 would be removed: old" in out.getvalue()
    assert SystemSettings.load().retention_ran_at is None


def test_old_segment_files_go_unless_a_campaign_still_uses_them(places):
    unused = a_segment(places, "unused", 400)
    a_segment(places, "wanted", 400)
    Job.objects.create(campaign=a_campaign("new-round", segment="wanted"), kind=Job.Kind.TEST)  # asked for today
    a_segment(places, "sending", 400)
    sent_long_ago(places, "live", 10, segment="sending")  # a campaign DB sent from it lately
    replaced = a_segment(places, "replaced", 400)
    os.utime(replaced.path, None)  # its file is new
    a_segment(places, "young", 30)

    removed = retention.remove_old_numbers()
    assert removed.segments == ["unused"]
    unused.refresh_from_db()
    assert unused.status == Segment.Status.REMOVED and unused.numbers_removed_at is not None
    assert not unused.path.exists()
    assert all(Segment.objects.get(slug=s).path.exists() for s in ("wanted", "sending", "replaced", "young"))


def test_old_downloads_and_backups_go(places):
    old, new = places / "exports" / "old.csv", places / "exports" / "new.csv"
    for path in (old, new):
        path.write_text("phone\n", encoding="utf-8")
    then = time.time() - 400 * DAY
    os.utime(old, (then, then))
    for name, made in (("20240101T090000Z", "2024-01-01T09:00:00+00:00"),
                       (timezone.now().strftime("%Y%m%dT%H%M%SZ"), timezone.now().isoformat())):
        folder = places / "backups" / name
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"created_at": made, "files": []}), encoding="utf-8")
    removed = retention.remove_old_numbers()
    assert (removed.exports, removed.backups) == (1, ["20240101T090000Z"])
    assert not old.exists() and new.exists()
    assert [p.name for p in (places / "backups").iterdir()] != [] and not (places / "backups" / "20240101T090000Z").exists()


def test_old_records_keep_only_masked_numbers(places, make_user):
    campaign = a_campaign("coin-7")
    old = Job.objects.create(
        campaign=campaign, kind=Job.Kind.TEST, params={"test_number": A, "team_numbers": [B]},
        result={"top_errors": [[f"[411] invalid receptor {B}", 1]]}, last_error=f"to {A}: refused",
    )
    JobEvent.objects.create(job=old, key="test_sending", text=f"Approval test: sending to {A} …", data={"phone": A})
    event = AuditEvent.objects.create(username="op", action="phone_revealed", detail={"phone": A})
    recent = Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, params={"test_number": A})
    long_ago = timezone.now() - timedelta(days=400)
    Job.objects.filter(pk=old.pk).update(created_at=long_ago)
    JobEvent.objects.filter(job=old).update(at=long_ago)
    AuditEvent.objects.filter(pk=event.pk).update(at=long_ago)

    assert retention.remove_old_numbers().records == 3
    old.refresh_from_db()
    assert old.params == {"test_number": "0912*****01", "team_numbers": ["0912*****02"]}
    assert old.result["top_errors"] == [["[411] invalid receptor ***", 1]]
    assert old.last_error == "to ***: refused"
    note = old.events.get()
    assert (note.data, note.text) == ({"phone": "0912*****01"}, "Approval test: sending to *** …")
    event.refresh_from_db()
    assert event.detail == {"phone": "0912*****01"}
    recent.refresh_from_db()
    assert recent.params == {"test_number": A}  # still within the period
    assert retention.remove_old_numbers().records == 0  # masked once


# ---------- once a day ----------

def test_the_daily_run_is_recorded_and_announced(places, monkeypatch):
    told = []
    monkeypatch.setattr("sms_sender.notify.notify_text", lambda target, text: told.append(text) or True)
    SystemSettings.objects.update_or_create(pk=1, defaults={"notify_targets": ["slack:https://hooks.slack.com/x"]})
    sent_long_ago(places, "old", 400)
    now = timezone.now()
    assert retention.due(now)
    worker = Worker(worker_id="w1")
    thread = worker.remove_old_numbers_if_due(now)
    thread.join(30)
    current = SystemSettings.load()
    assert current.retention_ran_at is not None and current.retention_result["campaigns"] == ["old"]
    assert not retention.due(now + timedelta(hours=23)) and retention.due(now + timedelta(days=1, minutes=1))
    assert worker.remove_old_numbers_if_due(now + timedelta(hours=1)) is None
    event = AuditEvent.objects.get(action="numbers_removed")
    assert event.username == "worker"
    assert str(describe(event)) == "قدیمی‌تر از ۱۲ ماه: ۱ کمپین، ۰ فایل گروه مخاطبان، ۰ نسخه پشتیبان"
    assert told == ["sms-sender: phone numbers older than 12 months were removed: 1 campaign(s), "
                    "0 segment file(s), 0 download(s), 0 backup(s). Counts stay."]


def test_a_quiet_day_records_nothing(places):
    retention.run_and_record()
    assert SystemSettings.load().retention_ran_at is not None
    assert not AuditEvent.objects.filter(action="numbers_removed").exists()


# ---------- the pages ----------

@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


@pytest.fixture
def operator_client(make_user, verified):
    user = make_user("operator1", "operator")
    Profile.objects.create(user=user, test_phone="09150000077")
    client = Client()
    verified(client, user)
    return client


def test_an_admin_sets_how_long_numbers_are_kept(admin_client, places):
    html = admin_client.get("/system/").content.decode()
    assert 'data-cli="manage.py remove_old_numbers"' in html and '<option value="12" selected>' in html
    html = admin_client.post("/system/", {"section": "retention", "retention_months": "۲۴"}, follow=True).content.decode()
    assert SystemSettings.load().retention_months == 24
    assert "ذخیره شد. اجراکننده کارها در بررسی روزانه بعدی" in html
    event = AuditEvent.objects.get(action="system_settings_changed")
    assert str(describe(event)) == "مدت نگهداری شماره‌ها: از ۱۲ به ۲۴ ماه"
    html = admin_client.post("/system/", {"section": "retention", "retention_months": "3"}).content.decode()
    assert "مدت نگهداری را انتخاب کنید." in html and SystemSettings.load().retention_months == 24

    sent_long_ago(places, "paused", 800)
    Job.objects.create(campaign=a_campaign("paused"), kind=Job.Kind.SEND, state=Job.State.PAUSED)
    retention.run_and_record()
    html = admin_client.get("/system/").content.decode()
    assert "آخرین بررسی" in html and "تا پایان کاری که در جریان است نگه داشته می‌شوند" in html


def test_a_campaign_without_its_numbers_offers_only_its_history(operator_client, places):
    campaign = a_campaign("old")
    sent_long_ago(places, "old", 400)
    retention.remove_old_numbers()
    html = operator_client.get("/campaigns/old/").content.decode()
    assert "شماره‌های آن در" in html and "ساخت کمپین مشابه" in html
    assert 'id="test-form"' not in html
    response = operator_client.get("/campaigns/old/settings/", follow=True)
    assert "شماره‌های این کمپین حذف شده است" in response.content.decode()
    for attempt in (lambda: services.request_test(campaign, None), lambda: services.start_send(campaign, None),
                    lambda: services.requeue(campaign, "failed_permanent")):
        with pytest.raises(services.JobConflict) as e:
            attempt()
        assert e.value.code == "numbers_removed"
    assert not services.can_unlock(campaign)


def test_its_report_keeps_the_counts_without_the_numbers(operator_client, places):
    a_campaign("old")
    sent_long_ago(places, "old", 400)
    retention.remove_old_numbers()
    html = operator_client.get("/reports/old/").content.decode()
    assert "شماره‌های آن در" in html
    assert html.count("حذف‌شده") >= 2  # each recipient's number
    assert 'id="audience"' not in html
    assert "/recipients.csv" not in html and "/clickers.csv" not in html
    assert "/attribution.csv" in html  # no numbers in it, so it stays
    assert operator_client.post("/reports/old/reveal/", {"row": "1"}).status_code == 404


def test_a_segment_without_its_file_keeps_its_counts(operator_client, places):
    a_segment(places, "unused", 400)
    retention.remove_old_numbers()
    html = operator_client.get("/segments/unused/").content.decode()
    assert "فایل آن در" in html and "آمار آن می‌ماند" in html
    assert "/segments/unused/download/" not in html
    assert operator_client.get("/segments/unused/download/").status_code == 404


def test_the_help_page_says_how_long(operator_client):
    html = operator_client.get("/help/").content.decode()
    assert "شماره‌ها تا ۱۲ ماه پس از آخرین ارسال هر کمپین نگهداری" in html
