"""CLI-level tests for the new `retry-failed` and `preview` commands."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from sms_sender import cli as cli_module
from sms_sender.cli import cli
from sms_sender.state import (
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SENT,
    StateStore,
)


def _seed_state(db_path: Path) -> StateStore:
    store = StateStore(db_path)
    store.upsert_pending([
        ("09120000001", "09120000001"),
        ("09120000002", "09120000002"),
        ("09120000003", "09120000003"),
    ])
    # Manually flip statuses to simulate a prior run.
    with store._tx() as conn:
        conn.execute(
            "UPDATE recipients SET status=? WHERE phone=?", (SENT, "09120000001")
        )
        conn.execute(
            "UPDATE recipients SET status=? WHERE phone=?",
            (FAILED_RETRIABLE, "09120000002"),
        )
        conn.execute(
            "UPDATE recipients SET status=? WHERE phone=?",
            (FAILED_PERMANENT, "09120000003"),
        )
    return store


def test_retry_failed_resets_only_retriable_by_default(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    _seed_state(db)
    inp = tmp_path / "in.txt"
    inp.write_text("09120000002\n09120000003\n", encoding="utf-8")

    # Stub out the actual send so the test doesn't hit the network.
    called = {}
    def fake_do_send(**kwargs):
        called.update(kwargs)
    monkeypatch.setattr(cli_module, "_do_send", fake_do_send)

    result = CliRunner().invoke(
        cli,
        [
            "retry-failed",
            "--input", str(inp),
            "--template", "t",
            "--state", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Reset 1 row(s)" in result.output
    assert called  # _do_send was invoked

    # failed_retriable → pending; failed_permanent untouched.
    counts = StateStore(db).counts()
    assert counts.get(PENDING, 0) == 1
    assert counts.get(FAILED_PERMANENT, 0) == 1
    assert counts.get(SENT, 0) == 1


def test_retry_failed_include_permanent_flag(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    _seed_state(db)
    inp = tmp_path / "in.txt"
    inp.write_text("09120000002\n09120000003\n", encoding="utf-8")

    monkeypatch.setattr(cli_module, "_do_send", lambda **_: None)

    result = CliRunner().invoke(
        cli,
        [
            "retry-failed",
            "--include-permanent",
            "--input", str(inp),
            "--template", "t",
            "--state", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Reset 2 row(s)" in result.output
    counts = StateStore(db).counts()
    assert counts.get(PENDING, 0) == 2
    assert counts.get(FAILED_PERMANENT, 0) == 0
    assert counts.get(FAILED_RETRIABLE, 0) == 0


def test_retry_failed_skips_send_when_nothing_to_retry(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    StateStore(db)  # empty
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    called = {"n": 0}
    def fake_do_send(**_):
        called["n"] += 1
    monkeypatch.setattr(cli_module, "_do_send", fake_do_send)

    result = CliRunner().invoke(
        cli,
        [
            "retry-failed",
            "--input", str(inp),
            "--template", "t",
            "--state", str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Nothing to retry" in result.output
    assert called["n"] == 0


def test_preview_single_phone_no_api_call():
    result = CliRunner().invoke(
        cli,
        [
            "preview",
            "--phone", "09123456789",
            "--template", "verify-tpl",
            "--token", "12345",
            "--token2", "John",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "POST https://api.kavenegar.com/v1/" in result.output
    assert "receptor=09123456789" in result.output
    assert "template=verify-tpl" in result.output
    assert "token=12345" in result.output
    assert "token2=John" in result.output


def test_preview_input_file_limits_output(tmp_path):
    inp = tmp_path / "in.txt"
    inp.write_text("\n".join(f"0912000000{i}" for i in range(1, 8)) + "\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "preview",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--limit", "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "previewing first 3" in result.output
    # Three POST sections, no more.
    assert result.output.count("POST https://api.kavenegar.com/v1/") == 3


def test_preview_send_requires_phone(tmp_path):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "preview",
            "--input", str(inp),
            "--template", "t",
            "--send",
        ],
    )
    assert result.exit_code != 0
    assert "--send requires --phone" in result.output
