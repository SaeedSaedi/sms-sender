"""Plan 05, decision 8: the attribution API. Read only, for the company's
backend: admin-issued tokens shown once and stored as a digest, no phone
numbers in any answer, and every call recorded."""
from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.clicks import ATTRIBUTION_HEADER, sync_clicks  # noqa: E402
from sms_sender_web.api import views as api_views  # noqa: E402
from sms_sender_web.api.models import ApiToken  # noqa: E402
from sms_sender_web.api.tokens import issue, revoke  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402

from ..test_clicks import A, B, C, Visits  # noqa: E402
from ..test_clicks import campaign_db as clicks_campaign  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def store(settings, tmp_path):
    settings.SMS_SENDER_DB_DIR = tmp_path
    store = clicks_campaign(tmp_path / "coin-7.db")
    sync_clicks(store, Visits({"c1": 2, "c2": 1, "c3": 0}), "coin-7")
    return store


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def call(path: str, token: str | None = None, **params):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return Client().get(path, params, **headers)


def test_an_admin_issues_a_token_that_is_shown_once(admin_client, make_user, verified):
    response = admin_client.post("/api-tokens/", {"name": "backend"})
    html = response.content.decode()
    token = ApiToken.objects.get()
    raw = html.split('data-copy="')[1].split('"')[0]
    assert raw.startswith("smsk_") and len(raw) > 40 and token.prefix == raw[:10]
    assert token.digest == hashlib.sha256(raw.encode()).hexdigest() and raw not in token.digest
    assert raw not in admin_client.get("/api-tokens/").content.decode()  # never again
    assert AuditEvent.objects.get(action="api_token_created").detail == {"name": "backend", "prefix": raw[:10]}
    operator = Client()
    verified(operator, make_user("operator1", "operator"))
    assert operator.get("/api-tokens/").status_code == 403


def test_every_call_needs_a_live_token(store):
    token, raw = issue("backend", None)
    assert call("/api/v1/campaigns/").status_code == 401
    assert call("/api/v1/campaigns/")["WWW-Authenticate"].startswith("Bearer")
    assert call("/api/v1/campaigns/", "smsk_not-a-token").status_code == 401
    assert Client().get("/api/v1/campaigns/", HTTP_AUTHORIZATION=f"Basic {raw}").status_code == 401
    assert call("/api/v1/campaigns/", raw).status_code == 200
    revoke(token)
    assert call("/api/v1/campaigns/", raw).status_code == 401
    # Read only: no session, no CSRF, and nothing but GET.
    _, other = issue("other", None)
    assert Client().post("/api/v1/campaigns/", HTTP_AUTHORIZATION=f"Bearer {other}").status_code == 405


def test_attribution_by_r_without_a_phone_number(store, monkeypatch):
    token, raw = issue("backend", None)
    listing = call("/api/v1/campaigns/", raw).json()
    assert listing["campaigns"][0]["slug"] == "coin-7" and listing["campaigns"][0]["accepted"] == 3
    monkeypatch.setattr(api_views, "PAGE", 2)
    first = call("/api/v1/campaigns/coin-7/attribution/", raw)
    body = first.json()
    assert body["columns"] == ATTRIBUTION_HEADER and (body["page"], body["pages"], body["total"]) == (1, 2, 3)
    assert len(body["rows"]) == 2 and call("/api/v1/campaigns/coin-7/attribution/", raw, page="2").json()["rows"]
    for phone in (A, B, C):
        assert phone not in first.content.decode() and phone not in json.dumps(listing)
    assert call("/api/v1/campaigns/nope/attribution/", raw).status_code == 404
    assert call("/api/v1/campaigns/Bad_Slug/attribution/", raw).status_code == 404
    calls = AuditEvent.objects.filter(action="api_called")
    assert calls.count() == 3 and {e.username for e in calls} == {"api:backend"}
    assert calls.filter(campaign="coin-7").first().detail["rows"] in (1, 2)
    token.refresh_from_db()
    assert token.last_used_at is not None
