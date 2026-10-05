"""The frequency cap (plan 05, decision 6): at most N accepted SMS to one
number in D days, counted across every campaign DB in the folder. A run
holds back whoever is over it as `capped`, and counts them afresh next
time; test SMS don't count."""
from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from sms_sender.cli import cli
from sms_sender.frequency import FrequencyCap, parse_cap
from sms_sender.runner import Runner, format_report
from sms_sender.state import CAPPED, SENT, StateStore, folder_sends_since

from .test_runner import FakeSender, RecordingReporter

A, B = "09120000001", "09120000002"
DAY = 86400


def test_a_cap_is_n_sms_in_d_days():
    assert parse_cap("2/7") == FrequencyCap(2, 7) and parse_cap(" 3 / 30d ") == FrequencyCap(3, 30)
    assert parse_cap("") is None and parse_cap("off") is None and parse_cap(None) is None
    for bad in ("0/7", "2/0", "two", "2"):
        with pytest.raises(ValueError):
            parse_cap(bad)


def setup(tmp_path, *, sent_ago: float, kind: str = "send"):
    """Another campaign in the folder sent A twice, `sent_ago` seconds ago."""
    folder = tmp_path / "db"
    folder.mkdir()
    other = StateStore(folder / "other.db")
    for message_id in (1, 2):
        other.record_attempt(phone=A, kind=kind, outcome="accepted", started_at=time.time() - sent_ago,
                             message_id=message_id)
    inp = tmp_path / "in.txt"
    inp.write_text(f"{A}\n{B}\n", encoding="utf-8")
    return folder, inp


def run(folder, inp, cap) -> tuple:
    state, sender = StateStore(folder / "coin-7.db"), FakeSender()
    summary = Runner(input_path=inp, state=state, sender=sender, workers=1, reporter=RecordingReporter(),
                     preflight=False, frequency_cap=cap).run()
    return state, sender, summary


def test_a_run_holds_back_whoever_hit_the_cap_elsewhere(tmp_path):
    folder, inp = setup(tmp_path, sent_ago=DAY)
    assert folder_sends_since(folder, time.time() - 7 * DAY) == {A: 2}
    state, sender, summary = run(folder, inp, FrequencyCap(2, 7))
    assert sender.calls == [B] and state.counts() == {SENT: 1, CAPPED: 1}
    assert summary.capped == 1 and "capped            1" in format_report(summary)


def test_capped_rows_are_counted_afresh_as_the_window_moves(tmp_path):
    folder, inp = setup(tmp_path, sent_ago=DAY)
    run(folder, inp, FrequencyCap(2, 7))
    # A week and more later, the two SMS fall out of the window.
    other = StateStore(folder / "other.db")
    other._conn().execute("UPDATE attempts SET started_at = started_at - ?", (8 * DAY,))
    state, sender, summary = run(folder, inp, FrequencyCap(2, 7))
    assert sender.calls == [A] and state.counts() == {SENT: 2} and summary.capped == 0


def test_without_a_cap_nobody_is_held_back(tmp_path):
    folder, inp = setup(tmp_path, sent_ago=DAY)
    state, sender, _ = run(folder, inp, None)
    assert sorted(sender.calls) == [A, B]


def test_test_sms_dont_count(tmp_path):
    folder, inp = setup(tmp_path, sent_ago=DAY, kind="test")
    _, sender, _ = run(folder, inp, FrequencyCap(1, 7))
    assert sorted(sender.calls) == [A, B]


def test_the_cli_refuses_a_cap_it_cant_read(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVENEGAR_API_KEY", "test-key-not-real")
    inp = tmp_path / "in.txt"
    inp.write_text(f"{A}\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["send", "--input", str(inp), "--template", "t", "--token", "x",
                                      "--state", str(tmp_path / "s.db"), "--frequency-cap", "lots"])
    assert result.exit_code == 2 and "--frequency-cap" in result.output
