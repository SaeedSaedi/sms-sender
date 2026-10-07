"""`sms-dashboard` (plan 06, L1): the dashboard run natively on a Mac by a
small supervisor. It keeps gunicorn and the worker going, stops them
together, cleans up after a supervisor that was killed, and refuses to start
while another worker can use the same data folder."""
from __future__ import annotations

import json
import logging
import os
import plistlib
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("django")

from sms_sender.sharing import HEARTBEAT  # noqa: E402
from sms_sender_web import local  # noqa: E402
from sms_sender_web.local import Child, Place, Refused, Supervisor  # noqa: E402


@pytest.fixture(autouse=True)
def _only_the_test_env(monkeypatch):
    """The launcher reads the environment, as it does on the Mac: these
    tests give it only their own `.env` (the test settings' data folder and
    the like stay out)."""
    for name in list(os.environ):
        if name.startswith(("SMS_SENDER_", "DJANGO_")):
            monkeypatch.delenv(name)


def _root(tmp_path: Path, dotenv: str = "DJANGO_SECRET_KEY=test-only\n") -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".env").write_text(dotenv, encoding="utf-8")
    return root


def _place(tmp_path: Path, sandbox: bool = False, dotenv: str = "DJANGO_SECRET_KEY=test-only\n") -> Place:
    return Place.find(sandbox, _root(tmp_path, dotenv))


# ---------- where things are ----------

def test_the_env_file_fills_in_and_the_launcher_decides_the_rest(tmp_path):
    root = _root(tmp_path, "DJANGO_SECRET_KEY=from-dotenv\nKAVENEGAR_API_KEY=dotenv-key\n")
    env = local.environment(root, False, base={"KAVENEGAR_API_KEY": "shell-key", "PATH": "/bin"})
    assert env["KAVENEGAR_API_KEY"] == "shell-key"  # the shell wins, as with load_dotenv
    assert env["DJANGO_SECRET_KEY"] == "from-dotenv"
    assert env["SMS_SENDER_ENVIRONMENT"] == "local"
    assert env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] == "YES"
    assert env["SMS_SENDER_SANDBOX"] == ""
    assert local.environment(root, True, base={})["SMS_SENDER_SANDBOX"] == "1"


def test_the_real_one_and_the_sandbox_keep_apart(tmp_path):
    real = _place(tmp_path)
    sandbox = Place.find(True, real.root)
    assert real.data_dir == real.root / "data"
    assert sandbox.data_dir == real.root / "data" / "sandbox"
    assert real.backup_dir == real.root / "data" / "backups"
    assert sandbox.backup_dir == real.root / "data" / "sandbox" / "backups"
    assert real.lock_file != sandbox.lock_file and real.log_file != sandbox.log_file
    assert (real.label, sandbox.label) == (local.LABEL, f"{local.LABEL}.sandbox")
    assert local.PORTS == {False: 8000, True: 8001}


def test_a_backup_folder_set_in_env_still_keeps_the_sandbox_apart(tmp_path):
    place = _place(tmp_path, dotenv="DJANGO_SECRET_KEY=x\nSMS_SENDER_BACKUP_DIR=/srv/backups\nSMS_SENDER_DATA_DIR=d\n")
    assert place.data_dir == place.root / "d"
    assert place.backup_dir == Path("/srv/backups").resolve()
    assert Place.find(True, place.root).backup_dir == Path("/srv/backups").resolve() / "sandbox"


def test_an_env_that_asks_for_the_sandbox_is_not_overridden(tmp_path, monkeypatch):
    monkeypatch.delenv("SMS_SENDER_SANDBOX", raising=False)
    place = _place(tmp_path, dotenv="DJANGO_SECRET_KEY=x\nSMS_SENDER_SANDBOX=1\n")
    with pytest.raises(Refused, match="--sandbox"):
        place.check_mode()
    Place.find(True, place.root).check_mode()  # asked for, and given
    assert "SMS_SENDER_SANDBOX_ASKED" not in place.env


# ---------- who else uses the folder ----------

