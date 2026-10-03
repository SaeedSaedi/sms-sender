"""CLI-level tests for the new `retry-failed` and `preview` commands."""
from __future__ import annotations

from pathlib import Path

import pytest
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


# ---------- footgun guards ----------


def test_reset_status_sent_requires_confirmation(tmp_path):
    """`reset --status sent` is a footgun — it must prompt unless --yes is given."""
    db = tmp_path / "s.db"
    _seed_state(db)
    # No --yes, and we send "n" to the prompt → should abort.
    result = CliRunner().invoke(
        cli,
        ["reset", "--status", "sent", "--state", str(db)],
        input="n\n",
    )
    assert result.exit_code != 0  # click.confirm(abort=True) returns non-zero
    # The sent row must still be sent (not promoted to pending).
    counts = StateStore(db).counts()
    assert counts.get(SENT, 0) == 1


def test_reset_status_sent_with_yes_flag_skips_prompt(tmp_path):
    db = tmp_path / "s.db"
    _seed_state(db)
    result = CliRunner().invoke(
        cli, ["reset", "--status", "sent", "--state", str(db), "--yes"],
    )
    assert result.exit_code == 0, result.output
    counts = StateStore(db).counts()
    assert counts.get(SENT, 0) == 0
    assert counts.get(PENDING, 0) == 1  # the previously-sent row


def test_reset_failed_does_not_prompt(tmp_path):
    """Resetting failed_* statuses is the safe path — no confirmation required."""
    db = tmp_path / "s.db"
    _seed_state(db)
    result = CliRunner().invoke(
        cli, ["reset", "--status", "failed_retriable", "--state", str(db)],
    )
    assert result.exit_code == 0, result.output


def test_send_rejects_token_with_spaces(tmp_path):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "has spaces",
            "--state", str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code != 0
    assert "token" in result.output.lower()
    assert "space" in result.output.lower()


def test_send_accepts_token10_with_up_to_5_spaces(tmp_path, monkeypatch):
    """token10 is the multi-word slot — 5 spaces is the documented max."""
    monkeypatch.setattr(cli_module, "_do_send", lambda **_: None)
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token10", "five word phrase right here",   # 5 words → 4 spaces
            "--state", str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code == 0, result.output


def test_send_rejects_token10_with_too_many_spaces(tmp_path):
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token10", "this has more than five spaces here please",   # 7 spaces
            "--state", str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code != 0
    assert "token10" in result.output.lower()


# ---------- CLI smoke tests (full Click round-trip) ----------


