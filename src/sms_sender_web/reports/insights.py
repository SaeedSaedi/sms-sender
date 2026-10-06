"""Insights (plan 06, L5): numbers with something to compare them with.

- A campaign: its figures per segment, how fast its SMS were delivered,
  and, for an alert, each figure against the other alerts of its preset.
- A series: every alert of one preset over time, and whether it's getting
  more or less effective.
- The audience: how many SMS each person got in 7 and 30 days (the
  frequency cap's view), the best hour to send from your own clicks, and
  how far segments overlap.

Read only, and numbers only: no phone number leaves this module. Campaign
DBs are read with query_only (as the control room does) or through the
report's own StateStore, never created."""
from __future__ import annotations

import csv
import logging
import sqlite3
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from sms_sender.clicks import SegmentClicks
from sms_sender.frequency import FrequencyCap
from sms_sender.phone import InvalidPhoneError, normalize
from sms_sender.state import StateStore, folder_sends_since
from sms_sender.window import TEHRAN

from ..campaigns.present import say
from ..dashboard.activity import Totals, all_time
from ..dashboard.templatetags.fa import fa_digits, fa_percent, jalali
from ..jobs.models import Campaign
from ..segments.models import Segment
from .terms import (
    CHANGE, FATIGUE_ROWS, METRIC_LABELS, RIALS, TREND, WITHIN_HOURS,
)

logger = logging.getLogger(__name__)


def _db(slug: str) -> Path:
    return Path(settings.SMS_SENDER_DB_DIR) / f"{slug}.db"


def _decimal(x: float) -> str:
    """2.14 → «۲٫۱»."""
    return fa_digits(f"{x:.1f}".replace(".", "٫"))


# ---------- comparing figures ----------

# (Totals property, how it's shown, whether more is better)
METRICS = (
    ("delivered_rate", "rate", True),
    ("click_rate", "rate", True),
    ("cost_per_sms", "rials", False),
    ("cost_per_click", "rials", False),
)


