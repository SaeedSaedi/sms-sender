"""The link stage: every short link a run needs exists before any SMS.

Each link is first written to the campaign DB (`links`), with its long URL,
title, tags and expiry exactly as they'll be sent. Shlink is only asked
after that, so a crash or a retry repeats the same request, and Shlink's
`findIfExists` returns the link it already made instead of a second one.

No personal data leaves for Shlink (decided 2026-10-04). A recipient's
long URL carries only `r`, a random reference that is never derived from
the phone or the user ID; the mapping from `r` back to them stays in this
DB. Titles and tags carry the campaign and segment names only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit

from .rate import TokenBucket
from .sender import TOKEN_MAX_SPACES, token_problem
from .shortlink import ShlinkError, ShlinkHaltError, ShlinkPermanentError, ShortLink
from .state import LINK_READY, LinkRow, StateStore

logger = logging.getLogger(__name__)

STRATEGIES = ("recipient", "segment", "campaign")
FORMATS = ("url", "code")  # or a pattern with {code}, e.g. "u/{code}"
CODE = "{code}"
DEFAULT_EXPIRY_DAYS = 7
DEFAULT_RATE = "10/s"  # conservative until Shlink's real speed is measured
DEFAULT_WORKERS = 4

ENV_LINK_DOMAINS = "SMS_SENDER_LINK_DOMAINS"
DEFAULT_LINK_DOMAINS = ("kifpool.me",)

REF_ALPHABET = string.digits + string.ascii_letters
REF_LENGTH = 10  # 62^10 ≈ 8·10^17, about 59 bits

# A link that would expire this soon gets more time before its SMS goes out.
EXTEND_MARGIN = timedelta(hours=24)

# Query parameters the stage adds itself; a destination must not carry them.
ADDED_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "r")


class LinkError(Exception):
    """The link stage couldn't make every link: nothing is sent."""


class LinkClient(Protocol):
    def create(self, *, long_url: str, title: str, tags: list[str], valid_until: str) -> ShortLink: ...
    def extend(self, short_code: str, valid_until: str) -> None: ...


@dataclass(frozen=True)
class LinkSettings:
    """How a campaign's SMS carry their link (spec 4.4 and 4.5)."""
    destination: str
    token: str                        # which Kavenegar token carries the link
    # url: the whole short URL; code: only the short code; or a pattern with
    # {code} for a template whose text already holds part of the URL, e.g.
    # "u/{code}" after "https://kifpool.me/" (decided: support the existing
    # template configuration).
    format: str = "url"
    strategy: str = "recipient"       # recipient | segment | campaign
    expiry_days: int = DEFAULT_EXPIRY_DAYS
    utm_source: str = "sms"
    utm_medium: str = "sms"
    utm_campaign: str | None = None   # None: the campaign name
    utm_content: str | None = None    # None: each recipient's segment

    def problems(self) -> list[str]:
        out = []
        if self.token not in TOKEN_MAX_SPACES:
            out.append(f"link token must be one of {', '.join(TOKEN_MAX_SPACES)}")
        problem = format_problem(self.format)
        if problem:
            out.append(problem)
        if self.strategy not in STRATEGIES:
            out.append(f"link strategy must be one of {', '.join(STRATEGIES)}")
        if self.expiry_days < 1:
            out.append("links must stay valid for at least 1 day")
        for name in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                out.append(f"{name} is empty")
        return out

    def as_settings(self) -> dict:
        """The part a campaign's DB is bound to (expiry may change freely)."""
        return {
            "destination": self.destination, "token": self.token, "format": self.format,
            "strategy": self.strategy, "utm_source": self.utm_source,
            "utm_medium": self.utm_medium, "utm_campaign": self.utm_campaign,
            "utm_content": self.utm_content,
        }


def allowed_domains() -> tuple[str, ...]:
    """Destination domains links may point to (their subdomains included)."""
    raw = os.environ.get(ENV_LINK_DOMAINS, "")
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip()) or DEFAULT_LINK_DOMAINS


