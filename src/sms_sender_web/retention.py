"""How long phone numbers are kept (plan 06, D2): 12 months by default, an
admin's setting. Then:

- a campaign DB whose last send (or new number) is older keeps every count,
  cost, delivery, click and conversion, but no number
  (`StateStore.remove_numbers`). It never sends again: it no longer knows
  who it sent to. The CLI's campaign DBs in the folder count too;
- a segment's file that no campaign has used since is deleted. The segment
  keeps its counts and shows its numbers were removed;
- downloads (`exports/`) and backups older than that are deleted;
- the jobs' records (test numbers, notes, errors) and the activity log keep
  only masked numbers.

The suppression list stays as it is: it's what keeps those people from
getting SMS. Deleting by hand (a campaign's purge, a segment's delete)
stays as it was. The worker runs this once a day; nothing that's on its
way (a send, a test, even a paused send) is touched until it ends."""
from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings as django_settings
from django.utils import timezone

from sms_sender.locking import RunLock, RunLockError
from sms_sender.state import StateSchemaError, StateStore, scrub_numbers

from .privacy import mask_phone

logger = logging.getLogger(__name__)

MONTHS = (6, 12, 18, 24, 36)  # what an admin chooses from (6: still longer than the frequency cap's 90 days)
DEFAULT_MONTHS = 12
EVERY = timedelta(days=1)
RETRY = timedelta(hours=1)


