"""Operations an admin runs from the dashboard (plan 05, P5): backups,
purging a campaign's records, adopting a campaign the CLI made. Each is
recorded where it's called."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings as django_settings
from django.db import transaction

from sms_sender.locking import RunLock
from sms_sender.state import StateStore

from ..backup import MANIFEST, list_backups, make_backup, verify
from ..jobs.engine import campaign_db
from ..jobs.models import Campaign, Job
from ..jobs.services import ACTIVE


@dataclass(frozen=True)
class BackupInfo:
    name: str
    made_at: datetime | None
    files: int
    bytes: int


def backups() -> list[BackupInfo]:
    """Complete backups, newest first, from their manifests."""
    out = []
    for path in reversed(list_backups(django_settings.BACKUP_DIR)):
        try:
            manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(BackupInfo(path.name, None, 0, 0))
            continue
        made = manifest.get("created_at")
        out.append(BackupInfo(
            path.name, datetime.fromisoformat(made) if made else None,
            len(manifest.get("files", [])), sum(f.get("bytes", 0) for f in manifest.get("files", [])),
        ))
    return out


def back_up_now() -> str:
    """A backup that prunes nothing (the scheduled ones keep their count)."""
    return make_backup(Path(django_settings.DATA_DIR), django_settings.BACKUP_DIR, keep=None).path.name


def check_backup(name: str) -> list[str] | None:
    """What's wrong with one backup ([] when it's whole); None if it isn't one."""
    path = next((p for p in list_backups(django_settings.BACKUP_DIR) if p.name == name), None)
    return None if path is None else verify(path)


class PurgeRefused(Exception):
    """A job of the campaign is on its way."""


def purge(campaign: Campaign) -> str:
    """The campaign's records and the campaign itself, after a backup (the
    CLI's purge, for a dashboard campaign). Refused while any of its jobs is
    queued, running or paused. Returns the backup's name."""
    if Job.objects.filter(campaign=campaign, state__in=ACTIVE).exists():
        raise PurgeRefused(campaign.slug)
    backup = back_up_now()
    db = campaign_db(campaign)
    with RunLock(db):
        for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            path.unlink(missing_ok=True)
    campaign.delete()  # its jobs and its own suppressions go with it
    return backup


def adoptable(slug: str) -> bool:
    db = Path(django_settings.SMS_SENDER_DB_DIR) / f"{slug}.db"
    return db.exists() and not Campaign.objects.filter(slug=slug).exists()


def adopt(slug: str, user) -> Campaign:
    """A dashboard campaign for a DB the CLI made: what it sends, as its DB
    recorded it. It has no segment yet, so it can't send until it gets one
    and a test SMS; its follow-ups and reports work at once."""
    store = StateStore(Path(django_settings.SMS_SENDER_DB_DIR) / f"{slug}.db")
    bound = json.loads(store.get_meta("settings") or "{}")
    settings = {
        "template": bound.get("template", ""), "tokens": bound.get("tokens", {}),
        "token_columns": bound.get("token_columns", {}), "value_maps": bound.get("value_maps", {}),
        "send_window": "08:00-21:00", "rate": None, "workers": 5,
    }
    if bound.get("links"):
        settings["links"] = {**bound["links"], "expiry_days": 7}
    with transaction.atomic():
        return Campaign.objects.create(slug=slug, name=store.get_meta("campaign") or slug, created_by=user,
                                       settings=settings)


def queue() -> dict:
    """The worker's queue at a glance, for the status page."""
    jobs = Job.objects.filter(state__in=ACTIVE)
    oldest = jobs.filter(state=Job.State.QUEUED, not_before__isnull=True).order_by("created_at").first()
    return {
        "queued": jobs.filter(state=Job.State.QUEUED).count(),
        "running": jobs.filter(state=Job.State.RUNNING).count(),
        "paused": jobs.filter(state=Job.State.PAUSED).count(),
        "oldest": oldest.created_at if oldest else None,
    }


def version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    try:
        return package_version("sms-sender")
    except PackageNotFoundError:
        return "?"


def last_backup() -> datetime | None:
    found = backups()
    return found[0].made_at if found else None


def stamp(name: str) -> datetime | None:
    """A backup folder's UTC stamp as a moment (manifests may be missing)."""
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
