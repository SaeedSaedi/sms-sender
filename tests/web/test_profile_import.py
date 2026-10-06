"""The CLI's profiles as draft campaigns (G4 of the 2026-10-05 review): an
admin uploads sms-sender.toml and each profile, merged over
[profile.default] as the CLI merges it, becomes a draft campaign with what
it sends and how. Paths stay with the CLI; nothing is sent."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns import lifecycle as lc  # noqa: E402
from sms_sender_web.campaigns.forms import slug_taken  # noqa: E402
from sms_sender_web.campaigns.profiles import ProfileFileError, draft, drafts, read  # noqa: E402
from sms_sender_web.jobs.engine import campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402

from .test_rounds import signed_in  # noqa: E402
from .world import build_world  # noqa: E402

pytestmark = pytest.mark.django_db

PROFILES = """\
[profile.default]
workers = 5
max_attempts = 5
timeout = 15.0
backoff_max = 30.0
log_file = "./logs/sms-sender.log"

[profile.coin-price-7]
template = "coin-price"
token = "نفت"
input = "data/segments/vip.csv"
state = "data/db/coin-price-7.db"

[profile.transaction-1-seg2]
template = "transaction"
token = "x"
token_column = ["token10=first_name"]
value_map = ["first_name:Ali=علي"]
input = "/Users/someone/elsewhere/new-list.csv"
smoke_test = true
workers = 8

