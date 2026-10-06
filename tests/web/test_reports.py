"""Step 3.6: reports per campaign, segment and recipient, downloads, and
the status page (spec 4.9, 4.12). Numbers are masked; an operator can
reveal one at a time, and that's recorded. Nothing here sends."""
from datetime import timedelta

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from sms_sender.clicks import sync_clicks  # noqa: E402
from sms_sender.sender import AccountConfig, AccountInfo, HaltError  # noqa: E402
from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.engine import Engine  # noqa: E402
from sms_sender_web.jobs.models import WorkerBeat  # noqa: E402
from sms_sender_web.jobs.worker import Worker, worker_alive  # noqa: E402

from ..test_clicks import A, B, C, Visits  # noqa: E402
from ..test_clicks import campaign_db as clicks_campaign  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def store(settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    store = clicks_campaign(tmp_path / "coin-7.db")
    sync_clicks(store, Visits({"c1": 2, "c2": 1, "c3": 0}), "coin-7")
    store.record_invalid_many([("0912000", "not a phone number")])
    return store


@pytest.fixture
def operator_client(make_user, verified):
    client = Client()
    verified(client, make_user("operator1", "operator"))
    return client


def test_the_report_is_persian_and_masks_every_number(signed_in, store):
    html = signed_in.get("/reports/coin-7/").content.decode()
    assert "گزارش" in html and "پذیرفته‌شده" in html
    assert "تحویل‌شده" in html and "استعلام‌نشده" in html  # delivery, merged by meaning
    assert "vip-2" in html and "افرادی که کلیک کردند" in html
    for phone in (A, B, C):
        assert phone not in html
    assert "۰۹۱۲*****۰۱" in html and "*******" in html  # the invalid row too
    assert "ردشده" not in html  # an invalid input row is «نامعتبر», as in the badges
    assert "بدون شناسه کاربر" in html
    # Viewers: the aggregate download only, and no "show" buttons.
    assert 'href="/reports/coin-7/summary.csv"' in html
    assert "attribution.csv" not in html and "clickers.csv" not in html
    assert "/reveal/" not in html


def test_unknown_campaigns_are_not_found(signed_in, store):
    assert signed_in.get("/reports/nope/").status_code == 404
    assert signed_in.get("/reports/Bad_Slug/").status_code == 404


def test_finding_one_number_never_puts_it_in_a_url(signed_in, store):
    html = signed_in.post("/reports/coin-7/", {"q": "+98 912 000 0002"}).content.decode()
    assert "۰۹۱۲*****۰۲" in html and "۰۹۱۲*****۰۱" not in html
    assert "این شماره در این کمپین نیست" in signed_in.post("/reports/coin-7/", {"q": "09120009999"}).content.decode()
    assert "این شماره موبایل معتبر نیست" in signed_in.post("/reports/coin-7/", {"q": "x"}).content.decode()
    # The form posts it; a number in the address is ignored.
    html = signed_in.get("/reports/coin-7/", {"q": "09120000002"}).content.decode()
    assert "۰۹۱۲*****۰۱" in html
    assert 'method="post" action="/reports/coin-7/#recipients" class="inline search" id="find-number"' in html


def test_recipients_come_a_hundred_to_a_page(signed_in, settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    big = StateStore(tmp_path / "big.db")
    big.upsert_pending([(f"0912000{i:04d}", "x") for i in range(150)])
    html = signed_in.get("/reports/big/").content.decode()
    assert "۱ / ۲" in html and "?page=2" in html
    html = signed_in.get("/reports/big/", {"page": "2"}).content.decode()
    assert "۲ / ۲" in html and html.count("۰۹۱۲*****") == 50


def test_an_operator_reveals_one_number_and_it_is_recorded(operator_client, signed_in, store):
    html = operator_client.get("/reports/coin-7/").content.decode()
    assert 'hx-post="/reports/coin-7/reveal/"' in html
    row = store.recipients_page(limit=10, phone=A)[0]["id"]
    response = operator_client.post("/reports/coin-7/reveal/", {"row": row})
    assert response.status_code == 200
    assert "۰۹۱۲۰۰۰۰۰۰۱" in response.content.decode()
    assert "no-store" in response["Cache-Control"]
    (event,) = AuditEvent.objects.filter(action="phone_revealed")
    assert (event.username, event.campaign, event.detail) == ("operator1", "coin-7", {"phone": A})
    assert signed_in.post("/reports/coin-7/reveal/", {"row": row}).status_code == 403


def test_invalid_rows_and_unknown_rows_are_never_revealed(operator_client, store):
    invalid = [r["id"] for r in store.recipients_page(limit=10) if r["phone"].startswith("INVALID:")]
    assert operator_client.post("/reports/coin-7/reveal/", {"row": invalid[0]}).status_code == 404
    assert operator_client.post("/reports/coin-7/reveal/", {"row": "999"}).status_code == 404
    assert operator_client.post("/reports/coin-7/reveal/", {"row": "x"}).status_code == 404
    assert not AuditEvent.objects.filter(action="phone_revealed").exists()


def test_the_summary_download_has_no_personal_data(signed_in, store):
    response = signed_in.get("/reports/coin-7/summary.csv")
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert 'filename="coin-7-summary.csv"' in response["Content-Disposition"]
    text = response.content.decode("utf-8")
    assert text.startswith("﻿section,name,value")
    assert "status,sent,3" in text and "delivery,delivered,1" in text
    assert "segment:vip-2,clicked,2" in text
    assert not any(p in text for p in (A, B, C))
    assert AuditEvent.objects.get(action="report_downloaded").detail == {"kind": "summary"}


def test_operators_download_attribution_and_clickers(operator_client, signed_in, store):
    attribution = operator_client.get("/reports/coin-7/attribution.csv").content.decode("utf-8")
    assert attribution.startswith("﻿ref,user_id,user_id_status,segment,short_url")
    assert not any(p in attribution for p in (A, B, C))  # no phone numbers
    clickers = operator_client.get("/reports/coin-7/clickers.csv").content.decode("utf-8")
    assert A in clickers and B in clickers and C not in clickers  # C didn't click
    assert [e.detail["kind"] for e in AuditEvent.objects.filter(action="report_downloaded").order_by("id")] == [
        "attribution", "clickers",
    ]
    assert signed_in.get("/reports/coin-7/attribution.csv").status_code == 403
    assert signed_in.get("/reports/coin-7/clickers.csv").status_code == 403


def test_the_activity_log_names_downloads_and_masks_revealed_numbers(make_user, verified, operator_client, store):
    row = store.recipients_page(limit=10, phone=A)[0]["id"]
    operator_client.post("/reports/coin-7/reveal/", {"row": row})
    operator_client.get("/reports/coin-7/clickers.csv")
    admin = Client()
    verified(admin, make_user("admin1", "admin"))
    html = admin.get("/activity/").content.decode()
    assert "نمایش شماره تلفن" in html and "۰۹۱۲*****۰۱" in html and A not in html
    assert "افرادی که کلیک کردند، با شماره تلفن" in html


# --- Status page -------------------------------------------------------------------


class FakeAccount:
    def __init__(self, info=None, config=None, error=None):
        self.info, self.config, self.error = info, config, error

    def account_info(self):
        if self.error:
            raise self.error
        return self.info

    def account_config(self):
        return self.config


class FakeShlink:
    def __init__(self, version="5.1.7"):
        self.version = version

    def health(self):
        return self.version


def test_the_status_page_asks_kavenegar_shlink_and_the_worker(signed_in, monkeypatch):
    account = FakeAccount(AccountInfo(remaining_credit=264_731_842, expire_date=None, type="master"),
                          AccountConfig(debug_mode=False, resend_failed=True))
    monkeypatch.setattr(Engine, "sender", lambda self: account)
    monkeypatch.setattr(Engine, "link_client", lambda self: FakeShlink())
    WorkerBeat.objects.create(worker_id="w1", seen_at=timezone.now())
    html = signed_in.get("/status/").content.decode()
    assert '<span class="amount">۲۶۴٬۷۳۱٬۸۴۲</span> <span class="unit">ریال</span>' in html
    assert "حالت آزمایشی (debug) خاموش است" in html and "resend failed" in html
    assert "فعال" in html and "5.1.7" in html
    assert "در حال اجرا" in html


def status_with_expiry(signed_in, monkeypatch, expire_date):
    account = FakeAccount(AccountInfo(remaining_credit=1_000, expire_date=expire_date, type="Customer"),
                          AccountConfig(debug_mode=False, resend_failed=False))
    monkeypatch.setattr(Engine, "sender", lambda self: account)
    monkeypatch.setattr(Engine, "link_client", lambda self: FakeShlink())
    return signed_in.get("/status/")


def test_an_account_that_never_expires_says_so(signed_in, monkeypatch):
    """Kavenegar reports "never" as 9999-12-31, Tehran time (253402201800),
    beyond any Solar Hijri date: the real account's status page failed on it."""
    response = status_with_expiry(signed_in, monkeypatch, "253402201800")
    assert response.status_code == 200
    assert "بدون تاریخ انقضا" in response.content.decode()


def test_an_account_expiry_is_a_solar_hijri_date(signed_in, monkeypatch):
    html = status_with_expiry(signed_in, monkeypatch, "1767225600").content.decode()  # 2026-01-01 UTC
    assert "۱۴۰۴/۱۰/۱۱" in html and "بدون تاریخ انقضا" not in html


def test_the_status_page_explains_problems_in_persian(signed_in, monkeypatch):
    monkeypatch.setattr(Engine, "sender", lambda self: FakeAccount(error=HaltError(401, "invalid key")))
    monkeypatch.setattr(Engine, "link_client", lambda self: FakeShlink(version=None))
    html = signed_in.get("/status/").content.decode()
    assert "کاوه‌نگار در بررسی حساب خطای ۴۰۱ برگرداند: حساب کاوه‌نگار غیرفعال است." in html
    assert "سرویس لینک کوتاه مشکلی گزارش می‌کند" in html
    assert "در حال اجرا نیست" in html  # no worker seen


def test_the_status_page_without_keys(signed_in, monkeypatch):
    monkeypatch.setattr(Engine, "link_client", lambda self: FakeShlink())  # no network in tests
    html = signed_in.get("/status/").content.decode()  # the test env has no Kavenegar key
    assert "کلید کاوه‌نگار روی سرور تنظیم نشده است" in html


def test_a_worker_is_alive_while_it_beats():
    assert not worker_alive()
    Worker(worker_id="w1").beat()
    assert worker_alive()
    WorkerBeat.objects.update(seen_at=timezone.now() - timedelta(minutes=5))
    assert not worker_alive()


def test_campaigns_link_to_their_report(signed_in, store):
    assert 'href="/reports/coin-7/"' in signed_in.get("/").content.decode()


# ---------- the recipients list: filters and its downloads (plan 05, P3) ----------

def listed(html: str) -> int:
    """Rows in the recipients table (each has one "accepted at" cell)."""
    from django.utils import translation
    from django.utils.translation import gettext

    with translation.override("fa"):
        return html.count(f'<td data-label="{gettext("Accepted at")}">')


def test_the_list_filters_by_what_people_ask(signed_in, store):
    def get(**params) -> str:
        return signed_in.get("/reports/coin-7/", params).content.decode()

    assert listed(get()) == 5 and "۵ گیرنده در این کمپین." in get()
    assert listed(get(status="sent")) == 3 and "۳ گیرنده با این فیلترها." in get(status="sent")
    assert listed(get(status="invalid")) == 1
    assert listed(get(segment="new-users")) == 2
    assert listed(get(delivery="delivered")) == 1 and listed(get(delivery="unchecked")) == 2
    assert listed(get(clicked="yes")) == 2 and listed(get(clicked="no")) == 1
    assert listed(get(missing="1")) == 1
    assert listed(get(status="nonsense", delivery="x", clicked="maybe")) == 5  # unknown values: ignored
    assert "هیچ گیرنده‌ای با این فیلترها پیدا نشد" in get(status="pending", segment="vip-2")
    html = get(status="sent")
    assert '<option value="sent" selected>' in html and "حذف فیلترها" in html


def test_pages_keep_the_filters_and_never_a_number(signed_in, settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    big = StateStore(tmp_path / "big.db")
    big.upsert_pending([(f"0912000{i:04d}", "x") for i in range(150)])
    html = signed_in.get("/reports/big/", {"status": "pending"}).content.decode()
    assert '?status=pending&amp;page=2#recipients' in html and "0912" not in html.split('class="pager"')[1]


def test_only_filters_that_mean_something_are_offered(signed_in, store, tmp_path):
    html = signed_in.get("/reports/coin-7/").content.decode()
    assert 'name="segment"' in html and 'name="clicked"' in html and 'name="missing"' in html
    plain = StateStore(tmp_path / "plain.db")  # one list, no links, no user IDs
    plain.upsert_pending([("09120000001", "09120000001")])
    plain.claim("09120000001")
    plain.mark_sent("09120000001", message_id=1, status_code=200)
    html = signed_in.get("/reports/plain/").content.decode()
    assert 'name="segment"' not in html and 'name="clicked"' not in html and 'name="missing"' not in html
    assert "بدون شناسه کاربر" not in html  # it never had user IDs: none is missing


def test_operators_download_the_rows_they_filtered_and_the_rejected_ones(operator_client, signed_in, store):
    import csv
    import io

    from sms_sender_web.reports.views import RECIPIENTS_HEADER

    def rows(response) -> list[list[str]]:
        return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))

    html = operator_client.get("/reports/coin-7/", {"status": "sent"}).content.decode()
    assert 'href="/reports/coin-7/recipients.csv?status=sent"' in html and 'data-cli="export-failed"' in html
    got = rows(operator_client.get("/reports/coin-7/recipients.csv", {"status": "sent", "clicked": "yes"}))
    assert got[0] == RECIPIENTS_HEADER and [r[0] for r in got[1:]] == [A, B]
    assert (got[1][2], got[2][2]) == ("delivered", "not checked")
    event = AuditEvent.objects.get(action="report_downloaded", detail__kind="recipients")
    assert event.detail["filters"] == {"status": "sent", "clicked": "yes"}  # what, never whom
    # The CLI's export-failed, column for column.
    failed = rows(operator_client.get("/reports/coin-7/failed.csv"))
    assert failed[0] == list(StateStore.FAILED_HEADER) and failed[1][0] == "INVALID:0912000"
    assert AuditEvent.objects.filter(action="report_downloaded", detail__kind="failed").exists()
    assert signed_in.get("/reports/coin-7/recipients.csv").status_code == 403
    assert signed_in.get("/reports/coin-7/failed.csv").status_code == 403