def show(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    return fa_percent(value) if kind == "rate" else say(RIALS, {"n": round(value)})


def figures(t: Totals) -> dict:
    """A campaign's (or a series') figures as shown."""
    return {
        "accepted": t.accepted, "cost": t.cost,
        "delivered": show(t.delivered_rate, "rate"), "click_rate": show(t.click_rate, "rate"),
        "per_sms": show(t.cost_per_sms, "rials"), "per_click": show(t.cost_per_click, "rials"),
    }


@dataclass(frozen=True)
class Change:
    text: str   # «۲٫۱ واحد درصد بیشتر», «۱۲٪ گران‌تر», «تقریباً برابر»
    tone: str   # success (better), danger (worse), neutral


def change(value: float, base: float, kind: str, higher_better: bool) -> Change:
    """How `value` stands against `base`: rates in percentage points, costs
    in percent of the base."""
    if kind == "rate":
        diff = (value - base) * 100
        if abs(diff) < 0.05:
            return Change(str(CHANGE["same"]), "neutral")
        text = say(CHANGE["points_up" if diff > 0 else "points_down"], {"n": _decimal(abs(diff))})
    else:
        diff = (value - base) / base * 100 if base else 0
        if abs(diff) < 1:
            return Change(str(CHANGE["same"]), "neutral")
        text = say(CHANGE["dearer" if diff > 0 else "cheaper"], {"n": round(abs(diff))})
    return Change(text, "success" if (diff > 0) == higher_better else "danger")


@dataclass(frozen=True)
class Versus:
    key: str
    label: str
    value: str
    average: str
    change: Change


def _versus(mine: Totals, base: Totals) -> list[Versus]:
    out = []
    for key, kind, higher_better in METRICS:
        value, average = getattr(mine, key), getattr(base, key)
        if value is None or average is None:
            continue
        out.append(Versus(key, str(METRIC_LABELS[key]), show(value, kind), show(average, kind),
                          change(value, average, kind, higher_better)))
    return out


# ---------- one campaign ----------

@dataclass(frozen=True)
class SegmentRow:
    segment: str
    name: str
    sent: int
    delivered: int
    delivery_known: int
    clicked: int | None    # None: nobody in it had a link of their own
    clicks: int
    cost: int

    @property
    def delivered_rate(self) -> float | None:
        return self.delivered / self.delivery_known if self.delivery_known else None

    @property
    def click_rate(self) -> float | None:
        return self.clicked / self.sent if self.clicked is not None and self.sent else None

    @property
    def cost_per_click(self) -> float | None:
        return self.cost / self.clicks if self.clicks and self.cost else None


def by_segment(store: StateStore, clicks: list[SegmentClicks]) -> list[SegmentRow]:
    """Each segment's accepted SMS, delivery, clicks (`click_report`'s, which
    the report has already) and cost; only when the campaign went to more
    than one (else it's the campaign's own figures)."""
    if len(clicks) < 2:
        return []
    totals = store.segment_totals()
    names = dict(Segment.objects.values_list("slug", "name"))
    rows = []
    for c in clicks:
        key = "" if c.segment == "(none)" else c.segment
        t = totals.get(key)
        rows.append(SegmentRow(
            segment=key, name=names.get(key) or key or "—", sent=c.sent,
            delivered=(t["delivered"] or 0) if t else 0, delivery_known=(t["delivery_known"] or 0) if t else 0,
            clicked=c.clicked, clicks=c.clicks, cost=t["cost"] if t else 0,
        ))
    return rows


# Hours after sending at which the delivered share is read.
MARKS = (1, 2, 4, 8, 24, 48)


def delivery_speed(store: StateStore) -> list[dict]:
    """The share of accepted SMS seen delivered within 1, 2, 4, 8, 24 and 48
    hours of sending: as the delivery checks (every 15 minutes, for 48
    hours) first saw them. Empty before the first delivered one."""
    sent, within = store.delivery_speed([h * 3600 for h in MARKS])
    if not sent or not within[-1]:
        return []
    return [{"label": say(WITHIN_HOURS, {"h": h}), "n": n, "share": round(100 * n / sent)}
            for h, n in zip(MARKS, within)]


def against_preset(campaign: Campaign | None) -> dict | None:
    """An alert's figures against the other alerts of its preset that sent
    anything (pooled: every SMS counts once). None for a campaign without a
    preset, or before there's anything to compare."""
    if campaign is None or campaign.preset_id is None:
        return None
    mine = all_time(_db(campaign.slug))
    if mine is None or not mine.accepted:
        return None
    base, n = Totals(), 0
    for other in Campaign.objects.filter(preset_id=campaign.preset_id).exclude(pk=campaign.pk).only("slug"):
        totals = all_time(_db(other.slug))
        if totals is not None and totals.accepted:
            base += totals
            n += 1
    rows = _versus(mine, base) if n else []
    if not rows:
        return None
    return {"preset": campaign.preset, "n": n, "rows": rows,
            "url": reverse("series_detail", args=[campaign.preset.slug])}


# ---------- a series: the alerts of one preset ----------

@dataclass(frozen=True)
class Alert:
    campaign: Campaign
    url: str
    when: float | None   # its first accepted SMS (unix seconds)
    totals: Totals


def _first_sent(path: Path) -> float | None:
    conn = sqlite3.connect(str(path), timeout=5)  # the caller made sure it exists
    try:
        conn.execute("PRAGMA query_only = ON")
        return conn.execute("SELECT MIN(sent_at) FROM recipients WHERE status = 'sent'").fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def alerts_of(preset) -> list[Alert]:
    """The preset's alerts that sent anything, the oldest first."""
    out = []
    for campaign in Campaign.objects.filter(preset=preset):
        path = _db(campaign.slug)
        totals = all_time(path)
        if totals is None or not totals.accepted:
            continue
        out.append(Alert(campaign, reverse("report", args=[campaign.slug]), _first_sent(path), totals))
    out.sort(key=lambda a: (a.when or 0, a.campaign.pk))
    return out


RECENT = 3  # a trend compares the latest alerts with the ones before them


def trends(alerts: list[Alert]) -> list[dict]:
    """For each figure: the latest RECENT alerts against those before them,
    in a sentence. Needs at least RECENT + 1 alerts."""
    if len(alerts) <= RECENT:
        return []
    newer, older = Totals(), Totals()
    for a in alerts[-RECENT:]:
        newer += a.totals
    for a in alerts[:-RECENT]:
        older += a.totals
    out = []
    for key, kind, higher_better in METRICS:
        now, before = getattr(newer, key), getattr(older, key)
        if now is None or before is None:
            continue
        moved = change(now, before, kind, higher_better)
        text = say(TREND, {"what": METRIC_LABELS[key], "n": RECENT, "now": show(now, kind),
                           "before": show(before, kind), "change": moved.text})
        out.append({"key": key, "tone": moved.tone, "label": METRIC_LABELS[key], "change": moved.text,
                    "text": text})
    return out


def pooled(alerts: list[Alert]) -> Totals:
    total = Totals()
    for a in alerts:
        total += a.totals
    return total


def series_points(alerts: list[Alert]) -> tuple[str, list[tuple[str, float | None]]]:
    """What the series' chart draws: click rate where people had links of
    their own, else the delivered share; one bar per alert."""
    key = "click_rate" if any(a.totals.own_links for a in alerts) else "delivered_rate"
    return key, [(jalali(a.when, "%Y/%m/%d") if a.when else a.campaign.slug, getattr(a.totals, key))
                 for a in alerts]


# ---------- the audience ----------

def fatigue(cap: FrequencyCap | None, now: datetime | None = None) -> dict:
    """How many SMS each person got in the last 7 and 30 days, across every
    campaign (accepted sends; test SMS aside), and how many are at the
    frequency cap now, so a send today would hold them back."""
    now = now or timezone.now()
    folder = Path(settings.SMS_SENDER_DB_DIR)
    counts = {days: folder_sends_since(folder, now.timestamp() - days * 86400) for days in (7, 30)}
    # People by how many SMS they got: 1, 2, 3, 4, then 5 or more.
    spread = {days: Counter(min(n, len(FATIGUE_ROWS)) for n in c.values()) for days, c in counts.items()}
    rows = []
    for i, label in enumerate(FATIGUE_ROWS, start=1):
        row = {"label": label}
        for days, c in counts.items():
            row[f"d{days}"] = spread[days][i]
            row[f"s{days}"] = round(100 * spread[days][i] / len(c)) if c else 0
        rows.append(row)
    at_cap = None
    if cap is not None:
        window = counts.get(cap.days)
        if window is None:
            window = folder_sends_since(folder, now.timestamp() - cap.seconds)
        at_cap = sum(1 for n in window.values() if n >= cap.sms)
    return {
        "people": {days: len(c) for days, c in counts.items()},
        "sms": {days: sum(c.values()) for days, c in counts.items()},
        "rows": rows, "cap": cap, "at_cap": at_cap,
    }


MIN_SENT = 100  # people sent in an hour before its click rate is compared


def _tables(conn) -> dict[str, set[str]]:
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}
    return out


