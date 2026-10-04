"""Clicks: who opened their link, per campaign, segment and recipient.

Read-only at Shlink: each link's visit count with bots and link-preview
fetchers excluded (`visitsSummary.nonBots`). Shlink has had no webhooks
since 4.0, so counts are polled — `sms-sender clicks`, or the dashboard's
schedule — and stored on each link's row.

A recipient's clicks are their own link's (recipient strategy). Segment
and campaign links are shared, so their clicks belong to the segment or
the campaign, never to a person. Recipients without a user ID are counted
apart as "missing user ID" (decided 2026-10-04).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Protocol

from .delivery import STATUS_NAMES
from .shortlink import LinkVisits
from .state import StateStore

MISSING_USER_ID = "missing user ID"


class VisitSource(Protocol):
    def visits_by_tag(self, tag: str) -> Iterable[LinkVisits]: ...


@dataclass(frozen=True)
class ClickSync:
    links: int    # the campaign's links Shlink reported
    updated: int  # of ours, with a count stored
    clicks: int   # non-bot visits on them


@dataclass(frozen=True)
class SegmentClicks:
    segment: str
    sent: int
    clicks: int                    # own links + the segment's shared link
    clicked: int | None            # recipients who clicked; None: no personal links
    missing_user_id: int           # sent without a user ID
    clicked_missing_user_id: int | None


def sync_clicks(
    state: StateStore, source: VisitSource, campaign: str,
    now: Callable[[], float] = time.time,
) -> ClickSync:
    counts = {v.short_code: v.non_bots for v in source.visits_by_tag(f"campaign-{campaign}")}
    updated = state.record_clicks(counts, now())
    return ClickSync(links=len(counts), updated=updated, clicks=sum(counts.values()))


def click_report(state: StateStore) -> tuple[list[SegmentClicks], int]:
    """Per-segment clicks, and the clicks on a campaign-wide link (0 if none)."""
    shared = state.shared_link_clicks()
    segments = []
    for row in state.clicks_by_segment():
        personal = row["own"] > 0
        segments.append(SegmentClicks(
            segment=row["segment"] or "(none)",
            sent=row["sent"],
            clicks=row["clicks"] + shared.get(f"segment:{row['segment']}", 0),
            clicked=row["clicked"] if personal else None,
            missing_user_id=row["missing_user_id"],
            clicked_missing_user_id=row["clicked_missing_user_id"] if personal else None,
        ))
    return segments, shared.get("campaign", 0)


def describe(segments: list[SegmentClicks], campaign_clicks: int) -> list[str]:
    """Lines for the CLI; empty when nothing was sent."""
    lines = []
    for s in segments:
        line = f"  {s.segment}: sent {s.sent}, clicks {s.clicks}"
        if s.clicked is not None:
            rate = f" ({s.clicked / s.sent:.1%})" if s.sent else ""
            line += f", clicked {s.clicked}{rate}"
        if s.missing_user_id:
            line += f"; {MISSING_USER_ID}: {s.missing_user_id}"
            if s.clicked_missing_user_id is not None:
                line += f" ({s.clicked_missing_user_id} clicked)"
        lines.append(line)
    if campaign_clicks:
        lines.append(f"  campaign-wide link: clicks {campaign_clicks}")
    return lines


ATTRIBUTION_HEADER = [
    "ref", "user_id", "user_id_status", "segment", "short_url", "accepted_at",
    "delivery", "clicks",
]
CLICKERS_HEADER = ["phone", "user_id", "user_id_status", "segment", "ref", "clicks"]


def _user_id_status(user_id: str | None) -> str:
    return "ok" if user_id else MISSING_USER_ID


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


def attribution_rows(state: StateStore) -> Iterator[list]:
    """For the backend: which recipient each `r` belongs to. No phone numbers;
    dates in ISO 8601 (UTC), plain digits — machine input."""
    for r in state.iter_attribution():
        delivery = STATUS_NAMES.get(r["delivery_status"], str(r["delivery_status"])) \
            if r["delivery_status"] is not None else "not checked"
        yield [
            r["ref"], r["user_id"] or "", _user_id_status(r["user_id"]), r["segment"] or "",
            r["short_url"] or "", _iso(r["sent_at"]), delivery, r["clicks"] or 0,
        ]


def clicker_rows(state: StateStore) -> Iterator[list]:
    """Recipients who clicked their own link — with phone numbers."""
    for r in state.iter_clickers():
        yield [
            r["phone"], r["user_id"] or "", _user_id_status(r["user_id"]), r["segment"] or "",
            r["ref"], r["clicks"],
        ]