def test_dry_run_summarizes_input(tmp_path):
    inp = tmp_path / "in.txt"
    inp.write_text(
        "09120000001\n"
        "09120000002\n"
        "+989120000002\n"   # duplicate
        "junk\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["dry-run", "--input", str(inp)])
    assert result.exit_code == 0, result.output
    assert "valid=2" in result.output
    assert "invalid=1" in result.output
    assert "duplicates_collapsed=1" in result.output
    assert "INVALID" in result.output


def test_status_against_empty_db(tmp_path):
    db = tmp_path / "fresh.db"
    StateStore(db)  # creates schema, no rows
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert result.exit_code == 0, result.output
    assert "(empty)" in result.output


def test_status_shows_counts(tmp_path):
    db = tmp_path / "s.db"
    _seed_state(db)
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert result.exit_code == 0, result.output
    # Each status line is: "<status>   <count>"
    assert "sent" in result.output
    assert "failed_permanent" in result.output
    assert "failed_retriable" in result.output


def test_purge_with_yes_flag_deletes_db(tmp_path):
    db = tmp_path / "doomed.db"
    StateStore(db)
    assert db.exists()
    result = CliRunner().invoke(cli, ["purge", "--state", str(db), "-y"])
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert not db.exists()


def test_purge_no_db_at_path(tmp_path):
    result = CliRunner().invoke(
        cli, ["purge", "--state", str(tmp_path / "nope.db"), "-y"],
    )
    assert result.exit_code == 0
    assert "No state DB" in result.output


def test_export_failed_writes_csv(tmp_path):
    db = tmp_path / "s.db"
    _seed_state(db)
    out = tmp_path / "failed.csv"
    result = CliRunner().invoke(
        cli, ["export-failed", "--state", str(db), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "phone_or_raw" in content                # header
    assert "09120000003" in content                 # the failed_permanent row


def test_preview_redacts_api_key_in_url():
    """The preview command's URL prefix should print `<API_KEY>`, not the
    user's real key (preview never loads the env unless --check-account/--send)."""
    result = CliRunner().invoke(
        cli,
        [
            "preview",
            "--phone", "09123456789",
            "--template", "t",
            "--token", "x",
        ],
    )
    assert result.exit_code == 0, result.output
    # The literal placeholder appears, not a real key.
    assert "<API_KEY>" in result.output


def test_preview_check_account_never_prints_the_real_key(monkeypatch):
    """--check-account / --send load the real key; the printed URL must
    still show the placeholder."""
    from sms_sender.sender import AccountInfo, Sender

    monkeypatch.setattr(cli_module, "load_api_key", lambda: "SECRET_KEY_DO_NOT_PRINT")
    monkeypatch.setattr(
        Sender, "account_info",
        lambda self: AccountInfo(remaining_credit=1, expire_date=None, type=None),
    )
    result = CliRunner().invoke(
        cli,
        [
            "preview",
            "--phone", "09123456789",
            "--template", "t",
            "--token", "x",
            "--check-account",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "SECRET_KEY_DO_NOT_PRINT" not in result.output
    assert "<API_KEY>" in result.output


def test_send_on_a_busy_db_exits_2(tmp_path, monkeypatch):
    from sms_sender.locking import RunLock

    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    db = tmp_path / "s.db"
    with RunLock(db):
        result = CliRunner().invoke(
            cli,
            [
                "send",
                "--input", str(inp),
                "--template", "t",
                "--state", str(db),
                "--log-file", str(tmp_path / "test.log"),
            ],
        )
    assert result.exit_code == 2, result.output
    assert "another sms-sender process" in result.output


@pytest.mark.parametrize("args", [
    ["reset", "--status", "failed_permanent"],
    ["purge", "--yes"],
])
def test_db_changing_commands_refuse_a_busy_db(tmp_path, args):
    from sms_sender.locking import RunLock

    db = tmp_path / "s.db"
    _seed_state(db)
    with RunLock(db):
        result = CliRunner().invoke(cli, [*args, "--state", str(db)])
    assert result.exit_code == 2, result.output
    assert db.exists()  # purge didn't delete it


def test_dry_run_reports_skipped_header(tmp_path):
    inp = tmp_path / "seg.csv"
    inp.write_text("Phone Number\n09123456789\n", encoding="utf-8")
    result = CliRunner().invoke(cli, ["dry-run", "--input", str(inp)])
    assert result.exit_code == 0, result.output
    assert "valid=1 invalid=0" in result.output
    assert "skipped header row 'Phone Number'" in result.output


def test_send_end_to_end_through_real_sender(tmp_path, monkeypatch):
    """Real CLI → Click → Runner → Sender → _KavenegarHTTP → patched HTTP.

    This exercises the full call graph (no FakeSender duck-typed shortcut),
    verifying the wiring is intact: API key from env, real Sender retry loop,
    state DB at the path the user passed, exit code 0 when all sent.
    """
    import sms_sender.sender as sender_mod

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n09120000002\n", encoding="utf-8")
    db = tmp_path / "s.db"
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY_NOT_A_REAL_ONE")

    posted: list[str] = []

    class _Resp:
        status_code = 200
        def json(self):
            return {
                "return": {"status": 200, "message": "OK"},
                "entries": [{"messageid": 12345, "status": 200}],
            }

    def fake_post(url, data=None, timeout=None, **_):
        posted.append(url)
        return _Resp()

    # Patch the session.post inside _KavenegarHTTP. Both verify and account
    # endpoints will return success.
    monkeypatch.setattr(
        sender_mod.requests.Session, "post",
        lambda self, url, data=None, timeout=None, **kw: fake_post(url, data, timeout, **kw),
    )

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "tpl",
            "--token", "x",
            "--state", str(db),
            "--no-preflight",            # skip account_info call
            "--workers", "2",
            "--log-file", str(tmp_path / "test.log"),
        ],
    )
    assert result.exit_code == 0, result.output
    # Both phones posted to /verify/lookup.json.
    assert len(posted) == 2
    assert all("/verify/lookup.json" in u for u in posted)
    # The state DB has both rows as sent.
    assert StateStore(db).counts() == {SENT: 2}


def test_send_read_timeout_parks_the_row_as_unknown(tmp_path, monkeypatch):
    """Real CLI → Runner → Sender → _KavenegarHTTP with a read timeout: the
    request may have been accepted, so it's sent exactly once, recorded as
    `unknown` with its attempt, and the exit code is 1."""
    import requests
    import sms_sender.sender as sender_mod

    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY_NOT_A_REAL_ONE")
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    db = tmp_path / "s.db"
    posts: list[str] = []

    def timeout_post(self, url, data=None, timeout=None, **kw):
        posts.append(url)
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(sender_mod.requests.Session, "post", timeout_post)
    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "tpl",
            "--state", str(db),
            "--no-preflight",
            "--log-file", str(tmp_path / "test.log"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert len(posts) == 1  # never retried
    store = StateStore(db)
    assert store.counts() == {"unknown": 1}
    assert [r["outcome"] for r in store.attempts_for("09120000001")] == ["unknown"]
    assert "unknown" in result.output


def _unknown_db(tmp_path) -> Path:
    db = tmp_path / "s.db"
    store = StateStore(db)
    store.upsert_pending([("09120000001", "09120000001"), ("09120000002", "09120000002")])
    for phone in ("09120000001", "09120000002"):
        store.claim(phone)
        store.mark_unknown(phone, "outcome unknown: read timed out")
    return db


def test_reconcile_command_settles_unknown_rows(tmp_path, monkeypatch):
    from sms_sender.sender import ProviderMessage, Sender

    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    db = _unknown_db(tmp_path)
    at_kavenegar = {"09120000001": [ProviderMessage(4242, 10)]}
    monkeypatch.setattr(
        Sender, "find_messages", lambda self, phone, start, end: at_kavenegar.get(phone, []),
    )
    result = CliRunner().invoke(
        cli,
        ["reconcile", "--state", str(db), "--min-age", "0",
         "--log-file", str(tmp_path / "test.log")],
    )
    assert result.exit_code == 0, result.output
    assert "sent (found at Kavenegar)      1" in result.output
    assert "not sent (safe to send again)  1" in result.output
    assert StateStore(db).counts() == {SENT: 1, FAILED_RETRIABLE: 1}


def test_reconcile_command_leaves_recent_rows_and_exits_1(tmp_path, monkeypatch):
    from sms_sender.sender import Sender

    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    db = _unknown_db(tmp_path)
    monkeypatch.setattr(
        Sender, "find_messages", lambda *a, **kw: pytest.fail("looked up a recent row"),
    )
    result = CliRunner().invoke(
        cli, ["reconcile", "--state", str(db), "--log-file", str(tmp_path / "test.log")],
    )
    assert result.exit_code == 1, result.output
    assert "not checked yet                2" in result.output
    assert StateStore(db).counts() == {"unknown": 2}


def test_reset_unknown_needs_confirmation(tmp_path):
    db = _unknown_db(tmp_path)
    result = CliRunner().invoke(
        cli, ["reset", "--status", "unknown", "--state", str(db)], input="n\n",
    )
    assert result.exit_code != 0
    assert "may already have the SMS" in result.output
    assert StateStore(db).counts() == {"unknown": 2}


# ---------- approval test (manual gate) ----------


def _stub_make_runner(captured: dict):
    """Return a fake `make_runner` that records the kwargs it was called with
    and yields a runner whose `.run()` produces a no-failure summary.

    Used to verify the CLI wiring — what flags + env vars resolve to — without
    actually running a Runner.
    """
    from sms_sender.runner import RunSummary

    class _FakeRunner:
        def run(self):
            return RunSummary(
                total_input=0, new_recipients=0, duplicates_collapsed=0,
                invalid=0, sent=0, failed_permanent=0, failed_retriable=0,
                halted=False,
            )

    def fake(**kwargs):
        captured.update(kwargs)
        return _FakeRunner()

    return fake


def test_send_approval_test_uses_env_var(tmp_path, monkeypatch):
    """`--approval-test` with no flag value falls back to SMS_SENDER_TEST_NUMBER."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    monkeypatch.setenv(cli_module.TEST_NUMBER_ENV, "09151097710")
    captured: dict = {}
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured))

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
            "--approval-test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["approval_test_number"] == "09151097710"


def test_send_test_number_overrides_env(tmp_path, monkeypatch):
    """`--test-number` on the CLI wins over the env var."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    monkeypatch.setenv(cli_module.TEST_NUMBER_ENV, "09151111111")
    captured: dict = {}
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured))

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
            "--approval-test",
            "--test-number", "09152222222",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["approval_test_number"] == "09152222222"


def test_send_test_number_normalized(tmp_path, monkeypatch):
    """Non-canonical forms (+98…, Persian digits, spaces) are accepted and
    normalized to canonical 09… before reaching the runner."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    captured: dict = {}
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured))

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
            "--approval-test",
            "--test-number", "+98 915 109 7710",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["approval_test_number"] == "09151097710"


def test_send_approval_test_without_number_errors(tmp_path, monkeypatch):
    """`--approval-test` set but no flag and no env var → UsageError."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    # `load_api_key` runs `load_dotenv` which finds the project's real `.env`
    # by walking up from config.py (cwd-independent). Setting the var to ""
    # blocks load_dotenv from overwriting it (default override=False) so the
    # "no value" code path can be exercised here.
    monkeypatch.setenv(cli_module.TEST_NUMBER_ENV, "")

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
            "--approval-test",
        ],
    )
    assert result.exit_code != 0
    assert "--approval-test" in result.output
    assert cli_module.TEST_NUMBER_ENV in result.output


