"""Thread-safe token-bucket rate limiter.

Used by Runner to cap *total* throughput across worker threads. Workers control
parallelism (`--workers`); this caps requests-per-second regardless of how many
workers are configured. With 20 workers and a 10/s bucket, 10 of them will be
blocked on `acquire()` at any moment.
"""
from __future__ import annotations

import threading
import time

UNITS: dict[str, float] = {
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
}


def parse_rate(spec: str | float | int | None) -> float:
    """Parse a rate spec into tokens per second.

    Accepted forms: `"10/s"`, `"60/m"`, `"3600/h"`, plain `"5"` (per second),
    `0` / `""` / `None` to disable. Returns 0.0 when disabled.
    """
    if spec is None:
        return 0.0
    if isinstance(spec, (int, float)):
        return max(0.0, float(spec))
    s = spec.strip()
    if not s or s == "0":
        return 0.0
    if "/" not in s:
        return max(0.0, float(s))
    n_str, unit = s.split("/", 1)
    unit = unit.strip().lower()
    if unit not in UNITS:
        raise ValueError(f"unknown rate unit {unit!r} in {spec!r}")
    return float(n_str.strip()) / UNITS[unit]


class TokenBucket:
    """Classic token bucket. `acquire()` blocks until one token is available.

    `rate_per_sec <= 0` disables limiting (acquire is a no-op).
    `burst` defaults to max(1, ceil(rate)) so short bursts of `--workers` don't
    each wait their turn from a cold start.
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None):
        self.rate = max(0.0, float(rate_per_sec))
        if burst is None:
            burst = max(1, int(self.rate)) if self.rate > 0 else 0
        self.capacity = float(burst)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0

    def acquire(self) -> None:
        if not self.enabled:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(wait)
