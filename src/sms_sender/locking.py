"""Exclusive per-DB run lock: only one process may work on a state DB.

Two runs on one DB can double-send: each treats the other's `in_flight`
rows as left over from a crash. `fcntl.flock` on a `<db>.lock` file next
to the DB prevents that. The OS releases the lock when the holding
process exits — even on a crash or SIGKILL — so a stale lock never
blocks the next run, and crash recovery keeps working.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]


class RunLockError(RuntimeError):
    """Another process is working on this state DB."""


class RunLock:
    """`with RunLock(db_path): …` — raises RunLockError if already held.

    The lock belongs to the open file, so a second RunLock on the same DB
    fails even inside the same process.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.path = Path(f"{db_path}.lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:  # pragma: no cover
            raise RunLockError("the run lock needs macOS or Linux (or Docker)")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 32).decode(errors="replace").strip() or "?"
            os.close(fd)
            raise RunLockError(
                f"another sms-sender process (pid {holder}) is using {self.db_path}; "
                "wait for it to finish"
            ) from None
        # The holder's pid, for the error above. Diagnostics only.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the file drops the lock
            self._fd = None

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
