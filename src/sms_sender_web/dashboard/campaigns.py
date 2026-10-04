"""What the dashboard shows about each campaign, read from the campaign DBs
in data/db — one per campaign, the same files the CLI writes."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from sms_sender.state import StateSchemaError, StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CampaignSummary:
    slug: str                     # the DB file's name
    name: str                     # the campaign name it was bound to, else the slug
    template: str | None
    counts: dict[str, int]        # status → recipients; invalid input rows as `invalid`
    last_run_at: datetime | None
    links: dict[str, int] = field(default_factory=dict)

    @property
    def recipients(self) -> int:
        return sum(self.counts.values())


def read_campaign(path: Path) -> CampaignSummary:
    # Opening a DB upgrades an older one in place, exactly as the CLI does.
    store = StateStore(path)
    stored_settings = store.get_meta("settings")
    last_run = store.get_meta("last_run")
    at = json.loads(last_run).get("at") if last_run else None
    return CampaignSummary(
        slug=path.stem,
        name=store.get_meta("campaign") or path.stem,
        template=json.loads(stored_settings).get("template") if stored_settings else None,
        counts=store.display_counts(),
        last_run_at=datetime.fromtimestamp(at, timezone.utc) if at else None,
        links=store.link_counts(),
    )


def list_campaigns(db_dir: Path | str | None = None) -> list[CampaignSummary]:
    """Every readable campaign DB, the most recently run first."""
    folder = Path(db_dir or settings.SMS_SENDER_DB_DIR)
    if not folder.is_dir():
        return []
    campaigns = []
    for path in sorted(folder.glob("*.db")):
        try:
            campaigns.append(read_campaign(path))
        except (sqlite3.DatabaseError, StateSchemaError) as e:
            logger.warning("campaign_db_unreadable", extra={"path": str(path), "detail": str(e)})
    never = datetime.min.replace(tzinfo=timezone.utc)
    campaigns.sort(key=lambda c: (c.last_run_at or never, c.name), reverse=True)
    return campaigns
