"""One campaign-DB folder, one kernel at a time (sharing.py). A dashboard
worker inside Docker Desktop's VM and a CLI on the host can open the same
files, but their locks don't meet: while the worker's heartbeat is fresh, a
CLI on another kernel changes nothing in that folder, and only warns when
it reads."""
import json
import time

from click.testing import CliRunner

from sms_sender import sharing
from sms_sender.cli import cli
from sms_sender.state import PENDING, SENT, StateStore


def seed(db) -> None:
    store = StateStore(db)
    store.upsert_pending([("09120000001", "1"), ("09120000002", "2")])
    store.claim("09120000001")
    store.mark_sent("09120000001", 1001, 200)


def beat(folder, *, kernel: str, age: float = 0.0) -> None:
    (folder / sharing.HEARTBEAT).write_text(json.dumps({"kernel": kernel, "worker": "web-1:7", "at": time.time() - age}))


def test_the_kernel_is_known_and_stable():
    assert sharing.kernel_id() and sharing.kernel_id() == sharing.kernel_id()


def test_only_a_fresh_heartbeat_from_another_kernel_counts(tmp_path):
    assert sharing.foreign_worker(tmp_path) is None  # no worker here
    beat(tmp_path, kernel=sharing.kernel_id())
    assert sharing.foreign_worker(tmp_path) is None  # the same kernel: locks work
    beat(tmp_path, kernel="docker-desktop-vm", age=sharing.FRESH_SEC + 5)
    assert sharing.foreign_worker(tmp_path) is None  # the worker stopped
    beat(tmp_path, kernel="docker-desktop-vm")
    assert sharing.foreign_worker(tmp_path)["worker"] == "web-1:7"
    (tmp_path / sharing.HEARTBEAT).write_text("not json")
    assert sharing.foreign_worker(tmp_path) is None


def test_the_worker_leaves_its_own_kernel_in_the_folder(tmp_path):
    sharing.write_heartbeat(tmp_path, "w1")
    data = json.loads((tmp_path / sharing.HEARTBEAT).read_text())
    assert data["kernel"] == sharing.kernel_id() and data["worker"] == "w1"
    assert sharing.foreign_worker(tmp_path) is None
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_cli_on_another_kernel_changes_nothing(tmp_path):
    db = tmp_path / "db" / "coin-7.db"
    db.parent.mkdir()
    seed(db)
    beat(db.parent, kernel="docker-desktop-vm")
    result = CliRunner().invoke(cli, ["reset", "--status", "sent", "--state", str(db), "--yes"])
    assert result.exit_code == 2
    assert "docker compose exec worker sms-sender" in result.output
    assert StateStore(db).counts() == {SENT: 1, PENDING: 1}  # untouched
    numbers = tmp_path / "numbers.txt"
    numbers.write_text("09120000002\n", encoding="utf-8")
    send = CliRunner().invoke(cli, ["send", "--state", str(db), "--input", str(numbers),
                                    "--template", "t", "--token", "x"], env={"KAVENEGAR_API_KEY": "test-not-real"})
    assert send.exit_code == 2 and "file locks don't reach across" in send.output


def test_reading_only_warns(tmp_path):
    db = tmp_path / "db" / "coin-7.db"
    db.parent.mkdir()
    seed(db)
    beat(db.parent, kernel="docker-desktop-vm")
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert result.exit_code == 0
    assert "Warning: the dashboard's worker is using" in result.output


def test_without_a_worker_nothing_changes(tmp_path):
    db = tmp_path / "db" / "coin-7.db"
    db.parent.mkdir()
    seed(db)
    result = CliRunner().invoke(cli, ["reset", "--status", "sent", "--state", str(db), "--yes"])
    assert result.exit_code == 0, result.output
    assert StateStore(db).counts() == {PENDING: 2}


def test_while_sending_is_held_no_cli_send_starts(tmp_path, monkeypatch):
    """The dashboard's emergency stop marks the folder: send, retry-failed and
    preview --send refuse there (exit 2) and change nothing."""
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "data" / "db"
    folder.mkdir(parents=True)
    db = folder / "coin-7.db"
    seed(db)
    sharing.write_hold(folder, by="admin1")
    (tmp_path / "in.txt").write_text("09120000002\n", encoding="utf-8")
    for args in (
        ["send", "--input", "in.txt", "--template", "t", "--token", "x", "--campaign", "coin-7"],
        ["retry-failed", "--input", "in.txt", "--template", "t", "--token", "x", "--campaign", "coin-7"],
        ["preview", "--phone", "09120000002", "--template", "t", "--token", "x", "--send"],
    ):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 2, (args, result.output)
        assert "held from the dashboard (by admin1" in result.output
    assert StateStore(db).counts() == {SENT: 1, PENDING: 1}
    # Reading is still fine.
    assert CliRunner().invoke(cli, ["status", "--campaign", "coin-7"]).exit_code == 0
    sharing.clear_hold(folder)
    assert sharing.held(folder) is None
