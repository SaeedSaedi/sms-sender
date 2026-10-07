"""Plan 07, Q3: a Mac stays awake while SMS go out, and lets go after."""
import sys

import pytest

pytest.importorskip("django")

from sms_sender_web.jobs import awake as awake_module  # noqa: E402


class FakeProcess:
    made: list = []

    def __init__(self, args, **kw):
        self.args, self.stopped = args, False
        FakeProcess.made.append(self)

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def test_caffeinate_is_held_while_inside(monkeypatch):
    FakeProcess.made = []
    monkeypatch.setattr(awake_module, "_caffeinate", lambda: "/usr/bin/caffeinate")
    monkeypatch.setattr(awake_module.subprocess, "Popen", FakeProcess)
    with awake_module.awake() as proc:
        assert proc.args[:2] == ["/usr/bin/caffeinate", "-i"] and proc.args[2] == "-w"
        assert not proc.stopped
    assert proc.stopped


def test_without_caffeinate_nothing_is_started(monkeypatch):
    FakeProcess.made = []
    monkeypatch.setattr(awake_module, "_caffeinate", lambda: None)
    monkeypatch.setattr(awake_module.subprocess, "Popen", FakeProcess)
    with awake_module.awake() as proc:
        assert proc is None
    assert FakeProcess.made == []


def test_a_caffeinate_that_cant_start_never_stops_the_send(monkeypatch):
    def refuse(*args, **kw):
        raise OSError("not allowed")

    monkeypatch.setattr(awake_module, "_caffeinate", lambda: "/usr/bin/caffeinate")
    monkeypatch.setattr(awake_module.subprocess, "Popen", refuse)
    with awake_module.awake() as proc:
        assert proc is None


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS's")
def test_the_real_caffeinate_starts_and_stops():
    with awake_module.awake() as proc:
        assert proc is not None and proc.poll() is None
    assert proc.poll() is not None
