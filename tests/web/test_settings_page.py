"""Plan 05, P2: the redesigned settings page. Its split controls (the
window's two times, the rate's number and unit, the link format's choice,
the translation rows) save the CLI's settings; tracking values reach the
link; the advanced settings are an admin's alone."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender_web.jobs.engine import Engine  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402

from .world import build_world  # noqa: E402

pytestmark = pytest.mark.django_db

FORM = {
    "segment": "vip", "template": "coin-price",
    "token_source": "value", "token_value": "نفت",
    "token10_source": "column", "token10_column": "first_name",
    "token20_source": "link", "link_destination": "https://kifpool.me/wallet",
    "link_format_kind": "pattern", "link_pattern": "u/{code}", "link_strategy": "recipient",
    "link_expiry_days": "7",
    "vm_column": ["first_name", ""], "vm_source": ["Ali", ""], "vm_target": ["علي", ""],
    "window_start": "09:30", "window_end": "۲۰:۰۰",
    "rate_value": "۱۲", "rate_unit": "m", "workers": "3",
    "utm_source": "sms", "utm_medium": "", "utm_campaign": "oil-week", "utm_content": "",
}


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def signed_in(user, verified) -> Client:
    client = Client()
    verified(client, user)
    return client


def saved(world) -> dict:
    return Campaign.objects.get(slug="fresh").settings


def test_the_split_controls_save_the_clis_settings(world, verified):
    client = signed_in(world.users["operator"], verified)
    response = client.post("/campaigns/fresh/settings/", FORM)
    assert response.status_code == 302, response.content.decode()[-3000:]
    s = saved(world)
    assert s["send_window"] == "09:30-20:00"
    assert s["rate"] == "12/m"  # Persian digits typed, stored as the CLI's
    assert s["value_maps"] == {"first_name": {"Ali": "علی"}}  # the blank row is ignored
    assert s["links"]["format"] == "u/{code}" and s["links"]["token"] == "token20"
    assert s["links"]["utm_campaign"] == "oil-week"
    assert s["links"]["utm_medium"] == "sms" and s["links"]["utm_content"] is None  # empty: defaults


def test_the_page_shows_the_saved_settings_in_its_controls(world, verified):
    client = signed_in(world.users["operator"], verified)
    client.post("/campaigns/fresh/settings/", FORM)
    html = client.get("/campaigns/fresh/settings/").content.decode()
    assert 'name="window_start" type="text" dir="ltr" inputmode="numeric" maxlength="5" placeholder="08:00" value="09:30"' in html
    assert 'name="rate_value" type="number" min="1" inputmode="numeric" value="12"' in html
    assert '<option value="m" selected>' in html
    assert 'name="link_format_kind" value="pattern" checked' in html and 'value="u/{code}"' in html
    assert 'name="vm_source" type="text" dir="auto" value="Ali"' in html


def test_a_wrong_window_is_explained_next_to_its_times(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.post("/campaigns/fresh/settings/", {**FORM, "window_end": ""}).content.decode()
    assert 'id="id_send_window_error"' in html and 'aria-describedby="id_send_window_error"' in html
    assert "هر زمان را مانند" in html
    assert saved(world)["send_window"] != "09:30-"


@pytest.mark.parametrize("start, end, stored", [
    ("8", "24", "08:00-00:00"),        # whole hours, and 24 for midnight
    ("۸", "۲:۳۰", "08:00-02:30"),       # Persian digits, past midnight
    ("22:00", "24:00", "22:00-00:00"),
])
def test_a_window_may_end_at_or_past_midnight(world, verified, start, end, stored):
    client = signed_in(world.users["operator"], verified)
    client.post("/campaigns/fresh/settings/", {**FORM, "window_start": start, "window_end": end})
    assert saved(world)["send_window"] == stored


def test_the_whole_day_is_not_a_window(world, verified):
    """The dashboard always keeps prohibited hours (the CLI alone has 'off')."""
    client = signed_in(world.users["operator"], verified)
    html = client.post("/campaigns/fresh/settings/", {**FORM, "window_start": "0", "window_end": "24"}).content.decode()
    assert "هر زمان را مانند" in html


def test_a_half_written_translation_is_refused(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.post("/campaigns/fresh/settings/", {
        **FORM, "vm_column": ["first_name"], "vm_source": ["Ali"], "vm_target": [""],
    }).content.decode()
    assert 'id="id_value_maps_error"' in html


def test_only_an_admin_changes_the_advanced_settings(world, verified):
    advanced = {"max_attempts": "3", "timeout": "30", "backoff_max": "10", "link_rate": "5/s"}
    operator = signed_in(world.users["operator"], verified)
    html = operator.get("/campaigns/fresh/settings/").content.decode()
    assert 'name="max_attempts"' not in html
    operator.post("/campaigns/fresh/settings/", {**FORM, **advanced})
    assert "max_attempts" not in saved(world)  # an operator's post can't set them

    admin = signed_in(world.users["admin"], verified)
    assert 'name="max_attempts"' in admin.get("/campaigns/fresh/settings/").content.decode()
    admin.post("/campaigns/fresh/settings/", {**FORM, **advanced})
    s = saved(world)
    assert (s["max_attempts"], s["timeout"], s["backoff_max"], s["link_rate"]) == (3, 30.0, 10.0, "5/s")
    # Emptied again: the engine's defaults.
    admin.post("/campaigns/fresh/settings/", {**FORM, "max_attempts": "", "timeout": "", "backoff_max": "", "link_rate": ""})
    assert not {"max_attempts", "timeout", "backoff_max", "link_rate"} & set(saved(world))


def test_follow_up_jobs_use_the_campaigns_timeout(world, settings, monkeypatch):
    settings.SANDBOX = False
    monkeypatch.setenv("KAVENEGAR_API_KEY", "test-key-not-real")
    campaign = world.campaigns["fresh"]
    campaign.settings["timeout"] = 40.0
    assert Engine().sender(campaign).cfg.timeout == 40.0
    assert Engine().sender().cfg.timeout == 15.0
