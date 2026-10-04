"""Backups of the data folder (Phase 5): the app DB, every campaign DB and
the segment files. Recipient numbers are personal data, so a backup is as
sensitive as the data folder: it's owner-only here, and must be encrypted
wherever it's copied to (docs/deploy.md).

- Databases are copied with SQLite's online backup, so a send and the
  dashboard can keep writing meanwhile. Each copy must pass an integrity
  check, and is stored as one self-contained file (no -wal).
- A backup is written to `<name>.partial` and renamed when it's complete:
  a folder without the suffix is whole. Old ones are pruned only after a
  backup succeeds.
- `manifest.json` lists every file with its size and SHA-256, and the row
  counts of each DB, so `verify` can tell a damaged copy.
- A restore never overwrites silently. Missing files are put back; an
  existing file only when it's named, and it's moved aside first. A
  campaign DB from before a send has no record of that send: resuming the
  campaign from it would send again.

No Django here: the management commands pass the folders in.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sms_sender.locking import RunLock, RunLockError

STAMP = "%Y%m%dT%H%M%SZ"
NAME_RE = re.compile(r"^\d{8}T\d{6}Z$")
MANIFEST = "manifest.json"
PARTIAL = ".partial"
STALE_PARTIAL_SEC = 24 * 3600


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    files: int
    bytes: int
    pruned: tuple[Path, ...]


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)            # were missing
    replaced: list[tuple[str, str]] = field(default_factory=list)  # (file, moved aside to)
    kept: list[str] = field(default_factory=list)                # exist, not named


def _sources(data_dir: Path) -> list[tuple[str, Path, str]]:
    """(path in the backup, file, "sqlite" or "file"). Not the sandbox,
    exports (made again from the DBs) or the backups themselves."""
    found = []
    if (data_dir / "app.db").is_file():
        found.append(("app.db", data_dir / "app.db", "sqlite"))
    for p in sorted((data_dir / "db").glob("*.db")):
        found.append((f"db/{p.name}", p, "sqlite"))
    for p in sorted((data_dir / "segments").glob("*")):
        if p.is_file():
            found.append((f"segments/{p.name}", p, "file"))
    return found


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _open(path: Path, mode: str) -> sqlite3.Connection:
    """Never creates a file, unlike a plain connect."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, timeout=30)


