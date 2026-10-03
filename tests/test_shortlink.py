"""Shlink client: the exact requests it sends (Shlink silently ignores fields
it doesn't know, so they're pinned here) and how it treats each failure."""
from __future__ import annotations

import pytest
import requests

from sms_sender.shortlink import (
    DEFAULT_BASE_URL,
    ShlinkClient,
    ShlinkConfig,
    ShlinkError,
    ShlinkHaltError,
    ShlinkPermanentError,
    load_shlink_config,
)


class FakeResponse:
    def __init__(self, status: int, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeSession:
    """Plays back scripted responses (or raises scripted exceptions)."""

    def __init__(self, *responses):
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, json=None, params=None, timeout=None):
        self.calls.append({
            "method": method, "url": url, "json": json, "params": params,
            "timeout": timeout, "headers": dict(self.headers),
        })
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def client(session: FakeSession, **cfg) -> ShlinkClient:
    config = ShlinkConfig(api_key="KEY", backoff_max=0, max_attempts=3, **cfg)
    return ShlinkClient(config, session_factory=lambda: session)


def short_url(code="aB3x9", url=None, long_url="https://kifpool.me/p?r=x"):
    return FakeResponse(200, {
        "shortCode": code, "shortUrl": url or f"https://kifpool.me/u/{code}",
        "longUrl": long_url, "meta": {"validUntil": "2026-10-11T08:00:00+00:00"},
    })


def test_create_sends_exactly_these_fields():
    session = FakeSession(short_url())
    link = client(session).create(
        long_url="https://kifpool.me/p?r=x", title="coin-7 x",
        tags=["campaign-coin-7"], valid_until="2026-10-11T08:00:00+00:00",
    )
    (call,) = session.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://kifpool.me/u/rest/v3/short-urls"
    assert call["json"] == {
        "longUrl": "https://kifpool.me/p?r=x",
        "title": "coin-7 x",
        "tags": ["campaign-coin-7"],
        "validUntil": "2026-10-11T08:00:00+00:00",
        "findIfExists": True,
        "forwardQuery": False,
        "crawlable": False,
    }
    assert call["headers"]["X-Api-Key"] == "KEY"
    assert call["timeout"] == 15.0
    assert (link.short_code, link.short_url) == ("aB3x9", "https://kifpool.me/u/aB3x9")


@pytest.mark.parametrize("returned", [
    "https://kifpool.me/aB3x9",          # base path lost
    "https://other.example/u/aB3x9",     # another domain
    "https://kifpool.me/u/zzzzz",        # not this link's code
])
def test_a_short_url_in_another_format_halts(returned):
    session = FakeSession(short_url(url=returned))
    with pytest.raises(ShlinkHaltError, match="links must be https://kifpool.me/u/<short-code>"):
        client(session).create(long_url="https://kifpool.me/p", title="t", tags=[], valid_until="v")


def test_a_bad_key_halts():
    session = FakeSession(FakeResponse(401, {
        "type": "https://shlink.io/api/error/invalid-api-key",
        "title": "Invalid API key", "detail": "Provided API key does not exist", "status": 401,
    }))
    with pytest.raises(ShlinkHaltError) as e:
        client(session).create(long_url="https://kifpool.me/p", title="t", tags=[], valid_until="v")
    assert (e.value.status, e.value.code) == (401, "invalid-api-key")
    assert len(session.calls) == 1  # never retried


def test_invalid_data_is_permanent_and_names_the_fields():
    session = FakeSession(FakeResponse(400, {
        "type": "https://shlink.io/api/error/invalid-data", "title": "Invalid data",
        "detail": "Provided data is not valid", "status": 400, "invalidElements": ["validUntil"],
    }))
    with pytest.raises(ShlinkPermanentError, match=r"invalid: validUntil"):
        client(session).create(long_url="https://kifpool.me/p", title="t", tags=[], valid_until="x")
    assert len(session.calls) == 1


def test_a_lost_answer_is_retried_because_create_is_idempotent():
    session = FakeSession(
        requests.exceptions.ReadTimeout("read timed out"),
        FakeResponse(503),
        short_url(),
    )
    link = client(session).create(
        long_url="https://kifpool.me/p", title="t", tags=["a"], valid_until="v",
    )
    assert link.short_code == "aB3x9"
    # The same request, three times.
    assert len({repr(c["json"]) for c in session.calls}) == 1 and len(session.calls) == 3


def test_retries_running_out_is_a_plain_shlink_error():
    session = FakeSession(FakeResponse(502), FakeResponse(502), FakeResponse(502))
    with pytest.raises(ShlinkError) as e:
        client(session).create(long_url="https://kifpool.me/p", title="t", tags=[], valid_until="v")
    assert type(e.value) is ShlinkError
    assert "retries exhausted" in str(e.value)


def test_a_200_without_the_link_fields_is_retried():
    session = FakeSession(FakeResponse(200, {"unexpected": True}), short_url())
    assert client(session).create(
        long_url="https://kifpool.me/p", title="t", tags=[], valid_until="v",
    ).short_code == "aB3x9"


def test_extend_patches_only_the_expiry():
    session = FakeSession(FakeResponse(200, {"shortCode": "aB3x9"}))
    client(session).extend("aB3x9", "2026-10-20T08:00:00+00:00")
    (call,) = session.calls
    assert call["method"] == "PATCH"
    assert call["url"] == "https://kifpool.me/u/rest/v3/short-urls/aB3x9"
    assert call["json"] == {"validUntil": "2026-10-20T08:00:00+00:00"}


def test_visits_by_tag_reads_every_page():
    def page(n, codes, pages=2):
        return FakeResponse(200, {"shortUrls": {
            "data": [{"shortCode": c, "visitsSummary": {"total": 3, "nonBots": 2, "bots": 1}}
                     for c in codes],
            "pagination": {"currentPage": n, "pagesCount": pages},
        }})
    session = FakeSession(page(1, ["a1", "a2"]), page(2, ["a3"]))
    visits = list(client(session).visits_by_tag("campaign-coin-7", page_size=2))
    assert [(v.short_code, v.total, v.non_bots) for v in visits] == [
        ("a1", 3, 2), ("a2", 3, 2), ("a3", 3, 2),
    ]
    assert [c["params"] for c in session.calls] == [
        {"tags[]": "campaign-coin-7", "page": 1, "itemsPerPage": 2},
        {"tags[]": "campaign-coin-7", "page": 2, "itemsPerPage": 2},
    ]


def test_health_reports_the_version():
    session = FakeSession(FakeResponse(200, {"status": "pass", "version": "5.1.7"}))
    assert client(session).health() == "5.1.7"
    assert session.calls[0]["url"] == "https://kifpool.me/u/rest/health"


def test_config_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("SHLINK_BASE_URL", raising=False)
    monkeypatch.delenv("SHLINK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SHLINK_API_KEY is not set"):
        load_shlink_config()
    monkeypatch.setenv("SHLINK_API_KEY", " k ")
    cfg = load_shlink_config()
    assert (cfg.api_key, cfg.base_url) == ("k", DEFAULT_BASE_URL)
    monkeypatch.setenv("SHLINK_BASE_URL", "https://example.test/s/")
    assert load_shlink_config().base_url == "https://example.test/s"
