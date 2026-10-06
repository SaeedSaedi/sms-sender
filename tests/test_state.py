import json
import sqlite3
import threading

import pytest

from sms_sender.state import (
    CampaignMismatchError,
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SCHEMA_VERSION,
    SENT,
    UNKNOWN,
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
    # The new columns and tables work on the upgraded DB.
    s.claim("09120000002")
    s.mark_sent("09120000002", message_id=8, status_code=200, cost=1100)
    s.record_attempt(phone="09120000002", kind="send", outcome="accepted", started_at=3.0)
    assert len(s.attempts_for("09120000002")) == 1


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


def test_may_have_sent_follows_what_could_have_gone_out(tmp_path):
    """The rule that fixes a campaign's message: rejected and not-sent rows
    don't count; an in-flight, unknown or sent one does."""
    s = make(tmp_path)
    s.upsert_pending([("09120000001", "09120000001"), ("09120000002", "09120000002")])
    assert not s.may_have_sent()
    s.claim("09120000001")
    s.mark_failed("09120000001", 411, "[411] invalid receptor", permanent=True)
    assert not s.may_have_sent()
    s.claim("09120000002")
    assert s.may_have_sent()  # in flight: Kavenegar may have it
    s.mark_unknown("09120000002", "read timed out")
    assert s.may_have_sent()


def test_a_reset_leaves_input_rows_that_werent_numbers(tmp_path):
    """They're kept as failed_permanent for export-failed; queued, they'd
    count as waiting with nobody to send to."""
    s = make(tmp_path)
    s.upsert_pending([("09120000001", "09120000001")])
    s.claim("09120000001")
    s.mark_failed("09120000001", 411, "[411] invalid receptor", permanent=True)
    s.record_invalid("not-a-number", "invalid_phone")
    assert s.reset_status(FAILED_PERMANENT) == 1
    assert s.counts() == {PENDING: 1, FAILED_PERMANENT: 1}


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
    # New "run" — the orphan sweep doesn't touch sent rows.
    assert s.mark_orphans_unknown() == 0
    assert s.list_claimable_phones() == []


def test_orphans_become_unknown_not_pending(tmp_path):
    """A row left in_flight by a crash may already have been accepted by
    Kavenegar. Making it claimable again would resend it blindly."""
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    s.claim("09123456789")
    # simulate crash: nothing was committed past in_flight
    s2 = StateStore(tmp_path / "s.db")  # reopen
    assert s2.mark_orphans_unknown() == 1
    assert s2.counts() == {UNKNOWN: 1}
    assert s2.list_claimable_phones() == []
    [row] = s2.attempts_for("09123456789")
    assert (row["kind"], row["outcome"]) == ("recovery", UNKNOWN)


def test_bind_campaign_records_then_enforces_the_name(tmp_path):
    s = make(tmp_path)
    settings = {"template": "a", "tokens": {"token": "x"}}
    assert s.bind_campaign("promo-1", settings) == []
    assert s.get_meta("campaign") == "promo-1"
    assert s.bind_campaign("promo-1", settings) == []  # same campaign, same settings
    with pytest.raises(CampaignMismatchError):
        s.bind_campaign("promo-2", settings)  # another campaign's DB


def test_settings_may_change_until_something_could_have_gone_out(tmp_path):
    s = make(tmp_path)
    s.bind_campaign(None, {"template": "wrong"})
    s.upsert_pending([("09120000001", "09120000001"), ("09120000002", "09120000002")])
    # Nothing sent yet: fixing a wrong template is allowed.
    assert s.bind_campaign(None, {"template": "right"}) == ["template"]

    s.claim("09120000001")
    s.mark_unknown("09120000001", "outcome unknown")  # may have gone out
    with pytest.raises(CampaignMismatchError) as exc:
        s.bind_campaign(None, {"template": "other"})
    assert "template" in str(exc.value)

    assert s.bind_campaign(None, {"template": "other"}, allow_change=True) == ["template"]
    assert json.loads(s.get_meta("settings")) == {"template": "other"}


def test_unknown_rows_are_never_claimed(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    s.claim("09123456789")
    s.mark_unknown("09123456789", "outcome unknown: read timed out")
    assert s.claim("09123456789") is None
    assert s.list_claimable_phones() == []


def test_mark_sent_stores_the_cost(tmp_path):
    s = make(tmp_path)
    s.upsert_pending([("09123456789", "09123456789")])
    s.claim("09123456789")
    s.mark_sent("09123456789", message_id=42, status_code=200, cost=1100)
    conn = sqlite3.connect(tmp_path / "s.db")
    assert conn.execute("SELECT cost FROM recipients").fetchone() == (1100,)
    conn.close()


def test_attempts_are_recorded_in_order_with_secrets_scrubbed(tmp_path):
    s = make(tmp_path)
    s.record_attempt(phone="09123456789", kind="send", outcome="retry", started_at=1.0,
                     detail="http: GET /v1/SECRET_KEY/verify/lookup.json refused")
    s.record_attempt(phone="09123456789", kind="send", outcome="accepted", started_at=2.0,
                     finished_at=2.5, status_code=200, message_id=7, cost=1100)
    rows = s.attempts_for("09123456789")
    assert [r["outcome"] for r in rows] == ["retry", "accepted"]
    assert "SECRET_KEY" not in rows[0]["detail"]
    assert (rows[1]["message_id"], rows[1]["cost"]) == (7, 1100)


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


def test_the_dashboards_recipient_list(tmp_path):
    store = StateStore(tmp_path / "s.db")
    store.upsert_pending([("09120000001", "0912 000 0001"), ("09120000002", "09120000002")], segment="vip")
    store.record_invalid_many([("nope", "not a phone number")])
    store.claim("09120000001")
    store.mark_sent("09120000001", message_id=7, status_code=200, cost=3020)
    assert store.total_cost() == 3020
    assert store.recipient_total() == 3 and store.recipient_total("09120000002") == 1
    rows = store.recipients_page(limit=2)
    assert [(r["phone"], r["status"], r["segment"], r["clicks"]) for r in rows] == [
        ("09120000001", "sent", "vip", None), ("09120000002", "pending", "vip", None),
    ]
    assert [r["phone"] for r in store.recipients_page(limit=2, offset=2)] == ["INVALID:nope"]
    assert [r["phone"] for r in store.recipients_page(limit=5, phone="09120000002")] == ["09120000002"]
    assert store.phone_of_row(rows[1]["id"]) == "09120000002"
    assert store.phone_of_row(999) is None


def test_each_segments_totals_and_how_fast_it_was_delivered(tmp_path):
    """Plan 06, L5: per segment, the sent, delivered, reported and cost; and
    of the sent, how many were seen delivered within each mark."""
    store = StateStore(tmp_path / "s.db")
    store.upsert_pending([("09120000001", "a"), ("09120000002", "b")], segment="vip")
    store.upsert_pending([("09120000003", "c"), ("09120000004", "d")], segment="new")
    conn = store._conn()
    # (phone, delivery status, seconds from sending to the check that saw it, cost)
    for phone, status, after, cost in (("09120000001", 10, 600, 3020), ("09120000002", 11, 600, 3020),
                                       ("09120000003", 10, 5 * 3600, 3020), ("09120000004", None, None, 3020)):
        conn.execute("UPDATE recipients SET status='sent', sent_at=1000, cost=?, delivery_status=?, "
                     "delivery_checked_at=? WHERE phone=?",
                     (cost, status, None if after is None else 1000 + after, phone))
    conn.commit()
    totals = store.segment_totals()
    assert {k: tuple(v) for k, v in totals.items()} == {"vip": ("vip", 2, 1, 2, 6040), "new": ("new", 2, 1, 1, 6040)}
    sent, within = store.delivery_speed([3600, 4 * 3600, 8 * 3600])
    assert (sent, within) == (4, [1, 1, 2])  # the undelivered and the unchecked never count
