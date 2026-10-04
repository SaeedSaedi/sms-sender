"""Phase 5: backups of the data folder, checked and restored; and the
worker's health check."""
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("django")

from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.utils import timezone as dj_timezone  # noqa: E402

from sms_sender.locking import RunLock  # noqa: E402
from sms_sender.state import SENT, StateStore  # noqa: E402
from sms_sender_web import backup as backups  # noqa: E402
from sms_sender_web.backup import BackupError, make_backup, restore, verify  # noqa: E402

T0 = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)


@pytest.fixture
def data(tmp_path):
    """A data folder in use: the app DB with a writer that hasn't
    checkpointed (its last rows are only in the -wal file), a campaign DB,
    a segment, and things that aren't backed up."""
    data = tmp_path / "data"
    (data / "db").mkdir(parents=True)
    (data / "segments").mkdir()
    (data / "sandbox" / "db").mkdir(parents=True)
    (data / "exports").mkdir()
    app = sqlite3.connect(data / "app.db")
    app.execute("PRAGMA journal_mode=WAL")
    app.execute("PRAGMA wal_autocheckpoint=0")
    app.execute("CREATE TABLE job (id INTEGER PRIMARY KEY, state TEXT)")
    app.executemany("INSERT INTO job (state) VALUES (?)", [("done",)] * 50)
    app.commit()
    store = StateStore(data / "db" / "coin-7.db")
    store.upsert_pending([(f"0912000{i:04d}", "x") for i in range(30)])
    for i in range(10):
        store.claim(f"0912000{i:04d}")
        store.mark_sent(f"0912000{i:04d}", 1000 + i, 200, 3020)
    (data / "segments" / "vip.csv").write_text("phone\n09120000001\n", encoding="utf-8")
    (data / "exports" / "x.csv").write_text("made again from the DBs\n", encoding="utf-8")
    (data / "sandbox" / "app.db").write_bytes(b"")
    yield data
    app.close()


def rows(path, table="job"):
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_backup_is_a_whole_consistent_copy(data, tmp_path):
    assert (data / "app.db-wal").stat().st_size > 0  # rows a file copy would miss
    result = make_backup(data, tmp_path / "backups", now=T0)
    assert result.path == tmp_path / "backups" / "20261005T003000Z"
    files = sorted(str(p.relative_to(result.path)) for p in result.path.rglob("*") if p.is_file())
    assert files == ["app.db", "db/coin-7.db", "manifest.json", "segments/vip.csv"]
    assert rows(result.path / "app.db") == 50
    assert not (result.path / "app.db-wal").exists()  # self-contained
    manifest = json.loads((result.path / "manifest.json").read_text())
    entry = next(f for f in manifest["files"] if f["path"] == "db/coin-7.db")
    assert entry["rows"]["recipients"] == 30 and entry["kind"] == "sqlite"
    assert verify(result.path) == []
    # Phone numbers inside: owner only.
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((result.path / "db" / "coin-7.db").stat().st_mode) == 0o600


def test_damage_is_found(data, tmp_path):
    path = make_backup(data, tmp_path / "backups", now=T0).path
    db = path / "db" / "coin-7.db"
    raw = bytearray(db.read_bytes())
    raw[-100] ^= 0xFF
    db.write_bytes(bytes(raw))
    (path / "segments" / "vip.csv").unlink()
    (path / "stray.txt").write_text("?")
    assert verify(path) == [
        "db/coin-7.db: changed since the backup (size or SHA-256 differs)",
        "segments/vip.csv: missing",
        "stray.txt: not in the manifest",
    ]
    with pytest.raises(BackupError, match="damaged"):
        restore(path, tmp_path / "empty")
    assert not (tmp_path / "empty").exists()


def test_a_lost_data_folder_is_restored_whole(data, tmp_path):
    path = make_backup(data, tmp_path / "backups", now=T0).path
    fresh = tmp_path / "fresh"
    result = restore(path, fresh)
    assert sorted(result.restored) == ["app.db", "db/coin-7.db", "segments/vip.csv"]
    assert rows(fresh / "app.db") == 50
    store = StateStore(fresh / "db" / "coin-7.db")
    assert store.counts() == {SENT: 10, "pending": 20} and store.total_cost() == 10 * 3020


