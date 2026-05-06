"""Tests for the token-bucket rate limiter."""
from __future__ import annotations

import threading
import time

import pytest

from sms_sender.rate import TokenBucket, parse_rate


# ---------- parse_rate ----------


@pytest.mark.parametrize("spec, expected", [
    (None, 0.0),
    ("", 0.0),
    ("0", 0.0),
    (0, 0.0),
    ("10/s", 10.0),
    ("10/sec", 10.0),
    ("60/m", 1.0),
    ("60/min", 1.0),
    ("3600/h", 1.0),
    ("3600/hour", 1.0),
    ("5", 5.0),         # bare number → per second
    (5, 5.0),
    (2.5, 2.5),
])
def test_parse_rate_accepted(spec, expected):
    assert parse_rate(spec) == pytest.approx(expected)


def test_parse_rate_rejects_unknown_unit():
    with pytest.raises(ValueError):
        parse_rate("10/year")


# ---------- TokenBucket ----------


def test_disabled_bucket_is_noop():
    b = TokenBucket(0)
    assert not b.enabled
    start = time.monotonic()
    for _ in range(1000):
        b.acquire()
    assert time.monotonic() - start < 0.05


def test_burst_lets_first_calls_through_immediately():
    # rate=2/s, burst=2 → first 2 acquires should be instant.
    b = TokenBucket(2.0, burst=2)
    start = time.monotonic()
    b.acquire()
    b.acquire()
    assert time.monotonic() - start < 0.05


def test_third_acquire_waits_for_refill():
    # rate=10/s, burst=1 → after the first, the second blocks ~0.1s.
    b = TokenBucket(10.0, burst=1)
    b.acquire()  # consume the initial token
    start = time.monotonic()
    b.acquire()
    elapsed = time.monotonic() - start
    # ~0.1s expected; allow generous margin for slow CI.
    assert 0.05 <= elapsed <= 0.5


def test_concurrent_workers_respect_total_rate():
    """20 acquires at 50/s with burst=1 should take roughly 0.4s wall-clock."""
    b = TokenBucket(50.0, burst=1)
    n_threads = 5
    per_thread = 4  # 20 total

    def worker():
        for _ in range(per_thread):
            b.acquire()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    # 20 acquires at 50/s with burst=1 → first is free, remaining 19 take 19/50 ≈ 0.38s.
    # Allow wide margin so the test isn't flaky on slow CI.
    assert 0.25 <= elapsed <= 1.5, f"elapsed={elapsed:.3f}s"