def test_send_invalid_test_number_errors(tmp_path, monkeypatch):
    """A garbage --test-number fails fast, before any send is attempted."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
            "--approval-test",
            "--test-number", "not-a-phone",
        ],
    )
    assert result.exit_code != 0
    assert "invalid test number" in result.output


def test_send_no_approval_test_does_not_require_number(tmp_path, monkeypatch):
    """Default behavior — no --approval-test, no env var needed, no error."""
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    monkeypatch.delenv(cli_module.TEST_NUMBER_ENV, raising=False)
    captured: dict = {}
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured))

    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--log-file", str(tmp_path / "test.log"),
            "--no-preflight",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["approval_test_number"] is None


def test_send_verbose_quiet_mutually_exclusive(tmp_path):
    """Conflict between --verbose and --quiet should fail at usage level."""
    inp = tmp_path / "in.txt"
    inp.write_text("09120000001\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "send",
            "--input", str(inp),
            "--template", "t",
            "--token", "x",
            "--state", str(tmp_path / "s.db"),
            "--verbose",
            "--quiet",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ---------- per-recipient token columns ----------


TRADE_FLAGS = [
    "--token-column", "token=trade_side",
    "--token-column", "token10=first_name",
    "--token-column", "token20=last_token",
    "--value-map", "trade_side:Buy=خرید",
    "--value-map", "trade_side:Sell=فروش",
]


def _write_trade_csv(tmp_path: Path, extra_rows: str = "") -> Path:
    p = tmp_path / "in.csv"
    p.write_text(
        "phone_number,first_name,last_token,trade_side\n"
        "09120000001,علی,ترون,Buy\n"
        "09120000002,سارا,بیت کوین,Sell\n" + extra_rows,
        encoding="utf-8",
    )
    return p


def test_send_token_columns_reach_the_post_body(tmp_path, monkeypatch):
    """Real CLI → Runner → Sender → patched HTTP: each receptor's POST body
    carries its own row's tokens, with trade_side translated to Persian."""
    import sms_sender.sender as sender_mod

    inp = _write_trade_csv(tmp_path)
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY_NOT_A_REAL_ONE")
    bodies: list[dict] = []

    class _Resp:
        status_code = 200
        def json(self):
            return {"return": {"status": 200, "message": "OK"},
                    "entries": [{"messageid": 1, "status": 5}]}

    def fake_post(self, url, data=None, timeout=None, **_):
        bodies.append(dict(data))
        return _Resp()

    monkeypatch.setattr(sender_mod.requests.Session, "post", fake_post)
    result = CliRunner().invoke(cli, [
        "send", "--input", str(inp), "--template", "transaction-1", *TRADE_FLAGS,
        "--state", str(tmp_path / "s.db"), "--log-file", str(tmp_path / "t.log"),
        "--no-preflight", "--workers", "1",
    ])
    assert result.exit_code == 0, result.output
    assert sorted(bodies, key=lambda b: b["receptor"]) == [
        {"receptor": "09120000001", "template": "transaction-1",
         "token": "خرید", "token10": "علی", "token20": "ترون"},
        {"receptor": "09120000002", "template": "transaction-1",
         "token": "فروش", "token10": "سارا", "token20": "بیت کوین"},
    ]


