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

from sms_sender.state import FAILED_PERMANENT, SENT, UNKNOWN, StateStore  # noqa: E402
from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.sandbox import SandboxKavenegar, SandboxShlink  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

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
        "link_strategy": "recipient", "link_expiry_days": "7", "send_window": "00:00-23:59", "workers": "2",
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
    # One link per recipient, and one of its own for the test SMS.
    assert test.result["cost_per_sms"] == 3020 and test.result["links_ready"] == len(ROWS) + 1
    operator_client.post("/campaigns/try-1/approve/", {"job": test.pk})
    operator_client.post("/campaigns/try-1/send/")
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.DONE, send.last_error
    store = StateStore(campaign_db(campaign))
    assert store.counts() == {SENT: 2, FAILED_PERMANENT: 1, UNKNOWN: 1}
    assert store.total_cost() == 2 * 3020

    operator_client.post("/campaigns/try-1/delivery/")
    run(Job.Kind.DELIVERY)
    assert sum(store.delivery_counts().values()) == 2 and None not in store.delivery_counts()
    operator_client.post("/campaigns/try-1/clicks/")
    run(Job.Kind.CLICKS)
    assert store.last_click_sync() is not None


def test_every_page_says_it_is_the_sandbox(operator_client, campaign):
    for page in ("/", "/campaigns/try-1/", "/status/"):
        html = operator_client.get(page).content.decode()
        assert "محیط شبیه‌سازی: پاسخ کاوه‌نگار و سرویس لینک کوتاه ساختگی است" in html
    assert "۱٬۰۰۰٬۰۰۰٬۰۰۰ ریال" in operator_client.get("/status/").content.decode()


def test_outside_the_sandbox_there_is_no_banner(settings, signed_in):
    settings.SANDBOX = False
    assert "محیط شبیه‌سازی" not in signed_in.get("/").content.decode()


def test_sandbox_data_lives_apart_from_real_campaigns(tmp_path):
    """Read in a fresh process: settings are decided at import."""
    code = "from sms_sender_web import settings; print(settings.SANDBOX, settings.DATA_DIR)"
    env = {**os.environ, "DJANGO_SECRET_KEY": "x", "SMS_SENDER_DATA_DIR": str(tmp_path)}
    for flag, expected in (("1", tmp_path / "sandbox"), ("", tmp_path)):
        out = subprocess.run([sys.executable, "-c", code], env={**env, "SMS_SENDER_SANDBOX": flag},
                             capture_output=True, text=True, check=True).stdout.split()
        assert out == [str(bool(flag)), str(expected.resolve())]