def cutoff(now: datetime, months: int) -> datetime:
    """The moment before which numbers go."""
    return now - timedelta(days=months * 365 // 12)


@dataclass
class Removed:
    """What one run removed, or would remove (`dry_run`)."""
    campaigns: list[str] = field(default_factory=list)  # campaign DBs whose numbers went
    numbers: int = 0                                     # numbers taken out of them
    segments: list[str] = field(default_factory=list)   # segments whose files went
    exports: int = 0                                     # old downloads deleted
    backups: list[str] = field(default_factory=list)    # old backups deleted
    records: int = 0                                     # jobs, notes and activity-log events masked
    kept: list[str] = field(default_factory=list)       # old campaigns kept for now: a job is on its way

    @property
    def anything(self) -> bool:
        return bool(self.campaigns or self.segments or self.exports or self.backups or self.records)

    def as_dict(self) -> dict:
        return asdict(self)


def remove_old_numbers(
    now: datetime | None = None, months: int | None = None, *, dry_run: bool = False,
) -> Removed:
    """Remove every number older than `months` (the system setting by
    default). With `dry_run`, only say what would go."""
    from .system.models import SystemSettings

    now = now or timezone.now()
    before = cutoff(now, months or SystemSettings.load().retention_months)
    removed = Removed()
    _campaigns(before, removed, dry_run)
    _segments(before, now, removed, dry_run)
    _exports(before, removed, dry_run)
    _backups(before, removed, dry_run)
    _records(before, removed, dry_run)
    if removed.anything and not dry_run:
        logger.info("numbers_removed", extra={
            "campaigns": len(removed.campaigns), "numbers": removed.numbers, "segments": len(removed.segments),
            "exports": removed.exports, "backups": len(removed.backups), "records": removed.records,
        })
    return removed


# ---------- campaign DBs ----------

def _stores() -> list[tuple[Path, StateStore]]:
    folder = Path(django_settings.SMS_SENDER_DB_DIR)
    out = []
    for path in sorted(folder.glob("*.db")) if folder.is_dir() else []:
        try:
            out.append((path, StateStore(path)))
        except (sqlite3.DatabaseError, StateSchemaError) as e:
            logger.warning("campaign_db_unreadable", extra={"path": str(path), "detail": str(e)})
    return out


def _busy_slugs() -> set[str]:
    """Campaigns with a job queued, running or paused: never touched."""
    from .jobs.models import Job
    from .jobs.services import ACTIVE

    return set(Job.objects.filter(state__in=ACTIVE).values_list("campaign__slug", flat=True))


def _campaigns(before: datetime, removed: Removed, dry_run: bool) -> None:
    busy = _busy_slugs()
    for path, store in _stores():
        if store.numbers_removed_at() is not None:
            continue
        last = store.last_activity()
        if last is None or last >= before.timestamp():
            continue
        if path.stem in busy:
            removed.kept.append(path.stem)
            continue
        if dry_run:
            removed.campaigns.append(path.stem)
            continue
        lock = RunLock(path)
        try:
            lock.acquire()
        except RunLockError:
            removed.kept.append(path.stem)  # the CLI is using it: next time
            continue
        try:
            removed.numbers += store.remove_numbers()
        finally:
            lock.release()
        removed.campaigns.append(path.stem)
        logger.info("campaign_numbers_removed", extra={"campaign": path.stem})


# ---------- segment files ----------

def _used_since(before: datetime) -> set[str]:
    """Segments some campaign still wants: one made or asked to test or send
    since `before`, or with a job on its way; and segments a campaign DB has
    added or sent to since then (the CLI's too)."""
    from .jobs.models import Campaign, Job
    from .jobs.services import ACTIVE
    from .segments.models import campaign_slugs

    used: set[str] = set()
    recent = set(Job.objects.filter(created_at__gte=before).values_list("campaign_id", flat=True))
    recent |= set(Job.objects.filter(state__in=ACTIVE).values_list("campaign_id", flat=True))
    for campaign in Campaign.objects.all():
        if campaign.created_at >= before or campaign.pk in recent:
            used.update(campaign_slugs(campaign.settings))
    for _path, store in _stores():
        if store.numbers_removed_at() is None:
            used.update(store.segments_active_since(before.timestamp()))
    return used


def _segments(before: datetime, now: datetime, removed: Removed, dry_run: bool) -> None:
    from .segments.models import Segment

    used = _used_since(before)
    for segment in Segment.objects.exclude(status=Segment.Status.REMOVED):
        if segment.slug in used:
            continue
        files = [p for p in (segment.path, segment.upload_path) if p.exists()]
        # A replaced file is newer than the segment: its own time counts.
        newest = max([segment.uploaded_at, *(
            datetime.fromtimestamp(p.stat().st_mtime, dt_timezone.utc) for p in files
        )])
        if newest >= before:
            continue
        removed.segments.append(segment.slug)
        if dry_run:
            continue
        segment.delete_files()
        segment.status, segment.numbers_removed_at = Segment.Status.REMOVED, now
        segment.save(update_fields=["status", "numbers_removed_at"])


# ---------- downloads and backups ----------

def _exports(before: datetime, removed: Removed, dry_run: bool) -> None:
    folder = Path(django_settings.DATA_DIR) / "exports"
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if path.is_file() and path.stat().st_mtime < before.timestamp():
            removed.exports += 1
            if not dry_run:
                path.unlink(missing_ok=True)


def _backups(before: datetime, removed: Removed, dry_run: bool) -> None:
    """A backup holds every number of its day: one older than the period
    goes, the daily ones and those taken before a risky change alike."""
    from .system import operations

    for backup in operations.backups():
        made = backup.made_at or operations.stamp(backup.name)
        if made is None or made >= before:
            continue
        removed.backups.append(backup.name)
        if not dry_run:
            shutil.rmtree(Path(django_settings.BACKUP_DIR) / backup.name, ignore_errors=True)


# ---------- the app's own records ----------

def _masked(value):
    """A whole number masked (one already masked is left as it is)."""
    if isinstance(value, str) and sum(ch.isdigit() for ch in value) >= 10:
        return mask_phone(value)
    return value


def _records(before: datetime, removed: Removed, dry_run: bool) -> None:
    """Test numbers in old jobs, numbers in their notes and errors, and
    numbers in old activity-log events: masked, as the pages show them."""
    from .audit.models import AuditEvent
    from .jobs.models import Job, JobEvent

    for job in Job.objects.filter(created_at__lt=before):
        params = dict(job.params or {})
        if "test_number" in params:
            params["test_number"] = _masked(params["test_number"])
        if params.get("team_numbers"):
            params["team_numbers"] = [_masked(p) for p in params["team_numbers"]]
        result = dict(job.result or {})
        if result.get("top_errors"):
            result["top_errors"] = [[scrub_numbers(m), n] for m, n in result["top_errors"]]
        error = scrub_numbers(job.last_error)
        if (params, result, error) != (job.params or {}, job.result or {}, job.last_error):
            removed.records += 1
            if not dry_run:
                Job.objects.filter(pk=job.pk).update(params=params, result=result, last_error=error)
    # Notes about a number carry it in `phone` (and their English text).
    for event in JobEvent.objects.filter(at__lt=before, data__has_key="phone"):
        data = dict(event.data or {})
        if "phone" in data:
            data["phone"] = _masked(data["phone"])
        text = scrub_numbers(event.text)
        if (data, text) != (event.data or {}, event.text):
            removed.records += 1
            if not dry_run:
                JobEvent.objects.filter(pk=event.pk).update(data=data, text=text)
    # The log is append-only; only the numbers in old events are masked.
    for event in AuditEvent.objects.filter(at__lt=before, detail__has_key="phone"):
        detail = {k: _masked(v) if k == "phone" else v for k, v in (event.detail or {}).items()}
        if detail != (event.detail or {}):
            removed.records += 1
            if not dry_run:
                AuditEvent.objects.filter(pk=event.pk).update(detail=detail)


# ---------- once a day (the worker) ----------

def due(now: datetime) -> bool:
    from .system.models import SystemSettings

    ran = SystemSettings.load().retention_ran_at
    return ran is None or now - ran >= EVERY


def run_and_record(now: datetime | None = None) -> Removed:
    """The worker's daily run: remove, keep what was done on the system
    settings, and tell the activity log and the notification targets when
    anything went."""
    from sms_sender.notify import notify_text

    from .audit.record import record
    from .system.models import SystemSettings

    now = now or timezone.now()
    current = SystemSettings.load()
    removed = remove_old_numbers(now, current.retention_months)
    SystemSettings.objects.filter(pk=current.pk).update(retention_ran_at=now, retention_result=removed.as_dict())
    if removed.anything:
        record("numbers_removed", username="worker", months=current.retention_months,
               campaigns=len(removed.campaigns), numbers=removed.numbers, segments=len(removed.segments),
               exports=removed.exports, backups=len(removed.backups), records=removed.records)
        line = (f"sms-sender: phone numbers older than {current.retention_months} months were removed: "
                f"{len(removed.campaigns)} campaign(s), {len(removed.segments)} segment file(s), "
                f"{removed.exports} download(s), {len(removed.backups)} backup(s). Counts stay.")
        for target in current.notify_targets or []:
            notify_text(target, line)  # best-effort: never raises
    return removed
