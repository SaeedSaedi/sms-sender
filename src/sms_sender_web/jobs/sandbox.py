"""Sandbox mode (spec 4.9, step 3.7): a simulated Kavenegar and Shlink, so
every page and job can be tried without sending anything.

On with SMS_SENDER_SANDBOX=1. Everything then lives in data/sandbox/ — the
app DB included — so simulated sends never mix with real campaigns, and a
worker that isn't in sandbox mode can't even see a sandbox job.

The simulation, so every state can be seen:
- every SMS is accepted and costs COST rials, except numbers ending in 000
  (rejected, code 411) and 999 (no answer: the outcome is unknown);
- delivery: delivered, except message IDs ending in 7 (undelivered);
- a lookup of an unknown message finds nothing;
- clicks: 0 to 3 per link, the same on every update.
"""
from __future__ import annotations

import hashlib
import itertools
import secrets
import string
import threading
import time

from sms_sender.sender import (
    AccountConfig,
    AccountInfo,
    PermanentSendError,
    ProviderMessage,
    SenderConfig,
    SendResult,
    UncertainSendError,
)
from sms_sender.shortlink import LinkVisits, ShortLink

COST = 3020  # rials: what the real test SMS cost on 2026-10-04
CREDIT = 1_000_000_000
BASE_URL = "https://sandbox.invalid/u"  # .invalid never resolves

_message_ids = itertools.count(int(time.time() * 1000))
_lock = threading.Lock()
# Links made in this process, by tag, so click updates have codes to count.
_codes_by_tag: dict[str, set[str]] = {}


class SandboxKavenegar:
    """Stands in for `sms_sender.sender.Sender`. Never makes a request."""

    def __init__(self, cfg: SenderConfig | None = None, delay: float = 0.05):
        self.cfg = cfg  # the runner logs cfg.template
        self.delay = delay  # so progress can be watched

    def send(self, phone: str, tokens: dict[str, str] | None = None) -> SendResult:
        time.sleep(self.delay)
        if phone.endswith("000"):
            raise PermanentSendError(411, "sandbox: this number is rejected")
        if phone.endswith("999"):
            raise UncertainSendError(None, "sandbox: no answer")
        with _lock:
            message_id = next(_message_ids)
        return SendResult(message_id=message_id, status_code=200, cost=COST)

    def account_info(self) -> AccountInfo:
        return AccountInfo(remaining_credit=CREDIT, expire_date=None, type="sandbox")

    def account_config(self) -> AccountConfig:
        return AccountConfig(debug_mode=False, resend_failed=False)

    def delivery_statuses(self, message_ids: list[int]) -> dict[int, int]:
        return {mid: 11 if mid % 10 == 7 else 10 for mid in message_ids}

    def find_messages(self, phone: str, start: float, end: float) -> list[ProviderMessage]:
        return []


class SandboxShlink:
    """Stands in for `sms_sender.shortlink.ShlinkClient`."""

    base_url = BASE_URL

    def create(self, *, long_url: str, title: str, tags: list[str], valid_until: str) -> ShortLink:
        code = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
        with _lock:
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

    def health(self) -> str:
        return "sandbox"