def _fake_docker(tmp_path: Path, mounts: dict[str, list[str]], *, code: int = 0, sleep: float = 0) -> str:
    """A `docker` that lists these containers and their mounts."""
    lines = "\n".join(f"/{name}\t{json.dumps([{'Source': s} for s in sources])}" for name, sources in mounts.items())
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep {sleep}\n"
        f"[ {code} -ne 0 ] && exit {code}\n"
        'if [ "$1" = ps ]; then\n'
        f"  printf '%s\\n' {' '.join(name for name in mounts) or 'nothing'}\n"
        "else\n"
        f"  cat <<'EOF'\n{lines}\nEOF\n"
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_docker_containers_with_the_folder_mounted_are_found(tmp_path):
    data = tmp_path / "checkout" / "data"
    data.mkdir(parents=True)
    docker = _fake_docker(tmp_path, {
        "sms-sender-worker-1": [str(data)],
        "sms-sender-web-1": [f"/host_mnt{data}"],          # Docker Desktop's spelling
        "kifpool-core-app-1": [str(tmp_path / "elsewhere")],
        "backups-1": [str(data / "backups")],              # a folder inside it counts
    })
    assert sorted(local.docker_containers_using(data, docker)) == ["backups-1", "sms-sender-web-1",
                                                                   "sms-sender-worker-1"]
    assert local.docker_containers_using(data / "sandbox", docker)  # a folder around it counts too


def test_docker_that_is_not_running_uses_nothing(tmp_path):
    assert local.docker_containers_using(tmp_path, _fake_docker(tmp_path, {}, code=1)) == []
    assert local.docker_containers_using(tmp_path, str(tmp_path / "no-docker-here")) == []


def test_docker_that_does_not_answer_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "DOCKER_TIMEOUT_SEC", 0.5)
    with pytest.raises(Refused, match="Docker didn't answer"):
        local.docker_containers_using(tmp_path, _fake_docker(tmp_path, {"x": [str(tmp_path)]}, sleep=3))


def test_it_refuses_while_docker_or_another_kernel_uses_the_folder(tmp_path):
    place = _place(tmp_path)
    docker = _fake_docker(tmp_path, {"sms-sender-worker-1": [str(place.root / "data")]})
    with pytest.raises(Refused, match="sms-sender-worker-1.*docker compose stop"):
        local.check_folder(place, docker)
    nobody = str(tmp_path / "no-docker")
    local.check_folder(place, nobody)  # nothing in the way
    (place.data_dir / "db").mkdir(parents=True)
    (place.data_dir / "db" / HEARTBEAT).write_text(json.dumps(
        {"kernel": "linux:docker-desktop-vm", "worker": "abc123:1", "at": time.time()}), encoding="utf-8")
    with pytest.raises(Refused, match="another VM"):
        local.check_folder(place, nobody)


# ---------- is it running? ----------

def _hold_lock(place: Place) -> subprocess.Popen:
    """Another process holding the supervisor's lock, as a running one does."""
    place.run_dir.mkdir(parents=True, exist_ok=True)
    code = ("import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT); "
            "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(place.lock_file)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_the_lock_says_whether_it_runs(tmp_path):
    place = _place(tmp_path)
    assert local.running(place) is None
    holder = _hold_lock(place)
    try:
        place.state_file.write_text(json.dumps({"pid": holder.pid, "url": "http://127.0.0.1:8000/"}))
        assert local.running(place)["url"] == "http://127.0.0.1:8000/"
        with pytest.raises(Refused, match="already running"):
            local.take_lock(place)
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()
    assert local.running(place) is None  # the file stays, but nobody holds the lock
    fd = local.take_lock(place)
    try:
        assert local.running(place) is not None
    finally:
        os.close(fd)


def test_processes_left_by_a_killed_supervisor_are_found(tmp_path):
    place = _place(tmp_path)
    place.run_dir.mkdir(parents=True)
    # A process whose command line reads like the worker's.
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "run_worker"])
    theirs = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        place.state_file.write_text(json.dumps({"children": {"worker": ours.pid, "web": theirs.pid}}))
        assert local.leftovers(place) == [ours.pid]
    finally:
        for proc in (ours, theirs):
            proc.kill()
            proc.wait()
    assert local.leftovers(place) == []


# ---------- the supervisor ----------

def _logger(tmp_path: Path) -> logging.Logger:
    log = logging.getLogger(f"test-dashboard-{tmp_path.name}")
    log.handlers[:] = [logging.FileHandler(tmp_path / "supervisor.log")]
    log.propagate = False
    log.setLevel(logging.INFO)
    return log


def _child(name: str, code: str, stop_sec: float = 5) -> Child:
    return Child(name, [sys.executable, "-c", code], stop_sec)


SLEEPER = "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); time.sleep(60)"