def send_hours(now: datetime | None = None) -> dict | None:
    """By hour of the day (Tehran), across every campaign: the people sent a
    link of their own at that hour and the share who clicked it, and the
    clicks made in that hour. The best hour to send is the one with the
    highest share, among hours with at least MIN_SENT people. None before
    anything was sent.

    Clicks are stored per UTC hour (`clicks._sync_hours`), which in Tehran
    runs from half past to half past; each is put under the Tehran hour it
    starts in, as the report's clicks chart does, so the page says "give or
    take half an hour"."""
    offset = int((now or timezone.now()).astimezone(TEHRAN).utcoffset().total_seconds())
    sent: Counter[int] = Counter()
    clicked: Counter[int] = Counter()
    clicks: Counter[int] = Counter()
    hour_of = "CAST(((CAST({} AS INTEGER) + :offset) % 86400) / 3600 AS INTEGER)"
    for path in sorted(Path(settings.SMS_SENDER_DB_DIR).glob("*.db")):
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            try:
                conn.execute("PRAGMA query_only = ON")
                tables = _tables(conn)
                if "links" in tables and {"link_key", "sent_at"} <= tables.get("recipients", set()):
                    for h, n, c in conn.execute(
                        f"SELECT {hour_of.format('r.sent_at')} AS h, COUNT(*), "
                        "SUM(COALESCE(l.clicks, 0) > 0) FROM recipients r LEFT JOIN links l ON l.key = r.link_key "
                        "WHERE r.status = 'sent' AND r.link_key = r.phone AND r.sent_at IS NOT NULL GROUP BY h",
                        {"offset": offset},
                    ):
                        sent[h] += n
                        clicked[h] += c or 0
                if "click_hours" in tables:
                    for h, n in conn.execute(
                        f"SELECT {hour_of.format('hour')} AS h, SUM(clicks) FROM click_hours GROUP BY h",
                        {"offset": offset},
                    ):
                        clicks[h] += n or 0
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("campaign_db_unreadable", extra={"path": str(path), "detail": str(e)})
    hours = sorted(set(sent) | {h for h, n in clicks.items() if n})
    if not hours:
        return None
    rates = {h: clicked[h] / sent[h] for h in hours if sent[h] >= MIN_SENT}
    best = max(rates, key=lambda h: (rates[h], -h)) if rates else None
    busiest = max(hours, key=lambda h: (clicks[h], -h)) if any(clicks.values()) else None
    top_rate = max(rates.values(), default=0) or 1
    top_clicks = max(clicks.values(), default=0) or 1
    rows = [{
        "hour": h, "label": fa_digits(f"{h:02d}:00"), "sent": sent[h], "clicked": clicked[h],
        "rate": rates.get(h), "rate_bar": round(1000 * rates[h] / top_rate) if h in rates else 0,
        "clicks": clicks[h], "clicks_bar": round(1000 * clicks[h] / top_clicks),
        "best": h == best, "busiest": h == busiest,
    } for h in hours]
    return {"rows": rows, "best": next((r for r in rows if r["best"]), None),
            "busiest": next((r for r in rows if r["busiest"]), None), "min_sent": MIN_SENT}


