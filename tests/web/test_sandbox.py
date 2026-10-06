"""Step 3.7: sandbox mode (spec 4.9). Kavenegar and Shlink are simulated,
so the whole flow can be tried from the browser without sending anything.
These tests use the real engine and worker: there's no Kavenegar key in
the test environment, so a real client would fail at once."""
import os
import subprocess
import sys

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.reconcile import reconcile_unknown  # noqa: E402
from sms_sender.sendcheck import check_sends  # noqa: E402
from sms_sender.state import FAILED_PERMANENT, SENT, UNKNOWN, StateStore  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.reporter import JobReporter  # noqa: E402
from sms_sender_web.jobs.sandbox import SandboxKavenegar, SandboxShlink, read_outbox  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from .world import open_window  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

ROWS = ["09120000001", "09120000002", "09120001000", "09120001999"]  # …000 rejected, …999 unknown


@pytest.fixture(autouse=True)
def sandbox(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


@pytest.fixture
def operator_client(make_user, verified):
    user = make_user("operator1", "operator")
    Profile.objects.create(user=user, test_phone="09120000099")
    client = Client()
    verified(client, user)
    return client


@pytest.fixture
def campaign(operator_client, tmp_path):
    (tmp_path / "segments").mkdir()
    (tmp_path / "segments" / "vip.csv").write_text(
        "phone,first_name\n" + "".join(f"{p},Ali\n" for p in ROWS), encoding="utf-8",
    )
    Segment.objects.create(slug="vip", name="VIP", status=Segment.Status.READY,
                           columns=["phone", "first_name"], token_columns=["first_name"])
    operator_client.post("/campaigns/new/", {
        "name": "Sandbox", "slug": "try-1", "segment": "vip", "template": "coin-price",
    })
    operator_client.post("/campaigns/try-1/settings/", {
        "segment": "vip", "template": "coin-price", "token_source": "column", "token_column": "first_name",
        "token2_source": "link", "link_destination": "https://kifpool.me/wallet", "link_format": "url",
        "link_strategy": "recipient", "link_expiry_days": "7", "send_window": open_window(), "workers": "2",
    })
    return Campaign.objects.get(slug="try-1")


def run(kind):
    worker = Worker(worker_id="w1", heartbeat_sec=0.05)
    job = worker.run_once()
    assert job is not None and job.kind == kind
    job.refresh_from_db()
    return job


def test_the_engine_never_builds_a_real_client(campaign):
    engine = Engine()
    assert isinstance(engine.sender(), SandboxKavenegar)
    assert isinstance(engine.link_client(), SandboxShlink)
    runner = engine.runner(campaign, reporter=None)
    assert isinstance(runner.sender, SandboxKavenegar)


def test_the_whole_flow_runs_without_sending(operator_client, campaign):
    operator_client.post("/campaigns/try-1/test/")
    test = run(Job.Kind.TEST)
    assert test.state == Job.State.DONE, test.last_error
    # Only the test SMS's own link: the recipients' are made when sending
    # starts (plan 05, decision 4), so the test SMS doesn't wait for them.
    assert test.result["cost_per_sms"] == 3020 and test.result["links_ready"] == 1
    operator_client.post("/campaigns/try-1/approve/", {"job": test.pk})
    operator_client.post("/campaigns/try-1/send/")
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.DONE, send.last_error
    assert send.result["links_ready"] == len(ROWS)  # one per recipient, before any SMS
    store = StateStore(campaign_db(campaign))
    assert store.counts() == {SENT: 2, FAILED_PERMANENT: 1, UNKNOWN: 1}
    assert store.total_cost() == 2 * 3020
    # The outbox: the test SMS, and each accepted number once — the lost
    # reply (…999) included, since Kavenegar did accept it.
    sent_to = [sms["phone"] for sms in read_outbox()]
    assert sorted(sent_to) == sorted(["09120000099", "09120000001", "09120000002", "09120001999"])
    sms = next(s for s in read_outbox() if s["phone"] == "09120000001")
    assert sms["template"] == "coin-price"
    assert sms["tokens"]["token"] == "Ali" and sms["tokens"]["token2"].startswith("https://sandbox.invalid/u/")

    # Reconciliation finds the lost reply's message and settles the row:
    # sent, and nothing sent again.
    result = reconcile_unknown(store, SandboxKavenegar(), min_age_sec=0)
    assert result.sent == 1 and store.counts() == {SENT: 3, FAILED_PERMANENT: 1}
    assert len(read_outbox()) == 4
    # The simulated calls are recorded like real ones, the test SMS apart.
    check = check_sends(store)
    assert check.ok and (check.sms, check.test_sms, check.unrecorded) == (3, 1, 0)
    outcomes = {phone: [a["outcome"] for a in store.attempts_for(phone)] for phone in ROWS}
    assert outcomes["09120001000"] == ["rejected"]
    assert outcomes["09120001999"] == ["unknown", "reconciled_sent"]

    operator_client.post("/campaigns/try-1/delivery/")
    run(Job.Kind.DELIVERY)
    assert sum(store.delivery_counts().values()) == 3 and None not in store.delivery_counts()
    operator_client.post("/campaigns/try-1/clicks/")
    run(Job.Kind.CLICKS)
    assert store.last_click_sync() is not None


def test_the_status_page_shows_what_would_have_been_sent(operator_client, campaign):
    SandboxKavenegar(delay=0).send("09120000001", {"token": "نفت"})
    html = operator_client.get("/status/").content.decode()
    assert "پیامک‌های شبیه‌سازی‌شده" in html and "۰۹۱۲*****۰۱" in html and "نفت" in html
    assert "09120000001" not in html


def test_the_link_stage_shows_progress_and_time_left(operator_client, campaign):
    job = Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, state=Job.State.RUNNING,
                             params={"test_number": "09120000099"})
    reporter = JobReporter(job, every=0)
    reporter.links(0, 2000)
    reporter._links_started -= 60  # a minute in, 500 made: 3 more minutes at this pace
    reporter.links(500, 2000)
    job.refresh_from_db()
    assert job.progress["stage"] == "links" and job.progress["processed"] == 500
    assert job.progress["eta_sec"] == 180
    html = operator_client.get("/campaigns/try-1/live/").content.decode()
    assert "ساخت لینک‌های کوتاه: ۵۰۰ از ۲٬۰۰۰ · حدود ۳ دقیقه مانده" in html


