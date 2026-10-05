"""One campaign-DB folder, one kernel at a time.

The dashboard's worker and the CLI can share a folder of campaign DBs. On
one machine they share a kernel (Linux, with the worker in Docker or not),
and the run lock (flock) and SQLite's own locks keep them apart. Docker
Desktop on a Mac or on Windows runs the worker inside a VM: a CLI on the
host opens the same files through the mount, but neither kind of lock
reaches across it, so a CLI send could claim recipients the worker is
sending to. The worker leaves a heartbeat in the folder; a CLI on another
kernel refuses to change anything there while that heartbeat is fresh."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from functools import lru_cache
from pathlib import Path

HEARTBEAT = ".dashboard-worker.json"
FRESH_SEC = 120  # the worker beats every 10 s while busy, every loop while idle


@lru_cache(maxsize=1)
def kernel_id() -> str:
    """This kernel's boot: the same for a Linux host and its containers,
    different for a Mac and the VM Docker Desktop runs."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return "darwin:" + out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "host:" + socket.gethostname()


def write_heartbeat(folder: Path | str, worker: str) -> None:
    """Called by the dashboard's worker as it beats. Best-effort: a folder it
    can't write to just leaves the CLI unguarded, as before."""
    folder = Path(folder)
    data = {"kernel": kernel_id(), "worker": worker, "at": time.time()}
    tmp = folder / f"{HEARTBEAT}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(folder / HEARTBEAT)
    except OSError:
        tmp.unlink(missing_ok=True)


def foreign_worker(folder: Path | str, *, now: float | None = None) -> dict | None:
    """A live dashboard worker on another kernel using this folder, or None."""
    try:
        data = json.loads((Path(folder) / HEARTBEAT).read_text(encoding="utf-8"))
        at = float(data.get("at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if (now if now is not None else time.time()) - at > FRESH_SEC:
        return None
    if data.get("kernel") == kernel_id():
        return None
    return data
