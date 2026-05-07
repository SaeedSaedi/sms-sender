import threading

from sms_sender.state import (
    FAILED_PERMANENT,
    FAILED_RETRIABLE,
    PENDING,
    SENT,
    StateStore,
)


def make(tmp_path):
    return StateStore(tmp_path / "s.db")


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
