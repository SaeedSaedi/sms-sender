"""One send, several lists (the dashboard's segments, in order): every
list is read before anything is sent, a number in two lists gets one SMS
from the first, each recipient keeps its own list's segment, and a user
ID that disagrees across lists stops that number as it would within one
file."""
from __future__ import annotations

from pathlib import Path

from sms_sender.input_loader import InputPart, TokenColumns, load, load_parts
from sms_sender.runner import Runner
from sms_sender.sender import HaltError
from sms_sender.state import FAILED_PERMANENT, INVALID, PENDING, SENT, StateStore

from .test_runner import AccountFakeSender, FakeSender, RecordingReporter

A, B, C, D = "09120000001", "09120000002", "09120000003", "09120000004"
NAMES = TokenColumns(columns={"token10": "name"})


def csv_file(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def part(tmp_path, slug, text, user_id_column=None) -> InputPart:
    return InputPart(csv_file(tmp_path, f"{slug}.csv", text), slug, user_id_column)


# ---------- the loader ----------

def test_one_list_reads_as_load_does(tmp_path):
    p = part(tmp_path, "vip", f"phone,uid,name\n{A},u-1,Ali\n{A},u-1,Ali\nnope,u-2,x\n", "uid")
    alone = load(p.path, NAMES, "uid")
    both = load_parts([p], NAMES)
    assert (both.valid, both.invalid, both.duplicates_collapsed) == (
        alone.valid, alone.invalid, alone.duplicates_collapsed,
    )
    assert both.segments == {A: "vip"}


def test_a_number_in_two_lists_keeps_the_first_lists_row(tmp_path):
    first = part(tmp_path, "vip", f"phone,name\n{A},Ali\n{B},Sara\n")
    second = part(tmp_path, "vip-2", f"phone,name\n{B},Reza\n{C},Mina\n")
    loaded = load_parts([first, second], NAMES)
    assert [(r.phone, r.tokens["token10"]) for r in loaded.valid] == [
        (A, "Ali"), (B, "Sara"), (C, "Mina"),
    ]
    assert loaded.segments == {A: "vip", B: "vip", C: "vip-2"}
    assert loaded.duplicates_collapsed == 1


def test_user_ids_that_disagree_across_lists_stop_the_number(tmp_path):
    first = part(tmp_path, "vip", f"phone,uid\n{A},u-1\n{B},u-2\n{C},\n", "uid")
    second = part(tmp_path, "vip-2", f"phone,uid\n{A},u-9\n{B},u-2\n{C},u-3\n", "uid")
    loaded = load_parts([first, second])
    # A has two IDs: no row of it is sent. B agrees. C's ID comes from the second list.
    assert [(r.phone, r.user_id) for r in loaded.valid] == [(B, "u-2"), (C, "u-3")]
    assert loaded.conflicts == frozenset({A})
    assert [(r.raw, r.key, r.reason) for r in loaded.invalid] == [
        (A, "conflicting_user_ids", "conflicting user IDs (u-1, u-9); not sent"),
        (A, "conflicting_user_ids", "conflicting user IDs (u-1, u-9); not sent"),
    ]
    assert loaded.missing_user_id == 0


def test_a_conflict_within_one_list_holds_for_the_others(tmp_path):
    first = part(tmp_path, "vip", f"phone,uid\n{A},u-1\n{A},u-2\n", "uid")
    second = part(tmp_path, "plain", f"phone\n{A}\n{B}\n")
    loaded = load_parts([first, second])
    assert [r.phone for r in loaded.valid] == [B]
    assert loaded.conflicts == frozenset({A})
    assert [r.reason for r in loaded.invalid][-1] == "conflicting user IDs; not sent"


def test_missing_user_ids_count_only_where_a_list_has_the_column(tmp_path):
    first = part(tmp_path, "vip", f"phone,uid\n{A},\n{B},u-2\n", "uid")
    second = part(tmp_path, "plain", f"phone\n{C}\n")
    loaded = load_parts([first, second])
    assert [(r.phone, r.user_id) for r in loaded.valid] == [(A, None), (B, "u-2"), (C, None)]
    assert loaded.missing_user_id == 1


# ---------- the runner ----------

def runner(tmp_path, state, sender, parts: list[InputPart], **kw) -> Runner:
    first, *more = parts
    return Runner(
        input_path=first.path, segment=first.segment, user_id_column=first.user_id_column,
        more_inputs=more, state=state, sender=sender, workers=1, reporter=RecordingReporter(), **kw,
    )


def segments_of(state: StateStore) -> dict[str, str]:
    rows = state._conn().execute(
        "SELECT phone, segment FROM recipients WHERE phone NOT LIKE 'INVALID:%'"
    ).fetchall()
    return dict(rows)


def test_every_list_is_sent_once_with_its_own_tokens_and_segment(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    parts = [
        part(tmp_path, "vip", f"phone,uid,name\n{A},u-1,Ali\n{B},u-2,Sara\n", "uid"),
        part(tmp_path, "vip-2", f"phone,name\n{B},Reza\n{C},Mina\nnope,x\n"),
    ]
    summary = runner(tmp_path, state, sender, parts, token_columns=NAMES).run()
    assert sorted(sender.calls) == [A, B, C]
    assert {phone: tokens["token10"] for phone, tokens in sender.tokens.items()} == {
        A: "Ali", B: "Sara", C: "Mina",
    }
    assert segments_of(state) == {A: "vip", B: "vip", C: "vip-2"}
    assert (summary.sent, summary.duplicates_collapsed, summary.invalid) == (3, 1, 1)
    assert summary.total_input == 4  # 3 valid + 1 invalid; the repeat isn't counted
    assert state.get_meta("user_id_column") == "uid"
    ids = dict(state._conn().execute("SELECT phone, user_id FROM recipients WHERE user_id IS NOT NULL"))
    assert ids == {A: "u-1", B: "u-2"}


def test_every_list_is_in_the_campaign_db_before_the_first_sms(tmp_path):
    """So a stop in the first list leaves the later lists' recipients
    waiting, where a cancel finds them."""
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()

    def no_credit():
        raise HaltError(418, "insufficient credit")

    sender.behavior[A] = no_credit
    parts = [part(tmp_path, "vip", f"phone\n{A}\n"), part(tmp_path, "vip-2", f"phone\n{C}\n{D}\n")]
    summary = runner(tmp_path, state, sender, parts).run()
    assert summary.halted and sender.calls == [A]
    assert segments_of(state) == {A: "vip", C: "vip-2", D: "vip-2"}
    assert state.counts() == {"failed_retriable": 1, PENDING: 2}
    assert state.cancel_remaining() == 3


def test_the_credit_estimate_covers_every_list(tmp_path):
    """3 recipients across two lists at 1,200 each need 3,600; 3,000 is
    enough for the first list alone, but nothing goes out."""
    sender = AccountFakeSender(credit=3_000, cost=1_200)
    parts = [part(tmp_path, "vip", f"phone\n{A}\n"), part(tmp_path, "vip-2", f"phone\n{B}\n{C}\n")]
    summary = runner(tmp_path, StateStore(tmp_path / "s.db"), sender, parts, cost_per_sms=1_200).run()
    assert summary.halted and sender.calls == []
    assert (summary.stop_reason, summary.stop_fields) == (
        "not_enough_credit", {"estimate": 3_600, "recipients": 3, "credit": 3_000},
    )


def test_a_later_run_skips_whoever_any_list_already_got(tmp_path):
    state = StateStore(tmp_path / "s.db")
    runner(tmp_path, state, FakeSender(), [part(tmp_path, "vip", f"phone\n{A}\n{B}\n")]).run()
    sender = FakeSender()
    parts = [part(tmp_path, "vip", f"phone\n{A}\n{B}\n"), part(tmp_path, "vip-2", f"phone\n{B}\n{C}\n")]
    summary = runner(tmp_path, state, sender, parts).run()
    assert sender.calls == [C]
    assert summary.sent == 1
    assert state.counts() == {SENT: 3}
    assert segments_of(state)[C] == "vip-2"


def test_a_number_whose_lists_disagree_on_its_user_id_is_not_sent(tmp_path):
    state = StateStore(tmp_path / "s.db")
    sender = FakeSender()
    parts = [
        part(tmp_path, "vip", f"phone,uid\n{A},u-1\n{B},u-2\n", "uid"),
        part(tmp_path, "vip-2", f"phone,uid\n{A},u-9\n", "uid"),
    ]
    summary = runner(tmp_path, state, sender, parts).run()
    assert sender.calls == [B]
    assert summary.user_id_conflicts == 1
    # A's rows are recorded as invalid input (one row per text, as within one file).
    assert state.counts() == {SENT: 1, FAILED_PERMANENT: 1}
    assert INVALID not in state.counts()
