"""SQLite-backed state store. Owns the "never send twice" guarantee.

Connection-per-thread (sqlite3 connections are not shareable across threads).
WAL mode + immediate commits keep the crash window tiny: a row only flips to
`sent` after the API confirmed 200, and that flip is committed before we
release the row.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .redact import redact_secrets

# Schema upgrades, tracked with `PRAGMA user_version`: _MIGRATIONS[i] takes a
# DB from version i to i + 1. Version 1 is the original schema; DBs created
# before versioning report version 0 but already have it, so that step uses
# IF NOT EXISTS and is a no-op for them. Append new steps; never edit one
# that has shipped — existing DBs have already applied it.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # 0 → 1: original schema
        """
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
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status)",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)


class StateSchemaError(RuntimeError):
    """The DB was written by a newer sms-sender than this one."""


# Status values
PENDING = "pending"
IN_FLIGHT = "in_flight"
SENT = "sent"
FAILED_PERMANENT = "failed_permanent"
FAILED_RETRIABLE = "failed_retriable"

CLAIMABLE = (PENDING, FAILED_RETRIABLE)


@dataclass(frozen=True)
class Recipient:
    phone: str
    raw: str
    attempts: int


class StateStore:
    """Thread-safe SQLite repository.

    Each thread gets its own connection on first use; the file is opened in
    WAL mode so concurrent writers don't block each other for long.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        # New connection — used for setup. Not cached.
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        """Bring the DB up to SCHEMA_VERSION in one transaction.

        BEGIN IMMEDIATE serializes concurrent openers and the version is read
        inside the transaction, so two processes opening an old DB at once
        apply each step exactly once.
        """
        conn = self._connect()
        try:
            self._ensure_wal(conn)  # can't be changed inside a transaction
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise StateSchemaError(
                        f"{self.db_path} has schema version {version}, but this "
                        f"sms-sender only knows up to {SCHEMA_VERSION}; upgrade sms-sender"
                    )
                for step in _MIGRATIONS[version:]:
                    for statement in step:
                        conn.execute(statement)
                if version < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _ensure_wal(conn: sqlite3.Connection) -> None:
        """Switch the DB to WAL. The mode is stored in the file, so this only
        does work the first time a DB is opened.

        The switch needs an exclusive lock, and SQLite reports a concurrent
        opener as "database is locked" at once instead of waiting out the
        busy timeout — so retry for a while.
        """
        deadline = time.monotonic() + 30
        while True:
            try:
                if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            # WAL itself is persistent (set by _migrate); synchronous is per-connection.
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ---------- bulk loading ----------

    def upsert_pending(self, rows: list[tuple[str, str]]) -> int:
        """Insert (phone, raw) pairs as pending. Returns number of new rows."""
        if not rows:
            return 0
        now = time.time()
        with self._tx() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO recipients "
                "(phone, raw, status, first_seen_at) VALUES (?, ?, ?, ?)",
                [(p, r, PENDING, now) for p, r in rows],
            )
            return conn.total_changes - before

    def record_invalid(self, raw: str, reason: str) -> None:
        """Persist a single structurally invalid input as a permanent failure.

        Prefer `record_invalid_many` when seeding from a parsed input — this
        single-row variant runs its own transaction.
        """
        self.record_invalid_many([(raw, reason)])

    def record_invalid_many(self, rows: list[tuple[str, str]]) -> None:
        """Persist a batch of invalid inputs in one transaction.

        Each row is keyed by a synthetic `INVALID:<raw>` phone so it can't
        collide with real numbers and so re-feeding the same input is a no-op.
        """
        if not rows:
            return
        now = time.time()
        payload = [
            (f"INVALID:{raw}", raw, FAILED_PERMANENT, reason, now, now)
            for raw, reason in rows
        ]
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO recipients "
                "(phone, raw, status, attempts, last_error, first_seen_at, last_attempt_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                payload,
            )

    # ---------- run lifecycle ----------

    def reset_status(self, from_status: str) -> int:
        """Promote rows in `from_status` back to pending. Returns rows changed."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE recipients SET status=?, last_error=NULL, status_code=NULL "
                "WHERE status=?",
                (PENDING, from_status),
            )
            return cur.rowcount

    def reset_orphan_in_flight(self) -> int:
        """On startup, any `in_flight` row is from a prior crash — reclaim it."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE recipients SET status=? WHERE status=?",
                (PENDING, IN_FLIGHT),
            )
            return cur.rowcount

    def list_claimable_phones(self) -> list[str]:
        rows = self._conn().execute(
            f"SELECT phone FROM recipients WHERE status IN ({','.join('?' * len(CLAIMABLE))}) "
            "ORDER BY first_seen_at",
            CLAIMABLE,
        ).fetchall()
        return [r["phone"] for r in rows]

    # ---------- per-row workflow ----------

    def claim(self, phone: str) -> Recipient | None:
        """Atomically transition a row to in_flight. Returns None if not claimable."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE recipients SET status=?, attempts=attempts+1, last_attempt_at=? "
                f"WHERE phone=? AND status IN ({','.join('?' * len(CLAIMABLE))})",
                (IN_FLIGHT, now, phone, *CLAIMABLE),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT phone, raw, attempts FROM recipients WHERE phone=?", (phone,)
            ).fetchone()
        return Recipient(phone=row["phone"], raw=row["raw"], attempts=row["attempts"])

    def mark_sent(self, phone: str, message_id: int | None, status_code: int) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, message_id=?, status_code=?, "
                "sent_at=?, last_error=NULL WHERE phone=?",
                (SENT, message_id, status_code, now, phone),
            )

    def mark_failed(
        self, phone: str, status_code: int | None, error: str, *, permanent: bool
    ) -> None:
        target = FAILED_PERMANENT if permanent else FAILED_RETRIABLE
        with self._tx() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, status_code=?, last_error=? WHERE phone=?",
                (target, status_code, redact_secrets(error), phone),
            )

    # ---------- reporting ----------

    def counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM recipients GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def status_for_phones(self, phones: Iterable[str]) -> dict[str, str]:
        """Return {phone: status} for phones already in the DB. Read-only.

        Used by the wizard to preview "X already sent, Y new" without writing
        any rows. SQLite caps the IN-list at 999 params by default; we chunk
        at 500 to stay well under.
        """
        out: dict[str, str] = {}
        chunk: list[str] = []
        conn = self._conn()
        for p in phones:
            chunk.append(p)
            if len(chunk) >= 500:
                self._fill_status(conn, chunk, out)
                chunk = []
        if chunk:
            self._fill_status(conn, chunk, out)
        return out

    @staticmethod
    def _fill_status(conn: sqlite3.Connection, phones: list[str], out: dict[str, str]) -> None:
        placeholders = ",".join("?" * len(phones))
        for row in conn.execute(
            f"SELECT phone, status FROM recipients WHERE phone IN ({placeholders})",
            phones,
        ):
            out[row["phone"]] = row["status"]

    def iter_failed_permanent(self) -> Iterator[sqlite3.Row]:
        yield from self._conn().execute(
            "SELECT phone, raw, status_code, attempts, last_error, last_attempt_at "
            "FROM recipients WHERE status=?",
            (FAILED_PERMANENT,),
        )
