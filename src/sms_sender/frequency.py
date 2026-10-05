"""The frequency cap (plan 05, decision 6): at most N accepted SMS to one
number within D days, counted across every campaign DB in the folder.
Off unless set (`--frequency-cap 2/7`, or the dashboard's setting).

A recipient over the cap is held back as `capped`: never sent by this run,
and counted afresh at the next one, as the window moves on. They got
nothing from this campaign, so freeing them again can't send anyone twice."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FrequencyCap:
    sms: int
    days: int

    @property
    def seconds(self) -> float:
        return self.days * 86400.0

    def __str__(self) -> str:
        return f"{self.sms}/{self.days}"


def parse_cap(text) -> FrequencyCap | None:
    """'2/7' → at most 2 SMS in 7 days. '', 'off' or '0' → off."""
    value = str(text or "").strip().lower()
    if value in ("", "off", "0", "none"):
        return None
    match = re.fullmatch(r"(\d+)\s*/\s*(\d+)\s*d?", value)
    if not match or int(match.group(1)) < 1 or int(match.group(2)) < 1:
        raise ValueError(f"not a frequency cap: {text!r} (e.g. 2/7: two SMS in seven days)")
    return FrequencyCap(int(match.group(1)), int(match.group(2)))
