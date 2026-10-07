"""A Mac stays awake while SMS go out (plan 07, Q3).

A Mac that sleeps in the middle of a send stalls it: the requests in flight
end `unknown` and are reconciled, so nobody gets two SMS, but nothing goes
out until it wakes. So while a send or a test SMS runs, the worker holds
macOS's `caffeinate -i`, which keeps the Mac from sleeping when it's left
idle. A closed lid still puts a laptop to sleep. Elsewhere (the server's
Linux) it does nothing."""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from typing import Iterator

logger = logging.getLogger(__name__)


def _caffeinate() -> str | None:
    return shutil.which("caffeinate") if sys.platform == "darwin" else None


@contextlib.contextmanager
def awake() -> Iterator[subprocess.Popen | None]:
    """Hold `caffeinate` while inside; it yields the process, or None where
    there's none. `-w` lets it go by itself if the worker dies."""
    tool = _caffeinate()
    proc = None
    if tool:
        try:
            proc = subprocess.Popen([tool, "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.warning("keep_awake_failed", extra={"detail": str(e)})
    try:
        yield proc
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