def destination_issue(url: str, domains: Iterable[str], shlink_base: str) -> tuple[str, dict] | None:
    """Why `url` can't be a campaign's destination, as a key and its
    details, or None."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "not_https", {}
    try:
        host = (parts.hostname or "").lower()
    except ValueError:  # e.g. a malformed IPv6 host
        host = ""
    if not host:
        return "no_domain", {}
    if parts.username or parts.password:
        return "credentials", {}
    domains = tuple(domains)
    if not any(host == d or host.endswith("." + d) for d in domains):
        return "domain_not_allowed", {"host": host, "domains": ", ".join(domains)}
    if url.startswith(shlink_base.rstrip("/") + "/"):
        return "short_link", {}
    taken = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)} & set(ADDED_PARAMS))
    if taken:
        return "has_added_params", {"params": ", ".join(taken)}
    return None


def destination_problem(url: str, domains: Iterable[str], shlink_base: str) -> str | None:
    """`destination_issue` in words, for the CLI."""
    issue = destination_issue(url, domains, shlink_base)
    if issue is None:
        return None
    key, f = issue
    return {
        "not_https": f"{url!r} must start with https://",
        "no_domain": f"{url!r} has no domain",
        "credentials": f"{url!r} must not contain a user name or password",
        "domain_not_allowed": (f"{f.get('host')} isn't an allowed destination domain "
                               f"({f.get('domains')}); add it to {ENV_LINK_DOMAINS} if it should be"),
        "short_link": f"{url!r} is a short link itself",
        "has_added_params": f"{url!r} already has {f.get('params')}; the link stage adds those itself",
    }[key]


def build_long_url(destination: str, params: list[tuple[str, str]]) -> str:
    """Append `params` in this order, keeping the destination's own query
    string and fragment exactly as they were."""
    head, sep, fragment = destination.partition("#")
    joiner = "&" if "?" in head and not head.endswith(("?", "&")) else ("" if "?" in head else "?")
    return f"{head}{joiner}{urlencode(params)}{sep}{fragment}"


def new_ref() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(REF_LENGTH))


def _utc_iso(moment: datetime) -> str:
    # Whole seconds in UTC: Shlink's findIfExists compares validUntil exactly.
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def link_plan(settings: LinkSettings, campaign: str) -> str:
    """Fingerprint of what shapes a link (destination, token, format,
    strategy, UTM values, campaign) — not its expiry, which is extended in
    place. A link row planned under another fingerprint is stale."""
    shape = json.dumps({**settings.as_settings(), "campaign": campaign}, sort_keys=True)
    return hashlib.sha256(shape.encode()).hexdigest()[:12]


def plan_link(
    settings: LinkSettings, *, campaign: str, key: str, segment: str | None,
    test: bool = False, now: datetime | None = None,
) -> LinkRow:
    """One link's request: long URL (destination + UTM + a fresh random
    `r` on personal links), title, tags and expiry. Nothing personal."""
    s = settings
    ref = new_ref() if test or s.strategy == "recipient" else None
    params = [
        ("utm_source", s.utm_source),
        ("utm_medium", s.utm_medium),
        ("utm_campaign", s.utm_campaign or campaign),
    ]
    if test:
        content = "test"
    elif s.strategy == "campaign":
        content = s.utm_content  # one link for every segment
    else:
        content = s.utm_content or segment
    if content:
        params.append(("utm_content", content))
    if ref:
        params.append(("r", ref))
    if test:
        # Its own tag keeps the operator's test clicks out of the campaign's.
        title, tags = f"{campaign} test {ref}", (f"campaign-{campaign}-test",)
    elif s.strategy == "recipient":
        title, tags = f"{campaign} {ref}", (f"campaign-{campaign}",)
    elif s.strategy == "segment":
        title, tags = f"{campaign} segment {segment}", (f"campaign-{campaign}", f"segment-{segment}")
    else:
        title, tags = campaign, (f"campaign-{campaign}",)
    moment = now or datetime.now(timezone.utc)
    return LinkRow(
        key=key, ref=ref, long_url=build_long_url(s.destination, params), title=title,
        tags=tags, valid_until=_utc_iso(moment + timedelta(days=s.expiry_days)),
        plan=link_plan(s, campaign),
    )


@dataclass(frozen=True)
class LinkStageResult:
    needed: int     # links this run uses
    created: int    # made (or found again) at Shlink in this run
    extended: int   # expiry moved before sending
    tokens: dict[str, str]          # phone → link token value
    keys: dict[str, str] = field(default_factory=dict)  # phone → link key (recorded at claim)
    test_token: str | None = None   # for the approval test SMS
    # The team's numbers that get the test SMS too: phone → its own test link.
    team_tokens: dict[str, str] = field(default_factory=dict)


class LinkStage:
    """Plan → write → create → extend, for one run."""

    def __init__(
        self, state: StateStore, client: LinkClient, *, campaign: str,
        settings: LinkSettings, workers: int = DEFAULT_WORKERS, rate_per_sec: float = 10.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        stop: threading.Event | None = None, note: Callable[[str], None] = lambda _t: None,
        progress: Callable[[int, int], None] | None = None,
    ):
        problems = settings.problems()
        if problems:
            raise ValueError("; ".join(problems))
        self.state = state
        self.client = client
        self.campaign = campaign
        self.settings = settings
        self.workers = max(1, workers)
        self._bucket = TokenBucket(rate_per_sec)
        self._now = now
        self._stop = stop or threading.Event()
        self._note = note
        # (links made so far, links to make): the dashboard's progress bar.
        self._progress = progress or (lambda _done, _total: None)
        self.plan = link_plan(settings, campaign)

    # ---------- plan ----------

    def _key(self, phone: str, segment: str) -> str:
        # Shared links carry the plan in their key: after a settings change
        # a new one is made, and the old one stays for whoever already got it.
        if self.settings.strategy == "recipient":
            return phone
        if self.settings.strategy == "segment":
            return f"segment:{segment}:{self.plan}"
        return f"campaign:{self.plan}"

    def _row(self, key: str, segment: str | None, *, test: bool = False) -> LinkRow:
        return plan_link(
            self.settings, campaign=self.campaign, key=key, segment=segment, test=test,
            now=self._now(),
        )

    def _write(self, wanted: dict[str, tuple[str | None, bool]]) -> dict[str, LinkRow]:
        """Make sure every wanted key has a row planned with the current
        settings; returns them all. A current row is kept, so a re-run
        repeats its request. A stale one (the destination, token, format,
        strategy or UTM values changed since) is planned again: wanted keys
        belong to recipients still in the queue, or to the approval test, so
        nobody has received the old link."""
        for _ in range(3):  # a new ref clashing with an old one is retried
            rows = self.state.get_links(wanted)
            missing = [k for k in wanted if k not in rows]
            stale = [k for k, row in rows.items() if row.plan != self.plan]
            if not missing and not stale:
                return rows
            if stale:
                logger.warning("links_replanned", extra={"n": len(stale), "plan": self.plan})
                self.state.replan_links(
                    self._row(k, wanted[k][0], test=wanted[k][1]) for k in stale
                )
            if missing:
                self.state.add_links(
                    self._row(k, wanted[k][0], test=wanted[k][1]) for k in missing
                )
        raise LinkError("couldn't write link rows (random references kept clashing)")

    # ---------- create / extend ----------

    def _create(self, row: LinkRow) -> bool:
        """Create one link. Returns whether it's ready. Raises ShlinkHaltError."""
        if self._stop.is_set():
            return False
        self._bucket.acquire()
        if self._stop.is_set():
            return False
        try:
            link = self.client.create(
                long_url=row.long_url, title=row.title, tags=list(row.tags),
                valid_until=row.valid_until,
            )
        except ShlinkHaltError:
            self._stop.set()
            raise
        except ShlinkPermanentError as e:
            self.state.mark_link_not_ready(row.key, str(e), refused=True)
            logger.error("link_refused", extra={"key": row.key, "detail": str(e)})
            return False
        except ShlinkError as e:
            self.state.mark_link_not_ready(row.key, str(e), refused=False)
            logger.warning("link_unavailable", extra={"key": row.key, "detail": str(e)})
            return False
        value = token_value(self.settings.format, link.short_url, link.short_code)
        problem = token_problem(self.settings.token, value)
        if problem:
            # e.g. a custom slug with '_' — Kavenegar would reject the SMS.
            self.state.mark_link_not_ready(row.key, f"unusable in an SMS: {problem}", refused=True)
            return False
        self.state.mark_link_ready(row.key, link.short_code, link.short_url)
        return True

    def _create_all(self, rows: list[LinkRow]) -> int:
        if not rows:
            return 0
        eta = len(rows) / self._bucket.rate if self._bucket.rate else 0
        self._note(
            f"Creating {len(rows)} short link(s)"
            + (f" (about {_duration(eta)} at the current link rate)" if eta >= 60 else "")
            + " …"
        )
        created = 0
        step = max(1, len(rows) // 10)
        self._progress(0, len(rows))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._create, row) for row in rows]
            try:
                for done, fut in enumerate(as_completed(futures), start=1):
                    created += fut.result()
                    self._progress(done, len(rows))
                    if done % step == 0 and done < len(rows):
                        self._note(f"  links: {done}/{len(rows)}")
            except ShlinkHaltError:
                for f in futures:
                    f.cancel()
                raise
        return created

    def _extend_expiring(self, rows: list[LinkRow]) -> int:
        """Give a link more time if it would expire within a day of its SMS."""
        soon = self._now() + EXTEND_MARGIN
        expiring = [
            r for r in rows
            if r.short_code and datetime.fromisoformat(r.valid_until) < soon
        ]
        until = _utc_iso(self._now() + timedelta(days=self.settings.expiry_days))
        for row in expiring:
            assert row.short_code is not None
            try:
                self.client.extend(row.short_code, until)
            except ShlinkError as e:
                raise LinkError(f"couldn't extend an expiring link: {e}") from e
            self.state.set_link_expiry(row.key, until)
        return len(expiring)

    # ---------- run ----------

    def run(
        self, recipients: dict[str, str | None], test_phone: str | None = None, team: Sequence[str] = (),
    ) -> LinkStageResult:
        """Make every link for `recipients` (phone → segment) and, with an
        approval test, the test SMS's own link, and one for each of the
        `team` numbers that get it too. Raises LinkError — before any SMS —
        if one isn't ready; ShlinkHaltError if the key or setup is wrong."""
        wanted: dict[str, tuple[str | None, bool]] = {}
        key_of: dict[str, str] = {}
        for phone, segment in recipients.items():
            key = self._key(phone, segment or "segment")
            key_of[phone] = key
            wanted.setdefault(key, (segment or "segment", False))
        test_key = f"test:{test_phone}" if test_phone else None
        if test_key:
            wanted[test_key] = (None, True)
        team_keys = {phone: f"test:{phone}" for phone in team if test_key and phone != test_phone}
        for key in team_keys.values():
            wanted[key] = (None, True)

        rows = self._write(wanted)
        todo = [r for r in rows.values() if r.status != LINK_READY]
        created = self._create_all(todo)
        rows = self.state.get_links(wanted)
        not_ready = [r for r in rows.values() if r.status != LINK_READY]
        if not_ready:
            if self._stop.is_set():
                raise LinkError(f"stopped with {len(not_ready)} link(s) not created yet")
            raise LinkError(
                f"{len(not_ready)} of {len(rows)} link(s) aren't ready (see the log and "
                "`status`); nothing was sent. Run again to retry them."
            )
        extended = self._extend_expiring(list(rows.values()))

        def token(row: LinkRow) -> str:
            assert row.short_url is not None and row.short_code is not None
            return token_value(self.settings.format, row.short_url, row.short_code)

        return LinkStageResult(
            needed=len(rows), created=created, extended=extended,
            tokens={phone: token(rows[key]) for phone, key in key_of.items()},
            keys=dict(key_of),
            test_token=token(rows[test_key]) if test_key else None,
            team_tokens={phone: token(rows[key]) for phone, key in team_keys.items()},
        )


def format_problem(fmt: str) -> str | None:
    """Why `fmt` can't be a link format, or None."""
    if fmt in FORMATS or (fmt.count(CODE) == 1 and not any(ch.isspace() for ch in fmt)):
        return None
    return (f"link format must be {' or '.join(FORMATS)}, or a pattern with {CODE} "
            f"once and no spaces, e.g. 'u/{CODE}'")


def token_value(fmt: str, short_url: str, short_code: str) -> str:
    """What goes in the template token: the whole short URL, the code, or
    the pattern with the code filled in."""
    if fmt == "url":
        return short_url
    if fmt == "code":
        return short_code
    return fmt.replace(CODE, short_code)


def placeholder_token(fmt: str, base_url: str) -> str:
    """What `preview` and `dry-run` show where the real link will go."""
    return token_value(fmt, f"{base_url.rstrip('/')}/<short-code>", "<short-code>")


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 90 else f"{minutes / 60:.1f} h"