def test_send_token_column_missing_from_header_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVENEGAR_API_KEY", "TEST_KEY")
    inp = _write_trade_csv(tmp_path)
    result = CliRunner().invoke(cli, [
        "send", "--input", str(inp), "--template", "t",
        "--token-column", "token10=nickname",
        "--state", str(tmp_path / "s.db"), "--log-file", str(tmp_path / "t.log"),
        "--no-preflight",
    ])
    assert result.exit_code == 2
    assert "nickname" in result.output
    assert StateStore(tmp_path / "s.db").counts() == {}  # nothing seeded


@pytest.mark.parametrize("flags, message", [
    (["--token-column", "token10"], "TOKEN=COLUMN"),
    (["--token-column", "token9=first_name"], "unknown token"),
    (["--token-column", "token10=first_name", "--token-column", "token10=last_token"],
     "more than once"),
    (["--token", "x", "--token-column", "token=trade_side"], "pick one"),
    (["--value-map", "trade_side:Buy=خرید"], "only applies together"),
    (["--token-column", "token=trade_side", "--value-map", "side:Buy=خرید"],
     "isn't used"),
    (["--token-column", "token=trade_side", "--value-map", "trade_side:Buy"],
     "COLUMN:FROM=TO"),
])
def test_token_column_flag_errors(tmp_path, flags, message):
    inp = _write_trade_csv(tmp_path)
    result = CliRunner().invoke(
        cli, ["preview", "--input", str(inp), "--template", "t", *flags],
    )
    assert result.exit_code == 2
    assert message in result.output