def test_every_page_says_it_is_the_sandbox(operator_client, campaign):
    for page in ("/", "/campaigns/try-1/", "/status/"):
        html = operator_client.get(page).content.decode()
        assert "محیط شبیه‌سازی: پاسخ کاوه‌نگار و سرویس لینک کوتاه ساختگی است" in html
    assert '<span class="amount">۱٬۰۰۰٬۰۰۰٬۰۰۰</span> <span class="unit">ریال</span>' in operator_client.get("/status/").content.decode()


def test_outside_the_sandbox_there_is_no_banner(settings, signed_in):
    settings.SANDBOX = False
    assert "محیط شبیه‌سازی" not in signed_in.get("/").content.decode()


def test_sandbox_data_lives_apart_from_real_campaigns(tmp_path):
    """Read in a fresh process: settings are decided at import. The first
    start opens its app DB in a folder that doesn't exist yet."""
    code = (
        "import django; django.setup(); from django.conf import settings; "
        "from django.db import connection; connection.ensure_connection(); "
        "print(settings.SANDBOX, settings.DATA_DIR)"
    )
    env = {**os.environ, "DJANGO_SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "sms_sender_web.settings"}
    for flag, expected in (("1", tmp_path / "a" / "sandbox"), ("", tmp_path / "b")):
        data = tmp_path / ("a" if flag else "b")  # neither exists yet
        out = subprocess.run([sys.executable, "-c", code],
                             env={**env, "SMS_SENDER_SANDBOX": flag, "SMS_SENDER_DATA_DIR": str(data)},
                             capture_output=True, text=True, check=True).stdout.split()
        assert out == [str(bool(flag)), str(expected.resolve())]
        assert (expected / "app.db").exists() and (expected / "db").is_dir()
