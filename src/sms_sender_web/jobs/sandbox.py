"""Sandbox mode (spec 4.9, step 3.7): a simulated Kavenegar and Shlink, so
every page and job can be tried without sending anything.

On with SMS_SENDER_SANDBOX=1. Everything then lives in data/sandbox/ — the
app DB included — so simulated sends never mix with real campaigns, and a
worker that isn't in sandbox mode can't even see a sandbox job.

The simulation, so every state can be seen:
- every SMS is accepted and costs COST rials, except numbers ending in 000
  (rejected, code 411) and 999: accepted, but the reply is lost, so the row
  becomes `unknown` and reconciliation has to find it;
- every accepted SMS goes into the outbox, data/sandbox/sandbox-outbox.jsonl,
  with its final tokens: what would have been sent, and how often. Lookups
  (reconciliation) read it, like Kavenegar's own records;
- each call is reported to `on_attempt`, as the real Sender does, so the
  campaign DB's call records (and `sms-sender check-sends`) can be tried;
- delivery: delivered, except message IDs ending in 7 (undelivered);
- clicks: 0 to 3 per link, the same on every update.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from django.conf import settings as django_settings

from sms_sender.sender import (
    TOKEN_MAX_SPACES,
    AccountConfig,
    AccountInfo,
    Attempt,
    PermanentSendError,
    ProviderMessage,
    SenderConfig,
    SendResult,
    UncertainSendError,
)
from sms_sender.shortlink import LinkVisits, ShortLink

logger = logging.getLogger(__name__)

COST = 3020  # rials: what the real test SMS cost on 2026-10-04
CREDIT = 1_000_000_000
BASE_URL = "https://sandbox.invalid/u"  # .invalid never resolves

_message_ids = itertools.count(int(time.time() * 1000))
_lock = threading.Lock()
# Links made in this process, by tag, so click updates have codes to count.
_codes_by_tag: dict[str, set[str]] = {}
_created: dict[str, float] = {}  # code → when the link was made (for its visits' times)


def outbox_path() -> Path:
    return Path(django_settings.DATA_DIR) / "sandbox-outbox.jsonl"


def _record(entry: dict) -> None:
    """One line per accepted SMS. Appends are atomic, so the worker's threads
    and a restarted worker share the file safely."""
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _lock, outbox_path().open("a", encoding="utf-8") as f:
        f.write(line)


def read_outbox() -> list[dict]:
    try:
        text = outbox_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class SandboxKavenegar:
    """Stands in for `sms_sender.sender.Sender`. Never makes a request."""

    def __init__(
        self, cfg: SenderConfig | None = None, *,
        on_attempt: Callable[[Attempt], None] | None = None, delay: float = 0.05,
    ):
        self.cfg = cfg  # the runner logs cfg.template
        self.on_attempt = on_attempt
        self.delay = delay  # so progress can be watched

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        started = time.time()
        time.sleep(self.delay)
        if phone.endswith("000"):
            self._report(phone, "rejected", started, status_code=411)
            raise PermanentSendError(411, "sandbox: this number is rejected")
        with _lock:
            message_id = next(_message_ids)
        # The tokens Kavenegar would fill in: the static ones, then the row's.
        final = {name: getattr(self.cfg, name) for name in TOKEN_MAX_SPACES
                 if self.cfg is not None and getattr(self.cfg, name)}
        final.update(tokens or {})
        _record({"at": time.time(), "phone": phone, "message_id": message_id,
                 "template": self.cfg.template if self.cfg else "", "tokens": final})
        if phone.endswith("999"):
            # The real Sender learns no message ID when the reply is lost.
            self._report(phone, "unknown", started)
            raise UncertainSendError(None, "sandbox: accepted, but the reply was lost")
        self._report(phone, "accepted", started, status_code=200, message_id=message_id, cost=COST)
        return SendResult(message_id=message_id, status_code=200, cost=COST)

    def _report(self, phone: str, outcome: str, started: float, **fields) -> None:
        if self.on_attempt is None:
            return
        try:
            self.on_attempt(Attempt(phone=phone, outcome=outcome, started_at=started,
                                    finished_at=time.time(), **fields))
        except Exception:  # noqa: BLE001 — as in Sender: never changes the outcome
            logger.exception("attempt_record_failed", extra={"phone": phone})

    def account_info(self) -> AccountInfo:
        return AccountInfo(remaining_credit=CREDIT, expire_date=None, type="sandbox")

    def account_config(self) -> AccountConfig:
        return AccountConfig(debug_mode=False, resend_failed=False)

    def delivery_statuses(self, message_ids: list[int]) -> dict[int, int]:
        return {mid: 11 if mid % 10 == 7 else 10 for mid in message_ids}

    def find_messages(self, phone: str, start: float, end: float) -> list[ProviderMessage]:
        """What the outbox holds for this number in the window, as Kavenegar's
        sms/statusbyreceptor would answer."""
        return [
            ProviderMessage(entry["message_id"], 11 if entry["message_id"] % 10 == 7 else 10)
            for entry in read_outbox()
            if entry["phone"] == phone and start <= entry["at"] <= end
        ]


class SandboxShlink:
    """Stands in for `sms_sender.shortlink.ShlinkClient`."""

    base_url = BASE_URL

    def create(self, *, long_url: str, title: str, tags: list[str], valid_until: str) -> ShortLink:
        code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
        with _lock:
            _created[code] = time.time()
            for tag in tags:
                _codes_by_tag.setdefault(tag, set()).add(code)
        return ShortLink(short_code=code, short_url=f"{BASE_URL}/{code}", long_url=long_url,
                         valid_until=valid_until)

    def extend(self, short_code: str, valid_until: str) -> None:
        return None

    def visits_by_tag(self, tag: str) -> list[LinkVisits]:
        with _lock:
            codes = sorted(_codes_by_tag.get(tag, ()))
        visits = []
        for code in codes:
            clicks = hashlib.sha256(code.encode()).digest()[0] % 4
            visits.append(LinkVisits(short_code=code, total=clicks + 1, non_bots=clicks))
        return visits

    def visit_times(self, tag: str, *, since: datetime | None = None) -> list[datetime]:
        """The same clicks as visits_by_tag, each at a fixed moment within 36
        hours of its link being made, and only once that moment has come."""
        with _lock:
            codes = sorted((code, _created.get(code, 0.0)) for code in _codes_by_tag.get(tag, ()))
        now = datetime.now(timezone.utc)
        out = []
        for code, created in codes:
            digest = hashlib.sha256(code.encode()).digest()
            for i in range(digest[0] % 4):
                when = datetime.fromtimestamp(created, tz=timezone.utc) + timedelta(
                    hours=digest[1 + i] % 36, minutes=digest[5 + i] % 60,
                )
                if when <= now and (since is None or when >= since):
                    out.append(when)
        return sorted(out)

    def health(self) -> str:
        return "sandbox"
