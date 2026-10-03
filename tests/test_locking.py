import subprocess
import sys
import textwrap

import pytest

from sms_sender.locking import RunLock, RunLockError


def test_second_lock_on_the_same_db_fails_until_released(tmp_path):
    db = tmp_path / "s.db"
    first = RunLock(db)
    first.acquire()
    with pytest.raises(RunLockError):
        RunLock(db).acquire()
    first.release()
    with RunLock(db):
        pass


def test_locks_on_different_dbs_are_independent(tmp_path):
    with RunLock(tmp_path / "a.db"), RunLock(tmp_path / "b.db"):
        pass


def test_lock_held_by_another_process_is_refused_and_freed_when_it_dies(tmp_path):
    """The real scenario: a second process must be refused while the first
    runs, and a crashed holder (SIGKILL — no cleanup code runs) must not
    leave a stale lock behind."""
    db = tmp_path / "s.db"
    holder = textwrap.dedent(f"""
        import time
        from sms_sender.locking import RunLock
        RunLock({str(db)!r}).acquire()
        print("locked", flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(RunLockError) as exc:
            RunLock(db).acquire()
        assert f"pid {proc.pid}" in str(exc.value)
    finally:
        proc.kill()
        proc.wait()
    with RunLock(db):
        pass
