"""The Docker image installs the package, not the source tree. An app's
templates missing from package-data would break that app's pages in
Docker, and only there."""
import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import sms_sender_web

ROOT = Path(sms_sender_web.__file__).parent
PYPROJECT = ROOT.parents[1] / "pyproject.toml"
DOCKERFILE = ROOT.parents[1] / "Dockerfile"
REQUIREMENTS = ROOT.parents[1] / "requirements"
TESTS_ONLY = {"pytest-django"}  # in the `web` extra for developers, never in the image


def _pyproject() -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_image_installs_what_the_package_needs_and_nothing_for_tests():
    """requirements/server.in is pyproject's dependencies and `web` extra,
    without what only the tests need; server.lock pins each, with hashes."""
    project = _pyproject()["project"]
    wanted = {str(Requirement(r)) for r in project["dependencies"] + project["optional-dependencies"]["web"]
              if canonicalize_name(Requirement(r).name) not in TESTS_ONLY}
    listed = [line.strip() for line in (REQUIREMENTS / "server.in").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
    assert {str(Requirement(line)) for line in listed} == wanted
    lock = (REQUIREMENTS / "server.lock").read_text(encoding="utf-8")
    pinned = {canonicalize_name(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", lock, re.M)}
    image = {"python_version": "3.13", "python_full_version": "3.13.0"}
    needed = {canonicalize_name(r.name) for r in map(Requirement, listed) if not r.marker or r.marker.evaluate(image)}
    assert needed <= pinned
    assert not (pinned & TESTS_ONLY)
    hashed = re.findall(r"^[A-Za-z0-9_.-]+==\S+ \\\n(?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+", lock, re.M)
    assert len(hashed) == len(pinned)  # every pin carries its hashes


def test_the_image_installs_from_the_lock():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install --require-hashes -r requirements/server.lock" in text
    assert "pip install --no-deps ." in text


def test_every_apps_templates_ship_in_the_package():
    listed = PYPROJECT.read_text(encoding="utf-8")
    folders = sorted(p.parent.name for p in ROOT.glob("*/templates") if p.is_dir())
    assert folders, "found no app templates — the check is broken"
    missing = [app for app in folders if f'"{app}/templates/**/*"' not in listed]
    assert missing == []


def test_the_image_brings_a_sqlite_without_the_wal_reset_bug():
    """Debian's SQLite (3.46.1) has the WAL-reset bug, fixed in 3.51.3: the
    image builds its own, and its build stops if Python loads another."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    version = re.search(r"sqlite-autoconf-(\d)(\d\d)(\d\d)\d\d\.tar\.gz", text)
    assert version and tuple(int(part) for part in version.groups()) >= (3, 51, 3)
    assert "sqlite_version_info < (3, 51, 3)" in text and "LD_LIBRARY_PATH=/opt/sqlite/lib" in text


def test_the_image_serves_with_threads():
    """Browsers reach gunicorn directly and open idle connections ahead of
    time. A sync worker waits on one until it's killed, and a page gets
    gunicorn's "Internal Server Error" (seen in the real run, 2026-10-05)."""
    command = next(line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("CMD"))
    assert "--worker-class gthread" in command and "--threads" in command
