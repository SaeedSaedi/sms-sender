"""Plan 05, P3: a segment made from a report (ready-made audiences, or any
filter), and conversions imported by CSV, matched to the recipients who
were sent the SMS."""
from __future__ import annotations

import csv
import io

import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender.clicks import sync_clicks  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from ..test_clicks import A, B, C, Visits  # noqa: E402
from ..test_clicks import campaign_db as clicks_campaign  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def store(settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    settings.DATA_DIR = tmp_path
    (tmp_path / "db").mkdir()
    store = clicks_campaign(tmp_path / "db" / "coin-7.db")  # A, B (vip-2), C (new-users) sent
    sync_clicks(store, Visits({"c1": 2, "c2": 1, "c3": 0}), "coin-7")  # A and B clicked
    return store


@pytest.fixture
def operator_client(make_user, verified):
    client = Client()
    verified(client, make_user("operator1", "operator"))
    return client


def rows(path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_ready_made_audiences_show_who_is_in_them(operator_client, store):
    html = operator_client.get("/reports/coin-7/").content.decode()
    nav = html.split('class="audiences"')[1].split("</nav>")[0]
    assert "کلیک‌کرده" in nav and "(۲)" in nav and "کلیک نکرده" in nav and "(۱)" in nav
    assert "ردشده" not in nav  # nobody was rejected: no empty audience on offer
    assert 'href="?status=sent&amp;clicked=yes#recipients"' in nav


def test_a_segment_from_the_clickers_brings_their_token_column_back(operator_client, store, tmp_path):
    source = Segment.objects.create(slug="vip-2", name="VIP 2", status=Segment.Status.READY,
                                    columns=["phone", "user_id", "first_name"], user_id_column="user_id",
                                    token_columns=["first_name"])
    source.path.parent.mkdir(parents=True, exist_ok=True)
    source.path.write_text(f"phone,user_id,first_name\n+98{A[1:]},u-1,Ali\n{B},,Sara\n", encoding="utf-8")
    response = operator_client.post("/reports/coin-7/audience/", {
        "status": "sent", "clicked": "yes", "name": "کلیک‌کرده‌ها", "segment_slug": "coin-7-clicked",
    })
    assert response.status_code == 302 and response["Location"] == "/segments/coin-7-clicked/"
    made = Segment.objects.get(slug="coin-7-clicked")
    assert made.status == Segment.Status.READY and made.token_columns == ["first_name"]
    assert rows(made.path) == [["phone", "user_id", "first_name"], [A, "u-1", "Ali"], [B, "", "Sara"]]
    assert made.summary["valid"] == 2 and made.summary["missing_user_id"] == 1
    event = AuditEvent.objects.get(action="audience_created")
    assert event.detail["filters"] == {"status": "sent", "clicked": "yes"} and event.detail["count"] == 2


def test_without_every_source_file_only_numbers_and_ids_come_back(operator_client, store):
    operator_client.post("/reports/coin-7/audience/", {"status": "sent", "name": "x", "segment_slug": "all-sent"})
    made = Segment.objects.get(slug="all-sent")
    assert made.token_columns == [] and rows(made.path)[0] == ["phone", "user_id"]
    assert [r[0] for r in rows(made.path)[1:]] == [A, B, C]


def test_a_segment_needs_a_free_short_name_and_an_operator(operator_client, store, signed_in):
    Segment.objects.create(slug="taken", name="t")
    operator_client.post("/reports/coin-7/audience/", {"status": "sent", "name": "x", "segment_slug": "taken"})
    operator_client.post("/reports/coin-7/audience/", {"status": "sent", "name": "x", "segment_slug": "Bad Slug"})
    assert Segment.objects.count() == 1
    assert signed_in.post("/reports/coin-7/audience/", {"name": "x", "segment_slug": "ok-1"}).status_code == 403


def upload(text: str, name: str = "sales.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def test_conversions_are_imported_and_counted(operator_client, store):
    ref = store._conn().execute(
        "SELECT l.ref FROM recipients r JOIN links l ON l.key = r.link_key WHERE r.phone = ?", (A,)).fetchone()[0]
    response = operator_client.post("/reports/coin-7/conversions/", {
        "file": upload(f"r,user_id,value\n{ref},,120000\n,u-3,5000\nnope,,1\n"),
    }, follow=True)
    html = response.content.decode()
    assert "۲ تبدیل افزوده شد (۱ با r و ۱ با شناسه کاربر)" in html and "بدون تطبیق" in html
    conversions = html.split('id="conversions"')[1].split("</section>")[0]
    assert "۱۲۵,۰۰۰" in conversions or "۱۲۵٬۰۰۰" in conversions
    assert "(۶۷٪)" in conversions  # two of the three accepted
    assert "تبدیل‌شده" in html.split('id="funnel"')[1].split("</section>")[0]
    assert AuditEvent.objects.get(action="conversions_imported").detail["by_ref"] == 1


def test_a_conversion_file_needs_r_or_user_id(operator_client, store, signed_in):
    html = operator_client.post("/reports/coin-7/conversions/", {"file": upload("phone,value\n0912,1\n")},
                                follow=True).content.decode()
    assert "فایل باید ستونی به نام r" in html
    assert store.conversion_totals() == (0, 0, 0.0)
    assert signed_in.post("/reports/coin-7/conversions/", {"file": upload("r\nx\n")}).status_code == 403
