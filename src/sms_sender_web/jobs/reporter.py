"""A Runner reporter that writes into the job's rows, so the dashboard can
show live progress without a terminal."""
from __future__ import annotations

import threading
import time

from django.db import connection

from sms_sender.runner import SendCounts

from .models import Job, JobEvent


class JobReporter:
    """Notes become JobEvents; progress lands on `Job.progress`, at most
    once per `every` seconds (and once more at the end)."""

    def __init__(self, job: Job, every: float = 1.0):
        self.job_id = job.pk
        self.every = every
        self._last = 0.0
        self._progress: dict = {}
        self._links_started: float | None = None
        self.last_note: str | None = None  # e.g. why a run halted
        self._thread = threading.get_ident()

    def note(self, text: str, key: str = "note", **fields: object) -> None:
        """The engine's English stays in `text` (logs, developers); the page
        shows `key` and its `data` in Persian (campaigns/terms.NOTES)."""
        self.last_note = text
        try:
            JobEvent.objects.create(job_id=self.job_id, key=key, text=text, data=fields)
        finally:
            self._done_writing()

    def links(self, done: int, total: int) -> None:
        """The link stage's progress, with a time left from the pace so far
        (Shlink is rate-limited, so a big list takes minutes)."""
        now = time.monotonic()
        if done == 0 or self._links_started is None:
            self._links_started = now
        eta = None
        if done:
            eta = round((now - self._links_started) / done * (total - done))
        self._progress = {"stage": "links", "total": total, "processed": done, "eta_sec": eta}
        if done in (0, total) or now - self._last >= self.every:
            self._save()

    def start(self, total: int) -> None:
        self._progress = {"total": total, "processed": 0}
        self._save()

    def advance(self, counts: SendCounts) -> None:
        self._progress = {
            "total": counts.total,
            "processed": counts.processed + counts.already_done,
            "sent": counts.sent,
            "failed_permanent": counts.failed_permanent,
            "failed_retriable": counts.failed_retriable,
            "unknown": counts.unknown,
        }
        if time.monotonic() - self._last >= self.every:
            self._save()

    def finish(self) -> None:
        self._save()

    def _save(self) -> None:
        self._last = time.monotonic()
        try:
            Job.objects.filter(pk=self.job_id).update(progress=dict(self._progress))
        finally:
            self._done_writing()

    def _done_writing(self) -> None:
        """The engine's own threads (the link stage's) report too. Django
        opens a connection per thread and nothing closes theirs when they
        end, so it closes after each of their writes."""
        if threading.get_ident() != self._thread:
            connection.close()
