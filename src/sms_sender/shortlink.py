"""Shlink REST client (API v3) for the link stage.

Creating a short link is idempotent, unlike sending an SMS: every create
sends `findIfExists: true`, and Shlink then returns the link it already has
for the same long URL, `validUntil` and tags (its `findOneMatching`). The
link stage stores each request before the first call and repeats it byte
for byte, so a create whose answer was lost — a read timeout, a 5xx — is
simply retried.

The API key travels in the `X-Api-Key` header, never in a URL, so it can't
leak through exception text the way a Kavenegar key can.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, TypeVar
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)

ENV_API_KEY = "SHLINK_API_KEY"
ENV_BASE_URL = "SHLINK_BASE_URL"
# Links in SMS look like https://kifpool.me/u/{short-code} (decided 2026-10-04).
DEFAULT_BASE_URL = "https://kifpool.me/u"

# Shlink problem types (`https://shlink.io/api/error/<code>`) that mean this
# key or setup can't work at all: stop the link stage instead of failing link
# by link.
_HALT_CODES = frozenset({
    "invalid-api-key", "missing-authentication", "forbidden-tag-operation", "domain-not-found",
})
_PROBLEM_PREFIX = "https://shlink.io/api/error/"

T = TypeVar("T")


class ShlinkError(Exception):
    """Shlink couldn't be used right now (retries ran out). Nothing is lost:
    the link stays pending and the next run asks again."""

    def __init__(self, status: int | None, code: str | None, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        where = f"[{self.status}{' ' + self.code if self.code else ''}] " if self.status else ""
        return f"{where}{self.detail}"


class ShlinkHaltError(ShlinkError):
    """The key or the setup is wrong (bad key, forbidden, unexpected short
    URL format): every other request would fail the same way."""


class ShlinkPermanentError(ShlinkError):
    """Shlink refused this one request; repeating it won't help."""


class _Retriable(ShlinkError):
    """Internal: the answer was lost or Shlink was busy — safe to repeat."""


@dataclass(frozen=True)
class ShortLink:
    short_code: str
    short_url: str
    long_url: str
    valid_until: str | None


@dataclass(frozen=True)
class LinkVisits:
    short_code: str
    total: int
    non_bots: int


@dataclass(frozen=True)
class ShlinkConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 15.0
    max_attempts: int = 5
    backoff_max: float = 30.0


def shlink_base_url() -> str:
    """Where Shlink lives (no key needed): SHLINK_BASE_URL, else the default."""
    load_dotenv(".env")
    return (os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL).rstrip("/")


def load_shlink_config(timeout: float = 15.0) -> ShlinkConfig:
    """Shlink settings from the environment (or `.env` in cwd)."""
    base = shlink_base_url()
    key = os.environ.get(ENV_API_KEY, "").strip()
    if not key:
        raise RuntimeError(
            f"{ENV_API_KEY} is not set. Put it in `.env` or export it before using links."
        )
    return ShlinkConfig(api_key=key, base_url=base, timeout=timeout)