def _supervise(tmp_path, children):
    place = _place(tmp_path)
    place.run_dir.mkdir(parents=True)
    supervisor = Supervisor(place, 8999, _logger(tmp_path), children=children)
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    return supervisor, thread


def _wait(condition, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_a_child_that_ends_is_started_again_and_both_stop_together(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "RESTART_DELAY_SEC", 0.1)
    flaky = _child("worker", "import sys, time; time.sleep(0.3); sys.exit(1)")
    web = _child("web", SLEEPER)
    supervisor, thread = _supervise(tmp_path, [web, flaky])
    assert _wait(lambda: len(flaky.ends) >= 2)
    state = json.loads(supervisor.place.state_file.read_text())
    assert state["url"] == "http://127.0.0.1:8999/" and set(state["children"]) == {"web", "worker"}
    supervisor.stop.set()
    thread.join(timeout=20)
    assert web.proc.returncode == 0  # it was asked to stop, and did
    assert supervisor.exit_code == 0
    assert not supervisor.place.state_file.exists()


def test_a_child_that_keeps_ending_is_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "RESTART_DELAY_SEC", 0.05)
    monkeypatch.setattr(local, "MAX_RESTARTS", 3)
    supervisor, thread = _supervise(tmp_path, [_child("web", SLEEPER), _child("worker", "import sys; sys.exit(1)")])
    thread.join(timeout=20)
    assert not thread.is_alive() and supervisor.exit_code == 1


def test_a_worker_refused_for_another_worker_stops_everything_at_once(tmp_path):
    worker = _child("worker", f"import sys; sys.exit({local.WORKER_REFUSED})")
    supervisor, thread = _supervise(tmp_path, [_child("web", SLEEPER), worker])
    thread.join(timeout=20)
    assert supervisor.exit_code == 1 and len(worker.ends) == 1
    assert "another worker uses this data folder" in (tmp_path / "supervisor.log").read_text()


def test_a_child_that_will_not_stop_is_killed(tmp_path):
    stubborn = _child("worker", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                "print('ready', flush=True); time.sleep(60)", stop_sec=0.5)
    supervisor, thread = _supervise(tmp_path, [stubborn])
    assert _wait(lambda: "ready" in (tmp_path / "supervisor.log").read_text())
    supervisor.stop.set()
    thread.join(timeout=20)
    assert stubborn.proc.returncode == -9
    assert "killed" in (tmp_path / "supervisor.log").read_text()


# ---------- start at login ----------

def test_the_launchd_agent_runs_it_in_the_foreground_and_restarts_only_a_crash(tmp_path):
    place = _place(tmp_path)
    plist = local.agent_plist(place)
    assert plist["ProgramArguments"] == [sys.executable, "-m", "sms_sender_web.local", "run"]
    assert plist["WorkingDirectory"] == str(place.root)
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "/usr/local/bin" in plist["EnvironmentVariables"]["PATH"]  # docker, for the folder check
    sandbox = local.agent_plist(Place.find(True, place.root))
    assert sandbox["Label"].endswith(".sandbox") and sandbox["ProgramArguments"][-1] == "--sandbox"


def test_status_of_one_that_is_not_running(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.chdir(root)
    assert local.main(["status"]) == 3
    assert "Stopped (real data)" in capsys.readouterr().out


# ---------- the real thing ----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_it_runs_the_dashboard_and_stops_cleanly(tmp_path):
    """gunicorn and the worker, natively, in a folder of their own: migrated
    after a backup, answering, and gone after SIGTERM, with nothing left."""
    root = _root(tmp_path, "DJANGO_SECRET_KEY=test-only-not-a-secret\n")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "sms_sender_web.local", "run", "--sandbox", "--port", str(port)],
                            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    place = Place.find(True, root)
    try:
        assert _wait(lambda: local._healthy(f"http://127.0.0.1:{port}/"), timeout=90), \
            place.log_file.read_text() if place.log_file.exists() else "no log"
        assert local.running(place)["port"] == port
        assert _wait(lambda: (place.data_dir / "db" / HEARTBEAT).exists())
        log = place.log_file.read_text()
        assert "a backup first" in log and "Applying system.0004_daily_backup... OK" in log
        assert list(place.backup_dir.iterdir())  # the backup before migrating
    finally:
        proc.terminate()
        assert proc.wait(timeout=120) == 0
    assert local.running(place) is None
    assert not place.state_file.exists()
    assert not (place.data_dir / "db" / HEARTBEAT).exists()  # the worker signed off
    assert "dashboard | stopped" in place.log_file.read_text()


