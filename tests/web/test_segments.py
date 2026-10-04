"""Step 3.5: segments (spec 4.9) — upload a list, choose its columns, and
see what the engine will make of it. Phone numbers are masked on every
page; the prepared file is in the CLI's own format."""
import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402

from sms_sender import input_loader  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402
from sms_sender_web.privacy import mask_phone  # noqa: E402
from sms_sender_web.segments import files  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402
from sms_sender_web.suppression.models import Suppression  # noqa: E402

pytestmark = pytest.mark.django_db

CSV = (
    "mobile,user_id,first_name\n"
    "09120000001,u1,Ali\n"
    "+98 912 000 0002,u2,Sara\n"
    "۰۹۱۲۰۰۰۰۰۰۳,,Reza\n"       # Persian digits, no user ID
    "09120000001,u1,Ali\n"     # repeated
    "0912000,u4,Short\n"       # not a mobile number
    "09120000005,u5,Mina\n"
    "09120000005,u6,Mina\n"    # two user IDs: never sent
)


@pytest.fixture(autouse=True)
def data_dir(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    return tmp_path


@pytest.fixture
def operator_client(make_user, verified):
    client = Client()
    verified(client, make_user("operator1", "operator"))
    return client


def upload(client, content=CSV, filename="vip.csv", **fields):
    raw = content.encode() if isinstance(content, str) else content
    return client.post("/segments/upload/", {**fields, "file": SimpleUploadedFile(filename, raw)})


# --- Reading files -------------------------------------------------------------


def test_mask_phone():
    assert mask_phone("09120001234") == "0912*****34"
    assert mask_phone("+98 912 000 1234") == "+98 91* *** **34"
    assert mask_phone("12345") == "*****"
    assert mask_phone("0912000") == "*******"  # not a whole number: nothing shown
    assert mask_phone("abc") == "abc"


@pytest.mark.parametrize("encoded", [
    "شماره;نام\n09120000001;علی\n".encode("utf-8-sig"),
    "شماره;نام\n09120000001;علی\n".encode("utf-16"),
    # Excel's plain CSV on Persian Windows: cp1256 has only the Arabic «ي».
    "شماره;نام\n09120000001;علي\n".encode("cp1256"),
])
def test_excel_encodings_and_separators_are_read(encoded):
    table = files.parse(encoded)
    assert table.rows == [["شماره", "نام"], ["09120000001", "علی"]]
    assert table.first_row_is_header()


@pytest.mark.parametrize("data, code", [
    (b"PK\x03\x04rest-of-an-xlsx", "excel"),
    (b"\xd0\xcf\x11\xe0rest-of-an-xls", "excel"),
    ("abc\x00def".encode(), "binary"),
    (b"\n \n,,\n", "empty"),
])
def test_unusable_files_are_refused(data, code):
    with pytest.raises(files.UploadError) as e:
        files.parse(data)
    assert e.value.code == code


def test_ragged_rows_are_padded_and_a_data_first_row_is_not_a_header():
    table = files.parse(b"09120000001,a\n09120000002\n")
    assert table.rows == [["09120000001", "a"], ["09120000002", ""]]
    assert not table.first_row_is_header()
    assert table.column_names(False) == ["column-1", "column-2"]


@pytest.mark.parametrize("mapping, code", [
    (files.Mapping(True, phone=0, user_id=0, tokens=()), "column_twice"),
    (files.Mapping(True, phone=0, user_id=None, tokens=(5,)), "unknown_column"),
    (files.Mapping(True, phone=1, user_id=None, tokens=(0,)), "duplicate_name"),  # "Phone"
    (files.Mapping(True, phone=0, user_id=2, tokens=()), "unnamed_column"),
])
def test_mappings_that_cannot_work_are_refused(mapping, code):
    table = files.parse(b"Phone,mobile,\n1,09120000001,x\n")
    with pytest.raises(files.MappingError) as e:
        files.check_mapping(table, mapping)
    assert e.value.code == code


def test_the_prepared_file_is_what_the_cli_reads(tmp_path):
    table = files.parse(CSV.encode())
    dest = tmp_path / "vip.csv"
    header = files.write_prepared(table, files.Mapping(True, phone=0, user_id=1, tokens=(2,)), dest)
    assert header == ["phone", "user_id", "first_name"]
    assert dest.read_text(encoding="utf-8").splitlines()[:2] == ["phone,user_id,first_name", "09120000001,u1,Ali"]
    loaded = input_loader.load(dest, input_loader.TokenColumns({"token10": "first_name"}), "user_id")
    assert [(r.phone, r.user_id, r.tokens["token10"]) for r in loaded.valid] == [
        ("09120000001", "u1", "Ali"), ("09120000002", "u2", "Sara"), ("09120000003", None, "Reza"),
    ]
    assert loaded.conflicts == {"09120000005"}


def test_the_summary_counts_like_the_engine(tmp_path):
    table = files.parse(CSV.encode())
    dest = tmp_path / "vip.csv"
    files.write_prepared(table, files.Mapping(True, phone=0, user_id=1, tokens=(2,)), dest)
    summary = files.summarize(dest, "user_id", frozenset({"09120000002", "09120009999"}))
    assert {k: v for k, v in summary.items() if k != "invalid_sample"} == {
        "rows": 7, "valid": 3, "invalid": 3, "duplicates": 1, "conflicts": 1,
        "missing_user_id": 1, "suppressed": 1,
    }
    assert summary["invalid_sample"] == [
        {"value": "*******", "reason": "invalid_phone"},
        {"value": "0912*****05", "reason": "conflicting_user_ids"},
        {"value": "0912*****05", "reason": "conflicting_user_ids"},
    ]


# --- Uploading and choosing columns -------------------------------------------------


def test_an_operator_uploads_a_segment_and_chooses_its_columns(operator_client, data_dir):
    response = upload(operator_client, filename="VIP list.csv", name="فهرست ویژه", slug="vip-1")
    assert response.status_code == 302 and response["Location"] == "/segments/vip-1/columns/"
    segment = Segment.objects.get(slug="vip-1")
    assert segment.status == Segment.Status.DRAFT and segment.has_header
    assert (segment.name, segment.original_name) == ("فهرست ویژه", "VIP list.csv")
    assert segment.upload_path.exists()

    html = operator_client.get("/segments/vip-1/columns/").content.decode()
    assert "انتخاب ستون‌ها" in html
    assert "09120000001" not in html and "0912*****01" not in html  # masked, Persian digits
    assert "۰۹۱۲*****۰۱" in html
    # The phone and user ID columns are preselected.
    assert 'name="phone" value="0" checked' in html
    assert 'name="user_id" value="1" checked' in html

    response = operator_client.post("/segments/vip-1/columns/", {
        "has_header": "on", "phone": "0", "user_id": "1", "tokens": ["2"],
    })
    assert response["Location"] == "/segments/vip-1/"
    segment.refresh_from_db()
    assert segment.status == Segment.Status.READY
    assert segment.columns == ["phone", "user_id", "first_name"]
    assert (segment.user_id_column, segment.token_columns) == ("user_id", ["first_name"])
    assert segment.summary["valid"] == 3
    assert segment.path == data_dir / "segments" / "vip-1.csv" and segment.path.exists()
    assert not segment.upload_path.exists()  # only the prepared copy stays
    assert [e.action for e in AuditEvent.objects.filter(action__startswith="segment_").order_by("id")] == [
        "segment_uploaded", "segment_mapped",
    ]


def test_the_segment_page_is_persian_and_masks_numbers(operator_client, make_user):
    upload(operator_client, slug="vip")
    operator_client.post("/segments/vip/columns/", {"has_header": "on", "phone": "0", "user_id": "1"})
    viewer = Client()
    viewer.force_login(make_user("viewer9", "viewer"))
    html = viewer.get("/segments/vip/").content.decode()
    assert "شماره‌های معتبر" in html and "شناسه کاربر متناقض" in html
    assert '<dd class="num">۳</dd>' in html
    assert "۰۹۱۲*****۰۵" in html
    assert "09120000005" not in html and "0912*****05" not in html
    assert "حذف گروه مخاطبان" not in html  # viewers can't delete


def test_suppressed_numbers_are_counted_at_upload(operator_client):
    Suppression.objects.create(phone="09120000003")
    upload(operator_client, slug="vip")
    operator_client.post("/segments/vip/columns/", {"has_header": "on", "phone": "0"})
    assert Segment.objects.get(slug="vip").summary["suppressed"] == 1


def test_without_a_user_id_column_nothing_is_a_conflict(operator_client):
    upload(operator_client, slug="vip")
    operator_client.post("/segments/vip/columns/", {"has_header": "on", "phone": "0", "user_id": ""})
    summary = Segment.objects.get(slug="vip").summary
    assert (summary["valid"], summary["conflicts"], summary["duplicates"]) == (4, 0, 2)


def test_a_bad_mapping_is_explained_in_persian(operator_client):
    upload(operator_client, slug="vip")
    html = operator_client.post("/segments/vip/columns/", {
        "has_header": "on", "phone": "0", "user_id": "0",
    }).content.decode()
    assert "هر ستون فقط برای یک کاربرد انتخاب می‌شود" in html
    html = operator_client.post("/segments/vip/columns/", {"has_header": "on"}).content.decode()
    assert "ستون شماره‌های موبایل را انتخاب کنید" in html
    assert Segment.objects.get(slug="vip").status == Segment.Status.DRAFT


@pytest.mark.parametrize("fields, message", [
    ({"slug": "VIP List"}, "فقط حروف کوچک انگلیسی"),
    ({"slug": "upload"}, "فقط حروف کوچک انگلیسی"),
])
def test_short_names_follow_the_cli_rules(operator_client, fields, message):
    html = upload(operator_client, **fields).content.decode()
    assert message in html
    assert not Segment.objects.exists()


def test_the_short_name_comes_from_the_file_and_must_be_new(operator_client):
    assert upload(operator_client, filename="Gold Users.csv")["Location"] == "/segments/gold-users/columns/"
    html = upload(operator_client, filename="gold users.txt").content.decode()
    assert "گروه مخاطبانی با این نام کوتاه وجود دارد" in html


def test_excel_files_are_refused_with_a_hint(operator_client):
    html = upload(operator_client, content=b"PK\x03\x04xlsx", filename="list.csv").content.decode()
    assert "این یک فایل اکسل است" in html
    html = upload(operator_client, content=b"x", filename="list.xlsx").content.decode()
    assert "یک فایل CSV یا TXT بارگذاری کنید" in html


def test_viewers_see_segments_but_cannot_change_them(signed_in, operator_client):
    upload(operator_client, slug="vip")
    assert signed_in.get("/segments/").status_code == 200
    assert "بارگذاری گروه مخاطبان" not in signed_in.get("/segments/").content.decode()
    assert upload(signed_in, slug="other").status_code == 403
    assert signed_in.post("/segments/vip/columns/", {"phone": "0"}).status_code == 403
    assert signed_in.post("/segments/vip/delete/").status_code == 403


def test_deleting_a_segment_removes_its_file(operator_client):
    upload(operator_client, slug="vip")
    operator_client.post("/segments/vip/columns/", {"has_header": "on", "phone": "0"})
    path = Segment.objects.get(slug="vip").path
    response = operator_client.post("/segments/vip/delete/", follow=True)
    assert "گروه مخاطبان و فایل آن حذف شد" in response.content.decode()
    assert not Segment.objects.exists() and not path.exists()
    assert AuditEvent.objects.filter(action="segment_deleted", detail__segment="vip").exists()


def test_a_draft_can_be_deleted_with_its_upload(operator_client):
    upload(operator_client, slug="vip")
    upload_path = Segment.objects.get(slug="vip").upload_path
    operator_client.post("/segments/vip/delete/")
    assert not upload_path.exists() and not Segment.objects.exists()


def test_a_segment_a_campaign_uses_is_kept(operator_client):
    upload(operator_client, slug="vip")
    operator_client.post("/segments/vip/columns/", {"has_header": "on", "phone": "0"})
    Campaign.objects.create(slug="coin-7", name="Coin 7", settings={"segment": "vip"})
    response = operator_client.post("/segments/vip/delete/", follow=True)
    assert "یک کمپین از این گروه مخاطبان استفاده می‌کند" in response.content.decode()
    assert Segment.objects.filter(slug="vip").exists()
