"""Plan 06, L4: go to anything with Ctrl+K (Cmd+K). The menu's pages are on
every page; /palette/ adds the campaigns (the CLI's too), segments and
presets, each with where it opens for you, and nothing personal."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("django")

from django.utils import timezone  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.campaigns.models import Preset  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()


@pytest.fixture
def things(tmp_path):
    preset = Preset.objects.create(slug="coin-price", name="قیمت سکه", settings={"template": "t"})
    Preset.objects.create(slug="old", name="قدیمی", archived_at=timezone.now())
    Campaign.objects.create(slug="oil", name="نفت خام", settings={"segment": "vip"})
    Campaign.objects.create(slug="coin-price-1", name="قیمت سکه ۱", preset=preset)
    Segment.objects.create(slug="vip", name="مشتریان ویژه", status=Segment.Status.READY)
    cli = StateStore(tmp_path / "db" / "cli-run.db")  # the CLI's: no Campaign row
    cli.upsert_pending([("09120000001", "09120000001")])
    StateStore(tmp_path / "db" / "oil.db").upsert_pending([("09120000002", "09120000002")])
    (tmp_path / "db" / "not a slug.db").write_bytes(b"")


def _palette(client) -> dict:
    response = client.get("/palette/")
    assert response.status_code == 200 and response["Content-Type"] == "application/json"
    return json.loads(response.content)


def test_it_finds_every_campaign_segment_and_preset(things, signed_in):
    data = _palette(signed_in)
    assert data["campaigns"] == [
        {"name": "قیمت سکه ۱", "hint": "coin-price-1", "url": "/campaigns/coin-price-1/"},  # a viewer can't compose
        {"name": "نفت خام", "hint": "oil", "url": "/campaigns/oil/"},
        {"name": "cli-run", "hint": "cli-run", "url": "/reports/cli-run/"},  # the CLI's: its report
    ]
    assert data["segments"] == [{"name": "مشتریان ویژه", "hint": "vip", "url": "/segments/vip/"}]
    # A viewer can't start an alert or edit a preset: the list.
    assert [p["url"] for p in data["presets"]] == ["/presets/", "/presets/"]


def test_a_preset_starts_a_new_alert_for_those_who_run_campaigns(things, client, make_user, verified):
    verified(client, make_user("op", "operator"))
    data = _palette(client)
    presets = {p["hint"]: p["url"] for p in data["presets"]}
    assert presets == {"coin-price": "/compose/coin-price/", "old": "/presets/old/"}  # put away: edit only
    campaigns = {c["hint"]: c["url"] for c in data["campaigns"]}
    assert campaigns["coin-price-1"] == "/compose/c/coin-price-1/"  # an alert: its composer


def test_nothing_personal_goes_in_it(things, signed_in):
    body = signed_in.get("/palette/").content.decode()
    assert "0912" not in body


def test_it_needs_a_sign_in(client):
    assert client.get("/palette/").status_code == 302


def test_the_page_has_the_palette_and_the_lookup_only_for_who_may_look(signed_in, client, make_user, verified):
    html = signed_in.get("/").content.decode()
    assert 'id="palette"' in html and 'data-open-palette' in html
    assert "data-palette-number" not in html and "data-find" not in html  # a viewer can't look a number up
    verified(client, make_user("op", "operator"))
    html = client.get("/").content.decode()
    assert 'action="/numbers/" hidden data-palette-number' in html and 'data-find="' in html