def _check(conn: sqlite3.Connection) -> tuple[str | None, dict[str, int]]:
    """(problem or None, rows per table)."""
    result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    problem = None if result == ["ok"] else "; ".join(result[:5])
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    return problem, {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _copy_db(src: Path, out: Path, name: str) -> dict[str, int]:
    source = _open(src, "rw")  # rw: a reader of a WAL database may need to write its -shm
    try:
        target = sqlite3.connect(out)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
            problem, rows = _check(target)
        finally:
            target.close()
    finally:
        source.close()
    Path(f"{out}-shm").unlink(missing_ok=True)  # left from WAL mode; the file stands alone
    if problem:
        raise BackupError(f"{name} failed its integrity check: {problem}")
    return rows


def make_backup(
    data_dir: Path, dest: Path, *, keep: int = 14, now: datetime | None = None,
) -> BackupResult:
    """Back up `data_dir` into `dest/<UTC time>/`, then keep only the
    newest `keep` backups. Raises BackupError, leaving no partial backup
    and pruning nothing, if any file can't be copied whole."""
    if keep < 1:
        raise BackupError("keep at least one backup")
    now = now or datetime.now(timezone.utc)
    name = now.strftime(STAMP)
    final, partial = dest / name, dest / f"{name}{PARTIAL}"
    if final.exists():
        raise BackupError(f"{final} already exists")
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(mode=0o700)
    files = []
    try:
        for rel, src, kind in _sources(data_dir):
            out = partial / rel
            out.parent.mkdir(mode=0o700, exist_ok=True)
            entry: dict = {"path": rel, "kind": kind}
            if kind == "sqlite":
                entry["rows"] = _copy_db(src, out, rel)
            else:
                shutil.copyfile(src, out)
            os.chmod(out, 0o600)
            entry.update(bytes=out.stat().st_size, sha256=_sha256(out))
            files.append(entry)
        manifest = {"format": 1, "created_at": now.isoformat(timespec="seconds"), "files": files}
        (partial / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(partial / MANIFEST, 0o600)
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return BackupResult(final, len(files), sum(f["bytes"] for f in files), prune(dest, keep))


def list_backups(dest: Path) -> list[Path]:
    """Complete backups, oldest first."""
    if not dest.is_dir():
        return []
    return sorted(p for p in dest.iterdir() if p.is_dir() and NAME_RE.match(p.name))


def prune(dest: Path, keep: int) -> tuple[Path, ...]:
    """Remove all but the newest `keep` backups, and partial ones left by
    a backup that was killed more than a day ago."""
    gone = list_backups(dest)[:-keep]
    cutoff = time.time() - STALE_PARTIAL_SEC
    gone += [p for p in dest.iterdir()
             if p.is_dir() and p.name.endswith(PARTIAL) and p.stat().st_mtime < cutoff]
    for p in gone:
        shutil.rmtree(p)
    return tuple(gone)


def verify(backup: Path) -> list[str]:
    """What's wrong with this backup; empty when it's whole."""
    try:
        manifest = json.loads((backup / MANIFEST).read_text(encoding="utf-8"))
        files = manifest["files"]
    except (OSError, ValueError, KeyError) as e:
        return [f"{MANIFEST} unreadable: {e}"]
    problems = []
    listed = {MANIFEST}
    for entry in files:
        rel = entry["path"]
        listed.add(rel)
        path = backup / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        if path.stat().st_size != entry["bytes"] or _sha256(path) != entry["sha256"]:
            problems.append(f"{rel}: changed since the backup (size or SHA-256 differs)")
            continue
        if entry["kind"] == "sqlite":
            conn = _open(path, "ro")
            try:
                problem, rows = _check(conn)
            except sqlite3.DatabaseError as e:
                problem, rows = str(e), {}
            finally:
                conn.close()
            if problem:
                problems.append(f"{rel}: integrity check failed: {problem}")
            elif rows != entry["rows"]:
                problems.append(f"{rel}: row counts differ from the manifest")
    extra = sorted(
        str(p.relative_to(backup)) for p in backup.rglob("*")
        if p.is_file() and str(p.relative_to(backup)) not in listed
    )
    problems += [f"{rel}: not in the manifest" for rel in extra]
    return problems


def restore(backup: Path, data_dir: Path, *, replace: Iterable[str] = ()) -> RestoreResult:
    """Put the backup's files back into `data_dir`. Missing files are
    restored; an existing one only if its path (e.g. "db/coin-7.db") is in
    `replace`, and it's moved aside, never deleted. Stop the dashboard and
    the worker first. Raises BackupError before touching anything if the
    backup is damaged, a named file isn't in it, or a run holds a DB."""
    problems = verify(backup)
    if problems:
        raise BackupError("the backup is damaged: " + "; ".join(problems))
    files = json.loads((backup / MANIFEST).read_text(encoding="utf-8"))["files"]
    replace = set(replace)
    unknown = replace - {f["path"] for f in files}
    if unknown:
        raise BackupError(f"not in this backup: {', '.join(sorted(unknown))}")
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    result = RestoreResult()
    locks = []
    try:
        todo = []
        for entry in files:
            rel = entry["path"]
            target = data_dir / rel
            if target.exists() and rel not in replace:
                result.kept.append(rel)
                continue
            if rel.startswith("db/"):  # a send working on it would lose its records
                lock = RunLock(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    lock.acquire()
                except RunLockError as e:
                    raise BackupError(str(e)) from None
                locks.append(lock)
            todo.append((rel, target))
        for rel, target in todo:
            target.parent.mkdir(parents=True, exist_ok=True)
            incoming = target.with_name(f"{target.name}.restoring")
            shutil.copyfile(backup / rel, incoming)
            os.chmod(incoming, 0o600)
            # The old file's -wal / -shm belong to it: applied to the
            # restored copy, they would corrupt it.
            existed = target.exists()
            aside = target.with_name(f"{target.name}.before-restore-{stamp}")
            for suffix in ("", "-wal", "-shm"):
                old = target.with_name(target.name + suffix)
                if old.exists():
                    os.replace(old, aside.with_name(aside.name + suffix))
            os.replace(incoming, target)
            if existed:
                result.replaced.append((rel, aside.name))
            else:
                result.restored.append(rel)
    finally:
        for lock in locks:
            lock.release()
    return result
