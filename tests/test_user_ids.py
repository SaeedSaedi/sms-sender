"""User IDs and segments (Phase 2.2, decided 2026-10-04): a blank user ID
is still sent and reported as "missing user ID"; a phone that comes with
two different user IDs is invalid and never sent — within one file or
against an earlier import."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from sms_sender import cli as cli_module
from sms_sender.cli import cli
from sms_sender.input_loader import InputError, TokenColumns, load, segment_from_path, slugify
from sms_sender.runner import Runner, format_report
from sms_sender.state import INVALID, PENDING, SENT, UNKNOWN, StateStore

from .test_runner import FakeSender, RecordingReporter

A, B, C = "09120000001", "09120000002", "09120000003"


def csv_file(tmp_path: Path, text: str, name: str = "in.csv") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------- loader ----------

def test_user_ids_are_read_and_a_blank_one_is_still_sent(tmp_path):
    r = load(csv_file(tmp_path, f"phone,uid\n{A},u-1\n{B},\n"), user_id_column="uid")
    assert [(x.phone, x.user_id) for x in r.valid] == [(A, "u-1"), (B, None)]
    assert r.missing_user_id == 1
    assert r.invalid == [] and r.conflicts == frozenset()


def test_two_different_user_ids_make_every_row_of_that_phone_invalid(tmp_path):
    r = load(
        csv_file(tmp_path, f"phone,uid\n{A},u-1\n{B},u-2\n+98{A[1:]},u-9\n"),
        user_id_column="uid",
    )
    assert [x.phone for x in r.valid] == [B]
    assert r.conflicts == frozenset({A})
    assert [(x.line_no, x.reason) for x in r.invalid] == [
        (2, "conflicting user IDs (u-1, u-9); not sent"),
        (4, "conflicting user IDs (u-1, u-9); not sent"),
    ]


def test_the_same_or_a_blank_user_id_again_is_no_conflict(tmp_path):
    r = load(
        csv_file(tmp_path, f"phone,uid\n{A},\n{A},u-1\n{B},u-2\n{B},u-2\n"),
        user_id_column="uid",
    )
    # A's ID comes from its second row; the repeats are plain duplicates.
    assert [(x.phone, x.user_id) for x in r.valid] == [(A, "u-1"), (B, "u-2")]
    assert (r.duplicates_collapsed, r.missing_user_id, r.conflicts) == (2, 0, frozenset())


def test_invalid_rows_keep_file_order_with_user_ids_and_tokens(tmp_path):
    r = load(
        csv_file(tmp_path, f"phone,uid,name\nnope,u-0,x\n{A},u-1,\n{B},u-2,Sara\n{B},u-3,Sara\n"),
        TokenColumns(columns={"token10": "name"}), user_id_column="uid",
    )
    assert [x.line_no for x in r.invalid] == [2, 3, 4, 5]
    assert r.valid == []


def test_a_missing_or_wrong_user_id_column_is_refused(tmp_path):
    p = csv_file(tmp_path, f"phone,uid\n{A},u-1\n")
    with pytest.raises(InputError, match="user_id not in the header"):
        load(p, user_id_column="user_id")
    with pytest.raises(InputError, match="is the phone column"):
        load(p, user_id_column="phone")


def test_segment_names_come_from_the_file_name():
    assert segment_from_path("data/segments/Seg 2 (VIP).csv") == "seg-2-vip"
    assert segment_from_path("data/segments/سگمنت.csv") == "segment"
    assert slugify("transaction-1-seg2") == "transaction-1-seg2"


# ---------- state ----------

def test_segment_is_recorded_once_and_old_rows_get_one(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A)])  # e.g. created before segments existed
    state.upsert_pending([(A, A), (B, B)], segment="vip-2")
    state.upsert_pending([(B, B)], segment="other")
    rows = dict(state._conn().execute("SELECT phone, segment FROM recipients").fetchall())
    assert rows == {A: "vip-2", B: "vip-2"}


def test_assign_user_ids_fills_blanks_and_reports_conflicts(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)])
    assert state.assign_user_ids({A: "u-1"}) == []
    assert state.assign_user_ids({A: "u-1", B: "u-2"}) == []
    assert state.assign_user_ids({A: "u-9", C: "u-3"}) == [(A, "u-1", "u-9")]  # C isn't a row
    ids = dict(state._conn().execute("SELECT phone, user_id FROM recipients").fetchall())
    assert ids == {A: "u-1", B: "u-2"}
    assert state.user_id_counts() == (2, 0)


def test_exclude_only_takes_unsent_rows_out_of_the_queue(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)])
    state.claim(A)
    state.mark_sent(A, message_id=1, status_code=200)
    assert state.exclude({A, B}, "conflicting user IDs; not sent") == 1
    assert state.counts() == {SENT: 1, INVALID: 1}


# ---------- runner ----------

def run(tmp_path, inp, state, sender=None, **kw):
    sender = sender or FakeSender()
    summary = Runner(
        input_path=inp, state=state, sender=sender, workers=1,
        reporter=RecordingReporter(), **kw,
    ).run()
    return summary, sender


def test_blank_ids_are_sent_and_conflicts_are_not(tmp_path):
    state = StateStore(tmp_path / "s.db")
    inp = csv_file(tmp_path, f"phone,uid\n{A},u-1\n{B},\n{C},u-3\n{C},u-4\n", "vip-2.csv")
    summary, sender = run(tmp_path, inp, state, user_id_column="uid")
    assert sorted(sender.calls) == [A, B]
    assert (summary.missing_user_id, summary.user_id_conflicts) == (1, 1)
    assert "missing user ID   1" in format_report(summary)
    rows = state._conn().execute(
        "SELECT phone, user_id, segment FROM recipients WHERE phone NOT LIKE 'INVALID:%' "
        "ORDER BY phone"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(A, "u-1", "vip-2"), (B, None, "vip-2")]


def test_a_queued_phone_whose_new_import_disagrees_is_not_sent(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)], segment="seg-1")
    state.assign_user_ids({A: "u-1", B: "u-2"})
    inp = csv_file(tmp_path, f"phone,uid\n{A},u-9\n{B},u-2\n")
    summary, sender = run(tmp_path, inp, state, user_id_column="uid")
    assert sender.calls == [B]
    assert summary.user_id_conflicts == 1
    assert state.counts() == {SENT: 1, INVALID: 1}


def test_a_phone_conflicting_in_the_file_is_taken_out_even_if_queued_before(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A)], segment="seg-1")  # queued by an earlier import, no ID
    inp = csv_file(tmp_path, f"phone,uid\n{A},u-1\n{A},u-2\n{B},u-3\n")
    _, sender = run(tmp_path, inp, state, user_id_column="uid")
    assert sender.calls == [B]
    status = state._conn().execute("SELECT status FROM recipients WHERE phone=?", (A,)).fetchone()
    assert status[0] == INVALID


def test_an_explicit_segment_wins_over_the_file_name(tmp_path):
    state = StateStore(tmp_path / "s.db")
    inp = csv_file(tmp_path, f"{A}\n", "whatever.txt")
    run(tmp_path, inp, state, segment="vip-2")
    assert state._conn().execute("SELECT segment FROM recipients").fetchone()[0] == "vip-2"
    assert state.counts() == {SENT: 1}
    assert state.get_meta("user_id_column") is None  # no IDs in this campaign


# ---------- CLI ----------

def test_send_passes_user_id_column_and_segment(tmp_path, monkeypatch):
    from .test_cli import _send_args, _stub_make_runner

    monkeypatch.chdir(tmp_path)
    Path("in.txt").write_text(f"{A}\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "load_api_key", lambda: "k")
    captured: dict = {}
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured))
    result = CliRunner().invoke(cli, _send_args("--user-id-column", "uid", "--segment", "vip-2"))
    assert result.exit_code == 0, result.output
    assert (captured["user_id_column"], captured["segment"]) == ("uid", "vip-2")

    bad = CliRunner().invoke(cli, _send_args("--segment", "VIP 2"))
    assert bad.exit_code == 2 and "lowercase letters" in bad.output


def test_dry_run_reports_missing_and_conflicting_ids(tmp_path):
    inp = csv_file(tmp_path, f"phone,uid\n{A},u-1\n{B},\n{C},u-3\n{C},u-4\n")
    result = CliRunner().invoke(cli, ["dry-run", "--input", str(inp), "--user-id-column", "uid"])
    assert result.exit_code == 0, result.output
    assert "missing user ID=1 (still sent)" in result.output
    assert "conflicting user IDs=1 phone(s) (not sent)" in result.output
    assert f"{B}  (raw='{B}')  user_id=(missing user ID)" in result.output


def test_status_counts_missing_user_ids(tmp_path):
    db = tmp_path / "s.db"
    state = StateStore(db)
    state.upsert_pending([(A, A), (B, B)])
    state.assign_user_ids({A: "u-1"})
    state.set_meta("user_id_column", "uid")
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert "user IDs   1 recipient(s); missing user ID: 1" in result.output
    assert PENDING in result.output


def test_an_excluded_phone_stays_out_through_retries(tmp_path):
    """Unlike failed_permanent, no retry resets an invalid row."""
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)])
    state.assign_user_ids({A: "u-1"})
    inp = csv_file(tmp_path, f"phone,uid\n{A},u-9\n")
    _, first = run(tmp_path, inp, state, user_id_column="uid")
    assert first.calls == [B]  # queued earlier, no conflict
    assert state.reset_status("failed_permanent") == 0  # what retry-failed --include-permanent does
    _, second = run(tmp_path, csv_file(tmp_path, f"{A}\n", "plain.txt"), state)
    assert second.calls == []
    assert state.counts() == {SENT: 1, INVALID: 1}


def test_a_conflicting_leftover_requeued_by_reconciliation_is_not_sent(tmp_path):
    """The exclusion comes after the start-of-run reconciliation, so a row
    it makes claimable again is still caught."""
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)])
    state.assign_user_ids({A: "u-1"})
    state.claim(A)
    state.mark_unknown(A, "read timeout")  # Kavenegar never got it, as it turns out

    class NothingAtKavenegar(FakeSender):
        def find_messages(self, phone, start, end):
            return []

    inp = csv_file(tmp_path, f"phone,uid\n{A},u-9\n{B},u-2\n")
    _, sender = run(
        tmp_path, inp, state, sender=NothingAtKavenegar(), user_id_column="uid",
        reconcile_min_age_sec=0, reconcile_requeue_not_found=True,
    )
    assert sender.calls == [B]
    assert state.counts() == {SENT: 1, INVALID: 1}
    assert UNKNOWN not in state.counts()


def test_reset_invalid_asks_first(tmp_path):
    db = tmp_path / "s.db"
    state = StateStore(db)
    state.upsert_pending([(A, A)])
    state.exclude({A}, "conflicting user IDs; not sent")
    declined = CliRunner().invoke(cli, ["reset", "--status", "invalid", "--state", str(db)], input="n\n")
    assert declined.exit_code == 1 and "two different user IDs" in declined.output
    assert state.counts() == {INVALID: 1}
    done = CliRunner().invoke(cli, ["reset", "--status", "invalid", "--state", str(db), "-y"])
    assert done.exit_code == 0 and state.counts() == {PENDING: 1}