def test_an_existing_file_is_replaced_only_when_named_and_kept_aside(data, tmp_path):
    path = make_backup(data, tmp_path / "backups", now=T0).path
    live = StateStore(data / "db" / "coin-7.db")
    live.claim("09120000020")
    live.mark_sent("09120000020", 2000, 200, 3020)  # sent after the backup

    result = restore(path, data)
    assert result.restored == [] and sorted(result.kept) == ["app.db", "db/coin-7.db", "segments/vip.csv"]
    assert live.counts()[SENT] == 11  # untouched

    result = restore(path, data, replace=["db/coin-7.db"])
    ((rel, aside),) = result.replaced
    assert rel == "db/coin-7.db" and aside.startswith("coin-7.db.before-restore-")
    assert StateStore(data / "db" / "coin-7.db").counts()[SENT] == 10
    assert StateStore(data / "db" / aside).counts()[SENT] == 11  # the newer one, with its -wal

    with pytest.raises(BackupError, match="not in this backup: db/other.db"):
        restore(path, data, replace=["db/other.db"])


def test_a_restore_never_replaces_a_db_a_send_is_using(data, tmp_path):
    path = make_backup(data, tmp_path / "backups", now=T0).path
    with RunLock(data / "db" / "coin-7.db"), pytest.raises(BackupError, match="another sms-sender process"):
        restore(path, data, replace=["db/coin-7.db"])
    assert StateStore(data / "db" / "coin-7.db").counts()[SENT] == 10


def test_old_backups_are_pruned_only_after_a_good_one(data, tmp_path, monkeypatch):
    dest = tmp_path / "backups"
    for day in range(4):
        make_backup(data, dest, keep=3, now=T0 + timedelta(days=day))
    names = [p.name for p in backups.list_backups(dest)]
    assert names == ["20261006T003000Z", "20261007T003000Z", "20261008T003000Z"]

    monkeypatch.setattr(backups, "_check", lambda conn: ("page 7 is never used", {}))
    with pytest.raises(BackupError, match="app.db failed its integrity check: page 7"):
        make_backup(data, dest, keep=1, now=T0 + timedelta(days=9))
    assert sorted(p.name for p in dest.iterdir()) == names  # no partial left, none pruned


@pytest.mark.django_db
def test_the_commands(data, tmp_path, settings, capsys):
    settings.DATA_DIR = data
    settings.BACKUP_DIR = tmp_path / "backups"
    call_command("backup", keep=2)
    out = capsys.readouterr().out
    assert "Backed up 3 files" in out
    call_command("verify_backup")
    assert "is whole" in capsys.readouterr().out

    (newest,) = backups.list_backups(settings.BACKUP_DIR)
    (newest / "app.db").write_bytes(b"not a database")
    with pytest.raises(CommandError, match="damaged"):
        call_command("verify_backup", str(newest))

    good = make_backup(data, tmp_path / "other", now=T0).path
    (data / "segments" / "vip.csv").unlink()
    call_command("restore_backup", str(good), replace=["db/coin-7.db"])
    out = capsys.readouterr().out
    assert "restored  segments/vip.csv" in out
    assert "replaced  db/coin-7.db" in out and "kept      app.db" in out
    assert "don't know what was sent after the backup" in out
    with pytest.raises(CommandError, match="nothing restored"):
        call_command("restore_backup", str(newest))


@pytest.mark.django_db
def test_worker_status_is_the_workers_health_check():
    from sms_sender_web.jobs.models import WorkerBeat

    with pytest.raises(CommandError, match="no worker alive"):
        call_command("worker_status")
    WorkerBeat.objects.create(worker_id="other-host:1", seen_at=dj_timezone.now())
    with pytest.raises(CommandError):
        call_command("worker_status")  # another host's worker isn't this one
    call_command("worker_status", any_host=True)
    WorkerBeat.objects.create(worker_id=f"{socket.gethostname()}:7",
                              seen_at=dj_timezone.now() - timedelta(minutes=5))
    with pytest.raises(CommandError):
        call_command("worker_status")  # too long ago
    WorkerBeat.objects.filter(worker_id__endswith=":7").update(seen_at=dj_timezone.now())
    call_command("worker_status")


def test_a_tls_proxy_is_trusted_only_when_told(tmp_path):
    code = ("import django; django.setup(); from django.conf import settings; "
            "print(getattr(settings, 'SECURE_PROXY_SSL_HEADER', None))")
    env = {**os.environ, "DJANGO_SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "sms_sender_web.settings",
           "SMS_SENDER_DATA_DIR": str(tmp_path)}
    for flag, expected in (("", "None"), ("1", "('HTTP_X_FORWARDED_PROTO', 'https')")):
        out = subprocess.run([sys.executable, "-c", code], env={**env, "DJANGO_TRUST_PROXY_SSL": flag},
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == expected