# How far segments overlap: each chosen segment's numbers, read from its
# prepared file and normalized as a send would. The last few are kept, keyed
# by the file's version, time and size, so ticking one more doesn't read
# them all again.
MAX_CHOSEN = 6
_CACHE: OrderedDict[tuple, frozenset[str]] = OrderedDict()
_LOCK = threading.Lock()


def phones_of(segment: Segment) -> frozenset[str]:
    path = segment.path
    stat = path.stat()
    key = (segment.slug, segment.version, stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    phones = set()
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # the header
        for row in reader:
            raw = row[0].strip() if row else ""
            if len(raw) == 11 and raw.startswith("09") and raw.isdigit():
                phones.add(raw)
                continue
            try:
                phones.add(normalize(raw))
            except InvalidPhoneError:
                continue
    found = frozenset(phones)
    with _LOCK:
        _CACHE[key] = found
        while len(_CACHE) > MAX_CHOSEN:
            _CACHE.popitem(last=False)
    return found


def overlap(segments: list[Segment]) -> dict:
    """For each pair of chosen segments, the numbers they share (and that as
    a share of the row's segment); and over all of them, the numbers counted
    once and those in more than one."""
    sets = [(s, phones_of(s)) for s in segments]
    rows = []
    for s, mine in sets:
        cells = []
        for t, theirs in sets:
            same = s.pk == t.pk
            shared = len(mine) if same else len(mine & theirs)
            cells.append({"segment": t, "n": shared, "share": round(100 * shared / len(mine)) if mine else 0,
                          "same": same})
        rows.append({"segment": s, "size": len(mine), "cells": cells})
    seen: Counter[str] = Counter()
    for _s, mine in sets:
        seen.update(mine)
    return {"rows": rows, "segments": [s for s, _ in sets], "once": len(seen),
            "several": sum(1 for n in seen.values() if n > 1)}

