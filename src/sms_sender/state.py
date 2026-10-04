"""SQLite-backed state store. Owns the "never send twice" guarantee.

Connection-per-thread (sqlite3 connections are not shareable across threads).
WAL mode + immediate commits keep the crash window tiny: a row only flips to
`sent` after the API confirmed 200, and that flip is committed before we
release the row.
"""
from __future__ import annotations

import json
import math
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
    (  # 1 → 2: cost of each accepted SMS + one row per call to the provider
        "ALTER TABLE recipients ADD COLUMN cost INTEGER",
        """
        CREATE TABLE attempts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            kind        TEXT NOT NULL,
            outcome     TEXT NOT NULL,
            started_at  REAL NOT NULL,
            finished_at REAL,
            status_code INTEGER,
            message_id  INTEGER,
            cost        INTEGER,
            detail      TEXT
        )
        """,
        "CREATE INDEX idx_attempts_phone ON attempts(phone)",
    ),
    (  # 2 → 3: which campaign this DB is, what it sends with, last run
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
    (  # 3 → 4: whether each accepted SMS reached the phone (Kavenegar sms/status)
        "ALTER TABLE recipients ADD COLUMN delivery_status INTEGER",
        "ALTER TABLE recipients ADD COLUMN delivery_checked_at REAL",
    ),
    (  # 4 → 5: who each recipient is (user ID from the import) and which segment brought them
        "ALTER TABLE recipients ADD COLUMN user_id TEXT",
        "ALTER TABLE recipients ADD COLUMN segment TEXT",
    ),
    (  # 5 → 6: one row per short link, written before Shlink is called (links.py),
       # and which link each recipient was sent with
        """
        CREATE TABLE links (
            key              TEXT PRIMARY KEY,
            ref              TEXT UNIQUE,
            plan             TEXT NOT NULL,
            long_url         TEXT NOT NULL,
            title            TEXT NOT NULL,
            tags             TEXT NOT NULL,
            valid_until      TEXT NOT NULL,
            status           TEXT NOT NULL,
            short_code       TEXT,
            short_url        TEXT,
            attempts         INTEGER NOT NULL DEFAULT 0,
            last_error       TEXT,
            created_at       REAL NOT NULL,
            ready_at         REAL,
            clicks           INTEGER,
            clicks_synced_at REAL
        )
        """,
        "CREATE INDEX idx_links_status ON links(status)",
        "CREATE INDEX idx_links_short_code ON links(short_code)",
        "ALTER TABLE recipients ADD COLUMN link_key TEXT",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)

# `--campaign <slug>` keeps each campaign's state DB here (gitignored).
CAMPAIGN_DB_DIR = Path("data/db")


def campaign_db_path(campaign: str) -> Path:
    return CAMPAIGN_DB_DIR / f"{campaign}.db"


class StateSchemaError(RuntimeError):
    """The DB was written by a newer sms-sender than this one."""


class CampaignMismatchError(ValueError):
    """The DB belongs to another campaign, or was sent with other settings."""


# Status values
PENDING = "pending"
IN_FLIGHT = "in_flight"
SENT = "sent"
FAILED_PERMANENT = "failed_permanent"
FAILED_RETRIABLE = "failed_retriable"
# The request may have reached Kavenegar without a clear answer (timeout,
# dropped connection, crash mid-send). Never claimable: resending could
# deliver a second SMS, so it waits to be checked with the provider.
UNKNOWN = "unknown"
# Reconciliation found more than one candidate message: an operator decides.
NEEDS_REVIEW = "needs_review"
# On the opt-out list: never sent. Not claimable.
SUPPRESSED = "suppressed"
# The input says not to send (a phone with two different user IDs). Not
# claimable, and no retry resets it — only an explicit `reset --status invalid`.
INVALID = "invalid"
# The campaign was cancelled before this recipient was sent (spec 4.6). Not
# claimable; `reset --status cancelled` brings it back.
CANCELLED = "cancelled"

CLAIMABLE = (PENDING, FAILED_RETRIABLE)

# Link statuses (`links` table)
LINK_PENDING = "pending"  # written, not created at Shlink yet (or Shlink was unreachable)
LINK_READY = "ready"      # created; short_code / short_url set
LINK_FAILED = "failed"    # Shlink refused it; the next run asks again


@dataclass(frozen=True)
class Recipient:
    phone: str
    raw: str
    attempts: int


@dataclass(frozen=True)
class LinkRow:
    """A short link exactly as it is (or will be) requested from Shlink."""
    key: str             # phone, 'segment:<name>', 'campaign' or 'test:<phone>'
    ref: str | None      # random reference in the long URL; None on shared links
    long_url: str
    title: str
    tags: tuple[str, ...]
    valid_until: str     # ISO 8601, UTC
    plan: str = ""       # fingerprint of the link settings it was planned with
    status: str = LINK_PENDING
    short_code: str | None = None
    short_url: str | None = None


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

    def upsert_pending(self, rows: list[tuple[str, str]], *, segment: str | None = None) -> int:
        """Insert (phone, raw) pairs as pending. Returns number of new rows.

        `segment` records which segment brought each new recipient; rows
        that had none yet (created before segments existed) get it too."""
        if not rows:
            return 0
        now = time.time()
        with self._tx() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO recipients "
                "(phone, raw, status, first_seen_at, segment) VALUES (?, ?, ?, ?, ?)",
                [(p, r, PENDING, now, segment) for p, r in rows],
            )
            new = conn.total_changes - before
            if segment is not None:
                conn.executemany(
                    "UPDATE recipients SET segment=? WHERE phone=? AND segment IS NULL",
                    [(segment, p) for p, _ in rows],
                )
            return new

    def assign_user_ids(self, user_ids: dict[str, str]) -> list[tuple[str, str, str]]:
        """Record each phone's user ID from the import. A phone that already
        has a different one is a conflict, returned as (phone, stored, new)
        and left unchanged; the caller excludes it from sending."""
        if not user_ids:
            return []
        stored: dict[str, str | None] = {}
        phones = list(user_ids)
        with self._tx() as conn:
            for start in range(0, len(phones), 500):
                chunk = phones[start:start + 500]
                for row in conn.execute(
                    f"SELECT phone, user_id FROM recipients "
                    f"WHERE phone IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    stored[row["phone"]] = row["user_id"]
            conn.executemany(
                "UPDATE recipients SET user_id=? WHERE phone=? AND user_id IS NULL",
                [(uid, p) for p, uid in user_ids.items() if p in stored and stored[p] is None],
            )
        return [
            (p, old, user_ids[p]) for p, old in stored.items()
            if old is not None and old != user_ids[p]
        ]

    def exclude(self, phones: Iterable[str], reason: str) -> int:
        """Take recipients that must not be sent (e.g. conflicting user IDs)
        out of the send queue as `invalid`. Only claimable rows change;
        returns how many did."""
        return self._move_claimable(phones, INVALID, reason)

    def _move_claimable(self, phones: Iterable[str], status: str, reason: str) -> int:
        """Set `status` on the claimable rows among `phones`. Chunked:
        SQLite caps the IN-list."""
        changed = 0
        batch = list(phones)
        for start in range(0, len(batch), 500):
            chunk = batch[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            with self._tx() as conn:
                cur = conn.execute(
                    f"UPDATE recipients SET status=?, last_error=? "
                    f"WHERE phone IN ({placeholders}) "
                    f"AND status IN ({','.join('?' * len(CLAIMABLE))})",
                    (status, reason, *chunk, *CLAIMABLE),
                )
                changed += cur.rowcount
        return changed

    def user_id_counts(self) -> tuple[int, int]:
        """(recipients with a user ID, recipients missing one) — real
        recipients only, not invalid input rows."""
        row = self._conn().execute(
            "SELECT SUM(user_id IS NOT NULL), SUM(user_id IS NULL) FROM recipients "
            "WHERE phone NOT LIKE 'INVALID:%'"
        ).fetchone()
        return (row[0] or 0, row[1] or 0)

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

    def cancel_remaining(self) -> int:
        """Cancel a campaign: every recipient still waiting (pending or
        failed_retriable) becomes `cancelled`. Accepted SMS are final, and
        `unknown` rows stay as they are, to be reconciled. Returns rows changed."""
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE recipients SET status=?, last_error='campaign cancelled' "
                f"WHERE status IN ({','.join('?' * len(CLAIMABLE))}) "
                f"AND phone NOT LIKE 'INVALID:%'",
                (CANCELLED, *CLAIMABLE),
            )
            return cur.rowcount

    def suppress(self, phones: Iterable[str]) -> int:
        """Move opted-out phones out of the send queue. Only claimable rows
        change — a row that was already sent stays `sent`. Returns rows
        changed."""
        return self._move_claimable(phones, SUPPRESSED, "on the opt-out list")

    def reset_status(self, from_status: str) -> int:
        """Promote rows in `from_status` back to pending. Returns rows changed."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE recipients SET status=?, last_error=NULL, status_code=NULL "
                "WHERE status=?",
                (PENDING, from_status),
            )
            return cur.rowcount

    def mark_orphans_unknown(self) -> int:
        """On startup, any `in_flight` row is left over from a process that
        stopped mid-send (the run lock rules out a live one). Its request may
        or may not have reached Kavenegar, so it becomes `unknown` — never
        `pending`, which would send it again blindly. Returns rows changed."""
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO attempts (phone, kind, outcome, started_at, detail) "
                "SELECT phone, 'recovery', ?, ?, 'process stopped while the request was in flight' "
                "FROM recipients WHERE status=?",
                (UNKNOWN, now, IN_FLIGHT),
            )
            cur = conn.execute(
                "UPDATE recipients SET status=?, "
                "last_error='process stopped while the request was in flight' WHERE status=?",
                (UNKNOWN, IN_FLIGHT),
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

    def claim(self, phone: str, link_key: str | None = None) -> Recipient | None:
        """Atomically transition a row to in_flight. Returns None if not claimable.

        `link_key` records which short link this attempt carries (None: no
        link), so reports know exactly which link each recipient got."""
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE recipients SET status=?, attempts=attempts+1, last_attempt_at=?, "
                f"link_key=? WHERE phone=? AND status IN ({','.join('?' * len(CLAIMABLE))})",
                (IN_FLIGHT, now, link_key, phone, *CLAIMABLE),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT phone, raw, attempts FROM recipients WHERE phone=?", (phone,)
            ).fetchone()
        return Recipient(phone=row["phone"], raw=row["raw"], attempts=row["attempts"])

    def mark_sent(
        self, phone: str, message_id: int | None, status_code: int, cost: int | None = None,
    ) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, message_id=?, status_code=?, cost=?, "
                "sent_at=?, last_error=NULL WHERE phone=?",
                (SENT, message_id, status_code, cost, now, phone),
            )

    def mark_unknown(self, phone: str, error: str) -> None:
        """The request may have been accepted — park it until it's checked."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, status_code=NULL, last_error=? WHERE phone=?",
                (UNKNOWN, redact_secrets(error), phone),
            )

    def record_attempt(
        self, *, phone: str, kind: str, outcome: str, started_at: float,
        finished_at: float | None = None, status_code: int | None = None,
        message_id: int | None = None, cost: int | None = None, detail: str | None = None,
    ) -> None:
        """Append one row to the audit trail of calls to the provider."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO attempts (phone, kind, outcome, started_at, finished_at, "
                "status_code, message_id, cost, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (phone, kind, outcome, started_at, finished_at, status_code, message_id,
                 cost, redact_secrets(detail) if detail else detail),
            )

    def attempts_for(self, phone: str) -> list[sqlite3.Row]:
        """Every recorded call for one phone, oldest first."""
        return self._conn().execute(
            "SELECT * FROM attempts WHERE phone=? ORDER BY id", (phone,)
        ).fetchall()

    # ---------- campaign identity (meta) ----------

    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def bind_campaign(
        self, name: str | None, settings: dict | None, *, allow_change: bool = False,
    ) -> list[str]:
        """Tie this DB to one campaign and to what it sends.

        The first run records both. Later runs must match: one DB is one
        campaign, and two message versions in it would make its history
        meaningless. Settings may still change while nothing can have gone
        out yet — that's how a wrong template gets fixed — and later only
        with `allow_change`. Returns the names of changed settings.
        """
        with self._tx() as conn:
            def get(key: str) -> str | None:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return row["value"] if row else None

            def put(key: str, value: str) -> None:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

            stored_name = get("campaign")
            if name and stored_name and stored_name != name:
                raise CampaignMismatchError(
                    f"{self.db_path} belongs to campaign {stored_name!r}, not {name!r}"
                )
            if name and not stored_name:
                put("campaign", name)
            if settings is None:
                return []

            encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False)
            raw = get("settings")
            if raw is None:
                put("settings", encoded)
                return []
            stored = json.loads(raw)
            changed = sorted(k for k in set(stored) | set(settings) if stored.get(k) != settings.get(k))
            if not changed:
                return []
            may_have_gone_out = conn.execute(
                "SELECT 1 FROM recipients WHERE status IN (?, ?, ?, ?) LIMIT 1",
                (SENT, IN_FLIGHT, UNKNOWN, NEEDS_REVIEW),
            ).fetchone()
            if may_have_gone_out and not allow_change:
                raise CampaignMismatchError(
                    f"{self.db_path} already sent with different settings "
                    f"({', '.join(changed)} changed). Use a new campaign, or pass "
                    "--allow-settings-change if mixing both versions is intended."
                )
            put("settings", encoded)
            return changed

    # ---------- settling `unknown` rows (see reconcile.py) ----------

    def list_unknown(self) -> list[tuple[str, float | None]]:
        """(phone, last_attempt_at) for every `unknown` row, oldest first."""
        rows = self._conn().execute(
            "SELECT phone, last_attempt_at FROM recipients WHERE status=? "
            "ORDER BY last_attempt_at",
            (UNKNOWN,),
        ).fetchall()
        return [(r["phone"], r["last_attempt_at"]) for r in rows]

    def known_message_ids(self) -> set[int]:
        """Every Kavenegar message ID this DB already accounts for — sent rows
        and recorded calls (e.g. the approval test) — so reconciliation never
        claims one of them for an `unknown` row."""
        rows = self._conn().execute(
            "SELECT message_id FROM recipients WHERE message_id IS NOT NULL "
            "UNION SELECT message_id FROM attempts WHERE message_id IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}

    def neighbour_message_ids(self) -> set[int]:
        """Message IDs recorded by the other campaign DBs in this DB's folder.
        Kavenegar's lookup lists a phone's messages for the whole day, so
        another campaign's SMS to the same person must not be taken for
        ours. Never writes to them (query_only) or upgrades them; files
        that aren't readable databases are skipped."""
        if self.db_path == ":memory:":
            return set()
        me = Path(self.db_path).resolve()
        ids: set[int] = set()
        for other in sorted(me.parent.glob("*.db")):
            if other.resolve() == me:
                continue
            try:
                # Not `mode=ro`: SQLite can't open a WAL database read-only once
                # its -shm file is gone (any DB nobody has open). query_only
                # makes this connection unable to write instead.
                conn = sqlite3.connect(str(other), timeout=5)
                try:
                    conn.execute("PRAGMA query_only = ON")
                    tables = {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    for table in ("recipients", "attempts"):
                        if table in tables:
                            ids.update(r[0] for r in conn.execute(
                                f"SELECT message_id FROM {table} WHERE message_id IS NOT NULL"
                            ))
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
        return ids

    def settle_unknown_sent(self, phone: str, message_id: int) -> bool:
        """Kavenegar has the message, so it was sent. Only an `unknown` row
        changes; returns whether one did."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE recipients SET status=?, message_id=?, status_code=200, "
                "sent_at=last_attempt_at, last_error=NULL WHERE phone=? AND status=?",
                (SENT, message_id, phone, UNKNOWN),
            )
            return cur.rowcount == 1

    def settle_unknown_not_sent(self, phone: str, reason: str) -> bool:
        """Kavenegar never got it, so it's safe to send again."""
        return self._settle_unknown(phone, FAILED_RETRIABLE, reason)

    def settle_unknown_for_review(self, phone: str, reason: str) -> bool:
        """Can't tell which message is ours: an operator decides."""
        return self._settle_unknown(phone, NEEDS_REVIEW, reason)

    def _settle_unknown(self, phone: str, status: str, reason: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE recipients SET status=?, status_code=NULL, last_error=? "
                "WHERE phone=? AND status=?",
                (status, reason, phone, UNKNOWN),
            )
            return cur.rowcount == 1

    def mark_failed(
        self, phone: str, status_code: int | None, error: str, *, permanent: bool
    ) -> None:
        target = FAILED_PERMANENT if permanent else FAILED_RETRIABLE
        with self._tx() as conn:
            conn.execute(
                "UPDATE recipients SET status=?, status_code=?, last_error=? WHERE phone=?",
                (target, status_code, redact_secrets(error), phone),
            )

    # ---------- short links (see links.py) ----------

    def _select_in(self, sql: str, keys: Iterable[str]) -> list[sqlite3.Row]:
        """Run `sql` (with one `{in}` placeholder for an IN-list) over `keys`
        in chunks: SQLite caps the number of parameters."""
        batch, rows = list(keys), []
        conn = self._conn()
        for start in range(0, len(batch), 500):
            chunk = batch[start:start + 500]
            rows += conn.execute(sql.format(**{"in": ",".join("?" * len(chunk))}), chunk).fetchall()
        return rows

    def segments_for(self, phones: Iterable[str]) -> dict[str, str | None]:
        rows = self._select_in("SELECT phone, segment FROM recipients WHERE phone IN ({in})", phones)
        return {r["phone"]: r["segment"] for r in rows}

    def get_links(self, keys: Iterable[str]) -> dict[str, LinkRow]:
        rows = self._select_in(
            "SELECT key, ref, plan, long_url, title, tags, valid_until, status, short_code, "
            "short_url FROM links WHERE key IN ({in})",
            keys,
        )
        return {
            r["key"]: LinkRow(
                key=r["key"], ref=r["ref"], long_url=r["long_url"], title=r["title"],
                tags=tuple(json.loads(r["tags"])), valid_until=r["valid_until"],
                plan=r["plan"], status=r["status"], short_code=r["short_code"],
                short_url=r["short_url"],
            )
            for r in rows
        }

    def add_links(self, rows: Iterable[LinkRow]) -> int:
        """Write links before Shlink is asked for them. A key that already
        has a row keeps it — its request must be repeated exactly. Returns
        how many rows were added (a clashing `ref` is skipped too)."""
        now = time.time()
        with self._tx() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO links "
                "(key, ref, plan, long_url, title, tags, valid_until, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.key, r.ref, r.plan, r.long_url, r.title, json.dumps(list(r.tags)),
                     r.valid_until, LINK_PENDING, now)
                    for r in rows
                ],
            )
            return conn.total_changes - before

    def replan_links(self, rows: Iterable[LinkRow]) -> int:
        """Replace links planned with settings that changed since. Only for
        links nobody has received: the caller passes rows of recipients
        still in the queue (and approval-test links). The old link stays at
        Shlink, unused, until it expires. A clashing new `ref` is skipped
        (OR IGNORE) and the caller retries with another one."""
        now = time.time()
        with self._tx() as conn:
            before = conn.total_changes
            conn.executemany(
                "UPDATE OR IGNORE links SET ref=?, plan=?, long_url=?, title=?, tags=?, "
                "valid_until=?, status=?, short_code=NULL, short_url=NULL, attempts=0, "
                "last_error=NULL, created_at=?, ready_at=NULL, clicks=NULL, "
                "clicks_synced_at=NULL WHERE key=?",
                [
                    (r.ref, r.plan, r.long_url, r.title, json.dumps(list(r.tags)),
                     r.valid_until, LINK_PENDING, now, r.key)
                    for r in rows
                ],
            )
            return conn.total_changes - before

    def mark_link_ready(self, key: str, short_code: str, short_url: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE links SET status=?, short_code=?, short_url=?, attempts=attempts+1, "
                "last_error=NULL, ready_at=? WHERE key=?",
                (LINK_READY, short_code, short_url, time.time(), key),
            )

    def mark_link_not_ready(self, key: str, error: str, *, refused: bool) -> None:
        """Shlink refused the link (`failed`) or couldn't be reached (stays
        `pending`). Either way the next run asks again with the same request."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE links SET status=?, attempts=attempts+1, last_error=? WHERE key=?",
                (LINK_FAILED if refused else LINK_PENDING, error, key),
            )

    def set_link_expiry(self, key: str, valid_until: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE links SET valid_until=? WHERE key=?", (valid_until, key))

    def link_counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM links GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---------- clicks (see clicks.py) ----------

    def record_clicks(self, by_code: dict[str, int], synced_at: float) -> int:
        """Store each link's (non-bot) visit count. Returns links updated.

        Indexed by short code and committed in chunks, so a sync of 100k
        links never holds the write lock long enough to stall a send that
        runs at the same time (`clicks` doesn't take the run lock)."""
        updated = 0
        items = list(by_code.items())
        for start in range(0, len(items), 500):
            with self._tx() as conn:
                before = conn.total_changes
                conn.executemany(
                    "UPDATE links SET clicks=?, clicks_synced_at=? WHERE short_code=?",
                    [(n, synced_at, code) for code, n in items[start:start + 500]],
                )
                updated += conn.total_changes - before
        return updated

    def clicks_by_segment(self) -> list[sqlite3.Row]:
        """Per segment, over sent recipients: how many, how many lack a user
        ID, and — for those sent a link of their own — who clicked and how
        often. Joins on `link_key`, the link each recipient actually got."""
        return self._conn().execute(
            "SELECT COALESCE(r.segment, '') AS segment, COUNT(*) AS sent, "
            "SUM(r.user_id IS NULL) AS missing_user_id, "
            "SUM(CASE WHEN r.link_key = r.phone THEN 1 ELSE 0 END) AS own, "
            "SUM(CASE WHEN r.link_key = r.phone AND COALESCE(l.clicks, 0) > 0 "
            "    THEN 1 ELSE 0 END) AS clicked, "
            "SUM(CASE WHEN r.link_key = r.phone AND COALESCE(l.clicks, 0) > 0 "
            "    AND r.user_id IS NULL THEN 1 ELSE 0 END) AS clicked_missing_user_id, "
            "SUM(CASE WHEN r.link_key = r.phone THEN COALESCE(l.clicks, 0) ELSE 0 END) AS clicks "
            "FROM recipients r LEFT JOIN links l ON l.key = r.link_key "
            "WHERE r.status=? GROUP BY r.segment ORDER BY r.segment",
            (SENT,),
        ).fetchall()

    def shared_link_clicks(self) -> tuple[dict[str, int], int]:
        """Clicks on shared links that were actually sent: per segment (each
        segment link counted once) and on campaign-wide links."""
        rows = self._conn().execute(
            "SELECT DISTINCT r.segment AS segment, l.key AS key, COALESCE(l.clicks, 0) AS clicks "
            "FROM recipients r JOIN links l ON l.key = r.link_key "
            "WHERE r.status=? AND r.link_key != r.phone",
            (SENT,),
        ).fetchall()
        per_segment: dict[str, int] = {}
        campaign_keys: dict[str, int] = {}
        for r in rows:
            if r["key"].startswith("segment:"):
                per_segment[r["segment"] or ""] = per_segment.get(r["segment"] or "", 0) + r["clicks"]
            else:
                campaign_keys[r["key"]] = r["clicks"]
        return per_segment, sum(campaign_keys.values())

    def last_click_sync(self) -> float | None:
        row = self._conn().execute("SELECT MAX(clicks_synced_at) FROM links").fetchone()
        return row[0]

    def iter_attribution(self) -> Iterator[sqlite3.Row]:
        """Sent recipients with a link of their own — no phone numbers."""
        yield from self._conn().execute(
            "SELECT l.ref, r.user_id, r.segment, l.short_url, r.sent_at, "
            "r.delivery_status, l.clicks "
            "FROM recipients r JOIN links l ON l.key = r.link_key "
            "WHERE r.status=? AND r.link_key = r.phone ORDER BY r.sent_at, l.ref",
            (SENT,),
        )

    def iter_clickers(self) -> Iterator[sqlite3.Row]:
        """Recipients who clicked their own link, most clicks first."""
        yield from self._conn().execute(
            "SELECT r.phone, r.user_id, r.segment, l.ref, l.clicks "
            "FROM recipients r JOIN links l ON l.key = r.link_key "
            "WHERE r.link_key = r.phone AND l.clicks > 0 ORDER BY l.clicks DESC, r.phone"
        )

    # ---------- reporting ----------

    # ---------- delivery reports (see delivery.py) ----------

    def messages_awaiting_delivery(
        self, *, sent_after: float, final: Iterable[int],
    ) -> list[tuple[str, int]]:
        """(phone, message_id) of sent SMS from `sent_after` on whose delivery
        status isn't final yet — the ones worth asking Kavenegar about."""
        final = tuple(final)
        rows = self._conn().execute(
            "SELECT phone, message_id FROM recipients "
            "WHERE status=? AND message_id IS NOT NULL AND sent_at >= ? "
            f"AND (delivery_status IS NULL OR delivery_status NOT IN ({','.join('?' * len(final))})) "
            "ORDER BY sent_at",
            (SENT, sent_after, *final),
        ).fetchall()
        return [(r["phone"], r["message_id"]) for r in rows]

    def record_delivery(self, statuses: dict[str, int], checked_at: float) -> None:
        """Store Kavenegar's delivery status per phone, in one transaction."""
        if not statuses:
            return
        with self._tx() as conn:
            conn.executemany(
                "UPDATE recipients SET delivery_status=?, delivery_checked_at=? WHERE phone=?",
                [(status, checked_at, phone) for phone, status in statuses.items()],
            )

    def delivery_counts(self) -> dict[int | None, int]:
        """Sent rows by delivery status; None = not checked yet."""
        rows = self._conn().execute(
            "SELECT delivery_status, COUNT(*) AS n FROM recipients WHERE status=? "
            "GROUP BY delivery_status",
            (SENT,),
        ).fetchall()
        return {r["delivery_status"]: r["n"] for r in rows}

    def total_cost(self) -> int:
        """What this campaign paid for its accepted SMS, in rials."""
        row = self._conn().execute("SELECT COALESCE(SUM(cost), 0) FROM recipients").fetchone()
        return int(row[0])

    def recipients_page(
        self, *, limit: int, offset: int = 0, phone: str | None = None,
    ) -> list[sqlite3.Row]:
        """Recipients in the order they were added, for the dashboard's list:
        each row's status, delivery, segment, user ID and its own link's
        clicks (None for a shared link). `id` is the rowid, so a page can
        point at a row without putting the number in a URL."""
        where, args = ("WHERE r.phone = ? ", [phone]) if phone else ("", [])
        return self._conn().execute(
            "SELECT r.rowid AS id, r.phone, r.raw, r.status, r.delivery_status, r.segment, "
            "r.user_id, r.sent_at, r.cost, "
            "CASE WHEN r.link_key = r.phone THEN COALESCE(l.clicks, 0) END AS clicks "
            "FROM recipients r LEFT JOIN links l ON l.key = r.link_key "
            f"{where}ORDER BY r.rowid LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()

    def recipient_total(self, phone: str | None = None) -> int:
        if phone:
            row = self._conn().execute("SELECT COUNT(*) FROM recipients WHERE phone=?", (phone,)).fetchone()
        else:
            row = self._conn().execute("SELECT COUNT(*) FROM recipients").fetchone()
        return int(row[0])

    def phone_of_row(self, row_id: int) -> str | None:
        row = self._conn().execute("SELECT phone FROM recipients WHERE rowid=?", (row_id,)).fetchone()
        return row[0] if row else None

    def average_cost(self) -> int | None:
        """What this campaign paid per SMS so far (rials, rounded up), if any."""
        row = self._conn().execute(
            "SELECT AVG(cost) FROM recipients WHERE cost IS NOT NULL"
        ).fetchone()
        return math.ceil(row[0]) if row and row[0] is not None else None

    def counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM recipients GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def display_counts(self) -> dict[str, int]:
        """Like `counts`, but input rows that weren't valid (`INVALID:<raw>`,
        stored as failed_permanent) count as `invalid`, with conflicting user
        IDs — not as rejections by Kavenegar."""
        rows = self._conn().execute(
            "SELECT CASE WHEN phone LIKE 'INVALID:%' THEN ? ELSE status END AS s, "
            "COUNT(*) AS n FROM recipients GROUP BY s",
            (INVALID,),
        ).fetchall()
        return {r["s"]: r["n"] for r in rows}

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