[profile.broken]
template = "has space"
token = "a_b"
"""


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def one(values: dict, **kw):
    return draft("p", values, used=set(), names=set(), **kw)


# ---------- reading the file ----------

def test_profiles_are_merged_over_the_default_as_the_cli_merges_them():
    profiles = read(PROFILES.encode())
    assert list(profiles) == ["coin-price-7", "transaction-1-seg2", "broken"]
    assert profiles["coin-price-7"]["workers"] == 5 and profiles["transaction-1-seg2"]["workers"] == 8
    assert list(read(b"[profile.default]\ntemplate = 'x'\n")) == ["default"]  # alone, it's the one


@pytest.mark.parametrize("raw, code", [
    (b"x" * (256 * 1024 + 1), "too_big"),
    ("[profile.a]\ntemplate = 'x'\n".encode("utf-16"), "not_utf8"),
    (b"[profile.a\n", "not_toml"),
    (b"template = 'x'\n", "no_profiles"),
])
def test_a_file_that_isnt_profiles_is_refused(raw, code):
    with pytest.raises(ProfileFileError) as e:
        read(raw)
    assert e.value.code == code


# ---------- each profile ----------

def test_a_profile_brings_what_it_sends_and_how(world):
    found = {d.profile: d for d in drafts(read(PROFILES.encode()))}
    coin = found["coin-price-7"]
    assert coin.importable and coin.slug == "coin-price-7"
    assert coin.settings["template"] == "coin-price" and coin.settings["tokens"] == {"token": "نفت"}
    assert (coin.settings["workers"], coin.settings["max_attempts"], coin.settings["timeout"]) == (5, 5, 15.0)
    # Its input names a ready segment: the campaign gets it.
    assert coin.segment.slug == "vip" and coin.settings["user_id_column"] == "user_id"
    assert coin.left_out == ["log_file", "state"]

    trade = found["transaction-1-seg2"]
    assert trade.settings["token_columns"] == {"token10": "first_name"}
    assert trade.settings["value_maps"] == {"first_name": {"Ali": "علی"}}  # Persian «ی»
    assert trade.segment is None and "segment" not in trade.settings  # chosen later
    assert trade.left_out == ["log_file", "smoke_test"]


def test_a_profile_that_would_send_something_else_isnt_imported(world):
    broken = {d.profile: d for d in drafts(read(PROFILES.encode()))}["broken"]
    assert broken.problems == ["bad_template", "bad_token"]
    assert one({"token": "x"}).problems == ["no_template"]
    assert one({"template": "t", "token": "x", "token_column": ["token=name"]}).problems == ["token_twice"]
    assert one({"template": "t", "token_column": "token10 first_name"}).problems == ["bad_token_column"]
    assert one({"template": "t", "token_column": ["token10=a"], "value_map": ["b:x=y"]}).problems == ["bad_value_map"]
    assert one({"template": "t", "link_url": "https://evil.example/", "link_token": "token20"}).problems == ["bad_link"]
    assert one(None).problems == ["not_a_table"]


def test_a_short_link_comes_with_its_tracking_values(world):
    d = one({"template": "t", "token": "x", "link_url": "https://kifpool.me/offer", "link_token": "token20",
             "link_format": "code", "utm_campaign": "oil-week"})
    assert d.importable
    assert d.settings["links"] == {
        "destination": "https://kifpool.me/offer", "token": "token20", "format": "code", "strategy": "recipient",
        "expiry_days": 7, "utm_source": "sms", "utm_medium": "sms", "utm_campaign": "oil-week", "utm_content": None,
    }


def test_run_settings_the_dashboard_cant_take_are_set_back(world):
    d = one({"template": "t", "send_window": "off", "rate": "fast", "workers": 50, "max_attempts": 99,
             "timeout": 30, "link_rate": "5/s"})
    assert d.importable
    assert d.reset == ["send_window", "rate", "workers", "max_attempts"]
    assert (d.settings["send_window"], d.settings["rate"], d.settings["workers"]) == ("08:00-21:00", None, 5)
    assert "max_attempts" not in d.settings and d.settings["timeout"] == 30.0 and d.settings["link_rate"] == "5/s"
    assert one({"template": "t", "send_window": "09:00-20:00", "rate": "10/s"}).settings["send_window"] == "09:00-20:00"


def test_short_names_never_clash(world):
    Campaign.objects.create(slug="coin-price-7", name="already here")
    used: set[str] = set()
    first = draft("coin-price-7", {"template": "t"}, used=used, names=set())
    second = draft("coin price 7", {"template": "t"}, used=used, names=set())
    assert (first.slug, second.slug) == ("coin-price-8", "coin-price-9")
    assert draft("x", {"template": "t", "campaign": "vip-run"}, used=used, names=set()).slug == "vip-run"
    assert slug_taken("import")  # the page's own address


# ---------- the page ----------

def upload(client, text: str = PROFILES):
    return client.post("/campaigns/import/", {"file": SimpleUploadedFile("sms-sender.toml", text.encode())})


def test_only_an_admin_imports(world, verified):
    operator = signed_in(world.users["operator"], verified)
    assert operator.get("/campaigns/import/").status_code == 403
    assert "/campaigns/import/" not in operator.get("/campaigns/").content.decode()
    admin = signed_in(world.users["admin"], verified)
    assert 'href="/campaigns/import/"' in admin.get("/campaigns/").content.decode()
    assert 'name="file"' in admin.get("/campaigns/import/").content.decode()


def test_the_file_is_shown_as_campaigns_before_anything_is_made(world, verified):
    Campaign.objects.create(slug="cp", name="transaction-1-seg2")
    client = signed_in(world.users["admin"], verified)
    html = upload(client).content.decode()
    assert 'value="coin-price-7" aria-label="ورود: coin-price-7" checked' in html
    assert 'value="transaction-1-seg2" aria-label="ورود: transaction-1-seg2">' in html  # its name is taken
    assert 'value="broken" aria-label="ورود: broken" disabled' in html
    assert "کاوه‌نگار نام قالب آن را نمی‌پذیرد" in html
    assert not Campaign.objects.filter(slug="coin-price-7").exists()


def test_the_ticked_profiles_become_draft_campaigns(world, verified):
    client = signed_in(world.users["admin"], verified)
    upload(client)
    response = client.post("/campaigns/import/", {"step": "import", "profile": ["coin-price-7", "transaction-1-seg2", "broken"]})
    assert response.status_code == 302 and response["Location"] == "/campaigns/"
    coin = Campaign.objects.get(slug="coin-price-7")
    assert coin.created_by == world.users["admin"] and coin.settings["segment"] == "vip"
    assert lc.lifecycle(coin).stage == lc.READY  # its segment came along: a check and a test SMS next
    trade = Campaign.objects.get(slug="transaction-1-seg2")
    assert lc.lifecycle(trade).stage == lc.DRAFT  # its segment is chosen on the settings page
    assert not Campaign.objects.filter(slug="broken").exists()
    events = AuditEvent.objects.filter(action="campaign_imported").order_by("id")
    assert [(e.campaign, e.detail) for e in events] == [
        ("coin-price-7", {"profile": "coin-price-7"}), ("transaction-1-seg2", {"profile": "transaction-1-seg2"}),
    ]
    # Nothing was sent, and no campaign DB was made.
    assert not Job.objects.filter(campaign__in=[coin, trade]).exists()
    assert not campaign_db(coin).exists()
    # The file is gone from the session: importing again needs it again.
    again = client.post("/campaigns/import/", {"step": "import", "profile": ["broken"]})
    assert again["Location"] == "/campaigns/import/"


def test_nothing_ticked_shows_the_profiles_again(world, verified):
    client = signed_in(world.users["admin"], verified)
    upload(client)
    html = client.post("/campaigns/import/", {"step": "import"}).content.decode()
    assert "دست‌کم یک پروفایل" in html and 'name="profile"' in html
    assert not Campaign.objects.filter(slug="coin-price-7").exists()


def test_a_file_that_isnt_profiles_says_why(world, verified):
    client = signed_in(world.users["admin"], verified)
    assert "TOML معتبری نیست" in upload(client, "[profile.a\n").content.decode()
    assert "ابتدا فایل پروفایل‌ها" in client.post("/campaigns/import/", {}).content.decode()


def test_a_profile_whose_cli_campaign_has_records_points_there(world, verified):
    """Importing makes a new campaign; the CLI's own campaign, with its
    records, comes over from the campaign list instead."""
    campaign_db(Campaign(slug="coin-price-7")).write_bytes(b"")  # the CLI's DB of that name
    d = {x.profile: x for x in drafts(read(PROFILES.encode()))}["coin-price-7"]
    assert (d.slug, d.adoptable) == ("coin-price-8", "coin-price-7")
    html = upload(signed_in(world.users["admin"], verified)).content.decode()
    assert 'value="coin-price-7" aria-label="ورود: coin-price-7">' in html  # not ticked
    assert "از فهرست کمپین‌ها به داشبورد بیاورید" in html
