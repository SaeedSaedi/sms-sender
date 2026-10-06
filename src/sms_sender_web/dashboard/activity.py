"""How sending has gone (plan 06, L4): for each campaign DB in data/db, the
CLI's too, what was accepted, delivered and clicked, and what it cost, over
today, the last 7 days, the last 30 and all time. One read-only pass per DB
(query_only, never upgrading or creating one), so the control room stays
quick; an unreadable file is skipped."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from sms_sender.window import TEHRAN

logger = logging.getLogger(__name__)

WINDOWS = ("today", "week", "month", "all")
DELIVERED = 10


@dataclass
class Totals:
    accepted: int = 0          # SMS Kavenegar accepted (sent rows)
    delivered: int = 0         # of those, delivered
    delivery_known: int = 0    # of those, with a delivery status from Kavenegar
    own_links: int = 0         # of those, sent a short link of their own
    clicked: int = 0           # of those, who clicked it
    clicks: int = 0            # visits to the campaign's links (click_hours)
    cost: int = 0              # rials: accepted SMS and test SMS

    def __iadd__(self, other: "Totals") -> "Totals":
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))
        return self

    @property
    def delivered_rate(self) -> float | None:
        return self.delivered / self.delivery_known if self.delivery_known else None

    @property
    def click_rate(self) -> float | None:
        return self.clicked / self.own_links if self.own_links else None


def starts(now: datetime | None = None) -> dict[str, float]:
    """Where each window starts, in unix seconds: today from midnight in
    Tehran, the week and the month as the last 7 and 30 days."""
    local = (now or timezone.now()).astimezone(TEHRAN)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": midnight.timestamp(),
        "week": (midnight - timedelta(days=6)).timestamp(),
        "month": (midnight - timedelta(days=29)).timestamp(),
        "all": 0.0,
    }


def _tables(conn) -> dict[str, set[str]]:
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
    return out


def _read(path: Path, since: dict[str, float]) -> dict[str, Totals]:
    totals = {w: Totals() for w in WINDOWS}
    # Not mode=ro: that can't open a WAL database whose -shm is gone.
    # query_only keeps it from writing anything.
    conn = sqlite3.connect(str(path), timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        tables = _tables(conn)
        columns = tables.get("recipients", set())
        if {"status", "sent_at"} <= columns:
            links = "links" in tables and "link_key" in columns
            parts = []
            for w in WINDOWS:
                t = f"r.sent_at >= :{w}"
                parts += [
                    f"SUM({t})",
                    f"SUM({t} AND r.delivery_status = {DELIVERED})" if "delivery_status" in columns else "0",
                    f"SUM({t} AND r.delivery_status IS NOT NULL)" if "delivery_status" in columns else "0",
                    f"SUM({t} AND r.link_key = r.phone)" if links else "0",
                    f"SUM({t} AND r.link_key = r.phone AND COALESCE(l.clicks, 0) > 0)" if links else "0",
                    f"SUM(CASE WHEN {t} THEN COALESCE(r.cost, 0) ELSE 0 END)" if "cost" in columns else "0",
                ]
            join = "LEFT JOIN links l ON l.key = r.link_key" if links else ""
            row = conn.execute(
                f"SELECT {', '.join(parts)} FROM recipients r {join} WHERE r.status = 'sent'", since,
            ).fetchone()
            for i, w in enumerate(WINDOWS):
                accepted, delivered, known, own, clicked, cost = (v or 0 for v in row[i * 6:(i + 1) * 6])
                totals[w] = Totals(accepted, delivered, known, own, clicked, 0, cost)
        if "attempts" in tables and "cost" in tables["attempts"]:
            for w in WINDOWS:
                (cost,) = conn.execute(
                    "SELECT COALESCE(SUM(cost), 0) FROM attempts WHERE kind = 'test' AND outcome = 'accepted' "
                    "AND started_at >= ?", (since[w],),
                ).fetchone()
                totals[w].cost += cost
        if "click_hours" in tables:
            for w in WINDOWS:
                (clicks,) = conn.execute(
                    "SELECT COALESCE(SUM(clicks), 0) FROM click_hours WHERE hour >= ?", (since[w],),
                ).fetchone()
                totals[w].clicks = clicks
    finally:
        conn.close()
    return totals


def folder_activity(folder: Path | str | None = None, now: datetime | None = None) -> dict[str, dict[str, Totals]]:
    """Each campaign DB's totals by window: {slug: {"today": Totals, …}}."""
    folder = Path(folder or settings.SMS_SENDER_DB_DIR)
    since = starts(now)
    out = {}
    for path in sorted(folder.glob("*.db")):
        try:
            out[path.stem] = _read(path, since)
        except sqlite3.Error as e:
            logger.warning("campaign_db_unreadable", extra={"path": str(path), "detail": str(e)})
    return out


def overall(activity: dict[str, dict[str, Totals]]) -> dict[str, Totals]:
    """Every campaign's totals added up, by window."""
    total = {w: Totals() for w in WINDOWS}
    for by_window in activity.values():
        for w in WINDOWS:
            total[w] += by_window[w]
    return total
