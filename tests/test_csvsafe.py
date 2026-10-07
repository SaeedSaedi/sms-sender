"""Plan 07, Q2: downloads safe to open in a spreadsheet. A cell that starts
like a formula stays text; numbers stay numbers; the engine's own input
files are never touched."""
from __future__ import annotations

import csv

import pytest
from click.testing import CliRunner

from sms_sender import csvsafe
from sms_sender.cli import cli
from sms_sender.state import StateStore


@pytest.mark.parametrize("value, shown", [
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
    ("-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"),
    ("\tvalue", "'\tvalue"),
    ("+989121234567", "+989121234567"),   # a phone: a plain number
    ("-1200", "-1200"),
    ("3.5", "3.5"),
    ("علی", "علی"),
    ("u-123", "u-123"),
    (42, 42),
    (None, None),
])
def test_a_cell_that_would_run_as_a_formula_stays_text(value, shown):
    assert csvsafe.cell(value) == shown


def test_export_failed_writes_safe_cells(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.record_invalid("=HYPERLINK(\"http://evil\")", "not a phone number")
    out = tmp_path / "failed.csv"
    result = CliRunner().invoke(cli, ["export-failed", "--state", str(db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))
    assert rows[1][1] == "'=HYPERLINK(\"http://evil\")"
