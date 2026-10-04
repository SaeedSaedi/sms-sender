"""The check step (spec 3, stage 1): what a send would do right now, read
only — no API call, and the campaign DB isn't created or changed."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sms_sender import input_loader
from sms_sender.input_loader import InputError, TokenColumns
from sms_sender.links import allowed_domains, destination_issue
from sms_sender.shortlink import shlink_base_url
from sms_sender.state import CLAIMABLE, SENT, StateStore
from sms_sender.window import now_tehran, parse_window

from ..jobs.engine import campaign_db
from ..jobs.models import Campaign
from ..segments.models import Segment
from ..suppression.service import phones_for


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)  # keys that stop a send
    valid: int = 0
    invalid: dict[str, int] = field(default_factory=dict)  # by reason key
    duplicates: int = 0
    missing_user_id: int = 0
    suppressed: int = 0
    already_sent: int = 0
    settled: int = 0       # in the campaign DB and not to be sent again
    to_send: int = 0
    window_open: bool = True

    @property
    def invalid_total(self) -> int:
        return sum(self.invalid.values())

    @property
    def ok(self) -> bool:
        return not self.problems


def check_campaign(campaign: Campaign) -> CheckResult:
    s = campaign.settings or {}
    result = CheckResult()
    segment = Segment.objects.filter(slug=s.get("segment"), status=Segment.Status.READY).first()
    if segment is None or not segment.path.exists():
        result.problems.append("segment_missing")
        return result
    if not s.get("template"):
        result.problems.append("no_template")
    token_columns = (
        TokenColumns(columns=dict(s["token_columns"]), value_maps=dict(s.get("value_maps") or {}))
        if s.get("token_columns") else None
    )
    try:
        loaded = input_loader.load(segment.path, token_columns, segment.user_id_column or None)
    except InputError:
        result.problems.append("columns_missing")
        return result
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
    result.to_send = sum(
        1 for phone in phones - suppressed if statuses.get(phone, CLAIMABLE[0]) in CLAIMABLE
    )
    if result.to_send == 0:
        result.problems.append("nobody_to_send")

    links = s.get("links")
    if links and destination_issue(links.get("destination", ""), allowed_domains(), shlink_base_url()):
        result.problems.append("bad_destination")
    window = parse_window(s.get("send_window"))
    result.window_open = window is None or window.contains(now_tehran())
    return result
