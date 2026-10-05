"""When campaign SMS may go out: a daily window in Tehran time.

Providers don't cite one rule for promotional SMS hours (08:00–22:00, or
"nothing after 21:00"), so the default is the stricter 08:00–21:00. A run
refuses to start outside the window, and stops claiming when it closes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT_WINDOW = "08:00-21:00"
ENV_SEND_WINDOW = "SMS_SENDER_SEND_WINDOW"

_SPEC_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class SendWindow:
    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        t = moment.astimezone(TEHRAN).time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # crosses midnight, e.g. 22:00-02:00

    def __str__(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M} Tehran time"


def parse_window(spec: str | None) -> SendWindow | None:
    """'08:00-21:00' → a SendWindow; 'off', '' or None → no restriction.
    A window may cross midnight ('22:00-02:00'), and 24:00 is midnight."""
    if spec is None or spec.strip().lower() in ("", "off", "none"):
        return None
    match = _SPEC_RE.match(spec.strip())
    if not match:
        raise ValueError(f"sending window must look like 08:00-21:00 or 'off'; got {spec!r}")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if (h1, m1, h2, m2) == (0, 0, 24, 0):
        raise ValueError(f"sending window {spec!r} is the whole day: use 'off'")
    # 24:00 is the midnight that ends a day: the same moment as 00:00.
    h1, h2 = (0 if (h, m) == (24, 0) else h for h, m in ((h1, m1), (h2, m2)))
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError as e:
        raise ValueError(f"sending window {spec!r}: {e}") from e
    if start == end:
        raise ValueError(f"sending window {spec!r} is empty")
    return SendWindow(start, end)


def now_tehran() -> datetime:
    return datetime.now(TEHRAN)