class ShlinkClient:
    """Thread-safe: each thread gets its own `requests.Session`."""

    def __init__(
        self, cfg: ShlinkConfig, *,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        # Every short URL must be exactly this prefix + its short code.
        self.public_prefix = self.base_url + "/"
        self._api = f"{self.base_url}/rest/v3"
        self._session_factory = session_factory
        self._local = threading.local()

    # ---------- transport ----------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            session.headers.update({"X-Api-Key": self.cfg.api_key, "Accept": "application/json"})
            self._local.session = session
        return session

    def _request(
        self, method: str, url: str, *, body: dict | None = None, params: dict | None = None,
    ) -> dict:
        try:
            resp = self._session().request(
                method, url, json=body, params=params, timeout=self.cfg.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise _Retriable(None, None, f"{type(e).__name__}: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _Retriable(resp.status_code, None, f"http {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            code, detail = _problem(data, resp.status_code)
            if resp.status_code in (401, 403) or code in _HALT_CODES:
                raise ShlinkHaltError(resp.status_code, code, detail)
            raise ShlinkPermanentError(resp.status_code, code, detail)
        if not isinstance(data, dict):
            raise _Retriable(resp.status_code, None, "malformed response (not a JSON object)")
        return data

    def _call(self, what: str, attempt: Callable[[], T]) -> T:
        """Run `attempt` with backoff on retriable failures."""
        retryer = Retrying(
            stop=stop_after_attempt(self.cfg.max_attempts),
            wait=wait_random_exponential(multiplier=1, max=self.cfg.backoff_max),
            retry=retry_if_exception_type(_Retriable),
            before_sleep=lambda state: _log_retry(what, state),
            reraise=False,
        )
        try:
            for try_ in retryer:
                with try_:
                    return attempt()
        except RetryError as e:
            inner = e.last_attempt.exception()
            raise ShlinkError(
                getattr(inner, "status", None), None, f"{what}: retries exhausted: {inner}",
            ) from e
        raise ShlinkError(None, None, f"{what}: retry loop ended without a result")

    # ---------- API ----------

    def create(self, *, long_url: str, title: str, tags: list[str], valid_until: str) -> ShortLink:
        """Create a short link, or get the one Shlink already has for exactly
        this request. Raises ShlinkHaltError if the returned short URL isn't
        `<base>/<short-code>`: that format is what recipients must see."""
        body = {
            "longUrl": long_url,
            "title": title,
            "tags": tags,
            "validUntil": valid_until,
            "findIfExists": True,
            # Visitors must not be able to add or override query parameters.
            "forwardQuery": False,
            "crawlable": False,
        }

        def attempt() -> ShortLink:
            return _short_link(self._request("POST", f"{self._api}/short-urls", body=body))

        link = self._call("create short link", attempt)
        if link.short_url != self.public_prefix + link.short_code:
            raise ShlinkHaltError(
                None, "unexpected-short-url",
                f"Shlink returned {link.short_url!r}; links must be "
                f"{self.public_prefix}<short-code> (check {ENV_BASE_URL})",
            )
        return link

    def extend(self, short_code: str, valid_until: str) -> None:
        """Move a link's expiry (idempotent: it sets a value)."""
        url = f"{self._api}/short-urls/{quote(short_code, safe='')}"
        self._call("extend short link", lambda: self._request(
            "PATCH", url, body={"validUntil": valid_until},
        ))

    def visits_by_tag(self, tag: str, *, page_size: int = 500) -> Iterator[LinkVisits]:
        """Visit counts of every short link carrying `tag`, page by page."""
        page = 1
        while True:
            params = {"tags[]": tag, "page": page, "itemsPerPage": page_size}
            data = self._call("list short links", lambda params=params: self._request(
                "GET", f"{self._api}/short-urls", params=params,
            ))
            block = data.get("shortUrls") or {}
            for item in block.get("data") or []:
                summary = item.get("visitsSummary") or {}
                code = item.get("shortCode")
                if isinstance(code, str):
                    yield LinkVisits(
                        short_code=code,
                        total=_int(summary.get("total")),
                        non_bots=_int(summary.get("nonBots")),
                    )
            pages = _int((block.get("pagination") or {}).get("pagesCount"))
            if page >= pages:
                return
            page += 1

    def visit_times(
        self, tag: str, *, since: datetime | None = None, page_size: int = 1000,
    ) -> Iterator[datetime]:
        """When each visit to a tag's links happened (UTC), bots left out,
        page by page. `since`: from then on only (Shlink's startDate)."""
        page = 1
        while True:
            params = {"page": page, "itemsPerPage": page_size, "excludeBots": "true"}
            if since is not None:
                params["startDate"] = since.astimezone(timezone.utc).isoformat()
            data = self._call("list tag visits", lambda params=params: self._request(
                "GET", f"{self._api}/tags/{quote(tag, safe='')}/visits", params=params,
            ))
            block = data.get("visits") or {}
            for item in block.get("data") or []:
                when = _moment(item.get("date"))
                if when is not None and not item.get("potentialBot"):
                    yield when
            pages = _int((block.get("pagination") or {}).get("pagesCount"))
            if page >= pages:
                return
            page += 1

    def health(self) -> str | None:
        """Shlink's version if it reports itself healthy, else None. Needs no key."""
        def attempt() -> dict:
            return self._request("GET", f"{self.base_url}/rest/health")
        data = self._call("health check", attempt)
        return data.get("version") if data.get("status") == "pass" else None


def _moment(value) -> datetime | None:
    """Shlink's ISO-8601 dates ("…+00:00", or "…Z"), as aware datetimes."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _short_link(data: dict) -> ShortLink:
    code, url, long_url = data.get("shortCode"), data.get("shortUrl"), data.get("longUrl")
    if not (isinstance(code, str) and isinstance(url, str) and isinstance(long_url, str)):
        # A 200 without the fields: repeating the create is harmless.
        raise _Retriable(200, None, "malformed short URL in the response")
    meta = data.get("meta") or {}
    return ShortLink(
        short_code=code, short_url=url, long_url=long_url,
        valid_until=meta.get("validUntil"),
    )


def _problem(data: object, status: int) -> tuple[str | None, str]:
    """(error code, readable detail) from a Shlink problem+json body."""
    if not isinstance(data, dict):
        return None, f"http {status}"
    kind = str(data.get("type") or "")
    code = kind[len(_PROBLEM_PREFIX):] if kind.startswith(_PROBLEM_PREFIX) else (kind or None)
    detail = str(data.get("detail") or data.get("title") or f"http {status}")
    invalid = data.get("invalidElements")
    if invalid:
        detail += f" (invalid: {', '.join(map(str, invalid))})"
    return code, detail


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _log_retry(what: str, state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "shlink_retry",
        extra={"call": what, "attempt": state.attempt_number, "detail": str(exc)},
    )
