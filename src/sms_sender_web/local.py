"""Run the dashboard on your Mac (plan 06, L1): `./sms-dashboard start`.

A small supervisor runs gunicorn (threads, on 127.0.0.1 only) and the
worker, natively. Natively, the CLI and the worker share one kernel, so the
run lock and SQLite's locks just work (they don't reach into Docker
Desktop's VM, G3), and builds need no proxy.

    sms-dashboard start [--sandbox]    in the background; opens the browser
    sms-dashboard run [--sandbox]      the same in the foreground (launchd runs this)
    sms-dashboard stop [--sandbox]     a send lets its requests in flight finish, then waits
    sms-dashboard status [--sandbox]
    sms-dashboard logs [--sandbox] [-f]
    sms-dashboard upgrade              a backup, stop, install, start again
    sms-dashboard install-agent        start at login, restart after a crash
    sms-dashboard remove-agent

The real dashboard is http://127.0.0.1:8000 on data/; the sandbox is
http://127.0.0.1:8001 on data/sandbox/, where nothing is sent. Both can run
at once.

Before it starts anything, it refuses while another worker uses the same
data folder: a Docker container with the folder mounted, or a worker in
another kernel whose heartbeat is fresh. On start it backs up first when
the app DB needs migrating, then migrates and collects the static files.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from dotenv import dotenv_values

from sms_sender.sharing import HEARTBEAT, foreign_worker

LABEL = "team.kifpool.sms-dashboard"
PORTS = {False: 8000, True: 8001}
HOST = "127.0.0.1"
# The worker lets requests in flight finish; Docker gives it 90 s.
WORKER_STOP_SEC = 95
WEB_STOP_SEC = 30
HEALTH_WAIT_SEC = 120
# A process that ends this often within RESTART_WINDOW_SEC is given up on.
MAX_RESTARTS = 5
RESTART_WINDOW_SEC = 300
RESTART_DELAY_SEC = 2.0
DOCKER_TIMEOUT_SEC = 15
# run_worker's exit code when another worker stays alive on the folder.
WORKER_REFUSED = 2
LOG_BYTES, LOG_FILES = 10_000_000, 5  # the logs hold phone numbers: keep them small
TRUE = ("1", "true", "yes", "on")
CHECKOUT = Path(__file__).resolve().parents[2]


class Refused(Exception):
    """It can't start, or can't do what was asked: why, for the terminal."""


# ---------- where things are ----------

def project_root(cwd: Path | None = None) -> Path:
    """The folder with `.env` and `data/`: the current one if it has a
    `.env`, else this checkout (an editable install) if that has one."""
    cwd = cwd or Path.cwd()
    for folder in (cwd, CHECKOUT):
        if (folder / ".env").is_file():
            return folder
    return cwd


def environment(root: Path, sandbox: bool, base: dict[str, str] | None = None) -> dict[str, str]:
    """The children's environment: this process's, then `.env` for what it
    doesn't set (as manage.py loads it), then what the launcher decides."""
    env = dict(os.environ if base is None else base)
    for key, value in dotenv_values(root / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    env["SMS_SENDER_SANDBOX_ASKED"] = env.get("SMS_SENDER_SANDBOX", "")
    env.update({
        "DJANGO_SETTINGS_MODULE": "sms_sender_web.settings",
        "SMS_SENDER_ENVIRONMENT": "local",
        "SMS_SENDER_SANDBOX": "1" if sandbox else "",
        # Without it, a forked gunicorn worker crashes on macOS when a page
        # calls Kavenegar or Shlink.
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES",
        "PYTHONUNBUFFERED": "1",
    })
    return env


@dataclass(frozen=True)
class Place:
    """Where one dashboard, the real one or the sandbox, keeps its files."""
    root: Path
    sandbox: bool
    env: dict[str, str] = field(repr=False, compare=False)
    sandbox_asked: bool = False  # .env or the shell set SMS_SENDER_SANDBOX

    @classmethod
    def find(cls, sandbox: bool, root: Path | None = None) -> "Place":
        root = (root or project_root()).resolve()
        env = environment(root, sandbox)
        asked = env.pop("SMS_SENDER_SANDBOX_ASKED", "").strip().lower() in TRUE
        return cls(root, sandbox, env, asked)

    def check_mode(self) -> None:
        """`.env` (or the shell) asking for the sandbox means it: the real
        dashboard doesn't start over it."""
        if not self.sandbox and self.sandbox_asked:
            raise Refused(
                "SMS_SENDER_SANDBOX is set (in .env or the shell): use `sms-dashboard start --sandbox`, "
                "or take it out to work on real data."
            )

    @property
    def data_dir(self) -> Path:
        data = Path(self.env.get("SMS_SENDER_DATA_DIR") or "data")
        data = (data if data.is_absolute() else self.root / data).resolve()
        return data / "sandbox" if self.sandbox else data

    @property
    def backup_dir(self) -> Path:
        """As settings.BACKUP_DIR works it out for this mode."""
        raw = self.env.get("SMS_SENDER_BACKUP_DIR")
        if not raw:
            return self.data_dir / "backups"
        path = Path(raw)
        path = (path if path.is_absolute() else self.root / path).resolve()
        return path / "sandbox" if self.sandbox else path

    @property
    def run_dir(self) -> Path:
        return self.data_dir / "run"

    @property
    def state_file(self) -> Path:
        return self.run_dir / "dashboard.json"

    @property
    def lock_file(self) -> Path:
        return self.run_dir / "dashboard.lock"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / ("dashboard-sandbox.log" if self.sandbox else "dashboard.log")

    @property
    def label(self) -> str:
        return f"{LABEL}.sandbox" if self.sandbox else LABEL

    @property
    def mode(self) -> str:
        return "sandbox, nothing is sent" if self.sandbox else "real data"


# ---------- is it running? ----------

def running(place: Place) -> dict | None:
    """What the running supervisor wrote about itself; None when it isn't
    running. The lock it holds is the truth, not the file."""
    if not place.lock_file.exists():
        return None
    fd = os.open(place.lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            return json.loads(place.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def take_lock(place: Place) -> int:
    """Held for the supervisor's life; the OS lets go when it dies."""
    place.run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(place.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        info = running(place) or {}
        raise Refused(f"It's already running ({place.mode}): {info.get('url', '?')}, process {info.get('pid', '?')}.")
    return fd


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_line(pid: int) -> str:
    try:
        # -ww: the whole line; piped, ps cuts it at 80 columns (Linux) or the window's width.
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def leftovers(place: Place) -> list[int]:
    """Processes a supervisor that was killed outright left behind (its
    gunicorn and worker run in sessions of their own): still alive, and
    still ours."""
    try:
        children = json.loads(place.state_file.read_text(encoding="utf-8")).get("children") or {}
    except (OSError, ValueError, AttributeError):
        return []
    found = []
    for pid in children.values():
        if isinstance(pid, int) and _alive(pid):
            line = _command_line(pid)
            if "gunicorn" in line or "run_worker" in line:
                found.append(pid)
    return found


# ---------- who else uses the folder ----------

def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def docker_containers_using(folder: Path, docker: str | None = None) -> list[str]:
    """Running Docker containers with this folder (or one inside or around
    it) mounted. Raises Refused when Docker can't be asked in time."""
    docker = docker or shutil.which("docker") or "/usr/local/bin/docker"
    if not Path(docker).exists():
        return []
    try:
        ids = subprocess.run([docker, "ps", "-q"], capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as e:
        raise Refused("Docker didn't answer: start Docker Desktop or quit it fully, then try again.") from e
    except OSError:
        return []
    if ids.returncode != 0 or not ids.stdout.split():
        return []  # Docker isn't running, or nothing is
    try:
        out = subprocess.run(
            [docker, "inspect", "--format", "{{.Name}}\t{{json .Mounts}}", *ids.stdout.split()],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused("Docker didn't say what its containers use: try again.") from e
    folder = folder.resolve()
    names = []
    for line in out.stdout.splitlines():
        name, _, mounts = line.partition("\t")
        try:
            sources = [m.get("Source") or "" for m in json.loads(mounts or "[]")]
        except ValueError:
            continue
        for source in sources:
            # Docker Desktop may report the host path under /host_mnt.
            source = source.removeprefix("/host_mnt")
            if source and _overlaps(Path(source).resolve(), folder):
                names.append(name.lstrip("/"))
                break
    return names


def check_folder(place: Place, docker: str | None = None) -> None:
    """Refuse while another worker can use this data folder."""
    containers = docker_containers_using(place.data_dir, docker)
    if containers:
        raise Refused(
            f"Docker containers use {place.data_dir}: {', '.join(sorted(containers))}. Two workers on one "
            "folder could send to the same people. Stop them first (in this folder: docker compose stop), "
            "then start again."
        )
    foreign = foreign_worker(place.data_dir / "db")
    if foreign is not None:
        raise Refused(
            f"A worker in another VM or on another machine ({foreign.get('worker') or '?'}) is using "
            f"{place.data_dir}. Stop it first; its heartbeat ({HEARTBEAT}) goes stale within two minutes."
        )


# ---------- the supervisor ----------

def _log(place: Place, echo: bool) -> logging.Logger:
    place.log_file.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"sms_dashboard.{'sandbox' if place.sandbox else 'real'}")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = RotatingFileHandler(place.log_file, maxBytes=LOG_BYTES, backupCount=LOG_FILES, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
        if echo:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(stream)
    return log


def _no_control_socket() -> list[str]:
    """gunicorn 26 opens ~/.gunicorn/gunicorn.ctl, one path for every
    gunicorn: the real dashboard and the sandbox would share it. Nothing
    here uses it."""
    try:
        from gunicorn.config import Config
    except ImportError:
        return []
    return ["--no-control-socket"] if "control_socket_disable" in Config().settings else []


@dataclass
class Child:
    name: str
    argv: list[str]
    stop_sec: float
    proc: subprocess.Popen | None = None
    ends: deque = field(default_factory=deque)  # when it ended unasked, recently


class Supervisor:
    """Keeps gunicorn and the worker running, and stops them together."""

    def __init__(self, place: Place, port: int, log: logging.Logger, *,
                 children: list[Child] | None = None, stop: threading.Event | None = None):
        self.place, self.port, self.log = place, port, log
        self.stop = stop or threading.Event()
        self.children = children if children is not None else self.default_children()
        self.exit_code = 0

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    def default_children(self) -> list[Child]:
        python = sys.executable
        return [
            Child("web", [python, "-m", "gunicorn", "sms_sender_web.wsgi:application",
                          "--bind", f"{HOST}:{self.port}", "--workers", "2", "--worker-class", "gthread",
                          "--threads", "4", "--graceful-timeout", "20", *_no_control_socket()], WEB_STOP_SEC),
            Child("worker", [python, "-m", "django", "run_worker"], WORKER_STOP_SEC),
        ]

    def manage(self, *args: str) -> int:
        """A Django command, its output in the log."""
        proc = subprocess.Popen([sys.executable, "-m", "django", *args], cwd=self.place.root, env=self.place.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log.info("setup | %s", line.rstrip())
        return proc.wait()

    def prepare(self) -> None:
        """Back up when the app DB is about to change, then migrate and
        collect the static files."""
        if self.manage("migrate", "--check") != 0:
            self.log.info("dashboard | the app DB needs migrating: a backup first")
            from .backup import make_backup

            result = make_backup(self.place.data_dir, self.place.backup_dir, keep=None)
            self.log.info("dashboard | backed up to %s", result.path)
        if self.manage("migrate", "--noinput") != 0:
            raise Refused(f"The migration failed; the details are in {self.place.log_file}.")
        if self.manage("collectstatic", "--noinput", "--verbosity", "0") != 0:
            raise Refused(f"Collecting the static files failed; the details are in {self.place.log_file}.")

    def spawn(self, child: Child) -> None:
        child.proc = subprocess.Popen(
            child.argv, cwd=self.place.root, env=self.place.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,  # Ctrl-C reaches only the supervisor, which stops them in order
        )
        threading.Thread(target=self._relay, args=(child.name, child.proc), daemon=True).start()
        self.log.info("dashboard | %s started, process %s", child.name, child.proc.pid)
        self.write_state()

    def _relay(self, name: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log.info("%s | %s", name, line.rstrip())

    def write_state(self) -> None:
        state = {
            "pid": os.getpid(), "port": self.port, "url": self.url, "sandbox": self.place.sandbox,
            "started_at": getattr(self, "started_at", time.time()), "log": str(self.place.log_file),
            "children": {c.name: c.proc.pid for c in self.children if c.proc is not None},
        }
        tmp = self.place.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.place.state_file)

    def run(self) -> int:
        self.started_at = time.time()
        for child in self.children:
            self.spawn(child)
        self.log.info("dashboard | running (%s): %s", self.place.mode, self.url)
        while not self.stop.is_set():
            for child in self.children:
                code = child.proc.poll() if child.proc is not None else None
                if code is None or self.stop.is_set():
                    continue
                self._ended(child, code)
            self.stop.wait(1.0)
        self.shutdown()
        return self.exit_code

    def _ended(self, child: Child, code: int) -> None:
        now = time.monotonic()
        child.ends.append(now)
        while child.ends and now - child.ends[0] > RESTART_WINDOW_SEC:
            child.ends.popleft()
        if child.name == "worker" and code == WORKER_REFUSED:
            self.log.info("dashboard | the worker refused to start: another worker uses this data folder")
            return self._give_up()
        if len(child.ends) >= MAX_RESTARTS:
            self.log.info("dashboard | %s ended %s times in %s minutes", child.name, len(child.ends),
                          RESTART_WINDOW_SEC // 60)
            return self._give_up()
        self.log.info("dashboard | %s ended (exit %s): starting it again", child.name, code)
        self.stop.wait(RESTART_DELAY_SEC)
        if not self.stop.is_set():
            self.spawn(child)

    def _give_up(self) -> None:
        self.log.info("dashboard | giving up: everything stops")
        self.exit_code = 1
        self.stop.set()

    def shutdown(self) -> None:
        """SIGTERM to both: a send stops claiming, lets its requests in
        flight finish and is queued again for the next start. Anything
        still running when its time is up is killed; a send's `in_flight`
        rows are then reconciled at the next start, never resent."""
        live = [c for c in self.children if c.proc is not None and c.proc.poll() is None]
        for child in live:
            self.log.info("dashboard | stopping %s", child.name)
            child.proc.send_signal(signal.SIGTERM)
        for child in live:
            try:
                child.proc.wait(timeout=child.stop_sec)
            except subprocess.TimeoutExpired:
                self.log.info("dashboard | %s didn't stop within %s s: killed", child.name, child.stop_sec)
                child.proc.kill()
                child.proc.wait()
        self.place.state_file.unlink(missing_ok=True)
        self.log.info("dashboard | stopped")


def serve(place: Place, port: int, *, echo: bool) -> int:
    """`run`: everything in this process, until a signal stops it."""
    log = _log(place, echo)
    try:
        place.check_mode()
        lock = take_lock(place)
    except Refused as e:
        log.info("dashboard | %s", e)
        return 2
    try:
        for pid in leftovers(place):
            log.info("dashboard | stopping process %s, left by a supervisor that was killed", pid)
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + WORKER_STOP_SEC
        while leftovers(place) and time.monotonic() < deadline:
            time.sleep(1)
        check_folder(place)
        supervisor = Supervisor(place, port, log)
        supervisor.prepare()
    except Refused as e:
        log.info("dashboard | %s", e)
        os.close(lock)
        return 2
    except Exception:  # noqa: BLE001 — in the log, where `start` shows it from
        log.exception("dashboard | it couldn't get ready")
        os.close(lock)
        return 2

    def on_signal(signum, _frame):
        log.info("dashboard | signal %s: stopping", signum)
        supervisor.stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, on_signal)
    try:
        return supervisor.run()
    finally:
        os.close(lock)


# ---------- the commands ----------

# Straight to 127.0.0.1, never through a proxy set in the shell.
_LOOPBACK = build_opener(ProxyHandler({}))


def _healthy(url: str) -> bool:
    try:
        with _LOOPBACK.open(f"{url}healthz", timeout=3) as response:
            return response.status == 200
    except (URLError, OSError, ValueError):
        return False


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def agent_path(place: Place) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{place.label}.plist"


def agent_loaded(place: Place) -> bool:
    if sys.platform != "darwin":
        return False
    return _launchctl("print", f"gui/{os.getuid()}/{place.label}").returncode == 0


def start(place: Place, port: int, *, browser: bool) -> int:
    place.check_mode()
    info = running(place)
    if info is None:
        proc = None
        if agent_loaded(place):
            _launchctl("kickstart", f"gui/{os.getuid()}/{place.label}")
        else:
            argv = [sys.executable, "-m", "sms_sender_web.local", "run", "--port", str(port)]
            if place.sandbox:
                argv.append("--sandbox")
            proc = subprocess.Popen(argv, cwd=place.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        print(f"Starting the dashboard ({place.mode}); this can take a minute the first time …")
        began = time.monotonic()
        while time.monotonic() - began < HEALTH_WAIT_SEC:
            time.sleep(1)
            info = running(place)
            if info and info.get("url") and _healthy(info["url"]):
                break
            # It ended, or (under launchd) it isn't holding its lock any more.
            ended = proc.poll() is not None if proc is not None else info is None and time.monotonic() - began > 15
            if ended:
                print(f"It didn't start. The end of {place.log_file}:\n{_tail(place.log_file)}", file=sys.stderr)
                return 2
        else:
            print(f"It didn't answer within {HEALTH_WAIT_SEC} s. The end of {place.log_file}:\n"
                  f"{_tail(place.log_file)}", file=sys.stderr)
            return 1
    url = info.get("url") or f"http://{HOST}:{port}/"
    print(f"The dashboard is running ({place.mode}): {url}")
    if browser:
        webbrowser.open(url)
    return 0


def stop(place: Place) -> int:
    info = running(place)
    if info is None:
        print(f"It isn't running ({place.mode}).")
        return 0
    pid = info.get("pid")
    if not isinstance(pid, int):
        print("It's running, but its process isn't recorded: stop it from Activity Monitor.", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    print("Stopping: a send lets its requests in flight finish first …")
    deadline = time.monotonic() + WORKER_STOP_SEC + WEB_STOP_SEC
    while running(place) is not None and time.monotonic() < deadline:
        time.sleep(1)
    if running(place) is not None:
        print(f"It's still stopping; see {place.log_file}.", file=sys.stderr)
        return 1
    print("Stopped.")
    return 0


def status(place: Place) -> int:
    info = running(place)
    agent = "on" if agent_path(place).exists() else "off"
    if info is None:
        print(f"Stopped ({place.mode}). Data: {place.data_dir}. Start at login: {agent}.")
        return 3
    url = info.get("url", "?")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.get("started_at", time.time())))
    print(f"Running ({place.mode}): {url}, since {since}, process {info.get('pid', '?')}.")
    print(f"  Pages: {'answering' if _healthy(url) else 'not answering'}")
    try:
        beat = json.loads((place.data_dir / "db" / HEARTBEAT).read_text(encoding="utf-8"))
        print(f"  Worker: last seen {int(time.time() - float(beat['at']))} s ago")
    except (OSError, ValueError, KeyError, TypeError):
        print("  Worker: not seen yet")
    print(f"  Data: {place.data_dir}\n  Log: {place.log_file}\n  Start at login: {agent}")
    return 0


def logs(place: Place, follow: bool) -> int:
    if not place.log_file.exists():
        print(f"No log yet: {place.log_file}")
        return 0
    os.execvp("tail", ["tail", "-n", "200", *(["-F"] if follow else []), str(place.log_file)])
    return 0  # not reached


def upgrade(place: Place, port: int) -> int:
    """After `git pull`: a backup, stop, install what the new version needs,
    start again (start migrates)."""
    from .backup import make_backup

    place.check_mode()
    was_running = running(place) is not None
    if was_running and stop(place) != 0:
        return 1
    result = make_backup(place.data_dir, place.backup_dir, keep=None)
    print(f"Backed up to {result.path}")
    install = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-e", f"{place.root}[web]"],
                             cwd=place.root, env=place.env)
    if install.returncode != 0:
        print("Installing failed, so it wasn't started again. Fix that, then: sms-dashboard start",
              file=sys.stderr)
        return 1
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=place.root, capture_output=True,
                            text=True).stdout.strip()
    print(f"Installed{f' {commit}' if commit else ''}.")
    return start(place, port, browser=False) if was_running or agent_loaded(place) else 0


def agent_plist(place: Place) -> dict:
    """A launchd agent: starts at login, and again after a crash; a clean
    stop (`sms-dashboard stop`) stays stopped until the next login."""
    argv = [sys.executable, "-m", "sms_sender_web.local", "run"]
    if place.sandbox:
        argv.append("--sandbox")
    out = str(place.root / "logs" / f"{place.label}.launchd.log")
    return {
        "Label": place.label,
        "ProgramArguments": argv,
        "WorkingDirectory": str(place.root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": out,
        "StandardErrorPath": out,
        # docker (for the folder check) and the system tools.
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def install_agent(place: Place) -> int:
    if sys.platform != "darwin":
        raise Refused("Starting at login uses launchd, which only macOS has.")
    place.check_mode()
    if running(place) is not None and not agent_loaded(place):
        stop(place)  # launchd starts it again, as its own
    path = agent_path(place)
    path.parent.mkdir(parents=True, exist_ok=True)
    (place.root / "logs").mkdir(exist_ok=True)
    if agent_loaded(place):
        _launchctl("bootout", f"gui/{os.getuid()}/{place.label}")
    with path.open("wb") as f:
        plistlib.dump(agent_plist(place), f)
    done = _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    if done.returncode != 0:
        raise Refused(f"launchctl refused the agent: {done.stderr.strip() or done.stdout.strip()}")
    print(f"It starts at login now, and again after a crash ({place.mode}). Undo: sms-dashboard remove-agent")
    return start(place, PORTS[place.sandbox], browser=False)


def remove_agent(place: Place) -> int:
    path = agent_path(place)
    if agent_loaded(place):
        # Stops it too: launchd sends SIGTERM, so a send stops as with `stop`.
        _launchctl("bootout", f"gui/{os.getuid()}/{place.label}")
    path.unlink(missing_ok=True)
    print("It no longer starts at login. Start it by hand: sms-dashboard start")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sms-dashboard", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("start", "Start in the background and open the browser."),
        ("run", "Run in the foreground (what launchd runs)."),
        ("stop", "Stop: a send lets its requests in flight finish first."),
        ("status", "Is it running, and where."),
        ("logs", "The end of its log."),
        ("upgrade", "After git pull: a backup, stop, install, start again."),
        ("install-agent", "Start at login, and again after a crash (launchd)."),
        ("remove-agent", "No longer start at login."),
    ):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("--sandbox", action="store_true",
                             help="The sandbox: its own data in data/sandbox/, and nothing is sent.")
        if name in ("start", "run", "upgrade"):
            command.add_argument("--port", type=int, default=None,
                                 help="Default: 8000, or 8001 for the sandbox.")
        if name == "start":
            command.add_argument("--no-browser", action="store_true", help="Don't open the browser.")
        if name == "logs":
            command.add_argument("-f", "--follow", action="store_true", help="Keep showing new lines.")
    args = parser.parse_args(argv)
    try:
        place = Place.find(args.sandbox)
        port = getattr(args, "port", None) or PORTS[args.sandbox]
        if args.command == "run":
            return serve(place, port, echo=sys.stdout.isatty())
        if args.command == "start":
            return start(place, port, browser=not args.no_browser)
        if args.command == "stop":
            return stop(place)
        if args.command == "status":
            return status(place)
        if args.command == "logs":
            return logs(place, args.follow)
        if args.command == "upgrade":
            return upgrade(place, port)
        if args.command == "install-agent":
            return install_agent(place)
        return remove_agent(place)
    except Refused as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
