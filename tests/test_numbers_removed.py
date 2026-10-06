"""Plan 06, D2 in a campaign DB: `remove_numbers` leaves no phone number in
the file, keeps every count and join, and the DB never sends again."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from sms_sender.cli import cli
from sms_sender.reconcile import reconcile_unknown
from sms_sender.runner import Runner
from sms_sender.sendcheck import check_sends
from sms_sender.state import (
    REMOVED, LinkRow, NumbersRemovedError, StateStore, scrub_numbers,
)

from .test_runner import FakeSender, write_input

A, B, C, TEST = "09120000001", "09120000002", "09120000003", "09150000077"


def link(key: str, ref: str) -> LinkRow:
    return LinkRow(key=key, ref=ref, long_url=f"https://kifpool.me/offer?r={ref}", title="coin-7",
                   tags=("campaign-coin-7",), valid_until="2026-12-01T00:00:00+00:00", plan="p1")


def campaign(path: Path) -> StateStore:
    """A sent campaign with one of everything that holds a number."""
    store = StateStore(path)
    store.bind_campaign("coin-7", {"template": "coin-price"})
    store.upsert_pending([(A, A), (B, "+98 912 000 0002"), (C, C)], segment="vip")
    store.record_invalid("0912000000x", "not a phone number")
    store.add_links([link(A, "r1"), link(f"test:{TEST}", "r9")])
    store.mark_link_ready(A, "c0001", "https://kifpool.me/u/c0001")
    now = time.time()
    store.claim(A, link_key=A)
    store.mark_sent(A, 1001, 200, 3020)
    store.record_attempt(phone=A, kind="send", outcome="accepted", started_at=now, message_id=1001, cost=3020)
    store.claim(B)
    store.mark_failed(B, 411, f"[411] invalid receptor {B}", permanent=True)
    store.record_attempt(phone=B, kind="send", outcome="rejected", started_at=now, status_code=411,
                         detail=f"receptor {B} isn't valid")
    store.claim(C)
    store.mark_unknown(C, "read timed out")
    store.record_attempt(phone=C, kind="send", outcome="unknown", started_at=now)
    store.record_attempt(phone=TEST, kind="test", outcome="accepted", started_at=now, message_id=999)
    store.record_delivery({A: 10}, now)
    store.add_conversions([(A, "ref", "r1", None, 50_000.0, now)], batch="b1")
    return store


def everything(store: StateStore) -> dict:
    return {
        "counts": store.counts(), "shown": store.display_counts(), "delivery": store.delivery_counts(),
        "cost": store.total_cost(), "links": store.link_counts(), "conversions": store.conversion_totals(),
        "sends": (lambda s: (s.sms, s.recipients, s.test_sms, len(s.twice), len(s.maybe_twice)))(check_sends(store)),
    }


def test_no_number_is_left_in_the_file_and_every_count_stays(tmp_path):
    db = tmp_path / "coin-7.db"
    store = campaign(db)
    before = everything(store)
    assert store.remove_numbers(now=1_800_000_000.0) == 5  # A, B, C, the invalid row, the test number

    store = StateStore(db)
    assert everything(store) == before
    assert store.numbers_removed_at() == 1_800_000_000.0
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("coin-7.db"))
    for number in (b"0912000000", b"912 000 0002", b"0915000007"):
        assert number not in raw


def test_each_number_has_one_placeholder_everywhere(tmp_path):
    db = tmp_path / "coin-7.db"
    campaign(db).remove_numbers()
    store = StateStore(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["message_id"] or r["status_code"] or r["phone"][:8]: r for r in conn.execute("SELECT * FROM recipients")}
    sent, rejected, invalid = rows[1001], rows[411], rows["INVALID:"]
    assert sent["phone"].startswith(REMOVED) and sent["raw"] == ""
    assert invalid["phone"].startswith(f"INVALID:{REMOVED}") and invalid["raw"] == ""
    # Its link and its calls follow it, so clicks and the double-send check join as before.
    assert sent["link_key"] == sent["phone"]
    assert set(store.get_links([sent["phone"]])) == {sent["phone"]}
    assert [a["kind"] for a in store.attempts_for(sent["phone"])] == ["send"]
    assert store.phones_for_refs(["r1"]) == {"r1": sent["phone"]}
    # The test number's link and call share one placeholder too.
    (test_key,) = [r["key"] for r in conn.execute("SELECT key FROM links WHERE key LIKE 'test:%'")]
    assert test_key.startswith(f"test:{REMOVED}")
    assert store.attempts_for(test_key[len("test:"):])[0]["kind"] == "test"
    # Text that held a number is scrubbed; an invalid input row stays invalid.
    assert rejected["last_error"] == "[411] invalid receptor ***"
    assert store.attempts_for(rejected["phone"])[0]["detail"] == "receptor *** isn't valid"
    assert store.display_counts().get("invalid") == 1


def test_it_happens_once(tmp_path):
    store = campaign(tmp_path / "coin-7.db")
    assert store.remove_numbers() == 5
    assert StateStore(tmp_path / "coin-7.db").remove_numbers() == 0


def test_a_db_without_its_numbers_never_sends_again(tmp_path):
    db = tmp_path / "coin-7.db"
    campaign(db).remove_numbers()
    store = StateStore(db)
    with pytest.raises(NumbersRemovedError):
        store.upsert_pending([(A, A)])
    with pytest.raises(NumbersRemovedError):
        store.bind_campaign("coin-7", {"template": "coin-price"})
    sender = FakeSender()
    with pytest.raises(NumbersRemovedError):
        Runner(input_path=write_input(tmp_path, [A, B]), state=store, sender=sender, workers=1,
               campaign="coin-7", settings={"template": "coin-price"}).run()
    assert sender.calls == []  # nobody it already sent to gets it again


def test_reconciling_asks_kavenegar_nothing(tmp_path):
    db = tmp_path / "coin-7.db"
    campaign(db).remove_numbers()

    class Refuses:
        def find_messages(self, *a, **kw):
            raise AssertionError("a placeholder isn't a number Kavenegar knows")

    summary = reconcile_unknown(StateStore(db), Refuses(), min_age_sec=0)
    assert (summary.sent, summary.requeued, summary.needs_review) == (0, 0, 0)


def test_the_cli_says_so(tmp_path):
    db = tmp_path / "coin-7.db"
    campaign(db).remove_numbers(now=time.mktime((2027, 10, 7, 9, 0, 0, 0, 0, -1)))
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert result.exit_code == 0
    assert "numbers    removed 2027-10-07" in result.output and "never sends again" in result.output


def test_activity_and_segments_since(tmp_path):
    store = campaign(tmp_path / "coin-7.db")
    last = store.last_activity()
    assert last is not None and time.time() - last < 60
    assert store.segments_active_since(time.time() - 60) == {"vip"}
    assert store.segments_active_since(time.time() + 60) == set()
    assert StateStore(tmp_path / "empty.db").last_activity() is None


@pytest.mark.parametrize("text, scrubbed", [
    ("[411] invalid receptor 09121234567", "[411] invalid receptor ***"),
    ("to +989121234567 and 00989121234568", "to *** and ***"),
    ("۰۹۱۲۱۲۳۴۵۶۷ رد شد", "*** رد شد"),
    ("message 123456789012, cost 3020", "message 123456789012, cost 3020"),
    (None, None),
])
def test_numbers_in_text_are_scrubbed(text, scrubbed):
    assert scrub_numbers(text) == scrubbed


def test_the_cli_refuses_to_send_from_it(tmp_path, monkeypatch):
    db = tmp_path / "coin-7.db"
    campaign(db).remove_numbers()
    monkeypatch.setenv("KAVENEGAR_API_KEY", "not-a-real-key")
    calls = []
    monkeypatch.setattr("sms_sender.sender.requests.post", lambda *a, **kw: calls.append(a) or None)
    result = CliRunner().invoke(cli, [
        "send", "--state", str(db), "--input", str(write_input(tmp_path, [A, B])),
        "--template", "coin-price", "--token", "x", "--send-window", "off",
    ])
    assert result.exit_code == 2, result.output
    assert "phone numbers were removed" in result.output and calls == []
