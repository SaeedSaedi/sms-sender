"""One number across every campaign DB in a folder (R5 of the 2026-10-05
review): what each campaign recorded for it, read only, newest first."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

from sms_sender.state import SENT, UNKNOWN, StateStore, number_history

A = "09120000001"


def campaign(folder, slug: str) -> StateStore:
    return StateStore(folder / f"{slug}.db")


def sent(store: StateStore, phone: str = A, *, at: float, link: str | None = None) -> None:
    store.upsert_pending([(phone, phone)], segment="vip")
    store.claim(phone, link_key=link)
    store.mark_sent(phone, 1000, 200, 3020)
    store.record_attempt(phone=phone, kind="send", outcome="accepted", started_at=at, message_id=1000)
    store._conn().execute("UPDATE recipients SET sent_at = ? WHERE phone = ?", (at, phone))
    store._conn().commit()


def test_every_campaign_that_knows_the_number_newest_first(tmp_path):
    now = time.time()
    sent(campaign(tmp_path, "coin-price-6"), at=now - 7200)
    newer = campaign(tmp_path, "coin-price-7")
    sent(newer, at=now - 60, link=A)
    newer._conn().execute(
        "INSERT INTO links (key, plan, long_url, title, tags, valid_until, status, created_at, clicks) "
        "VALUES (?, 'p', 'u', 't', '[]', 'v', 'ready', 0, 3)", (A,),
    )
    newer._conn().commit()
    maybe = campaign(tmp_path, "oil-1")
    maybe.upsert_pending([(A, A)])
    maybe.claim(A)
    maybe.mark_unknown(A, "read timed out")
    maybe.record_attempt(phone=A, kind="send", outcome="unknown", started_at=now - 600)
    tested = campaign(tmp_path, "gold-1")  # only the operator's test SMS went to it
    tested.record_attempt(phone=A, kind="test", outcome="accepted", started_at=now - 3600)
    other = campaign(tmp_path, "vip-9")
    sent(other, "09120000002", at=now)  # doesn't know A
    (tmp_path / "broken.db").write_bytes(b"not a database")

    found = number_history(tmp_path, A)
    assert [s.campaign for s in found] == ["coin-price-7", "oil-1", "gold-1", "coin-price-6"]
    latest, unsure, test, oldest = found
    assert (latest.status, latest.sms, latest.clicks, latest.segment) == (SENT, 1, 3, "vip")
    assert (unsure.status, unsure.sms, unsure.undecided) == (UNKNOWN, 0, 1)
    assert (test.status, test.tests, test.sms) == (None, 1, 0)
    assert (oldest.sms, oldest.clicks) == (1, None)  # no link of its own


def test_a_db_from_before_calls_were_recorded_counts_its_sent_row(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE recipients (phone TEXT PRIMARY KEY, raw TEXT, status TEXT, message_id INTEGER, "
        "status_code INTEGER, attempts INTEGER, last_error TEXT, first_seen_at REAL, last_attempt_at REAL, "
        "sent_at REAL)"
    )
    conn.execute("INSERT INTO recipients VALUES (?, ?, 'sent', 1, 200, 1, NULL, 1.0, 2.0, 2.0)", (A, A))
    conn.commit()
    conn.close()
    (found,) = number_history(tmp_path, A)
    assert (found.campaign, found.sms, found.segment, found.delivery_status) == ("old", 1, None, None)
    # Read only: the old DB wasn't upgraded.
    with closing(sqlite3.connect(tmp_path / "old.db")) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(recipients)")]
    assert "segment" not in columns


def test_an_empty_folder_knows_nothing(tmp_path):
    assert number_history(tmp_path, A) == []
