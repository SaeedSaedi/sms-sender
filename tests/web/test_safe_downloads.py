"""Plan 07, Q2: what people download opens safely in a spreadsheet. A cell
from someone's file that starts like a formula stays text, while the
segment's copy on disk, which the engine reads, stays exactly as it was."""
import csv

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

pytestmark = pytest.mark.django_db

EVIL = '=HYPERLINK("http://evil.invalid","x")'


@pytest.fixture
def operator(make_user, verified, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    (tmp_path / "segments").mkdir()
    client = Client()
    verified(client, make_user("operator1", "operator"))
    return client


def test_a_report_download_keeps_formulas_as_text(operator, tmp_path):
    store = StateStore(tmp_path / "db" / "coin-7.db")
    store.upsert_pending([("09120000001", "09120000001")], segment="vip")
    store.assign_user_ids({"09120000001": EVIL})
    store.record_invalid("@SUM(1+1)", "not a phone number")
    body = operator.get("/reports/coin-7/recipients.csv").content.decode("utf-8-sig")
    rows = list(csv.reader(body.splitlines()))
    cells = {cell for row in rows[1:] for cell in row}
    assert "'" + EVIL in cells and "'@SUM(1+1)" in cells
    assert "09120000001" in cells  # numbers stay numbers


def test_a_segment_download_is_safe_and_its_file_unchanged(operator):
    segment = Segment.objects.create(slug="vip", name="VIP", status=Segment.Status.READY,
                                     columns=["phone", "first_name"], token_columns=["first_name"])
    original = f'phone,first_name\n09120000001,"{EVIL.replace(chr(34), chr(34) * 2)}"\n'
    segment.path.write_text(original, encoding="utf-8")
    body = operator.get("/segments/vip/download/").content.decode("utf-8-sig")
    rows = list(csv.reader(body.splitlines()))
    assert rows[1] == ["09120000001", "'" + EVIL]
    assert segment.path.read_text(encoding="utf-8") == original  # the engine's input, untouched
