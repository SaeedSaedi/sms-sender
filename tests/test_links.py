"""The link stage: rows written before Shlink is called, exact repeats,
no personal data in anything Shlink sees, and nothing sent unless every
link is ready."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

import pytest

from sms_sender.links import (
    REF_LENGTH,
    LinkError,
    LinkSettings,
    LinkStage,
    allowed_domains,
    build_long_url,
    destination_problem,
    new_ref,
)
from sms_sender.shortlink import (
    ShlinkError,
    ShlinkHaltError,
    ShlinkPermanentError,
    ShortLink,
)
from sms_sender.state import LINK_FAILED, LINK_PENDING, LINK_READY, StateStore

BASE = "https://kifpool.me/u"
A, B, C = "09120000001", "09120000002", "09120000003"
T0 = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


class FakeShlink:
    """Behaves like findIfExists: the same request returns the same link."""

    def __init__(self):
        self.created: list[dict] = []
        self.extended: list[tuple[str, str]] = []
        self.fail: list[tuple[str, BaseException]] = []  # (long_url marker, error), used once
        self._links: dict[tuple, ShortLink] = {}
        self._lock = threading.Lock()

    def create(self, *, long_url, title, tags, valid_until):
        with self._lock:
            self.created.append(
                {"long_url": long_url, "title": title, "tags": tags, "valid_until": valid_until}
            )
            for i, (marker, error) in enumerate(self.fail):
                if marker in long_url:
                    del self.fail[i]
                    raise error
            request = (long_url, valid_until, tuple(tags))
            if request not in self._links:
                code = f"c{len(self._links) + 1:04d}"
                self._links[request] = ShortLink(code, f"{BASE}/{code}", long_url, valid_until)
            return self._links[request]

    def extend(self, short_code, valid_until):
        self.extended.append((short_code, valid_until))


def settings(**kw) -> LinkSettings:
    return LinkSettings(destination="https://kifpool.me/offer?x=1", token="token3", **kw)


def stage(state, client, now=T0, **kw) -> LinkStage:
    return LinkStage(
        state, client, campaign="coin-7", settings=kw.pop("settings", settings()),
        rate_per_sec=0, now=lambda: now, **kw,
    )


def query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query)


# ---------- recipient links ----------

def test_one_link_per_recipient_with_only_a_random_reference(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    result = stage(state, shlink).run({A: "vip-2", B: "vip-2"})

    assert result.needed == 2 and result.created == 2
    assert set(result.tokens) == {A, B}
    assert all(t.startswith(f"{BASE}/c") for t in result.tokens.values())
    for call in shlink.created:
        params = query(call["long_url"])
        assert [k for k, _ in params] == [
            "x", "utm_source", "utm_medium", "utm_campaign", "utm_content", "r",
        ]
        assert dict(params)["utm_campaign"] == "coin-7"
        assert dict(params)["utm_content"] == "vip-2"
        assert re.fullmatch(r"[0-9A-Za-z]{%d}" % REF_LENGTH, dict(params)["r"])
        assert call["tags"] == ["campaign-coin-7"]
        assert call["valid_until"] == "2026-10-11T08:00:00+00:00"
        # Nothing Shlink sees may identify the person.
        seen = f"{call['long_url']} {call['title']} {call['tags']}"
        assert "0912" not in seen and "912000000" not in seen
    assert state.link_counts() == {LINK_READY: 2}


def test_a_second_run_asks_shlink_for_nothing_new(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    first = stage(state, shlink).run({A: "s", B: "s"})
    again = stage(state, shlink).run({A: "s", B: "s", C: "s"})
    assert again.created == 1 and len(shlink.created) == 3
    assert again.tokens[A] == first.tokens[A]


def test_an_unfinished_link_is_requested_again_exactly(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    shlink.fail.append(("utm", ShlinkError(None, None, "retries exhausted: read timeout")))
    with pytest.raises(LinkError, match="1 of 1 link"):
        stage(state, shlink).run({A: "s"})
    assert state.link_counts() == {LINK_PENDING: 1}

    # A day later, the same request goes out again, byte for byte.
    result = stage(state, shlink, now=T0 + timedelta(days=1)).run({A: "s"})
    assert shlink.created[0] == shlink.created[1]
    assert result.tokens[A] == f"{BASE}/c0001"


def test_a_refused_link_fails_the_stage_and_is_asked_again_next_run(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    shlink.fail.append(("utm", ShlinkPermanentError(400, "invalid-data", "bad")))
    with pytest.raises(LinkError, match="nothing was sent"):
        stage(state, shlink).run({A: "s", B: "s"})
    assert state.link_counts() == {LINK_READY: 1, LINK_FAILED: 1}
    assert stage(state, shlink).run({A: "s", B: "s"}).created == 1


def test_a_bad_key_stops_the_stage(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    shlink.fail.append(("utm", ShlinkHaltError(401, "invalid-api-key", "no such key")))
    with pytest.raises(ShlinkHaltError):
        stage(state, shlink, workers=1).run({A: "s", B: "s", C: "s"})
    assert len(shlink.created) == 1  # nothing more was asked


def test_a_stop_request_leaves_the_rest_for_the_next_run(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    stop = threading.Event()
    stop.set()
    with pytest.raises(LinkError, match="stopped"):
        stage(state, shlink, stop=stop).run({A: "s"})
    assert shlink.created == [] and state.link_counts() == {LINK_PENDING: 1}


def test_a_short_code_kavenegar_would_reject_is_not_used(tmp_path):
    state = StateStore(tmp_path / "s.db")

    class SlugShlink(FakeShlink):
        def create(self, **kw):
            return ShortLink("bad_slug", f"{BASE}/bad_slug", kw["long_url"], kw["valid_until"])

    with pytest.raises(LinkError):
        stage(state, SlugShlink()).run({A: "s"})
    assert state.link_counts() == {LINK_FAILED: 1}


def test_code_format_puts_only_the_short_code_in_the_token(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    result = stage(state, shlink, settings=settings(format="code")).run({A: "s"})
    assert result.tokens == {A: "c0001"}


# ---------- shared links ----------

def test_segment_links_are_shared_within_a_segment(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    result = stage(state, shlink, settings=settings(strategy="segment")).run(
        {A: "vip-2", B: "vip-2", C: "new-users"},
    )
    assert result.needed == 2
    assert result.tokens[A] == result.tokens[B] != result.tokens[C]
    by_title = {c["title"]: c for c in shlink.created}
    vip = by_title["coin-7 segment vip-2"]
    assert vip["tags"] == ["campaign-coin-7", "segment-vip-2"]
    assert dict(query(vip["long_url"]))["utm_content"] == "vip-2"
    assert "r" not in dict(query(vip["long_url"]))


def test_one_campaign_link_carries_no_segment(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    result = stage(state, shlink, settings=settings(strategy="campaign")).run(
        {A: "vip-2", B: "new-users"},
    )
    assert result.needed == 1 and len(set(result.tokens.values())) == 1
    params = dict(query(shlink.created[0]["long_url"]))
    assert "utm_content" not in params and "r" not in params


def test_the_approval_test_gets_its_own_link(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    result = stage(state, shlink, settings=settings(strategy="segment")).run(
        {A: "vip-2"}, test_phone=A,
    )
    assert result.test_token not in (None, result.tokens[A])
    test = next(c for c in shlink.created if "test" in c["title"])
    assert test["tags"] == ["campaign-coin-7-test"]  # kept out of the campaign's clicks
    assert dict(query(test["long_url"]))["utm_content"] == "test"


# ---------- expiry ----------

def test_a_link_about_to_expire_is_extended_before_sending(tmp_path):
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()
    stage(state, shlink).run({A: "s"})
    later = T0 + timedelta(days=6, hours=12)  # expires in 12 h
    result = stage(state, shlink, now=later).run({A: "s"})
    assert result.extended == 1
    assert shlink.extended == [("c0001", "2026-10-17T20:00:00+00:00")]
    assert state.get_links([A])[A].valid_until == "2026-10-17T20:00:00+00:00"
    assert result.created == 0  # the same link, more time


# ---------- destinations and URLs ----------

@pytest.mark.parametrize("url, problem", [
    ("https://kifpool.me/offer", None),
    ("https://app.kifpool.me/offer", None),
    ("http://kifpool.me/offer", "must start with https://"),
    ("https://kifpool.me.evil.example/x", "isn't an allowed destination domain"),
    ("https://user:pw@kifpool.me/x", "user name or password"),
    ("https://kifpool.me/u/abc12", "is a short link itself"),
    ("https://kifpool.me/x?utm_source=a&r=1", "already has r, utm_source"),
])
def test_destination_rules(url, problem):
    found = destination_problem(url, ("kifpool.me",), BASE)
    assert (found is None) if problem is None else (problem in found)


def test_allowed_domains_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("SMS_SENDER_LINK_DOMAINS", raising=False)
    assert allowed_domains() == ("kifpool.me",)
    monkeypatch.setenv("SMS_SENDER_LINK_DOMAINS", " Kifpool.me , example.org ")
    assert allowed_domains() == ("kifpool.me", "example.org")


def test_the_long_url_keeps_the_destinations_own_query_and_fragment():
    assert build_long_url("https://kifpool.me/p?a=%20b#top", [("utm_source", "sms")]) == \
        "https://kifpool.me/p?a=%20b&utm_source=sms#top"
    assert build_long_url("https://kifpool.me/p", [("r", "x")]) == "https://kifpool.me/p?r=x"


def test_references_are_random_alphanumerics():
    refs = {new_ref() for _ in range(2000)}
    assert len(refs) == 2000
    assert all(re.fullmatch(r"[0-9A-Za-z]{10}", r) for r in refs)


def test_settings_are_checked():
    assert LinkSettings(destination="https://kifpool.me", token="token4").problems()
    assert LinkSettings(destination="https://kifpool.me", token="token", format="html").problems()
    assert LinkSettings(destination="https://kifpool.me", token="token", strategy="x").problems()
    assert LinkSettings(destination="https://kifpool.me", token="token", expiry_days=0).problems()
    with pytest.raises(ValueError):
        stage(StateStore(":memory:"), FakeShlink(), settings=LinkSettings(
            destination="https://kifpool.me", token="token", utm_source=" ",
        ))