def test_preview_token_columns_show_each_rows_body(tmp_path):
    inp = _write_trade_csv(tmp_path)
    result = CliRunner().invoke(
        cli, ["preview", "--input", str(inp), "--template", "t", *TRADE_FLAGS],
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("POST https://api.kavenegar.com/v1/<API_KEY>/") == 2
    for line in ("token=خرید", "token10=علی", "token20=ترون",
                 "token=فروش", "token10=سارا", "token20=بیت کوین"):
        assert f"  {line}\n" in result.output


def test_preview_phone_picks_its_row_from_input(tmp_path):
    inp = _write_trade_csv(tmp_path)
    result = CliRunner().invoke(cli, [
        "preview", "--input", str(inp), "--phone", "+989120000002",
        "--template", "t", *TRADE_FLAGS,
    ])
    assert result.exit_code == 0, result.output
    assert result.output.count("POST ") == 1
    assert "token10=سارا" in result.output

    missing = CliRunner().invoke(cli, [
        "preview", "--input", str(inp), "--phone", "09129999999",
        "--template", "t", *TRADE_FLAGS,
    ])
    assert missing.exit_code == 2
    assert "no valid row" in missing.output


def test_preview_token_columns_require_input():
    result = CliRunner().invoke(cli, [
        "preview", "--phone", "09120000001", "--template", "t", *TRADE_FLAGS,
    ])
    assert result.exit_code == 2
    assert "needs --input" in result.output


def test_dry_run_token_columns_report_unsendable_rows(tmp_path):
    inp = _write_trade_csv(tmp_path, extra_rows="09120000003,رضا,تتر,Hold\n")
    result = CliRunner().invoke(cli, ["dry-run", "--input", str(inp), *TRADE_FLAGS])
    assert result.exit_code == 0, result.output
    assert "valid=2 invalid=1" in result.output
    assert "token=خرید  token10=علی  token20=ترون" in result.output
    assert "trade_side='Hold' has no entry in its value map" in result.output


def test_profile_can_carry_token_columns(tmp_path):
    """TOML lists feed the repeatable flags through Click's default_map."""
    cfg = tmp_path / "sms-sender.toml"
    cfg.write_text(
        "[profile.trade]\n"
        'template = "transaction-1"\n'
        'token_column = ["token=trade_side", "token10=first_name", "token20=last_token"]\n'
        'value_map = ["trade_side:Buy=خرید", "trade_side:Sell=فروش"]\n',
        encoding="utf-8",
    )
    inp = _write_trade_csv(tmp_path)
    result = CliRunner().invoke(cli, [
        "--config", str(cfg), "--profile", "trade", "preview", "--input", str(inp),
    ])
    assert result.exit_code == 0, result.output
    assert "template=transaction-1" in result.output
    assert "token=فروش" in result.output
    assert "token20=بیت کوین" in result.output
