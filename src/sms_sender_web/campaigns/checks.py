"""The check step (spec 3, stage 1): what a send would do right now, read
only — no API call, and the campaign DB isn't created or changed."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from django.conf import settings as django_settings

from sms_sender import input_loader
from sms_sender.allowlist import allowlist
from sms_sender.input_loader import InputError, TokenColumns
from sms_sender.links import allowed_domains, destination_issue
from sms_sender.shortlink import shlink_base_url
from sms_sender.state import CAPPED, CLAIMABLE, SENT, StateStore, folder_sends_since
from sms_sender.window import now_tehran, parse_window

from ..jobs.engine import campaign_db
from ..jobs.models import Campaign
from ..segments.models import campaign_slugs, ready_segments
from ..suppression.service import phones_for
from ..system.models import SystemSettings


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)  # keys that stop a send
    valid: int = 0
    invalid: dict[str, int] = field(default_factory=dict)  # by reason key
    duplicates: int = 0
    missing_user_id: int = 0
    suppressed: int = 0
    capped: int = 0        # over the frequency cap, held back by the next send
    cap: str = ""          # the cap, when one is set ("2/7")
    already_sent: int = 0
    settled: int = 0       # in the campaign DB and not to be sent again
    to_send: int = 0
    window_open: bool = True
    # Each segment the send reads, in order, with the valid numbers it adds
    # (a number in two segments counts for the first).
    segments: list[tuple[str, int]] = field(default_factory=list)
    missing_segments: list[str] = field(default_factory=list)  # more segments not ready
    # Restricted sending (allowlist.py): how many of those to send to aren't
    # allowed numbers; None while it's off (always, in the sandbox).
    not_allowed: int | None = None

    @property
    def invalid_total(self) -> int:
        return sum(self.invalid.values())

    @property
    def ok(self) -> bool:
        return not self.problems


def check_campaign(campaign: Campaign, loaded=None) -> CheckResult:
    """`loaded`: the segment already read with the campaign's settings
    (preview.load_segment), so a page reads it once."""
    s = campaign.settings or {}
    result = CheckResult()
    slugs = campaign_slugs(s)
    segments = ready_segments(slugs)
    if not s.get("segment") or segments[0] is None:
        result.problems.append("segment_missing")
        return result
    result.missing_segments = [slug for slug, seg in zip(slugs[1:], segments[1:]) if seg is None]
    if result.missing_segments:
        result.problems.append("more_segment_missing")
        return result
    if not s.get("template"):
        result.problems.append("no_template")
    token_columns = (
        TokenColumns(columns=dict(s["token_columns"]), value_maps=dict(s.get("value_maps") or {}))
        if s.get("token_columns") else None
    )
    if loaded is None:
        try:
            loaded = input_loader.load_parts([seg.part() for seg in segments], token_columns)
        except InputError:
            result.problems.append("columns_missing")
            return result
    added = Counter(loaded.segments.values())
    result.segments = [(seg.name, added.get(seg.slug, 0)) for seg in segments]
    result.valid = len(loaded.valid)
    result.invalid = dict(Counter(row.key for row in loaded.invalid))
    result.duplicates = loaded.duplicates_collapsed
    result.missing_user_id = loaded.missing_user_id

    phones = {r.phone for r in loaded.valid}
    suppressed = phones & phones_for(campaign)
    result.suppressed = len(suppressed)
    statuses: dict[str, str] = {}
    db = campaign_db(campaign)
    if db.exists():
        statuses = StateStore(db).status_for_phones(phones)
    result.already_sent = sum(1 for status in statuses.values() if status == SENT)
    result.settled = sum(1 for status in statuses.values() if status not in CLAIMABLE)
    waiting = {phone for phone in phones - suppressed if statuses.get(phone, CLAIMABLE[0]) in CLAIMABLE
               or statuses.get(phone) == CAPPED}
    cap = SystemSettings.load().frequency_cap
    if cap is not None:
        # As the next send counts it: across every campaign DB, this one too.
        sends = folder_sends_since(campaign_db(campaign).parent, time.time() - cap.seconds)
        over = {phone for phone in waiting if sends.get(phone, 0) >= cap.sms}
        result.capped, result.cap = len(over), str(cap)
        waiting -= over
    result.to_send = len(waiting)
    if result.to_send == 0:
        result.problems.append("nobody_to_send")
    allowed = None if django_settings.SANDBOX else allowlist()
    if allowed is not None:
        if allowed.invalid:
            result.problems.append("allowlist_invalid")  # not even a test SMS would go
        result.not_allowed = sum(1 for phone in waiting if not allowed.allows(phone))

    links = s.get("links")
    if links and destination_issue(links.get("destination", ""), allowed_domains(), shlink_base_url()):
        result.problems.append("bad_destination")
    window = parse_window(s.get("send_window"))
    result.window_open = window is None or window.contains(now_tehran())
    return result
