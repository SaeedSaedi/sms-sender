"""Plan 07, Q2: a send killed at each step, for real, then run again.

A child process runs the send and is killed with SIGKILL (nothing runs after
it: no `finally`, no handler) at one recipient's step:
- before_send: claimed, but the request never reached Kavenegar;
- before_mark: Kavenegar accepted it, the process died before marking it;
- after_mark: marked sent, then died.

The next run settles what the first left (reconciling with Kavenegar's own
records) and finishes. Every number must be accepted exactly once.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from sms_sender.runner import Runner
from sms_sender.sender import ProviderMessage, SendResult
from sms_sender.state import SENT, StateStore

PHONES = [f"0912000{i:04d}" for i in range(1, 7)]
VICTIM = PHONES[2]

CHILD = r'''
import json, os, signal, sys
from pathlib import Path
from sms_sender.runner import Runner
from sms_sender.sender import SendResult
from sms_sender.state import StateStore

db, inp, provider, step, victim = sys.argv[1:6]
provider = Path(provider)

def die():
    os.kill(os.getpid(), signal.SIGKILL)

class Kavenegar:
    def send(self, phone, tokens=None):
        if phone == victim and step == "before_send":
            die()
        message_id = 1000 + (len(provider.read_text().splitlines()) if provider.exists() else 0)
        with provider.open("a") as f:
            f.write(json.dumps({"phone": phone, "id": message_id}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if phone == victim and step == "before_mark":
            die()
        return SendResult(message_id=message_id, status_code=200)

store = StateStore(db)
mark_sent = store.mark_sent

def marked(phone, *args, **kwargs):
    mark_sent(phone, *args, **kwargs)
    if phone == victim and step == "after_mark":
        die()

store.mark_sent = marked
Runner(input_path=inp, state=store, sender=Kavenegar(), workers=1, preflight=False,
       install_signal_handlers=False).run()
'''


class Kavenegar:
    """The provider as the next run sees it: its own record of every SMS it
    accepted (shared with the child), and the lookup reconciliation asks."""

    def __init__(self, provider: Path):
        self.provider = provider

    def records(self) -> list[dict]:
        if not self.provider.exists():
            return []
        return [json.loads(line) for line in self.provider.read_text().splitlines()]

    def send(self, phone, tokens=None):
        message_id = 1000 + len(self.records())
        with self.provider.open("a") as f:
            f.write(json.dumps({"phone": phone, "id": message_id}) + "\n")
        return SendResult(message_id=message_id, status_code=200)

    def find_messages(self, phone, start, end):
        return [ProviderMessage(r["id"], 10) for r in self.records() if r["phone"] == phone]


@pytest.mark.parametrize("step", ["before_send", "before_mark", "after_mark"])
def test_a_send_killed_at_any_step_reaches_everyone_once(tmp_path, step):
    db, inp, provider = tmp_path / "c.db", tmp_path / "in.txt", tmp_path / "provider.jsonl"
    inp.write_text("\n".join(PHONES) + "\n", encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(db), str(inp), str(provider), step, VICTIM],
        capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr  # it really died there
    kavenegar = Kavenegar(provider)
    before = Counter(r["phone"] for r in kavenegar.records())
    assert set(before) == set(PHONES[:2]) | ({VICTIM} if step != "before_send" else set())

    Runner(input_path=inp, state=StateStore(db), sender=kavenegar, workers=1, preflight=False,
           install_signal_handlers=False, reconcile_min_age_sec=0).run()

    assert Counter(r["phone"] for r in kavenegar.records()) == Counter({phone: 1 for phone in PHONES})
    assert StateStore(db).counts() == {SENT: len(PHONES)}


def test_a_full_disk_after_kavenegar_accepted_never_sends_twice(tmp_path, monkeypatch):
    """Marking it sent fails (the disk is full): the row stays in flight, so
    the next run finds the SMS at Kavenegar instead of sending it again."""
    inp, provider = tmp_path / "in.txt", tmp_path / "provider.jsonl"
    inp.write_text("\n".join(PHONES) + "\n", encoding="utf-8")
    kavenegar = Kavenegar(provider)
    real = StateStore.mark_sent

    def full(self, phone, *args, **kwargs):
        if phone == VICTIM:
            raise sqlite3.OperationalError("database or disk is full")
        return real(self, phone, *args, **kwargs)

    monkeypatch.setattr(StateStore, "mark_sent", full)
    try:
        Runner(input_path=inp, state=StateStore(tmp_path / "c.db"), sender=kavenegar, workers=1,
               preflight=False, install_signal_handlers=False).run()
    except sqlite3.OperationalError:
        pass  # the run may stop on it; either way nothing is marked wrongly
    monkeypatch.setattr(StateStore, "mark_sent", real)

    Runner(input_path=inp, state=StateStore(tmp_path / "c.db"), sender=kavenegar, workers=1,
           preflight=False, install_signal_handlers=False, reconcile_min_age_sec=0).run()
    assert Counter(r["phone"] for r in kavenegar.records()) == Counter({phone: 1 for phone in PHONES})
    assert StateStore(tmp_path / "c.db").counts() == {SENT: len(PHONES)}
