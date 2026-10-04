"""Phone numbers are personal data (spec 4.12): pages show them masked."""
from __future__ import annotations

# Not «•»: next to Persian digits it reads as «۰».
MASK = "*"


def mask_phone(raw: str) -> str:
    """'09120001234' → '0912*****34'. Every digit after the first four and
    before the last two is hidden, whatever the formatting. A value with
    fewer than ten digits (not a whole number) has all of them hidden."""
    positions = [i for i, ch in enumerate(raw) if ch.isdigit()]
    hidden = set(positions[4:-2]) if len(positions) >= 10 else set(positions)
    return "".join(MASK if i in hidden else ch for i, ch in enumerate(raw))