# ---------- moving to the server (plan 06, L7) ----------

def _with_data(place: Place) -> None:
    import sqlite3

    from sms_sender.state import StateStore

    place.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(place.data_dir / "app.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    (place.data_dir / "db").mkdir(exist_ok=True)
    StateStore(place.data_dir / "db" / "coin.db").upsert_pending([("09120000001", "09120000001")])


def test_retiring_takes_a_verified_backup_and_fences_this_copy(tmp_path, capsys):
    from sms_sender_web.backup import verify

    from sms_sender.sharing import MOVED, held

    place = _place(tmp_path)
    _with_data(place)
    assert local.retire(place) == 0
    moved = json.loads(place.moved_file.read_text(encoding="utf-8"))
    backup = Path(moved["backup"])
    assert verify(backup) == [] and (backup / "db" / "coin.db").exists()
    assert not (backup / local.MOVED_FILE).exists()  # the server starts without the fences
    assert held(place.data_dir / "db")["reason"] == MOVED
    for refused in (lambda: local.start(place, 8000, browser=False), lambda: local.upgrade(place, 8000),
                    lambda: local.retire(place)):
        with pytest.raises(Refused, match="moved to the server"):
            refused()
    assert "Retired." in capsys.readouterr().out


def test_the_command_line_will_not_send_from_a_retired_copy(tmp_path, capsys):
    from sms_sender import cli
    from sms_sender.sharing import MOVED, write_hold

    write_hold(tmp_path, by="sms-dashboard retire", reason=MOVED)
    with pytest.raises(SystemExit) as stopped:
        cli._refuse_while_held(tmp_path)
    assert stopped.value.code == 2
    assert "moved to the server" in capsys.readouterr().err


def _agent_file(place: Place, root: Path) -> Path:
    path = local.agent_path(place)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump({**local.agent_plist(place), "WorkingDirectory": str(root)}, f)
    return path


def test_another_copys_login_agent_is_never_touched(tmp_path, launchd, capsys):
    """The agent's label is the same for every copy on the Mac: a worktree
    or a test's folder never stops, removes or replaces the real panel's."""
    place = _place(tmp_path)
    _with_data(place)
    path = _agent_file(place, tmp_path / "the-real-panel")
    launchd.loaded = True
    with pytest.raises(Refused, match="the-real-panel"):
        local.remove_agent(place)
    with pytest.raises(Refused, match="the-real-panel" if sys.platform == "darwin" else "launchd"):
        local.install_agent(place)
    local.status(place)
    assert "Start at login: off" in capsys.readouterr().out
    assert local.retire(place) == 0  # this copy retires; the other's agent stays
    assert path.exists()
    assert not [call for call in launchd.calls if call[0] in ("bootout", "bootstrap", "kickstart")]


def test_this_copys_own_agent_goes_when_it_retires(tmp_path, launchd):
    place = _place(tmp_path)
    _with_data(place)
    path = _agent_file(place, place.root)
    launchd.loaded = True
    assert local.retire(place) == 0
    assert not path.exists()
    assert ("bootout", f"gui/{os.getuid()}/{place.label}") in launchd.calls


def test_the_sandbox_has_nothing_to_retire(tmp_path):
    with pytest.raises(Refused, match="nothing to retire"):
        local.retire(Place.find(True, _root(tmp_path)))


def test_the_data_and_the_logs_are_their_owners_only(tmp_path):
    """They hold phone numbers. Folders and files made before (with the old
    0755 / 0644) are tightened at start."""
    place = _place(tmp_path)
    for folder in ("db", "segments", "exports", "backups"):
        (place.data_dir / folder).mkdir(parents=True, exist_ok=True)
    (place.root / "logs").mkdir(exist_ok=True)
    made = [place.data_dir / "app.db", place.data_dir / "db" / "coin-7.db",
            place.data_dir / "segments" / "vip.csv", place.data_dir / "exports" / "coin-7-clickers.csv",
            place.root / "logs" / "dashboard.log"]
    for path in made:
        path.write_text("x", encoding="utf-8")
        path.chmod(0o644)
    place.data_dir.chmod(0o755)
    place.make_private()
    for folder in (place.data_dir, place.data_dir / "db", place.data_dir / "segments", place.data_dir / "exports",
                   place.root / "logs"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    for path in made:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
