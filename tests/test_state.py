import sqlite3
import threading

import pytest

from sms_sender.state import (
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SCHEMA_VERSION,
    SENT,
    StateSchemaError,
    StateStore,
)


def make(tmp_path):
    return StateStore(tmp_path / "s.db")


def user_version(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------- schema versions ----------

# Every state DB written before versioning has exactly this schema and
# user_version 0 — e.g. the existing campaign DBs in data/db/.
_LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipients (
    phone           TEXT PRIMARY KEY,
    raw             TEXT NOT NULL,
    status          TEXT NOT NULL,
    message_id      INTEGER,
    status_code     INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    first_seen_at   REAL NOT NULL,
    last_attempt_at REAL,
    sent_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status);
"""


def test_new_db_is_created_at_the_current_version(tmp_path):
    make(tmp_path)
    assert user_version(tmp_path / "s.db") == SCHEMA_VERSION


def test_legacy_db_is_upgraded_in_place(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO recipients (phone, raw, status, message_id, status_code, attempts, "
        "first_seen_at, sent_at) VALUES ('09120000001', '09120000001', 'sent', 7, 200, 1, 1.0, 2.0)"
    )
    conn.execute(
        "INSERT INTO recipients (phone, raw, status, first_seen_at) "
        "VALUES ('09120000002', '09120000002', 'pending', 1.0)"
    )
    conn.commit()
    conn.close()

    s = StateStore(db)
    assert user_version(db) == SCHEMA_VERSION
    assert s.counts() == {SENT: 1, PENDING: 1}
    assert s.list_claimable_phones() == ["09120000002"]


def test_db_from_a_newer_version_is_refused(tmp_path):
    db = tmp_path / "s.db"
    StateStore(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(StateSchemaError):
        StateStore(db)


def test_concurrent_opens_migrate_once(tmp_path):
    """Processes opening the same old DB at once must not re-apply a step
    (an ALTER TABLE run twice fails with "duplicate column")."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(_LEGACY_SCHEMA)
    conn.close()
    errors: list[BaseException] = []

    def open_store():
        try:
            StateStore(db)
        except BaseException as e:  # noqa: BLE001 — surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert user_version(db) == SCHEMA_VERSION


def test_upsert_is_idempotent(tmp_path):
    s = make(tmp_path)
    n1 = s.upsert_pending([("09123456789", "09123456789"), ("09120000000", "09120000000")])
    n2 = s.upsert_pending([("09123456789", "09123456789")])
    assert n1 == 2
    assert n2 == 0


def test_claim_transitions_atomically(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    r = s.claim("09123456789")
    assert r is not None and r.phone == "09123456789" and r.attempts == 1
    # second claim returns None — already in_flight
    assert s.claim("09123456789") is None


def test_mark_sent_then_resume_skips(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    s.claim("09123456789")
    s.mark_sent("09123456789", message_id=42, status_code=200)
    counts = s.counts()
    assert counts == {SENT: 1}
    # New "run" — orphan reset doesn't touch sent rows.
    assert s.reset_orphan_in_flight() == 0
    assert s.list_claimable_phones() == []


def test_orphan_reset_brings_back_in_flight(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    s.claim("09123456789")
    # simulate crash: nothing was committed past in_flight
    s2 = StateStore(tmp_path / "s.db")  # reopen
    assert s2.reset_orphan_in_flight() == 1
    assert s2.list_claimable_phones() == ["09123456789"]


def test_mark_failed_permanent_vs_retriable(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789"), ("09120000000", "09120000000")])
    s.claim("09123456789")
    s.mark_failed("09123456789", 422, "bad chars", permanent=True)
    s.claim("09120000000")
    s.mark_failed("09120000000", 409, "server down", permanent=False)
    counts = s.counts()
    assert counts.get(FAILED_PERMANENT) == 1
    assert counts.get(FAILED_RETRIABLE) == 1
    # Retriable rows are claimable in the next run; permanent ones are not.
    assert s.list_claimable_phones() == ["09120000000"]


def test_concurrent_claims_only_one_wins(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(s.claim("09123456789"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].attempts == 1
    assert s.counts().get(PENDING, 0) == 0


def test_record_invalid(tmp_path):
    s = make(tmp_path)
    s.record_invalid("not-a-phone", "no digits")
    counts = s.counts()
    assert counts == {FAILED_PERMANENT: 1}
    rows = list(s.iter_failed_permanent())
    assert rows[0]["raw"] == "not-a-phone"
    assert rows[0]["last_error"] == "no digits"


def test_record_invalid_many_single_transaction(tmp_path):
    """Bulk-recording invalids should be one transaction, not N. Verifies
    correctness; performance is observable via this single-tx behaviour."""
    s = make(tmp_path)
    rows = [(f"junk{i}", f"reason {i}") for i in range(50)]
    s.record_invalid_many(rows)
    counts = s.counts()
    assert counts == {FAILED_PERMANENT: 50}
    # Re-feeding the same batch is a no-op (synthetic key keeps PK contract).
    s.record_invalid_many(rows)
    assert s.counts() == {FAILED_PERMANENT: 50}


def test_record_invalid_many_empty_is_noop(tmp_path):
    s = make(tmp_path)
    s.record_invalid_many([])
    assert s.counts() == {}


def test_upsert_pending_returns_only_newly_inserted(tmp_path):
    """Edge: re-inserting some-existing-some-new should count just the new ones."""
    s = make(tmp_path)
    assert s.upsert_pending([("09120000001", "x"), ("09120000002", "y")]) == 2
    assert s.upsert_pending([("09120000002", "y"), ("09120000003", "z")]) == 1
    assert s.counts().get(PENDING) == 3
